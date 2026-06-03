"""Ingredient name normalization helpers."""

import re
from typing import Dict, Optional


_WHITESPACE = re.compile(r"\s+")
_TRAILING_NOTES = re.compile(r"\s*\([^)]*\)\s*$")


def normalize_ingredient_name(
    name: str, aliases: Optional[Dict[str, str]] = None
) -> str:
    cleaned = name.strip().lower()
    cleaned = _TRAILING_NOTES.sub("", cleaned)
    cleaned = cleaned.replace("&", "and")
    cleaned = _WHITESPACE.sub(" ", cleaned)
    if aliases:
        cleaned = _apply_aliases(cleaned, aliases)
    return cleaned


def _apply_aliases(name: str, aliases: Dict[str, str]) -> str:
    """Map a normalized name to its alias, whole-name first then per token.

    Aliases are applied after lowercasing/whitespace cleanup but before any
    singularization in the tokenizers, so both ``milch`` and a multiword
    ``bio milch`` resolve to ``milk``.
    """
    if name in aliases:
        return aliases[name]
    tokens = name.split()
    if len(tokens) <= 1:
        return name
    return " ".join(aliases.get(token, token) for token in tokens)
