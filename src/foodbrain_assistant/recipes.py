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

Recipes can also come from live Grocy via ``parse_grocy_recipes_response``,
which joins the ``recipes``, ``recipes_pos``, and ``products`` object endpoints
into the same ``Recipe`` model used for local files.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from .models import Recipe, RecipeIngredient
from .normalization import blank_to_none as _blank_to_none, normalize_ingredient_name


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


# --- Grocy recipe objects -------------------------------------------------
#
# Grocy stores recipes across separate object endpoints rather than as
# free-text lines, so building Recipe objects is a join rather than a parse:
#
#   /api/objects/recipes        -> recipe id + name (and a `type`)
#   /api/objects/recipes_pos    -> ingredient rows (recipe_id, product_id, amount)
#   /api/objects/products       -> product id -> name
#   /api/objects/quantity_units -> optional unit id -> name
#
# Grocy also creates internal recipes for meal-plan entries (type
# "mealplan-day" / "mealplan-week"); only `type == "normal"` (or a missing
# type) is treated as a real recipe. Like stock parsing, this is tolerant of
# bad rows: unresolvable ingredients and empty recipes are skipped rather than
# raising, so one bad row does not break a live run. Use
# ``diagnose_grocy_recipes`` to see what was skipped.


def parse_grocy_recipes_response(
    recipes: Any,
    positions: Any,
    products: Any,
    quantity_units: Any = None,
) -> List[Recipe]:
    recipe_rows = _as_object_list(recipes, "recipes")
    position_rows = _as_object_list(positions, "recipes_pos")
    product_rows = _as_object_list(products, "products")
    unit_rows = _as_object_list(quantity_units, "quantity_units") if quantity_units else []

    product_name_by_id = _name_index(product_rows)
    unit_name_by_id = _name_index(unit_rows)
    positions_by_recipe = _group_positions_by_recipe(position_rows)

    parsed: List[Recipe] = []
    for row in recipe_rows:
        if not _is_normal_recipe(row):
            continue
        recipe_id = _str_id(row.get("id"))
        name = str(row.get("name") or "").strip()
        if not recipe_id or not name:
            continue

        ingredients = _grocy_recipe_ingredients(
            positions_by_recipe.get(recipe_id, []),
            product_name_by_id,
            unit_name_by_id,
        )
        if ingredients:
            parsed.append(Recipe(name=name, ingredients=ingredients, source="grocy"))
    return parsed


def diagnose_grocy_recipes(
    recipes: Any,
    positions: Any,
    products: Any,
    quantity_units: Any = None,
) -> Dict[str, object]:
    diagnostics: Dict[str, object] = {
        "recipe_count": 0,
        "normal_recipe_count": 0,
        "parsed_recipe_count": 0,
        "skipped_empty_recipe_count": 0,
        "unresolved_product_count": 0,
        "errors": [],
        "warnings": [],
    }
    errors = diagnostics["errors"]
    warnings = diagnostics["warnings"]
    assert isinstance(errors, list)
    assert isinstance(warnings, list)

    try:
        recipe_rows = _as_object_list(recipes, "recipes")
        position_rows = _as_object_list(positions, "recipes_pos")
        product_rows = _as_object_list(products, "products")
        unit_rows = (
            _as_object_list(quantity_units, "quantity_units") if quantity_units else []
        )
    except RecipesError as exc:
        errors.append(str(exc))
        return diagnostics

    product_name_by_id = _name_index(product_rows)
    unit_name_by_id = _name_index(unit_rows)
    positions_by_recipe = _group_positions_by_recipe(position_rows)

    diagnostics["recipe_count"] = len(recipe_rows)
    normal_recipes = [row for row in recipe_rows if _is_normal_recipe(row)]
    diagnostics["normal_recipe_count"] = len(normal_recipes)

    parsed = 0
    skipped_empty = 0
    unresolved = 0
    for row in normal_recipes:
        recipe_id = _str_id(row.get("id"))
        name = str(row.get("name") or "").strip()
        if not recipe_id or not name:
            errors.append("a normal recipe is missing an id or name")
            continue
        rows = positions_by_recipe.get(recipe_id, [])
        for position in rows:
            if _str_id(position.get("product_id")) not in product_name_by_id:
                unresolved += 1
                warnings.append(
                    f"recipe {name!r}: ingredient product "
                    f"{position.get('product_id')} not found in products"
                )
        ingredients = _grocy_recipe_ingredients(
            rows, product_name_by_id, unit_name_by_id
        )
        if ingredients:
            parsed += 1
        else:
            skipped_empty += 1
            warnings.append(f"recipe {name!r}: no resolvable ingredients, skipped")

    diagnostics["parsed_recipe_count"] = parsed
    diagnostics["skipped_empty_recipe_count"] = skipped_empty
    diagnostics["unresolved_product_count"] = unresolved
    if normal_recipes and not parsed:
        errors.append("no recipes could be parsed from a non-empty recipe set")
    return diagnostics


def _grocy_recipe_ingredients(
    positions: List[Dict[str, Any]],
    product_name_by_id: Dict[str, str],
    unit_name_by_id: Dict[str, str],
) -> List[RecipeIngredient]:
    ingredients: List[RecipeIngredient] = []
    for position in positions:
        product_id = _str_id(position.get("product_id"))
        product_name = product_name_by_id.get(product_id)
        if not product_name:
            continue
        ingredients.append(
            RecipeIngredient(
                raw=product_name,
                name=normalize_ingredient_name(product_name),
                quantity=_as_float(position.get("amount")),
                unit=unit_name_by_id.get(_str_id(position.get("qu_id"))) or None,
            )
        )
    return ingredients


def _group_positions_by_recipe(
    positions: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for position in positions:
        recipe_id = _str_id(position.get("recipe_id"))
        if recipe_id:
            grouped.setdefault(recipe_id, []).append(position)
    return grouped


def _name_index(rows: List[Dict[str, Any]]) -> Dict[str, str]:
    index: Dict[str, str] = {}
    for row in rows:
        row_id = _str_id(row.get("id"))
        name = row.get("name")
        if row_id and name:
            index[row_id] = str(name)
    return index


def _is_normal_recipe(row: Dict[str, Any]) -> bool:
    recipe_type = row.get("type")
    return recipe_type in (None, "", "normal")


def _str_id(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _as_object_list(payload: Any, label: str) -> List[Dict[str, Any]]:
    if not isinstance(payload, list):
        raise RecipesError(f"Grocy {label} payload was not a list")
    rows = []
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            raise RecipesError(f"Grocy {label} row {index} was not an object")
        rows.append(row)
    return rows
