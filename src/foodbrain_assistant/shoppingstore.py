"""Durable, stdlib-only shopping-list learning store.

Grocy's native ``shopping_list`` object is the source of truth for the items
themselves (shared across devices, survives, other Grocy clients stay in
sync). This store never duplicates item content — it holds two things Grocy
has no place for:

* ``overlay`` — WHY a Grocy shopping-list row is on the list, keyed by the
  Grocy row id: ``{source, reason, added_ts}``. Pruned on read: an overlay
  entry for a row that's gone from Grocy (bought, removed by another device)
  is dropped rather than lingering forever.
* ``habits`` — the household's learned buying rhythm per product name:
  recent ``buys``/``removals`` (capped) plus ``mode``, the user's explicit
  control over suggestions for that item (``auto``/``suggest``/``off``/None).

Mirrors :mod:`cookmemory`'s durability contract exactly: single JSON file,
atomic ``*.tmp`` + :func:`os.replace` write, a module ``_LOCK`` serializes
every read-modify-write, a missing or corrupt file degrades to an empty
skeleton (corrupt file renamed to ``*.corrupt-<ts>`` first so nothing already
learned is silently lost), and the path is always injected.
"""

from datetime import datetime, timezone
import json
import os
import threading
from pathlib import Path
from statistics import median
from typing import Iterable, List, Optional

_LOCK = threading.Lock()

# Keep only the most recent N buy/removal events per habit — enough for a
# stable median without the file growing without bound.
_MAX_EVENTS = 30

