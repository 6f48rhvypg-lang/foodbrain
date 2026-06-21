"""AI recipe inspiration: urgency-seeded ideas, dish guidance, twist extraction.

Three small functions, all mirroring :mod:`intake`'s shape — each calls
:func:`foodbrain_assistant.llm.post_chat_json`, accepts an injectable
``transport`` (so tests run without a network), and takes an explicit ``model``
so the client's Settings choice can override the configured default.

The model only ever *suggests*; every response is treated as untrusted and
normalized into a known shape, degrading to empty lists/strings rather than
throwing on a missing field.
"""

from math import ceil
from typing import List, Optional

from .llm import Transport, post_chat_json

# Curated allowlist of OpenRouter model ids. Used to (a) validate incoming model
# overrides — arbitrary strings are rejected — and (b) populate the SPA dropdowns
# via /api/health-style config. `id` is sent to OpenRouter; `label` is shown.
MODEL_CHOICES: List[dict] = [
    {"id": "google/gemini-3.1-flash-lite", "label": "Gemini 3.1 Flash Lite (günstig)"},
    {"id": "google/gemini-3.5-flash", "label": "Gemini 3.5 Flash"},
    {"id": "google/gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro (stark)"},
    {"id": "anthropic/claude-sonnet-4.6", "label": "Claude Sonnet 4.6"},
    {"id": "anthropic/claude-opus-4.8", "label": "Claude Opus 4.8 (stärkste)"},
]

VALID_MODELS = frozenset(choice["id"] for choice in MODEL_CHOICES)

# Strong default for the creative idea step; cheap default for the mechanical
# recipe step. config.py uses these as the fallbacks for its env-driven fields.
# IDs verified against the live OpenRouter /models catalogue.
DEFAULT_IDEA_MODEL = "google/gemini-3.1-pro-preview"
DEFAULT_RECIPE_MODEL = "google/gemini-3.1-flash-lite"


def is_valid_model(model: str) -> bool:
    return model in VALID_MODELS


# --- 1. ideas (headlines) --------------------------------------------------

_IDEAS_SYSTEM = (
    "Du bist ein erfahrener Koch, der beim Resteverwerten hilft. Der Nutzer hat "
    "Lebensmittel im Haus; einige müssen DRINGEND verbraucht werden ('seeds'), der "
    "Rest ist normaler Vorrat ('inventory').\n\n"
    "Schlage kurze, appetitliche Gericht-Ideen (nur Überschriften) vor. Regeln:\n"
    "- JEDE Idee muss mindestens eine 'seeds'-Zutat als Kern nutzen (das, was "
    "gerettet werden soll). Nenne diese Zutat im Feld 'uses'.\n"
    "- Modus 'stock': NUR aus 'inventory' + üblichen Vorratssachen (Salz, Öl, "
    "Gewürze, Mehl, etc.) kochen — 'buy' MUSS leer sein.\n"
    "- Modus 'shop': 'buy' darf 1–3 Zutaten enthalten, die NICHT im Vorrat sind "
    "und das Gericht spürbar aufwerten. Sonst leer lassen.\n"
    "- Bleib überwiegend frisch und überraschend; 'taste' (mag/mag nicht/Notizen) "
    "ist nur ein sanfter Hinweis, kein harter Filter.\n"
    "- Schlage NIEMALS ein Gericht vor, dessen Titel einem kürzlich gekochten "
    "Gericht stark ähnelt (Liste 'recently cooked').\n"
    "- Beachte die optionalen 'preferences' (Küche/Stil/Eigenschaften), wenn "
    "vorhanden.\n"
    "- Deutsche Titel und Haken-Sätze. Antworte mit NUR diesem JSON:\n"
    '{"ideas": [{"title": str, "hook": str, "uses": str, "buy": [str]}]}'
)


