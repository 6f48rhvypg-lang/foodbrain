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


def consume(client: GrocyClient, product_id: str, amount: float = 1.0) -> WriteOutcome:
    """Mark stock as used. Undoable via :func:`undo`."""
    response = client.consume_product(product_id, amount, spoiled=False)
    return WriteOutcome(
        action="consume",
        product_id=product_id,
        amount=amount,
        undo_transaction_id=extract_transaction_id(response),
    )


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
    response = client.consume_product(product_id, amount, spoiled=True)
    return WriteOutcome(
        action="toss",
        product_id=product_id,
        amount=amount,
        undo_transaction_id=extract_transaction_id(response),
    )


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


def undo(client: GrocyClient, outcome_or_transaction_id) -> None:
    """Reverse a previous consume/toss given its outcome or transaction id."""
    if isinstance(outcome_or_transaction_id, WriteOutcome):
        transaction_id = outcome_or_transaction_id.undo_transaction_id
    else:
        transaction_id = outcome_or_transaction_id
    if not transaction_id:
        raise ValueError("nothing to undo: no transaction id available")
    client.undo_transaction(transaction_id)
