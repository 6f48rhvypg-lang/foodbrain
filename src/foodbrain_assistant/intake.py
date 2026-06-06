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
import json
from typing import Any, Callable, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .normalization import normalize_ingredient_name

Transport = Callable[[str, Dict[str, str], bytes, int], str]


class IntakeError(RuntimeError):
    """A recoverable intake failure (bad model output, transport error)."""


class IntakeNotConfigured(IntakeError):
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
    "REUSE the exact name from the provided existing-products list whenever the "
    "item is clearly one of them, so we don't create duplicates.\n"
    "Only put a question in 'questions' when an answer would materially change "
    "what gets stored (e.g. quantity or whether something is open). Do not ask "
    "about things you can reasonably assume.\n\n"
    "Respond with ONLY a JSON object of this shape:\n"
    '{"items": [{"name": str, "quantity": number, "unit": str|null, '
    '"opened": bool, "freshness_days": number|null, "location": str|null, '
    '"confidence": number, "note": str|null}], '
    '"questions": [str], "summary": str}'
)


def understand_transcript(
    transcript: str,
    *,
    settings: Any,
    catalog: Optional[List[dict]] = None,
    answers: Optional[str] = None,
    transport: Optional[Transport] = None,
    timeout_seconds: int = 30,
) -> IntakeResult:
    """Call the OpenRouter model and parse its reply into an :class:`IntakeResult`.

    ``catalog`` is the existing product list (``[{"id", "name"}]``); only the
    names are sent so the model can reuse them. ``answers`` is appended when the
    user is replying to a previous round of clarifying questions.
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

    body = json.dumps(
        {
            "model": settings.openrouter_model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(user_parts)},
            ],
        }
    ).encode("utf-8")

    url = settings.openrouter_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        "X-Title": "FoodBrain",
    }
    send = transport or _http_post
    raw = send(url, headers, body, timeout_seconds)
    content = _extract_message_content(raw)
    return _parse_result(content)


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
        matched_product_id=product_id,
        matched_product_name=product_name,
        match=match,
    )


def _http_post(url: str, headers: Dict[str, str], body: bytes, timeout: int) -> str:
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:300]
        except Exception:  # pragma: no cover - best-effort error detail
            pass
        raise IntakeError(
            f"OpenRouter request failed with HTTP {exc.code}: {detail}".rstrip(": ")
        ) from exc
    except URLError as exc:
        raise IntakeError(f"OpenRouter request failed: {exc.reason}") from exc


def _extract_message_content(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IntakeError("OpenRouter response was not valid JSON") from exc
    if isinstance(payload, dict) and payload.get("error"):
        error = payload["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise IntakeError(f"OpenRouter error: {message}")
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise IntakeError("OpenRouter response had no message content") from exc


def _parse_result(content: str) -> IntakeResult:
    data = json.loads(_strip_fences(content)) if content else {}
    if not isinstance(data, dict):
        raise IntakeError("model did not return a JSON object")
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
    )


def _strip_fences(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text
        if text.endswith("```"):
            text = text[: -len("```")]
        # Drop a leading language tag like "json" left on the first line.
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[len("json") :]
    return text.strip()


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
