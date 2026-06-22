"""Ingredient name normalization helpers."""

import re
from typing import Any, Dict, List, Optional, Set


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


def tokenize(name: str, aliases: Optional[Dict[str, str]] = None) -> Set[str]:
    """Normalize, split, and singularize a name into a token set.

    Shared by recipe matching (matching.py), flavor pairing (pairing.py), and
    the API connect view (api.py) so stock names resolve identically everywhere.
    """
    normalized = normalize_ingredient_name(name, aliases)
    return {singularize(token) for token in normalized.split() if token}


def tokens_match(left: Set[str], right: Set[str]) -> bool:
    """True when one token set is a (non-empty) subset of the other."""
    if not left or not right:
        return False
    return left <= right or right <= left


def str_list(value: Any) -> List[str]:
    """Coerce a value into a list of non-empty, stripped strings."""
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if str(x).strip()]


def blank_to_none(value: Any) -> Optional[str]:
    """Stringify, strip, and collapse empty/None into None."""
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def singularize(word: str) -> str:
    if len(word) <= 3:
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("oes", "ches", "shes", "sses")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word
