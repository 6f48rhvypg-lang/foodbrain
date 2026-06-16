"""Grocy write-back actions with confirm + undo safety rails.

This sits on top of the low-level :class:`~foodbrain_assistant.grocy_client.GrocyClient`
write primitives and encodes the two safety rails from the UX design:

* **Confirm on destructive.** Tossing/removing stock requires an explicit
  ``confirm=True``; without it :class:`ConfirmationRequired` is raised so a caller
  can never silently waste-remove stock.
* **Undo on consume.** Consume and toss return an :class:`WriteOutcome` carrying
  the Grocy ``transaction_id``; :func:`undo` reverses it.

The underlying client is read-only unless built with ``allow_writes=True``, so
tests and dry-runs cannot accidentally mutate a live Grocy.
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional

from .grocy_client import GrocyClient, extract_transaction_id


class ConfirmationRequired(RuntimeError):
    """Raised when a destructive action is attempted without explicit confirmation."""


@dataclass(frozen=True)
class WriteOutcome:
    action: str
    product_id: str
    amount: float
    undo_transaction_id: Optional[str] = None
    detail: Optional[str] = None

    @property
    def undoable(self) -> bool:
        return self.undo_transaction_id is not None


def _live_stock_amount(client: GrocyClient, product_id: str) -> float:
    """Current consumable stock for a product, summed across its entries.

    The UI books an item's *cached* amount, which Grocy rejects (HTTP 400) once
    it exceeds the real stock. Reading live stock lets callers clamp to it.
    """
    return sum(entry.amount for entry in client.get_product_entries(product_id))


def _remove(
    client: GrocyClient, product_id: str, amount: float, *, spoiled: bool, action: str
) -> WriteOutcome:
    """Shared consume/toss body: clamp to live stock, then book the removal.

    Clamping to ``min(amount, live)`` means "remove the whole item" always
    succeeds regardless of a stale cached amount. If nothing is in stock the
    item is already gone, so we skip the Grocy call (nothing to undo).
    """
    live = _live_stock_amount(client, product_id)
    booked = min(amount, live)
    if booked <= 0:
        return WriteOutcome(action=action, product_id=product_id, amount=0.0)
    response = client.consume_product(product_id, booked, spoiled=spoiled)
    return WriteOutcome(
        action=action,
        product_id=product_id,
        amount=booked,
        undo_transaction_id=extract_transaction_id(response),
    )


def consume(client: GrocyClient, product_id: str, amount: float = 1.0) -> WriteOutcome:
    """Mark stock as used. Undoable via :func:`undo`."""
    return _remove(client, product_id, amount, spoiled=False, action="consume")


def toss(
    client: GrocyClient,
    product_id: str,
    amount: float = 1.0,
    *,
    confirm: bool = False,
) -> WriteOutcome:
    """Remove spoiled/wasted stock. Destructive: requires ``confirm=True``.

    Still undoable via :func:`undo` so an accidental confirm can be reversed.
    """
    if not confirm:
        raise ConfirmationRequired(
            f"tossing product {product_id} is destructive; pass confirm=True"
        )
    return _remove(client, product_id, amount, spoiled=True, action="toss")


def set_due_date(
    client: GrocyClient, stock_entry_id: str, best_before_date: date, *, product_id: str = ""
) -> WriteOutcome:
    """Correct the best-before date of a single stock entry."""
    client.set_entry_due_date(stock_entry_id, best_before_date)
    return WriteOutcome(
        action="set_due_date",
        product_id=product_id,
        amount=0.0,
        detail=best_before_date.isoformat(),
    )


def set_location(
    client: GrocyClient, stock_entry_id: str, location_id: str, *, product_id: str = ""
) -> WriteOutcome:
    """Move a stock entry to a different storage location."""
    client.set_entry_location(stock_entry_id, location_id)
    return WriteOutcome(
        action="set_location",
        product_id=product_id,
        amount=0.0,
        detail=location_id,
    )


def rename_product(client: GrocyClient, product_id: str, name: str) -> WriteOutcome:
    """Rename a product in the Grocy master catalogue."""
    client.rename_product(product_id, name)
    return WriteOutcome(action="rename_product", product_id=product_id, amount=0.0, detail=name)


def set_amount(client: GrocyClient, product_id: str, new_amount: float) -> WriteOutcome:
    """Correct the stock amount for a product via Grocy's inventory endpoint."""
    client.set_product_inventory(product_id, new_amount)
    return WriteOutcome(action="set_amount", product_id=product_id, amount=new_amount)


def undo(client: GrocyClient, outcome_or_transaction_id) -> None:
    """Reverse a previous consume/toss given its outcome or transaction id."""
    if isinstance(outcome_or_transaction_id, WriteOutcome):
        transaction_id = outcome_or_transaction_id.undo_transaction_id
    else:
        transaction_id = outcome_or_transaction_id
    if not transaction_id:
        raise ValueError("nothing to undo: no transaction id available")
    client.undo_transaction(transaction_id)
