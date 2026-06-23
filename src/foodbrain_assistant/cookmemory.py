"""Durable, stdlib-only cooking memory store (FoodBrain's first server-side state).

Holds the learning signal for the recipe-inspiration feature: the user's taste
(likes/dislikes/notes), the "twists" they describe when they cook a dish their
own way, an anti-repeat log of recently-cooked dishes, and a browsable
"Meine Rezepte" book. Grocy stays inventory-only — none of this is written there.

The store is a single JSON file written atomically (``*.tmp`` + :func:`os.replace`)
so a crash mid-write can't corrupt it. A missing or corrupt file degrades to an
empty skeleton rather than throwing, so the feature never hard-fails on read.

The path is always injected (tests pass a temp file); nothing here hardcodes it.
"""

from datetime import datetime, timezone
import json
import os
import threading
from pathlib import Path
from typing import Iterable, List, Optional
import uuid


# Every mutator does load -> modify -> save; without serialization two concurrent
# SPA requests could read the same state and the later save would clobber the
# earlier one. The server is a single ThreadingHTTPServer process, so a plain
# module-level lock makes the read-modify-write atomic across its threads.
_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _skeleton() -> dict:
    return {
        "taste": {"likes": [], "dislikes": [], "notes": ""},
        "twists": [],
        "cooked": [],
        "book": [],
        "sessions": [],
        # name(lower) -> emoji: per-item symbol overrides, durable across the
        # browser cache wipes that used to erase the localStorage-only choices.
        "icons": {},
    }


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
    """Preserve an unreadable store before the next save overwrites it.

    Returning a fresh skeleton on a corrupt file means the very next mutator
    would ``save`` over the (recoverable) book/history. Rename the bad file to
    ``*.corrupt-<ts>`` first so it can be inspected/recovered by hand.
    """
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


def add_twist(path, *, dish: str, change: str, note: str = "", tags: Optional[dict] = None) -> None:
    """Record a twist and fold its taste tags into ``taste.likes``/``dislikes``."""
    with _LOCK:
        data = load(path)
        data["twists"].append(
            {
                "dish": str(dish or "").strip(),
                "change": str(change or "").strip(),
                "note": str(note or "").strip(),
                "ts": _now_iso(),
            }
        )
        tags = tags or {}
        data["taste"]["likes"] = _merge_unique(data["taste"]["likes"], tags.get("likes"))
        data["taste"]["dislikes"] = _merge_unique(
            data["taste"]["dislikes"], tags.get("dislikes")
        )
        save(path, data)


def add_cooked(path, *, dish: str) -> None:
    """Log a cooked dish for anti-repeat (newest entries win in lookups)."""
    with _LOCK:
        data = load(path)
        data["cooked"].append({"dish": str(dish or "").strip(), "ts": _now_iso()})
        save(path, data)


def add_to_book(path, *, title: str, guidance: Iterable[str], buy: Iterable[str] = (), twist: str = "") -> dict:
    """Append a saved recipe to the book and return the stored entry (with id)."""
    with _LOCK:
        data = load(path)
        entry = {
            "id": str(uuid.uuid4()),
            "title": str(title or "").strip(),
            "guidance": [str(g).strip() for g in (guidance or []) if str(g).strip()],
            "buy": [str(b).strip() for b in (buy or []) if str(b).strip()],
            "twist": str(twist or "").strip(),
            "ts": _now_iso(),
        }
        data["book"].append(entry)
        save(path, data)
        return entry


def upsert_book(
    path,
    *,
    match_title: str,
    title: str,
    guidance: Iterable[str],
    buy: Iterable[str] = (),
    twist: str = "",
) -> dict:
    """Replace the book entry titled ``match_title`` in place, else append.

    Used by "Meine Version": revising a recipe should update its existing book
    entry (keeping its id) rather than pile up near-duplicates. Match is
    case-insensitive on the title; the stored entry takes the new ``title``.
    """
    with _LOCK:
        data = load(path)
        entry = {
            "title": str(title or "").strip(),
            "guidance": [str(g).strip() for g in (guidance or []) if str(g).strip()],
            "buy": [str(b).strip() for b in (buy or []) if str(b).strip()],
            "twist": str(twist or "").strip(),
            "ts": _now_iso(),
        }
        key = str(match_title or "").strip().lower()
        for existing in data["book"]:
            if str(existing.get("title") or "").strip().lower() == key and key:
                existing.update(entry, id=existing.get("id") or str(uuid.uuid4()))
                save(path, data)
                return existing
        entry["id"] = str(uuid.uuid4())
        data["book"].append(entry)
        save(path, data)
        return entry


def recent_cooked(path, *, days: int = 21) -> List[str]:
    """Dish titles cooked within ``days`` — the list to AVOID in idea generation."""
    data = load(path)
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    titles: List[str] = []
    for row in data.get("cooked", []):
        dish = str(row.get("dish") or "").strip()
        if not dish:
            continue
        ts = _parse_ts(row.get("ts"))
        if ts is None or ts >= cutoff:
            titles.append(dish)
    # De-dupe preserving most-recent-first order.
    seen = set()
    ordered: List[str] = []
    for dish in reversed(titles):
        key = dish.lower()
        if key not in seen:
            seen.add(key)
            ordered.append(dish)
    return ordered


