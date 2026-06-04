import json
import unittest
from datetime import date
from unittest import mock

from foodbrain_assistant import grocy_client as gc
from foodbrain_assistant import writeback
from foodbrain_assistant.grocy_client import (
    GrocyClient,
    GrocyClientError,
    GrocyWriteDisabledError,
    extract_transaction_id,
    parse_stock_entries_response,
)
from foodbrain_assistant.writeback import ConfirmationRequired


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def _capture(body: bytes = b"[]"):
    """Patch urlopen, returning (mock, list-that-collects-Request-objects)."""
    requests: list = []

    def fake_urlopen(request, timeout=None):
        requests.append(request)
        return _FakeResponse(body)

    return mock.patch.object(gc, "urlopen", fake_urlopen), requests


class WriteGuardTest(unittest.TestCase):
    def test_read_only_client_refuses_writes(self) -> None:
        client = GrocyClient("http://grocy", "key")  # allow_writes defaults to False
        with self.assertRaises(GrocyWriteDisabledError):
            client.consume_product("12")
        with self.assertRaises(GrocyWriteDisabledError):
            client.open_product("12")
        with self.assertRaises(GrocyWriteDisabledError):
            client.set_entry_due_date("5", date(2026, 7, 1))
        with self.assertRaises(GrocyWriteDisabledError):
            client.undo_transaction("abc")


class WriteRequestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = GrocyClient("http://grocy", "key", allow_writes=True)

    def test_consume_builds_post_with_spoiled_flag(self) -> None:
        patcher, requests = _capture(b'[{"transaction_id": "tx-1"}]')
        with patcher:
            response = self.client.consume_product("12", 2.0, spoiled=True)

        request = requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.full_url, "http://grocy/api/stock/products/12/consume")
        self.assertEqual(request.headers["Grocy-api-key"], "key")
        self.assertEqual(request.headers["Content-type"], "application/json")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body, {"amount": 2.0, "transaction_type": "consume", "spoiled": True})
        self.assertEqual(extract_transaction_id(response), "tx-1")

    def test_set_due_date_builds_put(self) -> None:
        patcher, requests = _capture(b"{}")
        with patcher:
            self.client.set_entry_due_date("5", date(2026, 7, 1))

        request = requests[0]
        self.assertEqual(request.method, "PUT")
        self.assertEqual(request.full_url, "http://grocy/api/stock/entry/5")
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {"best_before_date": "2026-07-01"},
        )

    def test_undo_posts_to_transaction_endpoint_with_empty_body(self) -> None:
        patcher, requests = _capture(b"")
        with patcher:
            result = self.client.undo_transaction("tx-1")

        request = requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            request.full_url, "http://grocy/api/stock/transactions/tx-1/undo"
        )
        self.assertEqual(request.data, b"")
        self.assertIsNone(result)


class ParseEntriesTest(unittest.TestCase):
    def test_parses_entries(self) -> None:
        entries = parse_stock_entries_response(
            [
                {
                    "id": 7,
                    "product_id": 12,
                    "amount": "1.5",
                    "best_before_date": "2026-06-10",
                    "open": "0",
                },
                {
                    "id": 8,
                    "product_id": 12,
                    "amount": "1",
                    "best_before_date": "2999-12-31",
                    "open": "1",
                },
            ]
        )
        self.assertEqual(entries[0].stock_entry_id, "7")
        self.assertEqual(entries[0].amount, 1.5)
        self.assertEqual(entries[0].best_before_date, date(2026, 6, 10))
        self.assertFalse(entries[0].opened)
        self.assertIsNone(entries[1].best_before_date)
        self.assertTrue(entries[1].opened)

    def test_rejects_non_list(self) -> None:
        with self.assertRaises(GrocyClientError):
            parse_stock_entries_response({"entries": []})

    def test_extract_transaction_id_missing(self) -> None:
        self.assertIsNone(extract_transaction_id([]))
        self.assertIsNone(extract_transaction_id([{"no_id": 1}]))


class WritebackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = GrocyClient("http://grocy", "key", allow_writes=True)

    def test_toss_requires_confirmation(self) -> None:
        with self.assertRaises(ConfirmationRequired):
            writeback.toss(self.client, "12")

    def test_consume_returns_undoable_outcome(self) -> None:
        patcher, _ = _capture(b'[{"transaction_id": "tx-9"}]')
        with patcher:
            outcome = writeback.consume(self.client, "12", 1.0)
        self.assertEqual(outcome.action, "consume")
        self.assertTrue(outcome.undoable)
        self.assertEqual(outcome.undo_transaction_id, "tx-9")

    def test_toss_with_confirm_marks_spoiled(self) -> None:
        patcher, requests = _capture(b'[{"transaction_id": "tx-3"}]')
        with patcher:
            outcome = writeback.toss(self.client, "12", confirm=True)
        body = json.loads(requests[0].data.decode("utf-8"))
        self.assertTrue(body["spoiled"])
        self.assertEqual(outcome.action, "toss")
        self.assertTrue(outcome.undoable)

    def test_undo_uses_outcome_transaction(self) -> None:
        patcher, requests = _capture(b"")
        outcome = writeback.WriteOutcome(
            action="consume", product_id="12", amount=1.0, undo_transaction_id="tx-9"
        )
        with patcher:
            writeback.undo(self.client, outcome)
        self.assertEqual(
            requests[0].full_url, "http://grocy/api/stock/transactions/tx-9/undo"
        )

    def test_undo_without_transaction_raises(self) -> None:
        outcome = writeback.WriteOutcome(action="set_due_date", product_id="12", amount=0.0)
        with self.assertRaises(ValueError):
            writeback.undo(self.client, outcome)


if __name__ == "__main__":
    unittest.main()
