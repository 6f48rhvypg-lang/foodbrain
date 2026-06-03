"""Command-line interface for FoodBrain."""

from argparse import ArgumentParser
from datetime import date, timedelta
import json
from pathlib import Path
from typing import Optional, Sequence

from .aliases import AliasError, load_aliases, merge_aliases
from .config import Settings, load_settings
from .grocy_client import (
    GrocyClient,
    GrocyClientError,
    diagnose_stock_response,
    parse_stock_response,
)
from .models import Recipe, RunResult, StockItem
from .pairing import PairingError, PairingGraph, load_pairings
from .recipes import (
    RecipesError,
    diagnose_grocy_recipes,
    parse_grocy_recipes_response,
    parse_recipes_response,
)
from .service import run_once_with_source


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = ArgumentParser(description="FoodBrain kitchen recommendation helper")
    stock_source = parser.add_mutually_exclusive_group()
    stock_source.add_argument(
        "--sample",
        action="store_true",
        help="Run against built-in sample stock instead of Grocy.",
    )
    stock_source.add_argument(
        "--grocy-stock-json",
        type=Path,
        metavar="PATH",
        help="Run against an exported Grocy /api/stock JSON response.",
    )
    stock_source.add_argument(
        "--diagnose-grocy-stock-json",
        type=Path,
        metavar="PATH",
        help="Validate and summarize an exported Grocy /api/stock JSON response.",
    )
    stock_source.add_argument(
        "--diagnose-grocy-recipes-json",
        type=Path,
        metavar="PATH",
        help="Validate and summarize an exported Grocy recipes bundle JSON file.",
    )
    recipe_source = parser.add_mutually_exclusive_group()
    recipe_source.add_argument(
        "--recipes-json",
        type=Path,
        metavar="PATH",
        help="Match a local recipes JSON file against the chosen stock.",
    )
    recipe_source.add_argument(
        "--grocy-recipes-json",
        type=Path,
        metavar="PATH",
        help="Match an exported Grocy recipes bundle JSON file against the chosen stock.",
    )
    recipe_source.add_argument(
        "--grocy-recipes",
        action="store_true",
        help="Fetch recipes live from Grocy and match them against the chosen stock.",
    )
    parser.add_argument(
        "--pairings-json",
        type=Path,
        metavar="PATH",
        help="Suggest FlavorGraph-style pairings for urgent ingredients from a "
        "local pairings JSON file.",
    )
    parser.add_argument(
        "--aliases-json",
        type=Path,
        metavar="PATH",
        help="Map non-English ingredient names to the English vocabulary using a "
        "flat {\"source\": \"target\"} JSON file. Defaults to "
        "examples/aliases.sample.json plus any .foodbrain-local/aliases.json "
        "override when present.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output.",
    )
    args = parser.parse_args(argv)

    if args.diagnose_grocy_stock_json:
        diagnostics = diagnose_stock_response(
            _load_json_file(args.diagnose_grocy_stock_json)
        )
        print(json.dumps(diagnostics, indent=2))
        return 1 if diagnostics["errors"] else 0

    if args.diagnose_grocy_recipes_json:
        bundle = _grocy_recipes_bundle(_load_json_file(args.diagnose_grocy_recipes_json))
        diagnostics = diagnose_grocy_recipes(**bundle)
        print(json.dumps(diagnostics, indent=2))
        return 1 if diagnostics["errors"] else 0

    try:
        settings = load_settings()
        stock_items = None
        stock_source_name = "sample"
        if args.sample:
            stock_items = _sample_stock()
        elif args.grocy_stock_json:
            stock_items = _load_grocy_stock_json(args.grocy_stock_json)
            stock_source_name = "grocy-json"

        recipes = _load_recipes(args, settings)
        pairings = _load_pairings(args)
        aliases = _load_aliases(args)

        result = run_once_with_source(
            settings,
            stock_items=stock_items,
            stock_source=stock_source_name,
            recipes=recipes,
            pairings=pairings,
            aliases=aliases,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    if args.json:
        print(json.dumps(_result_to_json(result), indent=2))
    else:
        _print_text(result)
    return 0


def _sample_stock() -> list[StockItem]:
    today = date.today()
    return [
        StockItem("1", "Spinach", 1, "bag", today),
        StockItem("2", "Greek yogurt", 1, "tub", today + timedelta(days=2)),
        StockItem("3", "Carrots", 5, "pieces", today + timedelta(days=8)),
        StockItem("4", "Rice", 1, "kg", None),
    ]


def _load_grocy_stock_json(path: Path) -> list[StockItem]:
    payload = _load_json_file(path)
    try:
        return parse_stock_response(payload)
    except GrocyClientError as exc:
        raise SystemExit(str(exc)) from exc


def _load_recipes(args, settings: Settings) -> Optional[list[Recipe]]:
    try:
        if args.recipes_json:
            return parse_recipes_response(_load_json_file(args.recipes_json))
        if args.grocy_recipes_json:
            bundle = _grocy_recipes_bundle(_load_json_file(args.grocy_recipes_json))
            return parse_grocy_recipes_response(**bundle)
        if args.grocy_recipes:
            if not settings.grocy_enabled:
                raise SystemExit(
                    "Set FOODBRAIN_GROCY_BASE_URL and FOODBRAIN_GROCY_API_KEY to use "
                    "--grocy-recipes, or pass --recipes-json / --grocy-recipes-json."
                )
            return GrocyClient(
                base_url=settings.grocy_base_url or "",
                api_key=settings.grocy_api_key or "",
            ).get_recipes()
    except (RecipesError, GrocyClientError) as exc:
        raise SystemExit(str(exc)) from exc
    return None


def _grocy_recipes_bundle(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise SystemExit(
            "Grocy recipes bundle must be a JSON object with "
            "'recipes', 'recipes_pos', and 'products' keys."
        )
    missing = [key for key in ("recipes", "recipes_pos", "products") if key not in payload]
    if missing:
        raise SystemExit(
            "Grocy recipes bundle is missing required keys: " + ", ".join(missing)
        )
    return {
        "recipes": payload["recipes"],
        "positions": payload["recipes_pos"],
        "products": payload["products"],
        "quantity_units": payload.get("quantity_units"),
    }


def _load_pairings(args) -> Optional[PairingGraph]:
    if not args.pairings_json:
        return None
    try:
        return load_pairings(_load_json_file(args.pairings_json))
    except PairingError as exc:
        raise SystemExit(str(exc)) from exc


def _load_aliases(args) -> Optional[dict[str, str]]:
    """Resolve the alias map.

    An explicit ``--aliases-json`` wins outright. Otherwise the bundled
    ``examples/aliases.sample.json`` is used when present, with an optional
    private ``.foodbrain-local/aliases.json`` layered on top so household-specific
    mappings stay uncommitted. Returns ``None`` when no map is available.
    """
    try:
        if args.aliases_json:
            return load_aliases(_load_json_file(args.aliases_json))

        repo_root = Path(__file__).resolve().parents[2]
        sample = repo_root / "examples" / "aliases.sample.json"
        override = repo_root / ".foodbrain-local" / "aliases.json"

        aliases: Optional[dict[str, str]] = None
        if sample.is_file():
            aliases = load_aliases(_load_json_file(sample))
        if override.is_file():
            local = load_aliases(_load_json_file(override))
            aliases = merge_aliases(aliases or {}, local)
        return aliases
    except AliasError as exc:
        raise SystemExit(str(exc)) from exc


def _load_json_file(path: Path) -> object:
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except OSError as exc:
        raise SystemExit(f"Could not read JSON file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"File was not valid JSON: {exc}") from exc


def _print_text(result: RunResult) -> None:
    print(f"FoodBrain run source: {result.source}")
    print("Use these ingredients first:")
    for urgency in result.urgent_ingredients:
        unit = f" {urgency.item.unit}" if urgency.item.unit else ""
        print(
            f"- {urgency.item.name}: {urgency.item.amount:g}{unit}; "
            f"{urgency.reason}; score {urgency.urgency_score:g}"
        )

    if result.recipe_matches:
        print("\nCook one of these:")
        for match in result.recipe_matches:
            missing = ", ".join(ingredient.name for ingredient in match.missing)
            missing_note = f"; missing: {missing}" if missing else "; nothing missing"
            print(
                f"- {match.recipe.name}: {match.coverage * 100:.0f}% in stock; "
                f"score {match.score:g}{missing_note}"
            )

    if result.flavor_suggestions:
        print("\nFlavor pairings:")
        for suggestion in result.flavor_suggestions:
            partners = ", ".join(
                f"{partner.name}{' (in stock)' if partner.in_stock else ''}"
                for partner in suggestion.partners
            )
            print(f"- {suggestion.ingredient} pairs with: {partners}")


def _result_to_json(result: RunResult) -> dict[str, object]:
    return {
        "source": result.source,
        "urgent_ingredients": [
            {
                "name": urgency.item.name,
                "amount": urgency.item.amount,
                "unit": urgency.item.unit,
                "best_before_date": urgency.item.best_before_date.isoformat()
                if urgency.item.best_before_date
                else None,
                "days_until_expiry": urgency.days_until_expiry,
                "urgency_score": urgency.urgency_score,
                "reason": urgency.reason,
            }
            for urgency in result.urgent_ingredients
        ],
        "recipe_matches": [
            {
                "name": match.recipe.name,
                "coverage": match.coverage,
                "expiry_usefulness": match.expiry_usefulness,
                "score": match.score,
                "matched": [ingredient.name for ingredient in match.matched],
                "missing": [ingredient.name for ingredient in match.missing],
            }
            for match in result.recipe_matches
        ],
        "flavor_suggestions": [
            {
                "ingredient": suggestion.ingredient,
                "urgency_score": suggestion.urgency_score,
                "partners": [
                    {
                        "name": partner.name,
                        "score": partner.score,
                        "in_stock": partner.in_stock,
                    }
                    for partner in suggestion.partners
                ],
            }
            for suggestion in result.flavor_suggestions
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
