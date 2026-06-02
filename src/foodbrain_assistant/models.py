"""Domain models used by the recommendation service."""

from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class StockItem:
    product_id: str
    name: str
    amount: float
    unit: Optional[str]
    best_before_date: Optional[date]
    opened_date: Optional[date] = None
    location: Optional[str] = None


@dataclass(frozen=True)
class IngredientUrgency:
    item: StockItem
    days_until_expiry: Optional[int]
    urgency_score: float
    reason: str


@dataclass(frozen=True)
class RunResult:
    urgent_ingredients: list[IngredientUrgency]
    source: str