def generate_ideas(
    *,
    seeds: List[str],
    inventory: List[str],
    taste: dict,
    recent_cooked: List[str],
    mode: str,
    preferences: dict,
    balance: float,
    count: int,
    model: str,
    settings,
    transport: Optional[Transport] = None,
) -> dict:
    """Generate ``count`` short dish headlines anchored on the urgent ``seeds``."""
    mode = "shop" if mode == "shop" else "stock"
    count = max(1, min(int(count or 8), 12))
    fresh_n = ceil(max(0.0, min(1.0, float(balance))) * count)

    user = _ideas_user_message(
        seeds=seeds,
        inventory=inventory,
        taste=taste or {},
        recent_cooked=recent_cooked or [],
        mode=mode,
        preferences=preferences or {},
        count=count,
        fresh_n=fresh_n,
    )
    data = post_chat_json(
        settings=settings, model=model, system=_IDEAS_SYSTEM, user=user, transport=transport
    )
    ideas = []
    for row in data.get("ideas", []) if isinstance(data.get("ideas"), list) else []:
        idea = _clean_idea(row, mode)
        if idea is not None:
            ideas.append(idea)
    return {"ideas": ideas[:count]}


def _ideas_user_message(
    *, seeds, inventory, taste, recent_cooked, mode, preferences, count, fresh_n
) -> str:
    lines = [
        f"Modus: {mode}",
        f"Anzahl Ideen: {count} (davon mindestens {fresh_n} frisch/unerwartet).",
        "Dringend zu verbrauchen (seeds): " + (", ".join(seeds) or "—"),
        "Vorrat (inventory): " + (", ".join(inventory) or "—"),
        "Mag: " + (", ".join(taste.get("likes", [])) or "—"),
        "Mag nicht: " + (", ".join(taste.get("dislikes", [])) or "—"),
    ]
    notes = str(taste.get("notes") or "").strip()
    if notes:
        lines.append("Notizen: " + notes)
    if recent_cooked:
        lines.append("Kürzlich gekocht (NICHT wiederholen): " + ", ".join(recent_cooked))
    pref_line = _preferences_line(preferences)
    if pref_line:
        lines.append("Wünsche: " + pref_line)
    return "\n".join(lines)


def _preferences_line(preferences: dict) -> str:
    parts = []
    cuisine = str(preferences.get("cuisine") or "").strip()
    style = str(preferences.get("style") or "").strip()
    needs = preferences.get("needs") or []
    if isinstance(needs, str):
        needs = [needs]
    if cuisine:
        parts.append(f"Küche {cuisine}")
    if style:
        parts.append(f"Stil {style}")
    needs = [str(n).strip() for n in needs if str(n).strip()]
    if needs:
        parts.append("Eigenschaften " + ", ".join(needs))
    return "; ".join(parts)


def _clean_idea(row, mode: str) -> Optional[dict]:
    if not isinstance(row, dict):
        return None
    title = str(row.get("title") or "").strip()
    if not title:
        return None
    buy = [] if mode == "stock" else _str_list(row.get("buy"))[:3]
    return {
        "title": title,
        "hook": str(row.get("hook") or "").strip(),
        "uses": str(row.get("uses") or "").strip(),
        "buy": buy,
    }


# --- 2. recipe (phase guidance) -------------------------------------------

_RECIPE_SYSTEM = (
    "Du bist ein Koch, der ein bereits gewähltes Gericht erklärt. Gib KEINE "
    "nummerierte Schritt-für-Schritt-Anleitung. Beschreibe stattdessen 3–6 grobe "
    "PHASEN als kurze Stichpunkte (z.B. 'Gemüse anbraten → Proteine dazu → Pasta "
    "kochen → vor dem Servieren schmoren'). Nenne eine grobe Gesamtzeit.\n"
    "Im Modus 'stock' ist 'buy' leer; im Modus 'shop' wiederhole die zu kaufenden "
    "Zutaten in 'buy'. Deutsch. Antworte mit NUR diesem JSON:\n"
    '{"title": str, "time": str, "uses": str, "buy": [str], "guidance": [str]}'
)


