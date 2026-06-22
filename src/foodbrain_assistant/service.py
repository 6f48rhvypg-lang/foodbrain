"""FoodBrain service orchestration."""

from datetime import date
from typing import Dict, Optional

from .config import Settings
from .grocy_client import GrocyClient
from .matching import rank_recipes
from .models import Recipe, RunResult, StockItem
from .pairing import PairingGraph, suggest_pairings
from .scoring import rank_ingredients_by_urgency


def run_once_with_source(
    settings: Settings,
    stock_items: Optional[list[StockItem]] = None,
    stock_source: str = "sample",
    recipes: Optional[list[Recipe]] = None,
    pairings: Optional[PairingGraph] = None,
    aliases: Optional[Dict[str, str]] = None,
) -> RunResult:
    if stock_items is None:
        if not settings.grocy_enabled:
            raise ValueError(
                "Set FOODBRAIN_GROCY_BASE_URL and FOODBRAIN_GROCY_API_KEY in the environment or .env, "
                "or pass --sample / --grocy-stock-json."
            )
        stock_items = GrocyClient(
            base_url=settings.grocy_base_url or "",
            api_key=settings.grocy_api_key or "",
        ).get_stock_items()
        source = "grocy"
    else:
        source = stock_source

    today = date.today()

    recipe_matches = []
    if recipes:
        recipe_matches = rank_recipes(
            recipes,
            stock_items,
            today=today,
            expiry_window_days=settings.expiry_window_days,
            limit=settings.top_recipe_limit,
            aliases=aliases,
        )

    urgent_ingredients = rank_ingredients_by_urgency(
        stock_items,
        today=today,
        expiry_window_days=settings.expiry_window_days,
        limit=settings.top_ingredient_limit,
    )

    flavor_suggestions = []
    if pairings is not None:
        flavor_suggestions = suggest_pairings(
            pairings,
            urgent_ingredients,
            stock_items,
            ingredient_limit=settings.top_pairing_limit,
            partner_limit=settings.pairing_partner_limit,
            aliases=aliases,
        )

    return RunResult(
        urgent_ingredients=urgent_ingredients,
        source=source,
        recipe_matches=recipe_matches,
        flavor_suggestions=flavor_suggestions,
    )
