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
from pathlib import Path
from typing import Iterable, List, Optional
import uuid


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _skeleton() -> dict:
    return {
        "taste": {"likes": [], "dislikes": [], "notes": ""},
        "twists": [],
        "cooked": [],
        "book": [],
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
        return _skeleton()
    if not isinstance(data, dict):
        return _skeleton()
    return _normalize(data)


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
    data = load(path)
    data["cooked"].append({"dish": str(dish or "").strip(), "ts": _now_iso()})
    save(path, data)


def add_to_book(path, *, title: str, guidance: Iterable[str], buy: Iterable[str] = (), twist: str = "") -> dict:
    """Append a saved recipe to the book and return the stored entry (with id)."""
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


# --- internals -------------------------------------------------------------


def _normalize(data: dict) -> dict:
    skel = _skeleton()
    taste = data.get("taste") if isinstance(data.get("taste"), dict) else {}
    skel["taste"]["likes"] = [str(x) for x in taste.get("likes", []) if str(x).strip()]
    skel["taste"]["dislikes"] = [
        str(x) for x in taste.get("dislikes", []) if str(x).strip()
    ]
    skel["taste"]["notes"] = str(taste.get("notes", "") or "")
    for key in ("twists", "cooked", "book"):
        value = data.get(key)
        if isinstance(value, list):
            skel[key] = [row for row in value if isinstance(row, dict)]
    return skel


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
