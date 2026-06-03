"""Ingredient name aliases (e.g. German -> English).

FlavorGraph nodes and the bundled sample recipes are English, but a live Grocy
household may name products in another language (Milch, Eier, ...). An alias map
lets those names resolve to the English ingredient vocabulary used everywhere
else, keeping the lookup offline, deterministic, and data-driven.

The map is a flat ``{ "source": "target" }`` dict of normalized names. It is
applied inside :func:`foodbrain_assistant.normalization.normalize_ingredient_name`
so a single map fixes both recipe matching and flavor pairing lookups. Both the
whole normalized name and individual tokens are aliased, so ``Milch`` and a
multiword ``Bio Milch`` both map to ``milk``.
"""

from typing import Any, Dict

from .normalization import normalize_ingredient_name


class AliasError(RuntimeError):
    pass


def load_aliases(payload: Any) -> Dict[str, str]:
    """Build a normalized alias map from a flat string->string JSON object."""
    if not isinstance(payload, dict):
        raise AliasError('Aliases file must be a flat {"source": "target"} object')

    aliases: Dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise AliasError(
                f"Alias {key!r} -> {value!r} must map a string to a string"
            )
        source = normalize_ingredient_name(key)
        target = normalize_ingredient_name(value)
        if not source or not target:
            raise AliasError(f"Alias {key!r} -> {value!r} is empty after normalization")
        aliases[source] = target
    return aliases


def merge_aliases(base: Dict[str, str], override: Dict[str, str]) -> Dict[str, str]:
    """Return a new map with ``override`` entries layered over ``base``."""
    merged = dict(base)
    merged.update(override)
    return merged
