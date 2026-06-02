"""Home Assistant publishing helpers."""

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import RunResult


class HomeAssistantPublishError(RuntimeError):
    pass


def publish_webhook(webhook_url: str, result: RunResult, timeout_seconds: int = 10) -> None:
    payload = json.dumps(_to_payload(result)).encode("utf-8")
    request = Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds):
            return
    except HTTPError as exc:
        raise HomeAssistantPublishError(
            f"Home Assistant webhook failed with HTTP {exc.code}"
        ) from exc
    except URLError as exc:
        raise HomeAssistantPublishError(
            f"Home Assistant webhook failed: {exc.reason}"
        ) from exc


def _to_payload(result: RunResult) -> dict[str, object]:
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
    }