_MODES = {"auto", "suggest", "off"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _skeleton() -> dict:
    return {"overlay": {}, "habits": {}, "diet_focus": {"chips": [], "freetext": "", "updated_ts": None}}


def load(path) -> dict:
    """Read the store, returning a normalized skeleton on missing/corrupt file."""
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return _skeleton()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        _backup_corrupt(p)
        return _skeleton()
    if not isinstance(data, dict):
        _backup_corrupt(p)
        return _skeleton()
    return _normalize(data)


def _backup_corrupt(p: Path) -> None:
    if not p.exists():
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    try:
        os.replace(p, p.with_name(p.name + f".corrupt-{stamp}"))
    except OSError:  # pragma: no cover - best-effort, never block the read
        pass


def save(path, data: dict) -> None:
    """Atomically write the store: dump to ``*.tmp`` then :func:`os.replace`."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    payload = json.dumps(_normalize(data), ensure_ascii=False, indent=2)
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, p)


# --- overlay: why an item is on the list ------------------------------------


def get_overlay(path) -> dict:
    """The full overlay map, ``{grocy_item_id: {source, reason, added_ts}}``."""
    return dict(load(path).get("overlay", {}))


def set_overlay(path, item_id: str, *, source: str, reason: str = "") -> dict:
    """Attach/replace the reason a Grocy shopping-list row is on the list."""
    item_id = str(item_id or "").strip()
    entry = {
        "source": str(source or "manual").strip() or "manual",
        "reason": str(reason or "").strip(),
        "added_ts": _now_iso(),
    }
    if not item_id:
        return entry
    with _LOCK:
        data = load(path)
        data["overlay"][item_id] = entry
        save(path, data)
    return entry


def remove_overlay(path, item_id: str) -> None:
    item_id = str(item_id or "").strip()
    if not item_id:
        return
    with _LOCK:
        data = load(path)
        if data["overlay"].pop(item_id, None) is not None:
            save(path, data)


def prune_overlay(path, valid_ids: Iterable[str]) -> dict:
    """Drop overlay entries whose Grocy row no longer exists; return what's left."""
    valid = {str(v) for v in valid_ids}
    with _LOCK:
        data = load(path)
        kept = {k: v for k, v in data["overlay"].items() if k in valid}
        if kept != data["overlay"]:
            data["overlay"] = kept
            save(path, data)
        return dict(kept)


# --- habits: learned buying rhythm ------------------------------------------


def _key(name: str) -> str:
    return str(name or "").strip().lower()


def habits(path) -> dict:
    """All habits, keyed by lower-cased product name."""
    return dict(load(path).get("habits", {}))


def get_habit(path, name: str) -> Optional[dict]:
    return load(path).get("habits", {}).get(_key(name))


def record_buy(path, *, name: str, product_id: str = "", amount: float = 0.0) -> dict:
    """Log a purchase against a product's habit (feeds interval/typical-amount learning)."""
    return _touch_habit(
        path,
        name=name,
        product_id=product_id,
        list_key="buys",
        event={"amount": _as_float(amount), "ts": _now_iso()},
    )


def record_removal(path, *, name: str, product_id: str = "") -> dict:
    """Log a removal-to-zero (depletion signal for the suggestion engine)."""
    return _touch_habit(
        path, name=name, product_id=product_id, list_key="removals", event={"ts": _now_iso()}
    )


def set_mode(path, *, name: str, product_id: str = "", mode: Optional[str]) -> dict:
    """Set the user's explicit suggestion control for a product: auto/suggest/off/None."""
    mode = str(mode).strip().lower() if mode else None
    if mode is not None and mode not in _MODES:
        raise ValueError(f"mode must be one of {sorted(_MODES)} or None, got {mode!r}")
    key = _key(name)
    if not key:
        raise ValueError("name is required to set a habit mode")
    with _LOCK:
        data = load(path)
        habit = data["habits"].setdefault(key, _new_habit())
        if product_id:
            habit["product_id"] = str(product_id)
        habit["mode"] = mode
        save(path, data)
        return dict(habit)


def habit_stats(habit: Optional[dict]) -> dict:
    """Derive ``{typical_amount, median_interval_days, last_buy_ts, is_staple}`` from a habit.

    Pure function (no I/O) so the suggestion engine's math is unit-testable
    without a store on disk. ``habit`` may be ``None`` (product never bought
    through FoodBrain yet) — everything degrades to ``None``/``False``.
    """
    habit = habit or {}
    buys = [b for b in (habit.get("buys") or []) if isinstance(b, dict)]
    amounts = [_as_float(b.get("amount")) for b in buys if _as_float(b.get("amount")) > 0]
    typical_amount = median(amounts) if amounts else None

    timestamps = sorted(t for t in (_parse_ts(b.get("ts")) for b in buys) if t is not None)
    median_interval_days: Optional[float] = None
    if len(timestamps) >= 2:
        gaps_days = [
            (timestamps[i] - timestamps[i - 1]) / 86400.0 for i in range(1, len(timestamps))
        ]
        median_interval_days = median(gaps_days)
    last_buy_ts = timestamps[-1] if timestamps else None

    is_staple = len(buys) >= 3 or habit.get("mode") == "auto"
    return {
        "typical_amount": typical_amount,
        "median_interval_days": median_interval_days,
        "last_buy_ts": last_buy_ts,
        "is_staple": is_staple,
    }


# --- diet focus: a sticky preference, not a one-off ask ---------------------


def get_diet_focus(path) -> dict:
    """The persisted diet-focus setting: ``{chips, freetext, updated_ts}``."""
    return dict(load(path).get("diet_focus", {"chips": [], "freetext": "", "updated_ts": None}))


def set_diet_focus(path, *, chips: Optional[List[str]] = None, freetext: str = "") -> dict:
    """Replace the diet-focus setting; applies instantly, no save button upstream."""
    entry = {
        "chips": [str(c).strip() for c in (chips or []) if str(c).strip()],
        "freetext": str(freetext or "").strip(),
        "updated_ts": _now_iso(),
    }
    with _LOCK:
        data = load(path)
        data["diet_focus"] = entry
        save(path, data)
    return dict(entry)


# --- internals ---------------------------------------------------------------


def _new_habit() -> dict:
    return {"product_id": "", "buys": [], "removals": [], "mode": None}


def _touch_habit(path, *, name: str, product_id: str, list_key: str, event: dict) -> dict:
    key = _key(name)
    if not key:
        return {}
    with _LOCK:
        data = load(path)
        habit = data["habits"].setdefault(key, _new_habit())
        if product_id:
            habit["product_id"] = str(product_id)
        habit[list_key] = (habit.get(list_key) or []) + [event]
        habit[list_key] = habit[list_key][-_MAX_EVENTS:]
        save(path, data)
        return dict(habit)


def _normalize(data: dict) -> dict:
    skel = _skeleton()
    overlay = data.get("overlay")
    if isinstance(overlay, dict):
        for item_id, entry in overlay.items():
            if not isinstance(entry, dict):
                continue
            skel["overlay"][str(item_id)] = {
                "source": str(entry.get("source") or "manual"),
                "reason": str(entry.get("reason") or ""),
                "added_ts": str(entry.get("added_ts") or "") or None,
            }
    raw_habits = data.get("habits")
    if isinstance(raw_habits, dict):
        for name, habit in raw_habits.items():
            if not isinstance(habit, dict):
                continue
            key = _key(name)
            if not key:
                continue
            mode = habit.get("mode")
            mode = str(mode).strip().lower() if mode else None
            skel["habits"][key] = {
                "product_id": str(habit.get("product_id") or ""),
                "buys": _clean_events(habit.get("buys"), with_amount=True),
                "removals": _clean_events(habit.get("removals"), with_amount=False),
                "mode": mode if mode in _MODES else None,
            }
    diet_focus = data.get("diet_focus")
    if isinstance(diet_focus, dict):
        chips = diet_focus.get("chips")
        skel["diet_focus"] = {
            "chips": [str(c) for c in chips] if isinstance(chips, list) else [],
            "freetext": str(diet_focus.get("freetext") or ""),
            "updated_ts": str(diet_focus.get("updated_ts") or "") or None,
        }
    return skel


def _clean_events(value, *, with_amount: bool) -> List[dict]:
    if not isinstance(value, list):
        return []
    cleaned = []
    for row in value[-_MAX_EVENTS:]:
        if not isinstance(row, dict):
            continue
        event = {"ts": str(row.get("ts") or "") or None}
        if with_amount:
            event["amount"] = _as_float(row.get("amount"))
        cleaned.append(event)
    return cleaned


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_ts(value) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (ValueError, TypeError):
        return None