def generate_recipe(
    *, idea: dict, mode: str, model: str, settings, transport: Optional[Transport] = None
) -> dict:
    """Turn a chosen idea into rough phase guidance (never numbered micro-steps)."""
    mode = "shop" if mode == "shop" else "stock"
    idea = idea or {}
    title = str(idea.get("title") or "").strip()
    user = "\n".join(
        [
            f"Modus: {mode}",
            f"Gericht: {title}",
            "Kurzbeschreibung: " + str(idea.get("hook") or "").strip(),
            "Nutzt vor allem: " + str(idea.get("uses") or "").strip(),
            "Mögliche Einkäufe: " + (", ".join(_str_list(idea.get("buy"))) or "—"),
        ]
    )
    data = post_chat_json(
        settings=settings, model=model, system=_RECIPE_SYSTEM, user=user, transport=transport
    )
    return {
        "title": str(data.get("title") or title).strip() or title,
        "time": str(data.get("time") or "").strip(),
        "uses": str(data.get("uses") or idea.get("uses") or "").strip(),
        "buy": [] if mode == "stock" else _str_list(data.get("buy"))[:3],
        "guidance": _str_list(data.get("guidance")),
    }


# --- 2b. recipe revision ("Meine Version") ---------------------------------

_REVISE_SYSTEM = (
    "Der Nutzer hat ein vorhandenes Rezept und beschreibt (gesprochen oder "
    "getippt), was er ANDERS macht — SEINE Version. Schreibe das Rezept so um, "
    "dass es seine Änderungen WIRKLICH widerspiegelt: passe die Phasen, die Zeit "
    "und ggf. die Einkaufsliste an. Tausche ersetzte Zutaten aus, ergänze "
    "hinzugefügte, lass weggelassene weg. Behalte den Charakter des Gerichts, "
    "erfinde nichts Unnötiges dazu. Gib KEINE nummerierten Mikro-Schritte, "
    "sondern 3–6 grobe PHASEN als kurze Stichpunkte, wie im Original.\n"
    "Im Modus 'stock' ist 'buy' leer; im Modus 'shop' liste die zu kaufenden "
    "Zutaten in 'buy'. Deutsch. Antworte mit NUR diesem JSON:\n"
    '{"title": str, "time": str, "uses": str, "buy": [str], "guidance": [str]}'
)


def revise_recipe(
    *,
    recipe: dict,
    transcript: str,
    mode: str,
    model: str,
    settings,
    transport: Optional[Transport] = None,
) -> dict:
    """Rewrite an existing recipe to reflect the user's 'Meine Version' changes.

    Same output shape as :func:`generate_recipe`. ``recipe`` is the current
    recipe (title/time/uses/buy/guidance); ``transcript`` is the free-text or
    spoken description of what the user did differently. Degrades to the
    original fields when the model omits something.
    """
    mode = "shop" if mode == "shop" else "stock"
    recipe = recipe or {}
    title = str(recipe.get("title") or "").strip()
    phases = _str_list(recipe.get("guidance"))
    user = "\n".join(
        [
            f"Modus: {mode}",
            f"Gericht: {title}",
            "Bisherige Zeit: " + (str(recipe.get("time") or "").strip() or "—"),
            "Nutzt vor allem: " + (str(recipe.get("uses") or "").strip() or "—"),
            "Bisherige Einkäufe: " + (", ".join(_str_list(recipe.get("buy"))) or "—"),
            "Bisherige Phasen:",
            *([f"- {g}" for g in phases] or ["- —"]),
            "",
            "Meine Version (was ich anders mache): " + str(transcript or "").strip(),
        ]
    )
    data = post_chat_json(
        settings=settings, model=model, system=_REVISE_SYSTEM, user=user, transport=transport
    )
    return {
        "title": str(data.get("title") or title).strip() or title,
        "time": str(data.get("time") or recipe.get("time") or "").strip(),
        "uses": str(data.get("uses") or recipe.get("uses") or "").strip(),
        "buy": [] if mode == "stock" else _str_list(data.get("buy"))[:3],
        "guidance": _str_list(data.get("guidance")) or _str_list(recipe.get("guidance")),
    }


# --- 4. consumption estimate (after cooking) -------------------------------

