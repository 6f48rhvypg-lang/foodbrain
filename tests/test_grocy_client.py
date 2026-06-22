from datetime import date
import unittest
from unittest import mock
from urllib.error import URLError

from foodbrain_assistant import grocy_client as gc
from foodbrain_assistant.grocy_client import (
    GrocyClient,
    GrocyClientError,
    _short_grocy_error,
    clear_master_cache,
    diagnose_stock_response,
    parse_stock_response,
)


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


class MasterDataCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        clear_master_cache()
        self.addCleanup(clear_master_cache)

    def _patch_urlopen(self):
        calls: list = []

        def fake_urlopen(request, timeout=None):
            calls.append(request.full_url)
            return _FakeResponse(b'[{"id": "1", "name": "Fridge"}]')

        return mock.patch.object(gc, "urlopen", fake_urlopen), calls

    def test_locations_served_from_cache_within_ttl(self) -> None:
        patch, calls = self._patch_urlopen()
        client = GrocyClient("http://grocy", "key")
        with patch:
            first = client.get_locations()
            second = client.get_locations()
        self.assertEqual(first, [{"id": "1", "name": "Fridge"}])
        self.assertEqual(second, first)
        self.assertEqual(len(calls), 1)  # transport hit once across two reads

    def test_returned_list_copy_does_not_corrupt_cache(self) -> None:
        patch, calls = self._patch_urlopen()
        client = GrocyClient("http://grocy", "key")
        with patch:
            first = client.get_locations()
            first.append({"id": 99, "name": "junk"})
            second = client.get_locations()
        self.assertEqual(second, [{"id": "1", "name": "Fridge"}])
        self.assertEqual(len(calls), 1)

    def test_different_base_url_bypasses_cache(self) -> None:
        patch, calls = self._patch_urlopen()
        with patch:
            GrocyClient("http://grocy", "key").get_locations()
            GrocyClient("http://other", "key").get_locations()
        self.assertEqual(len(calls), 2)  # distinct base URLs are cached separately

    def test_quantity_units_and_locations_cached_separately(self) -> None:
        patch, calls = self._patch_urlopen()
        client = GrocyClient("http://grocy", "key")
        with patch:
            client.get_locations()
            client.get_quantity_units()
            client.get_locations()
            client.get_quantity_units()
        # one fetch per kind, then served from cache
        self.assertEqual(len(calls), 2)


class TransientRetryTest(unittest.TestCase):
    def setUp(self) -> None:
        clear_master_cache()
        self.addCleanup(clear_master_cache)

    def test_get_retried_once_then_succeeds(self) -> None:
        attempts = []

        def flaky_urlopen(request, timeout=None):
            attempts.append(request.full_url)
            if len(attempts) == 1:
                raise URLError("connection reset")
            return _FakeResponse(b'[{"id": 1, "name": "Fridge"}]')

        client = GrocyClient("http://grocy", "key")
        with mock.patch.object(gc, "urlopen", flaky_urlopen):
            result = client.get_locations()
        self.assertEqual(result, [{"id": "1", "name": "Fridge"}])
        self.assertEqual(len(attempts), 2)  # failed once, retried, succeeded

    def test_get_gives_up_after_one_retry(self) -> None:
        attempts = []

        def always_fails(request, timeout=None):
            attempts.append(request.full_url)
            raise URLError("down")

        client = GrocyClient("http://grocy", "key")
        with mock.patch.object(gc, "urlopen", always_fails):
            with self.assertRaises(GrocyClientError):
                client.get_locations()
        self.assertEqual(len(attempts), 2)  # original + one retry, then give up

    def test_write_is_not_retried(self) -> None:
        attempts = []

        def always_fails(request, timeout=None):
            attempts.append(request.full_url)
            raise URLError("down")

        client = GrocyClient("http://grocy", "key", allow_writes=True)
        with mock.patch.object(gc, "urlopen", always_fails):
            with self.assertRaises(GrocyClientError):
                client.consume_product("12", 1.0)
        self.assertEqual(len(attempts), 1)  # a non-idempotent write is tried once


class GrocyClientTest(unittest.TestCase):
    def test_parse_stock_response_handles_grocy_stock_shape(self) -> None:
        payload = [
            {
                "product": {"id": 12, "name": "Greek yogurt"},
                "amount": "2.5",
                "best_before_date": "2026-06-05",
                "quantity_unit_stock": {"name": "tub", "name_plural": "tubs"},
                "location": {"name": "Fridge"},
            }
        ]

        items = parse_stock_response(payload)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].product_id, "12")
        self.assertEqual(items[0].name, "Greek yogurt")
        self.assertEqual(items[0].amount, 2.5)
        self.assertEqual(items[0].unit, "tub")
        self.assertEqual(items[0].best_before_date, date(2026, 6, 5))
        self.assertEqual(items[0].location, "Fridge")

    def test_parse_stock_response_handles_flat_product_fields(self) -> None:
        payload = [
            {
                "product_id": 3,
                "product_name": "Rice",
                "stock_amount": "1",
                "best_before_date": "2999-12-31 00:00:00",
                "quantity_unit": {"name_plural": "kg"},
            }
        ]

        items = parse_stock_response(payload)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].product_id, "3")
        self.assertEqual(items[0].name, "Rice")
        self.assertEqual(items[0].amount, 1.0)
        self.assertEqual(items[0].unit, "kg")
        self.assertIsNone(items[0].best_before_date)

    def test_parse_stock_response_skips_empty_stock(self) -> None:
        payload = [{"product": {"id": 1, "name": "Spinach"}, "amount": "0"}]

        self.assertEqual(parse_stock_response(payload), [])

    def test_parse_stock_response_rejects_unexpected_payload_shape(self) -> None:
        with self.assertRaises(GrocyClientError):
            parse_stock_response({"stock": []})

        with self.assertRaises(GrocyClientError):
            parse_stock_response(["not an object"])

    def test_diagnose_stock_response_reports_contract_errors(self) -> None:
        diagnostics = diagnose_stock_response(
            [
                {"product": {"id": 1, "name": "Spinach"}, "amount": "1"},
                {"product": {"id": 2, "name": "Rice"}, "amount": "0"},
                {"amount": "1"},
            ]
        )

        self.assertEqual(diagnostics["row_count"], 3)
        self.assertEqual(diagnostics["parsed_item_count"], 2)
        self.assertEqual(diagnostics["skipped_empty_stock_count"], 1)
        self.assertIn(
            "row 2: positive stock row is missing product id",
            diagnostics["errors"],
        )
        self.assertIn(
            "row 2: positive stock row is missing product name",
            diagnostics["errors"],
        )

    def test_short_grocy_error_extracts_message(self) -> None:
        # Grocy returns its real reason in the response body; surface it.
        self.assertEqual(
            _short_grocy_error('{"error_message": "Product name already exists"}'),
            "Product name already exists",
        )
        # Non-JSON bodies are passed through (trimmed); empty stays empty.
        self.assertEqual(_short_grocy_error("plain text boom"), "plain text boom")
        self.assertEqual(_short_grocy_error(""), "")


if __name__ == "__main__":
    unittest.main()