"""Command-line interface for FoodBrain."""

from argparse import ArgumentParser
from datetime import date, timedelta
import json
from pathlib import Path
from typing import Optional, Sequence

from .config import load_settings
from .grocy_client import GrocyClientError, diagnose_stock_response, parse_stock_response
from .models import RunResult, StockItem
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

    try:
        settings = load_settings()
        stock_items = None
        stock_source_name = "sample"
        if args.sample:
            stock_items = _sample_stock()
        elif args.grocy_stock_json:
            stock_items = _load_grocy_stock_json(args.grocy_stock_json)
            stock_source_name = "grocy-json"

        result = run_once_with_source(
            settings,
            stock_items=stock_items,
            stock_source=stock_source_name,
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


def _load_json_file(path: Path) -> object:
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except OSError as exc:
        raise SystemExit(f"Could not read Grocy stock JSON file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Grocy stock JSON file was not valid JSON: {exc}") from exc


def _print_text(result: RunResult) -> None:
    print(f"FoodBrain run source: {result.source}")
    print("Use these ingredients first:")
    for urgency in result.urgent_ingredients:
        unit = f" {urgency.item.unit}" if urgency.item.unit else ""
        print(
            f"- {urgency.item.name}: {urgency.item.amount:g}{unit}; "
            f"{urgency.reason}; score {urgency.urgency_score:g}"
        )


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
    }


if __name__ == "__main__":
    raise SystemExit(main())
