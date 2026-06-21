"""Voice intake: turn a spoken fridge description into reconciled Grocy items.

This is the "talk into your phone at the fridge" pipeline (voice-only slice):

#. The SPA captures speech (browser ``SpeechRecognition``) into a transcript and
   POSTs it here.
#. :func:`understand_transcript` sends the transcript — plus the household's
   existing product names so the model reuses them verbatim — to an
   OpenRouter (OpenAI-compatible) chat model, asking for a strict JSON list of
   items and any clarifying questions.
#. :func:`reconcile_items` maps each understood item to an existing Grocy
   product id (via the shared :mod:`normalization`/alias rules) or flags it as a
   new product to be created on commit.

The HTTP call is isolated behind an injectable ``transport`` so the parsing and
reconciliation logic is unit-testable without a network or an API key. The
runtime stays dependency-free (stdlib :mod:`urllib`), matching the project rule.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .llm import LlmError, LlmNotConfigured, Transport, post_chat_json
from .normalization import normalize_ingredient_name


# Intake errors are the shared LLM errors under intake-specific names, so callers
# (api.py) and existing tests keep importing them from here unchanged.
class IntakeError(LlmError):
    """A recoverable intake failure (bad model output, transport error)."""


class IntakeNotConfigured(LlmNotConfigured, IntakeError):
    """Raised when intake is used without an OpenRouter API key configured."""


@dataclass(frozen=True)
class IntakeItem:
    """One food item the model heard, before reconciliation against Grocy."""

    name: str
    quantity: float = 1.0
    unit: Optional[str] = None
    opened: bool = False
    freshness_days: Optional[int] = None
    location: Optional[str] = None
    confidence: float = 0.0
    note: Optional[str] = None
    # What to do with this item on commit. "add" (default) stocks it; the edit
    # actions act on an item you already have: "consume" used some, "toss" threw
    # it away, "set_date" corrects its best-before. Add-mode always yields "add".
    action: str = "add"
    # Filled by reconcile_items: the matched existing product, if any.
    matched_product_id: Optional[str] = None
    matched_product_name: Optional[str] = None
    match: str = "new"  # "exact" | "fuzzy" | "new"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "quantity": self.quantity,
            "unit": self.unit,
            "opened": self.opened,
            "freshness_days": self.freshness_days,
            "location": self.location,
            "confidence": self.confidence,
            "note": self.note,
            "action": self.action,
            "matched_product_id": self.matched_product_id,
            "matched_product_name": self.matched_product_name,
            "match": self.match,
        }


@dataclass(frozen=True)
class IntakeResult:
    items: List[IntakeItem] = field(default_factory=list)
    questions: List[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "items": [item.to_dict() for item in self.items],
            "questions": list(self.questions),
            "summary": self.summary,
        }


_SYSTEM_PROMPT = (
    "You are a kitchen inventory assistant. The user is standing at their fridge "
    "or pantry describing food out loud; the text you receive is a rough speech "
    "transcript and may be messy, in English or German, with filler words.\n\n"
    "Extract every distinct food item mentioned. For each item return:\n"
    "  name           - a short canonical product name (singular, no quantity).\n"
    "  quantity       - a number (default 1 if unstated).\n"
    "  unit           - e.g. 'piece', 'g', 'ml', 'bottle', 'pack' (null if unsure).\n"
    "  opened         - true only if the user clearly said it is open/started.\n"
    "  freshness_days - your estimate of days until it should be eaten, based on "
    "what the user said ('fresh for about a week' -> 7) or typical shelf life; "
    "null if you truly cannot guess.\n"
    "  location       - 'fridge', 'freezer', or 'pantry' if inferable, else null.\n"
    "  confidence     - 0..1, how sure you are about this item.\n"
    "  note           - anything the user said that matters (else null).\n\n"
    "Return EVERY distinct food the user names as its OWN item. Never merge two "
    "different foods into one and never drop one because it resembles another or "
    "an existing product. 'Aufbackbrötchen' and a 'Pita Brötchen' are different "
    "products even though both are rolls.\n"
    "REUSE the exact name from the provided existing-products list ONLY when the "
    "item is genuinely the SAME product (so we don't create duplicates) — not "
    "merely the same kind/category. When unsure, keep the user's name and let it "
    "be a new product.\n"
    "Only put a question in 'questions' when an answer would materially change "
    "what gets stored (e.g. quantity or whether something is open). Do not ask "
    "about things you can reasonably assume.\n\n"
    "Write the 'summary' text and every entry in 'questions' in German "
    "(the user's app is German), regardless of the transcript language. "
    "Product names stay as the user said them.\n\n"
    "Respond with ONLY a JSON object of this shape:\n"
    '{"items": [{"name": str, "quantity": number, "unit": str|null, '
    '"opened": bool, "freshness_days": number|null, "location": str|null, '
    '"confidence": number, "note": str|null}], '
    '"questions": [str], "summary": str}'
)


_EDIT_SYSTEM_PROMPT = (
    "You are a kitchen inventory assistant. The user is standing at their fridge "
    "and saying what CHANGED about food they ALREADY have — what they used up, "
    "finished, threw away, or entered wrong. The text is a rough speech transcript, "
    "English or German, possibly messy.\n\n"
    "For each thing they mention, decide the action:\n"
    "  consume  - they used/ate/drank/finished some or all of it.\n"
    "  toss     - they threw it out / it spoiled / went bad.\n"
    "  set_date - they want to change its best-before / expiry date.\n"
    "  add      - ONLY if they clearly say they bought or added something new.\n\n"
    "For each item return:\n"
    "  name           - the product name. REUSE the exact name from the "
    "existing-products list whenever it matches, so we change the right item.\n"
    "  action         - one of 'consume', 'toss', 'set_date', 'add'.\n"
    "  quantity       - how much the action applies to (default 1). For 'half' use "
    "0.5; for 'all of it' / 'the rest' leave the whole amount (the app fills it).\n"
    "  unit           - if the user stated one, else null.\n"
    "  freshness_days - ONLY for set_date: days from today the new best-before is "
    "('good for another week' -> 7); null otherwise.\n"
    "  opened         - true only if they say they opened/started it.\n"
    "  note           - anything else relevant (else null).\n"
    "  confidence     - 0..1, how sure you are.\n\n"
    "Only put a question in 'questions' when the answer changes what happens (e.g. "
    "how much, or which item). Do not ask about things you can reasonably assume.\n\n"
    "Write the 'summary' text and every entry in 'questions' in German "
    "(the user's app is German), regardless of the transcript language. "
    "Product names stay as the user said them.\n\n"
    "Respond with ONLY a JSON object of this shape:\n"
    '{"items": [{"name": str, "action": str, "quantity": number, "unit": str|null, '
    '"freshness_days": number|null, "opened": bool, "note": str|null, '
    '"confidence": number}], "questions": [str], "summary": str}'
)


def understand_transcript(
    transcript: str,
    *,
    settings: Any,
    catalog: Optional[List[dict]] = None,
    answers: Optional[str] = None,
    mode: str = "add",
    transport: Optional[Transport] = None,
    timeout_seconds: int = 30,
) -> IntakeResult:
    """Call the OpenRouter model and parse its reply into an :class:`IntakeResult`.

    ``catalog`` is the existing product list (``[{"id", "name"}]``); only the
    names are sent so the model can reuse them. ``answers`` is appended when the
    user is replying to a previous round of clarifying questions. ``mode`` is
    ``"add"`` (stock new food) or ``"edit"`` (change food you already have —
    consume / toss / correct a date); it only switches the system prompt.
    """
    if not getattr(settings, "openrouter_api_key", None):
        raise IntakeNotConfigured(
            "voice intake needs FOODBRAIN_OPENROUTER_API_KEY to be set"
        )
    transcript = (transcript or "").strip()
    if not transcript:
        raise IntakeError("transcript is empty")

    names = sorted({str(p.get("name")) for p in (catalog or []) if p.get("name")})
    catalog_block = (
        "Existing products (reuse these names when they match):\n"
        + ", ".join(names)
        if names
        else "There are no existing products yet."
    )
    user_parts = [catalog_block, "", f"Transcript:\n{transcript}"]
    if answers and answers.strip():
        user_parts += ["", f"Answers to earlier questions:\n{answers.strip()}"]

    system_prompt = _EDIT_SYSTEM_PROMPT if mode == "edit" else _SYSTEM_PROMPT
    try:
        data = post_chat_json(
            settings=settings,
            model=settings.openrouter_model,
            system=system_prompt,
            user="\n".join(user_parts),
            transport=transport,
            timeout=timeout_seconds,
        )
    except LlmNotConfigured as exc:  # pragma: no cover - guarded above
        raise IntakeNotConfigured(str(exc)) from exc
    except LlmError as exc:
        raise IntakeError(str(exc)) from exc
    return _parse_result(data)


def reconcile_items(
    items: List[IntakeItem], catalog: List[dict], aliases: Optional[Dict[str, str]] = None
) -> List[IntakeItem]:
    """Attach an existing product id to each item, or leave it as a new product.

    Matching mirrors the recipe matcher's normalization (lowercase, alias map)
    so "Bio Milch" lands on the existing "Milk". Exact normalized equality wins;
    otherwise a containment match is accepted only when exactly one product
    matches, to avoid silently writing to the wrong product.
    """
    by_norm: Dict[str, dict] = {}
    normalized_catalog: List[tuple] = []
    for product in catalog:
        name = str(product.get("name") or "")
        if not name:
            continue
        norm = normalize_ingredient_name(name, aliases)
        by_norm.setdefault(norm, product)
        normalized_catalog.append((norm, product))

    resolved: List[IntakeItem] = []
    for item in items:
        norm = normalize_ingredient_name(item.name, aliases)
        product = by_norm.get(norm)
        match = "exact" if product else "new"
        if product is None and norm:
            contained = [
                prod
                for cand_norm, prod in normalized_catalog
                if norm in cand_norm or cand_norm in norm
            ]
            unique = {prod["id"]: prod for prod in contained}
            if len(unique) == 1:
                product = next(iter(unique.values()))
                match = "fuzzy"
        resolved.append(
            _with_match(
                item,
                product_id=str(product["id"]) if product else None,
                product_name=str(product["name"]) if product else None,
                match=match,
            )
        )
    return resolved


def _with_match(
    item: IntakeItem,
    *,
    product_id: Optional[str],
    product_name: Optional[str],
    match: str,
) -> IntakeItem:
    return IntakeItem(
        name=item.name,
        quantity=item.quantity,
        unit=item.unit,
        opened=item.opened,
        freshness_days=item.freshness_days,
        location=item.location,
        confidence=item.confidence,
        note=item.note,
        action=item.action,
        matched_product_id=product_id,
        matched_product_name=product_name,
        match=match,
    )


def _parse_result(data: dict) -> IntakeResult:
    raw_items = data.get("items")
    items: List[IntakeItem] = []
    if isinstance(raw_items, list):
        for row in raw_items:
            parsed = _parse_item(row)
            if parsed is not None:
                items.append(parsed)
    questions = [
        str(q).strip()
        for q in data.get("questions", [])
        if isinstance(data.get("questions"), list) and str(q).strip()
    ]
    summary = str(data.get("summary") or "").strip()
    return IntakeResult(items=items, questions=questions, summary=summary)


def _parse_item(row: Any) -> Optional[IntakeItem]:
    if not isinstance(row, dict):
        return None
    name = str(row.get("name") or "").strip()
    if not name:
        return None
    return IntakeItem(
        name=name,
        quantity=_to_float(row.get("quantity"), 1.0),
        unit=_clean_str(row.get("unit")),
        opened=bool(row.get("opened")),
        freshness_days=_to_int(row.get("freshness_days")),
        location=_clean_str(row.get("location")),
        confidence=_to_float(row.get("confidence"), 0.0),
        note=_clean_str(row.get("note")),
        action=_clean_action(row.get("action")),
    )


# Map the model's free-text action (and common synonyms) to our canonical set.
_VALID_ACTIONS = {"add", "consume", "toss", "set_date"}
_ACTION_SYNONYMS = {
    "use": "consume", "used": "consume", "consumed": "consume", "eat": "consume",
    "ate": "consume", "drink": "consume", "drank": "consume", "finish": "consume",
    "finished": "consume", "remove": "consume",
    "throw": "toss", "throw_away": "toss", "threw": "toss", "threw_away": "toss",
    "thrown": "toss", "thrown_away": "toss", "trash": "toss", "trashed": "toss",
    "waste": "toss", "wasted": "toss", "spoiled": "toss", "spoil": "toss",
    "discard": "toss", "discarded": "toss", "bin": "toss", "binned": "toss",
    "chuck": "toss",
    "date": "set_date", "set-date": "set_date", "setdate": "set_date",
    "expiry": "set_date", "best_before": "set_date", "redate": "set_date",
    "new": "add", "buy": "add", "bought": "add", "added": "add",
}


def _clean_action(value: Any) -> str:
    cleaned = str(value or "").strip().lower().replace(" ", "_")
    if cleaned in _VALID_ACTIONS:
        return cleaned
    return _ACTION_SYNONYMS.get(cleaned, "add")


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned or cleaned.lower() in {"null", "none"}:
        return None
    return cleaned