def taste_summary(path) -> dict:
    """Compact taste profile for prompts: ``{likes, dislikes, notes}``."""
    data = load(path)
    taste = data.get("taste", {})
    return {
        "likes": list(taste.get("likes", [])),
        "dislikes": list(taste.get("dislikes", [])),
        "notes": str(taste.get("notes", "") or ""),
    }


def book(path) -> List[dict]:
    """The saved-recipes book, newest first."""
    data = load(path)
    return list(reversed(data.get("book", [])))


def add_session(path, *, dish: str, lines: Iterable[dict]) -> dict:
    """Record a cooking session (its booked add/consume lines) and return it.

    Each line keeps what's needed to correct it later: ``product_id``, the
    booked ``amount``, the Grocy ``transaction_id`` (for undo), whether the
    product is now ``depleted``, and its ``kind`` (``consume``/``bought``).
    """
    with _LOCK:
        data = load(path)
        entry = {
            "id": str(uuid.uuid4()),
            "dish": str(dish or "").strip(),
            "lines": [_clean_line(line) for line in (lines or [])],
            "ts": _now_iso(),
        }
        data["sessions"].append(entry)
        save(path, data)
        return entry


def get_icons(path) -> dict:
    """The name->emoji override map (key is the lower-cased product name)."""
    data = load(path)
    return dict(data.get("icons", {}))


def set_icon(path, *, name: str, emoji: str) -> dict:
    """Attach an emoji to a product name forever, or clear it (empty emoji).

    Keyed by ``name.strip().lower()`` — the same trivial casefold the SPA uses —
    so the override follows the *name*, surviving refreshes, cache wipes, and
    other devices. An empty/None ``emoji`` removes the override (back to the
    auto-derived symbol).
    """
    key = str(name or "").strip().lower()
    emoji = str(emoji or "").strip()
    with _LOCK:
        data = load(path)
        if not key:
            return {"name": "", "emoji": emoji}
        if emoji:
            data["icons"][key] = emoji
        else:
            data["icons"].pop(key, None)
        save(path, data)
    return {"name": key, "emoji": emoji}


def sessions(path) -> List[dict]:
    """Cooking sessions, newest first (like :func:`book`)."""
    data = load(path)
    return list(reversed(data.get("sessions", [])))


def remove_session(path, session_id: str) -> Optional[dict]:
    """Drop a stored session (used after a whole-session undo); return it, or None."""
    with _LOCK:
        data = load(path)
        kept, removed = [], None
        for entry in data.get("sessions", []):
            if entry.get("id") == session_id and removed is None:
                removed = entry
            else:
                kept.append(entry)
        if removed is None:
            return None
        data["sessions"] = kept
        save(path, data)
        return removed


def update_session_line(path, session_id: str, line_index: int, **changes) -> Optional[dict]:
    """Patch a single line of a stored session; returns the updated session."""
    with _LOCK:
        data = load(path)
        for entry in data.get("sessions", []):
            if entry.get("id") != session_id:
                continue
            lines = entry.get("lines") or []
            if not (0 <= line_index < len(lines)):
                return None
            for key in ("amount", "transaction_id", "depleted"):
                if key in changes:
                    lines[line_index][key] = changes[key]
            save(path, data)
            return entry
        return None


# --- internals -------------------------------------------------------------


def _normalize(data: dict) -> dict:
    skel = _skeleton()
    taste = data.get("taste") if isinstance(data.get("taste"), dict) else {}
    skel["taste"]["likes"] = [str(x) for x in taste.get("likes", []) if str(x).strip()]
    skel["taste"]["dislikes"] = [
        str(x) for x in taste.get("dislikes", []) if str(x).strip()
    ]
    skel["taste"]["notes"] = str(taste.get("notes", "") or "")
    for key in ("twists", "cooked", "book", "sessions"):
        value = data.get(key)
        if isinstance(value, list):
            skel[key] = [row for row in value if isinstance(row, dict)]
    icons = data.get("icons")
    if isinstance(icons, dict):
        skel["icons"] = {
            str(k).strip().lower(): str(v).strip()
            for k, v in icons.items()
            if str(k).strip() and str(v).strip()
        }
    return skel


def _clean_line(line: dict) -> dict:
    line = line or {}
    return {
        "name": str(line.get("name") or "").strip(),
        "product_id": str(line.get("product_id") or ""),
        "amount": _as_float(line.get("amount")),
        "unit": str(line.get("unit") or "") or None,
        "transaction_id": (str(line.get("transaction_id")) if line.get("transaction_id") else None),
        # The purchase txn for a bought row (so a whole-session undo can remove
        # the added pack, not just the consumed part).
        "add_transaction_id": (
            str(line.get("add_transaction_id")) if line.get("add_transaction_id") else None
        ),
        "depleted": bool(line.get("depleted")),
        "kind": str(line.get("kind") or "consume"),
    }


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _merge_unique(existing: List[str], additions) -> List[str]:
    out = list(existing)
    seen = {x.lower() for x in out}
    for item in additions or []:
        text = str(item).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


def _parse_ts(value) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (ValueError, TypeError):
        return None
