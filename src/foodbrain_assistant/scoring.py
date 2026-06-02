"""Expiry-aware ingredient scoring."""

from datetime import date
from typing import Iterable

from .models import IngredientUrgency, StockItem
from .normalization import normalize_ingredient_name


def rank_ingredients_by_urgency(
    stock_items: Iterable[StockItem],
    today: date,
    expiry_window_days: int = 7,
    limit: int = 8,
) -> list[IngredientUrgency]:
    ranked = [
        score_stock_item(item, today=today, expiry_window_days=expiry_window_days)
        for item in stock_items
        if item.amount > 0
    ]
    ranked.sort(
        key=lambda urgency: (
            urgency.urgency_score,
            normalize_ingredient_name(urgency.item.name),
        ),
        reverse=True,
    )
    return ranked[:limit]


def score_stock_item(
    item: StockItem,
    today: date,
    expiry_window_days: int = 7,
) -> IngredientUrgency:
    if item.best_before_date is None:
        return IngredientUrgency(
            item=item,
            days_until_expiry=None,
            urgency_score=0.1,
            reason="No expiry date tracked yet",
        )

    days_until_expiry = (item.best_before_date - today).days
    if days_until_expiry < 0:
        return IngredientUrgency(
            item=item,
            days_until_expiry=days_until_expiry,
            urgency_score=1.0,
            reason="Overdue",
        )
    if days_until_expiry == 0:
        return IngredientUrgency(
            item=item,
            days_until_expiry=days_until_expiry,
            urgency_score=0.95,
            reason="Expires today",
        )
    if days_until_expiry <= expiry_window_days:
        urgency_score = 0.2 + (expiry_window_days - days_until_expiry + 1) / (
            expiry_window_days + 1
        ) * 0.7
        return IngredientUrgency(
            item=item,
            days_until_expiry=days_until_expiry,
            urgency_score=round(urgency_score, 3),
            reason=f"Expires in {days_until_expiry} days",
        )

    return IngredientUrgency(
        item=item,
        days_until_expiry=days_until_expiry,
        urgency_score=0.0,
        reason=f"Expires after the {expiry_window_days}-day planning window",
    )
