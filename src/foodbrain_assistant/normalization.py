"""Ingredient name normalization helpers."""

import re


_WHITESPACE = re.compile(r"\s+")
_TRAILING_NOTES = re.compile(r"\s*\([^)]*\)\s*$")


def normalize_ingredient_name(name: str) -> str:
    cleaned = name.strip().lower()
    cleaned = _TRAILING_NOTES.sub("", cleaned)
    cleaned = cleaned.replace("&", "and")
    cleaned = _WHITESPACE.sub(" ", cleaned)
    return cleaned
