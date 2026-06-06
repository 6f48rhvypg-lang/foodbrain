"""Minimal Grocy API client."""

from datetime import date
import json
from typing import Any, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from .models import Recipe, StockEntry, StockItem
from .recipes import parse_grocy_recipes_response


class GrocyClientError(RuntimeError):
    pass


class GrocyWriteDisabledError(GrocyClientError):
    """Raised when a write is attempted on a read-only client."""


class GrocyClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: int = 10,
        allow_writes: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.allow_writes = allow_writes

    def get_stock_items(self) -> list[StockItem]:
        payload = self._get_json("api/stock")
        return parse_stock_response(payload)

    def get_recipes(self) -> list[Recipe]:
        return parse_grocy_recipes_response(
            recipes=self._get_json("api/objects/recipes"),
            positions=self._get_json("api/objects/recipes_pos"),
            products=self._get_json("api/objects/products"),
            quantity_units=self._get_json("api/objects/quantity_units"),
        )

    def get_product_entries(self, product_id: str) -> list[StockEntry]:
        """Read the individual stock entries for a product (needed to edit a due date)."""
        payload = self._get_json(f"api/stock/products/{product_id}/entries")
        return parse_stock_entries_response(payload)

    def get_products(self) -> list[dict[str, Any]]:
        """The product master list as ``[{"id", "name"}, ...]`` for intake matching."""
        return parse_named_objects(self._get_json("api/objects/products"))

    def get_quantity_units(self) -> list[dict[str, Any]]:
        """Quantity units as ``[{"id", "name"}, ...]`` (for resolving a unit name)."""
        return parse_named_objects(self._get_json("api/objects/quantity_units"))

    def get_locations(self) -> list[dict[str, Any]]:
        """Storage locations as ``[{"id", "name"}, ...]`` (fridge/freezer/pantry…)."""
        return parse_named_objects(self._get_json("api/objects/locations"))

    # --- writes -----------------------------------------------------------

    def create_product(
        self,
        name: str,
        *,
        qu_id_stock: str,
        location_id: str,
        qu_id_purchase: Optional[str] = None,
    ) -> str:
        """Create a product master record and return its new id.

        Grocy needs a stock quantity unit and a default location; we reuse the
        stock unit for purchasing so a freshly created product can immediately
        take a stock-add (purchase == stock means no conversion is needed).

        Grocy 4.0 removed the ``qu_factor_purchase_to_stock`` column (unit
        conversions now live in a separate table), so sending it makes the
        insert fail with HTTP 400 ("table products has no column named ...").
        """
        purchase = qu_id_purchase or qu_id_stock
        response = self._write_json(
            "api/objects/products",
            "POST",
            {
                "name": name,
                "location_id": location_id,
                "qu_id_stock": qu_id_stock,
                "qu_id_purchase": purchase,
            },
        )
        created_id = response.get("created_object_id") if isinstance(response, dict) else None
        if not created_id:
            raise GrocyClientError(
                f"Grocy did not return a created product id for {name!r}"
            )
        return str(created_id)

    def add_stock(
        self,
        product_id: str,
        amount: float = 1.0,
        *,
        best_before_date: Optional[date] = None,
        location_id: Optional[str] = None,
    ) -> Any:
        """Book a purchase (add stock). Undoable via the returned transaction id."""
        body: dict[str, Any] = {
            "amount": amount,
            "transaction_type": "purchase",
        }
        # Grocy treats a missing best-before as "never expires" (2999-12-31).
        body["best_before_date"] = (
            best_before_date.isoformat() if best_before_date else "2999-12-31"
        )
        if location_id:
            body["location_id"] = location_id
        return self._write_json(
            f"api/stock/products/{product_id}/add", "POST", body
        )

    def consume_product(
        self, product_id: str, amount: float = 1.0, *, spoiled: bool = False
    ) -> Any:
        """Consume stock. Set ``spoiled`` for a toss/waste removal.

        Returns the Grocy response, which carries the transaction id used for undo.
        """
        return self._write_json(
            f"api/stock/products/{product_id}/consume",
            "POST",
            {
                "amount": amount,
                "transaction_type": "consume",
                "spoiled": spoiled,
            },
        )

    def open_product(self, product_id: str, amount: float = 1.0) -> Any:
        return self._write_json(
            f"api/stock/products/{product_id}/open",
            "POST",
            {"amount": amount},
        )

    def set_entry_due_date(self, stock_entry_id: str, best_before_date: date) -> Any:
        return self._write_json(
            f"api/stock/entry/{stock_entry_id}",
            "PUT",
            {"best_before_date": best_before_date.isoformat()},
        )

    def undo_transaction(self, transaction_id: str) -> Any:
        return self._write_json(
            f"api/stock/transactions/{transaction_id}/undo",
            "POST",
            None,
        )

    def _get_json(self, path: str) -> Any:
        url = urljoin(self.base_url, path)
        request = Request(url, headers={"GROCY-API-KEY": self.api_key})
        return self._send(request)

    def _write_json(self, path: str, method: str, body: Optional[dict[str, Any]]) -> Any:
        if not self.allow_writes:
            raise GrocyWriteDisabledError(
                f"refusing to {method} {path}: client is read-only "
                "(construct GrocyClient with allow_writes=True to enable writes)"
            )
        url = urljoin(self.base_url, path)
        data = json.dumps(body).encode("utf-8") if body is not None else b""
        request = Request(
            url,
            data=data,
            headers={
                "GROCY-API-KEY": self.api_key,
                "Content-Type": "application/json",
            },
            method=method,
        )
        return self._send(request, allow_empty=True)

    def _send(self, request: Request, allow_empty: bool = False) -> Any:
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            raise GrocyClientError(f"Grocy request failed with HTTP {exc.code}") from exc
        except URLError as exc:
            raise GrocyClientError(f"Grocy request failed: {exc.reason}") from exc
        if allow_empty and not raw.strip():
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GrocyClientError("Grocy response was not valid JSON") from exc


