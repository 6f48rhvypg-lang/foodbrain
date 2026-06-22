"""Match recipes against available stock and rank them.

Ranking favors recipes you can mostly cook from what is on hand
(``coverage``) and recipes that consume soon-to-expire ingredients
(``expiry_usefulness``). Both signals are deterministic and computed only from
stock and expiry data, in keeping with the project rule that inventory scoring
stays predictable.

Ingredient matching is intentionally a simple, explainable heuristic: an
ingredient and a stock item match when the word set of one is contained in the
word set of the other, after lowercasing and a light singularization. This lets
"spinach" match "baby spinach" and "yogurt" match "greek yogurt" without a
dependency on any fuzzy-matching library. It can occasionally over-match (for
example a recipe calling for "rice vinegar" when only "rice" is stocked); that
is an accepted limitation for the first implementation.
"""

from datetime import date
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .models import Recipe, RecipeIngredient, RecipeMatch, StockItem
from .normalization import tokenize, tokens_match
from .scoring import score_stock_item


COVERAGE_WEIGHT = 1.0
EXPIRY_WEIGHT = 0.5


def rank_recipes(
    recipes: Iterable[Recipe],
    stock_items: Iterable[StockItem],
    today: date,
    expiry_window_days: int = 7,
    limit: int = 10,
    aliases: Optional[Dict[str, str]] = None,
) -> List[RecipeMatch]:
    stock_index = _build_stock_index(stock_items, today, expiry_window_days, aliases)
    matches = [match_recipe(recipe, stock_index, aliases) for recipe in recipes]
    matches.sort(key=lambda match: (-match.score, -match.coverage, match.recipe.name))
    return matches[:limit]


def match_recipe(
    recipe: Recipe,
    stock_index: List[Tuple[Set[str], float]],
    aliases: Optional[Dict[str, str]] = None,
) -> RecipeMatch:
    matched: List[RecipeIngredient] = []
    missing: List[RecipeIngredient] = []
    expiry_usefulness = 0.0

    for ingredient in recipe.ingredients:
        urgency = _best_stock_urgency(ingredient, stock_index, aliases)
        if urgency is None:
            missing.append(ingredient)
        else:
            matched.append(ingredient)
            expiry_usefulness += urgency

    total = len(recipe.ingredients)
    coverage = len(matched) / total if total else 0.0
    expiry_usefulness = round(expiry_usefulness, 3)
    score = round(COVERAGE_WEIGHT * coverage + EXPIRY_WEIGHT * expiry_usefulness, 3)

    return RecipeMatch(
        recipe=recipe,
        matched=matched,
        missing=missing,
        coverage=round(coverage, 3),
        expiry_usefulness=expiry_usefulness,
        score=score,
    )


def _build_stock_index(
    stock_items: Iterable[StockItem],
    today: date,
    expiry_window_days: int,
    aliases: Optional[Dict[str, str]] = None,
) -> List[Tuple[Set[str], float]]:
    index = []
    for item in stock_items:
        if item.amount <= 0:
            continue
        urgency = score_stock_item(
            item, today=today, expiry_window_days=expiry_window_days
        )
        index.append((tokenize(item.name, aliases), urgency.urgency_score))
    return index


def _best_stock_urgency(
    ingredient: RecipeIngredient,
    stock_index: List[Tuple[Set[str], float]],
    aliases: Optional[Dict[str, str]] = None,
) -> Optional[float]:
    ingredient_tokens = tokenize(ingredient.name, aliases)
    if not ingredient_tokens:
        return None

    best: Optional[float] = None
    for stock_tokens, urgency_score in stock_index:
        if tokens_match(ingredient_tokens, stock_tokens):
            if best is None or urgency_score > best:
                best = urgency_score
    return best
