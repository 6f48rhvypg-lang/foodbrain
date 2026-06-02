"""FoodBrain service orchestration."""

from datetime import date
from typing import Optional

from .config import Settings
from .grocy_client import GrocyClient
from .home_assistant import publish_webhook
from .models import RunResult, StockItem
from .scoring import rank_ingredients_by_urgency


def run_once(settings: Settings, stock_items: Optional[list[StockItem]] = None) -> RunResult:
    return run_once_with_source(settings, stock_items=stock_items, stock_source="sample")


def run_once_with_source(
    settings: Settings,
    stock_items: Optional[list[StockItem]] = None,
    stock_source: str = "sample",
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

    result = RunResult(
        urgent_ingredients=rank_ingredients_by_urgency(
            stock_items,
            today=date.today(),
            expiry_window_days=settings.expiry_window_days,
            limit=settings.top_ingredient_limit,
        ),
        source=source,
    )

    if settings.home_assistant_webhook_url:
        publish_webhook(settings.home_assistant_webhook_url, result)

    return result
