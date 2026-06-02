"""Domain models used by the recommendation service."""

from dataclasses import dataclass, field
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
class RecipeIngredient:
    raw: str
    name: str
    quantity: Optional[float] = None
    unit: Optional[str] = None


@dataclass(frozen=True)
class Recipe:
    name: str
    ingredients: list[RecipeIngredient]
    source: str = "local"


@dataclass(frozen=True)
class RecipeMatch:
    recipe: Recipe
    matched: list[RecipeIngredient]
    missing: list[RecipeIngredient]
    coverage: float
    expiry_usefulness: float
    score: float


@dataclass(frozen=True)
class FlavorPartner:
    name: str
    score: float
    in_stock: bool


@dataclass(frozen=True)
class FlavorSuggestion:
    ingredient: str
    urgency_score: float
    partners: list[FlavorPartner]


@dataclass(frozen=True)
class RunResult:
    urgent_ingredients: list[IngredientUrgency]
    source: str
    recipe_matches: list[RecipeMatch] = field(default_factory=list)
    flavor_suggestions: list[FlavorSuggestion] = field(default_factory=list)