_CONSUME_SYSTEM = (
    "Der Nutzer hat ein Gericht gekocht und will den Verbrauch in seinem "
    "Inventar verbuchen. Schätze, WELCHE Zutaten und WIE VIEL davon das Gericht "
    "verbraucht hat. Du bekommst:\n"
    "- die Liste der vorhandenen Zutaten ('inventory') mit Menge+Einheit,\n"
    "- optional eine Einkaufsliste ('buy') mit Zutaten, die NICHT im Vorrat "
    "waren und für das Gericht gekauft wurden.\n\n"
    "Regeln:\n"
    "- Verbuche unter 'used' NUR Zutaten aus 'inventory', die das Gericht "
    "wirklich nutzt. Nimm den Namen EXAKT wie in der Liste. 'amount' ist die "
    "verbrauchte Menge in derselben Einheit; übertreibe nicht (eine typische "
    "Portion), und überschreite nie die vorhandene Menge.\n"
    "- Für jede gekaufte Zutat ('buy') gib unter 'bought' an: 'pack_amount' = "
    "wie viel man üblicherweise kauft (eine Packung/ein Bund), 'used_amount' = "
    "wie viel davon im Gericht landete (Rest bleibt im Kühlschrank), plus "
    "'unit'. Im Modus 'stock' ist 'bought' IMMER leer.\n"
    "- Wenn du dir bei einer Zutat unsicher bist, lass sie lieber weg.\n"
    "- NUR wenn der Nutzer in seiner Korrektur AUSDRÜCKLICH eine Zutat als "
    "benutzt nennt, die NICHT in 'inventory' steht (und auch nicht gekauft "
    "wurde), liste sie unter 'missing' mit {name, amount?, unit?}. Verbuche sie "
    "NICHT unter 'used' — sie kann nicht abgezogen werden, der Nutzer soll nur "
    "erfahren, dass sie nicht im Vorrat ist. Ohne solche Nennung bleibt "
    "'missing' LEER. Erfinde hier nichts (kein Salz/Öl/Gewürz von dir aus).\n"
    "- Antworte mit NUR diesem JSON:\n"
    '{"used": [{"name": str, "amount": number, "unit": str|null}], '
    '"bought": [{"name": str, "pack_amount": number, "used_amount": number, '
    '"unit": str|null}], '
    '"missing": [{"name": str, "amount": number|null, "unit": str|null}]}'
)


def estimate_consumption(
    *,
    dish: str,
    guidance: List[str],
    mode: str,
    candidates: List[dict],
    buy: List[str],
    model: str,
    settings,
    correction: str = "",
    transport: Optional[Transport] = None,
) -> dict:
    """Estimate the inventory a cooked dish consumed (+ leftovers from purchases).

    ``candidates`` are the in-stock items (``{name, amount, unit}``); ``buy`` are
    shop-mode purchase names. ``correction`` is an optional spoken/typed nudge
    ("ich hab mehr Knoblauch genommen") that re-shapes the estimate. The model
    refers to items by name only — id resolution happens in the API. Degrades to
    empty lists on any bad field.
    """
    mode = "shop" if mode == "shop" else "stock"
    user = _consume_user_message(
        dish=dish,
        guidance=guidance or [],
        mode=mode,
        candidates=candidates or [],
        buy=buy or [],
        correction=correction,
    )
    data = post_chat_json(
        settings=settings, model=model, system=_CONSUME_SYSTEM, user=user, transport=transport
    )
    used = [
        cleaned
        for row in (data.get("used") if isinstance(data.get("used"), list) else [])
        for cleaned in (_clean_used(row),)
        if cleaned is not None
    ]
    bought = (
        []
        if mode == "stock"
        else [
            cleaned
            for row in (data.get("bought") if isinstance(data.get("bought"), list) else [])
            for cleaned in (_clean_bought(row),)
            if cleaned is not None
        ]
    )
    missing = [
        cleaned
        for row in (data.get("missing") if isinstance(data.get("missing"), list) else [])
        for cleaned in (_clean_missing(row),)
        if cleaned is not None
    ]
    return {"used": used, "bought": bought, "missing": missing}