def parse_stock_entries_response(payload: Any) -> list[StockEntry]:
    if not isinstance(payload, list):
        raise GrocyClientError(
            "Grocy /api/stock/products/{id}/entries response was not a list"
        )

    entries: list[StockEntry] = []
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            raise GrocyClientError(f"Grocy stock entry {index} was not an object")
        entry_id = str(row.get("id") or "")
        if not entry_id:
            raise GrocyClientError(f"Grocy stock entry {index} is missing an id")
        entries.append(
            StockEntry(
                stock_entry_id=entry_id,
                product_id=str(row.get("product_id") or ""),
                amount=_parse_amount(row.get("amount")),
                best_before_date=_parse_date(row.get("best_before_date")),
                opened=bool(_parse_amount(row.get("open")) > 0 or row.get("opened")),
            )
        )
    return entries


def parse_named_objects(payload: Any) -> list[dict[str, Any]]:
    """Reduce a Grocy ``/api/objects/<table>`` response to ``[{"id", "name"}]``.

    Used for products, quantity units, and locations during intake. Rows without
    an id or name are skipped rather than raising, so one malformed master-data
    row can't break the whole intake flow.
    """
    if not isinstance(payload, list):
        raise GrocyClientError("Grocy /api/objects response was not a list")
    objects: list[dict[str, Any]] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        object_id = row.get("id")
        name = row.get("name")
        if object_id is None or not name:
            continue
        objects.append({"id": str(object_id), "name": str(name)})
    return objects


def extract_transaction_id(response: Any) -> Optional[str]:
    """Pull the transaction id from a Grocy consume/open response (used for undo)."""
    rows = response if isinstance(response, list) else [response]
    for row in rows:
        if isinstance(row, dict) and row.get("transaction_id"):
            return str(row["transaction_id"])
    return None


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
