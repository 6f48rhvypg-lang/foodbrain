"""Recipe loading and ingredient line parsing.

The first implementation reads recipes from local JSON files so recipe matching
can be developed and tested without a live Grocy, Mealie, or Tandoor instance.
The expected file shape is a list of recipe objects, or an object with a
``recipes`` list::

    [
      {
        "name": "Spinach Omelette",
        "ingredients": ["2 eggs", "1 bag spinach", "salt to taste"]
      }
    ]

Each ingredient may be a plain line (``"2 cups flour"``) or an object with an
explicit ``name`` and optional ``quantity`` / ``unit``. Parsing is deterministic
and dependency-free; it favors predictable behavior over clever extraction.
"""

import re
from typing import Any, Optional, Tuple

from .models import Recipe, RecipeIngredient
from .normalization import normalize_ingredient_name


class RecipesError(RuntimeError):
    pass


# Units we recognize at the start of an ingredient line. Anything else after the
# quantity is treated as part of the ingredient name.
_KNOWN_UNITS = {
    "g", "kg", "mg", "ml", "l", "cl", "dl",
    "tsp", "teaspoon", "teaspoons",
    "tbsp", "tablespoon", "tablespoons",
    "cup", "cups",
    "clove", "cloves",
    "slice", "slices",
    "piece", "pieces",
    "pinch", "pinches",
    "can", "cans",
    "tin", "tins",
    "tub", "tubs",
    "jar", "jars",
    "carton", "cartons",
    "box", "boxes",
    "bottle", "bottles",
    "bag", "bags",
    "bunch", "bunches",
    "head", "heads",
    "pack", "packs", "packet", "packets",
    "stick", "sticks",
    "sprig", "sprigs",
    "handful", "handfuls",
}

_TRAILING_QUALIFIER = re.compile(
    r",?\s*(to taste|finely chopped|chopped|diced|minced|sliced|grated|optional)\s*$",
    re.IGNORECASE,
)
_LEADING_QUANTITY = re.compile(
    r"^\s*(?:"
    r"(?P<whole>\d+)\s+(?P<mixed_frac>\d+\s*/\s*\d+)"  # "1 1/2"
    r"|(?P<frac>\d+\s*/\s*\d+)"  # "1/2"
    r"|(?P<dec>\d+\.\d+)"  # "0.5"
    r"|(?P<int>\d+)"  # "2"
    r")?\s*(?P<rest>.*)$"
)


def parse_recipes_response(payload: Any, source: str = "local") -> list[Recipe]:
    rows = _require_recipe_rows(payload)
    recipes = []
    for index, row in enumerate(rows):
        recipes.append(_parse_recipe(row, index=index, source=source))
    return recipes


def _require_recipe_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and "recipes" in payload:
        payload = payload["recipes"]
    if not isinstance(payload, list):
        raise RecipesError("Recipes file must be a list of recipes or {\"recipes\": [...]}")

    rows = []
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            raise RecipesError(f"Recipe {index} was not an object")
        rows.append(row)
    return rows


def _parse_recipe(row: dict[str, Any], index: int, source: str) -> Recipe:
    name = str(row.get("name") or "").strip()
    if not name:
        raise RecipesError(f"Recipe {index} is missing a name")

    raw_ingredients = row.get("ingredients")
    if not isinstance(raw_ingredients, list) or not raw_ingredients:
        raise RecipesError(f"Recipe {name!r} has no ingredients list")

    ingredients = []
    for raw in raw_ingredients:
        ingredient = _parse_ingredient(raw)
        if ingredient is not None:
            ingredients.append(ingredient)
    if not ingredients:
        raise RecipesError(f"Recipe {name!r} had no usable ingredients")

    return Recipe(name=name, ingredients=ingredients, source=source)


def _parse_ingredient(raw: Any) -> Optional[RecipeIngredient]:
    if isinstance(raw, dict):
        return _parse_ingredient_dict(raw)
    if isinstance(raw, str):
        return parse_ingredient_line(raw)
    return None


def _parse_ingredient_dict(raw: dict[str, Any]) -> Optional[RecipeIngredient]:
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    quantity = raw.get("quantity") or raw.get("amount")
    return RecipeIngredient(
        raw=name,
        name=normalize_ingredient_name(name),
        quantity=_as_float(quantity),
        unit=_blank_to_none(raw.get("unit")),
    )


def parse_ingredient_line(line: str) -> Optional[RecipeIngredient]:
    raw = line.strip()
    if not raw:
        return None

    remainder = _TRAILING_QUALIFIER.sub("", raw).strip()
    quantity, rest = _split_leading_quantity(remainder)
    unit, name_text = _split_leading_unit(rest)

    name = normalize_ingredient_name(name_text)
    if not name:
        # Lines like "2 cups" with no ingredient name are not usable.
        return None

    return RecipeIngredient(raw=raw, name=name, quantity=quantity, unit=unit)


def _split_leading_quantity(text: str) -> Tuple[Optional[float], str]:
    match = _LEADING_QUANTITY.match(text)
    if not match:
        return None, text

    rest = match.group("rest")
    integer = match.group("int")
    dec = match.group("dec")
    whole = match.group("whole")
    mixed_frac = match.group("mixed_frac")
    frac = match.group("frac")

    if not (integer or dec or whole or frac):
        return None, text

    total = 0.0
    if integer:
        total += float(integer)
    if dec:
        total += float(dec)
    if whole:
        total += float(whole)
    if mixed_frac:
        total += _fraction_value(mixed_frac)
    if frac:
        total += _fraction_value(frac)
    return total, rest.strip()


def _fraction_value(fraction: str) -> float:
    numerator, denominator = (part.strip() for part in fraction.split("/"))
    if float(denominator) == 0:
        return 0.0
    return float(numerator) / float(denominator)


def _split_leading_unit(text: str) -> Tuple[Optional[str], str]:
    parts = text.split(maxsplit=1)
    if len(parts) == 2 and parts[0].lower().strip(".") in _KNOWN_UNITS:
        return parts[0].lower().strip("."), parts[1]
    return None, text


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _blank_to_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None
