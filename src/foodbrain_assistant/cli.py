"""Command-line interface for FoodBrain."""

from argparse import ArgumentParser
from datetime import date, timedelta
import json
from typing import Optional, Sequence

from .config import load_settings
from .models import RunResult, StockItem
from .service import run_once


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = ArgumentParser(description="FoodBrain kitchen recommendation helper")
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Run against built-in sample stock instead of Grocy.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output.",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    result = run_once(settings, stock_items=_sample_stock() if args.sample else None)

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
