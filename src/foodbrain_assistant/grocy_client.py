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
        return parse_stock_response(payload)

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


def parse_stock_response(payload: Any) -> list[StockItem]:
    rows = _require_stock_rows(payload)
    return list(_parse_stock_items(rows))


def diagnose_stock_response(payload: Any) -> dict[str, object]:
    diagnostics: dict[str, object] = {
        "valid_shape": isinstance(payload, list),
        "row_count": len(payload) if isinstance(payload, list) else 0,
        "parsed_item_count": 0,
        "skipped_empty_stock_count": 0,
        "errors": [],
        "warnings": [],
    }
    errors = diagnostics["errors"]
    warnings = diagnostics["warnings"]
    assert isinstance(errors, list)
    assert isinstance(warnings, list)

    if not isinstance(payload, list):
        errors.append("Grocy /api/stock response was not a list")
        return diagnostics

    rows = []
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            errors.append(f"row {index}: expected an object, got {type(row).__name__}")
            continue
        rows.append(row)
        _diagnose_stock_row(row, index=index, warnings=warnings, errors=errors)

    parsed_items = list(_parse_stock_items(rows))
    diagnostics["parsed_item_count"] = len(parsed_items)
    diagnostics["skipped_empty_stock_count"] = sum(
        1 for row in rows if _parse_row_amount(row) <= 0
    )
    if rows and not parsed_items:
        errors.append("no stock items could be parsed from a non-empty response")

    return diagnostics


def _require_stock_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise GrocyClientError("Grocy /api/stock response was not a list")

    rows = []
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            raise GrocyClientError(f"Grocy /api/stock row {index} was not an object")
        rows.append(row)
    return rows


def _parse_stock_items(rows: list[dict[str, Any]]) -> Iterable[StockItem]:
    for row in rows:
        product = _read_product(row)
        amount = _parse_row_amount(row)
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


def _parse_row_amount(row: dict[str, Any]) -> float:
    stock = row.get("stock_amount")
    return _parse_amount(stock if stock is not None else row.get("amount"))


def _read_product(row: dict[str, Any]) -> dict[str, Any]:
    product = row.get("product") or {}
    return product if isinstance(product, dict) else {}


def _parse_date(value: Any) -> Optional[date]:
    if not value or str(value)[:10] == "2999-12-31":
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


def _diagnose_stock_row(
    row: dict[str, Any],
    index: int,
    warnings: list[str],
    errors: list[str],
) -> None:
    product = _read_product(row)
    amount = _parse_row_amount(row)
    has_amount_field = "stock_amount" in row or "amount" in row

    if not has_amount_field:
        errors.append(f"row {index}: missing stock_amount or amount")
    elif amount <= 0:
        return

    product_id = product.get("id") or row.get("product_id")
    product_name = product.get("name") or row.get("product_name")
    if not product_id:
        errors.append(f"row {index}: positive stock row is missing product id")
    if not product_name:
        errors.append(f"row {index}: positive stock row is missing product name")

    if "best_before_date" in row and _parse_date(row.get("best_before_date")) is None:
        best_before = str(row.get("best_before_date") or "")[:10]
        if best_before and best_before != "2999-12-31":
            warnings.append(f"row {index}: could not parse best_before_date")

    unit = row.get("quantity_unit_stock") or row.get("quantity_unit")
    if unit is not None and not isinstance(unit, dict):
        warnings.append(f"row {index}: quantity unit was not an object")

    location = row.get("location")
    if location is not None and not isinstance(location, dict):
        warnings.append(f"row {index}: location was not an object")