def _consume_user_message(*, dish, guidance, mode, candidates, buy, correction="") -> str:
    def line(row):
        name = str(row.get("name") or "").strip()
        amount = row.get("amount")
        unit = str(row.get("unit") or "").strip()
        qty = f"{_num(amount):g}" if amount is not None else "?"
        return f"- {name} ({qty} {unit})".rstrip()

    lines = [
        f"Modus: {mode}",
        f"Gericht: {str(dish or '').strip()}",
    ]
    if guidance:
        lines.append("Zubereitung: " + " → ".join(str(g).strip() for g in guidance if str(g).strip()))
    lines.append("Vorhandene Zutaten (inventory):")
    lines.extend(line(c) for c in candidates) if candidates else lines.append("- —")
    if mode == "shop":
        lines.append("Eingekauft (buy): " + (", ".join(str(b).strip() for b in buy if str(b).strip()) or "—"))
    correction = str(correction or "").strip()
    if correction:
        lines.append("Korrektur des Nutzers (berücksichtige sie unbedingt): " + correction)
    return "\n".join(lines)


def _clean_used(row) -> Optional[dict]:
    if not isinstance(row, dict):
        return None
    name = str(row.get("name") or "").strip()
    if not name:
        return None
    amount = _num(row.get("amount"))
    if amount <= 0:
        return None
    return {"name": name, "amount": amount, "unit": _opt_unit(row.get("unit"))}


def _clean_missing(row) -> Optional[dict]:
    """A user-named ingredient that isn't in stock — informational, no amount gate."""
    if not isinstance(row, dict):
        return None
    name = str(row.get("name") or "").strip()
    if not name:
        return None
    amount = _num(row.get("amount"))
    return {
        "name": name,
        "amount": amount if amount > 0 else None,
        "unit": _opt_unit(row.get("unit")),
    }


def _clean_bought(row) -> Optional[dict]:
    if not isinstance(row, dict):
        return None
    name = str(row.get("name") or "").strip()
    if not name:
        return None
    pack = _num(row.get("pack_amount"))
    if pack <= 0:
        pack = 1.0
    used = _num(row.get("used_amount"))
    # Can't use more than the pack holds; default to the whole pack if unstated.
    used = pack if used <= 0 or used > pack else used
    return {
        "name": name,
        "pack_amount": pack,
        "used_amount": used,
        "unit": _opt_unit(row.get("unit")),
    }


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _opt_unit(value) -> Optional[str]:
    unit = str(value or "").strip()
    return unit or None


# --- 3. twist extraction ---------------------------------------------------

_TWIST_SYSTEM = (
    "Der Nutzer beschreibt (gesprochen oder getippt), was er an einem Gericht "
    "ANDERS gemacht hat. Fasse es als strukturierte Notiz zusammen und leite "
    "Geschmacks-Tags ab (was er offenbar mag / nicht mag). Deutsch. Antworte mit "
    "NUR diesem JSON:\n"
    '{"change": str, "note": str, "tags": {"likes": [str], "dislikes": [str]}}'
)


def extract_twist(
    *, transcript: str, dish: str, model: str, settings, transport: Optional[Transport] = None
) -> dict:
    """Structure a free-text/spoken 'what I did differently' into a twist + tags."""
    user = "\n".join(
        [
            f"Gericht: {str(dish or '').strip()}",
            f"Beschreibung: {str(transcript or '').strip()}",
        ]
    )
    data = post_chat_json(
        settings=settings, model=model, system=_TWIST_SYSTEM, user=user, transport=transport
    )
    tags = data.get("tags") if isinstance(data.get("tags"), dict) else {}
    return {
        "change": str(data.get("change") or "").strip(),
        "note": str(data.get("note") or "").strip(),
        "tags": {
            "likes": _str_list(tags.get("likes")),
            "dislikes": _str_list(tags.get("dislikes")),
        },
    }


# --- helpers ---------------------------------------------------------------


def _str_list(value) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if str(x).strip()]
