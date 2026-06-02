"""Minimal Grocy API client."""

from datetime import date
import json
from typing import Any, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from .models import StockItem


class GrocyClientError(RuntimeError):
    pass


class GrocyClient:
    def __init__(self, base_url: str, api_key: str, timeout_seconds: int = 10) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def get_stock_items(self) -> list[StockItem]:
        payload = self._get_json("api/stock")
        if not isinstance(payload, list):
            raise GrocyClientError("Grocy /api/stock response was not a list")
        return list(_parse_stock_items(payload))

    def _get_json(self, path: str) -> Any:
        url = urljoin(self.base_url, path)
        request = Request(url, headers={"GROCY-API-KEY": self.api_key})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise GrocyClientError(f"Grocy request failed with HTTP {exc.code}") from exc
        except URLError as exc:
            raise GrocyClientError(f"Grocy request failed: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise GrocyClientError("Grocy response was not valid JSON") from exc


def _parse_stock_items(rows: list[dict[str, Any]]) -> Iterable[StockItem]:
    for row in rows:
        product = row.get("product") or {}
        stock = row.get("stock_amount")
        amount = _parse_amount(stock if stock is not None else row.get("amount"))
        if amount <= 0:
            continue

        product_id = str(product.get("id") or row.get("product_id") or "")
        name = str(product.get("name") or row.get("product_name") or product_id)
        yield StockItem(
            product_id=product_id,
            name=name,
            amount=amount,
            unit=_read_unit_name(row),
            best_before_date=_parse_date(row.get("best_before_date")),
            opened_date=_parse_date(row.get("open")),
            location=_read_location_name(row),
        )


def _parse_amount(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_date(value: Any) -> Optional[date]:
    if not value or value == "2999-12-31":
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _read_unit_name(row: dict[str, Any]) -> Optional[str]:
    unit = row.get("quantity_unit_stock") or row.get("quantity_unit") or {}
    if isinstance(unit, dict):
        name = unit.get("name") or unit.get("name_plural")
        return str(name) if name else None
    return None


def _read_location_name(row: dict[str, Any]) -> Optional[str]:
    location = row.get("location") or {}
    if isinstance(location, dict) and location.get("name"):
        return str(location["name"])
    return None
