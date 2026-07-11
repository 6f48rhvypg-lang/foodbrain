"""AI diet-focus suggestions for the shopping list ("mehr Gemüse", "proteinreich", ...).

Mirrors :mod:`recipes_llm`'s shape: one function, calling
:func:`foodbrain_assistant.llm.post_chat_json`, injectable ``transport`` for
tests, explicit ``model``. On-demand only (the user picks a focus and taps a
button) — never called automatically, to keep LLM spend opt-in.

Every suggestion the model returns carries a plain-language ``reason`` — the
shopping list's hard requirement that nothing gets added silently.
"""

from typing import List, Optional

from .llm import Transport, post_chat_json

_DIET_SYSTEM = (
    "Du bist ein Ernährungsberater, der eine Einkaufsliste ergänzt. Der Nutzer "
    "hat einen Fokus gewählt (z.B. 'mehr Gemüse', 'proteinreich', 'Vorrat "
    "auffüllen'). Du bekommst seinen aktuellen Vorrat ('inventory') und darfst "
    "NICHTS vorschlagen, das schon in ausreichender Menge da ist.\n\n"
    "Schlage 3–8 konkrete, einzeln kaufbare Lebensmittel vor, die den Fokus "
    "erfüllen. Für JEDEN Vorschlag ist ein kurzer, verständlicher Grund im Feld "
    "'reason' PFLICHT (z.B. 'liefert Protein für den Fokus', 'passt zu deinem "
    "Vorrat an Reis'). Berücksichtige 'taste' (mag/mag nicht) als sanften "
    "Hinweis, keinen harten Filter. Schlage keine Zutat vor, die schon im "
    "Vorrat ist. Deutsche Namen. Antworte mit NUR diesem JSON:\n"
    '{"items": [{"name": str, "amount": number|null, "unit": str|null, '
    '"reason": str}]}'
)


def suggest_diet_items(
    *,
    focus: str,
    inventory_lines: List[str],
    taste: dict,
    model: str,
    settings,
    transport: Optional[Transport] = None,
) -> dict:
    """Suggest shopping-list items that serve a diet ``focus`` given current stock."""
    focus = str(focus or "").strip()
    user = _diet_user_message(focus=focus, inventory_lines=inventory_lines or [], taste=taste or {})
    data = post_chat_json(
        settings=settings, model=model, system=_DIET_SYSTEM, user=user, transport=transport
    )
    items = []
    for row in data.get("items", []) if isinstance(data.get("items"), list) else []:
        item = _clean_item(row)
        if item is not None:
            items.append(item)
    return {"focus": focus, "items": items[:8]}


def _diet_user_message(*, focus: str, inventory_lines: List[str], taste: dict) -> str:
    lines = [f"Fokus: {focus or '—'}", "Vorrat (inventory):"]
    lines.extend(f"- {line}" for line in inventory_lines) if inventory_lines else lines.append("- —")
    lines.append("Mag: " + (", ".join(taste.get("likes", [])) or "—"))
    lines.append("Mag nicht: " + (", ".join(taste.get("dislikes", [])) or "—"))
    return "\n".join(lines)


def _clean_item(row) -> Optional[dict]:
    if not isinstance(row, dict):
        return None
    name = str(row.get("name") or "").strip()
    reason = str(row.get("reason") or "").strip()
    if not name or not reason:
        return None
    amount = row.get("amount")
    try:
        amount = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount = None
    if amount is not None and amount <= 0:
        amount = None
    unit = str(row.get("unit") or "").strip() or None
    return {"name": name, "amount": amount, "unit": unit, "reason": reason}
