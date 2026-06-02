from datetime import date
import unittest

from foodbrain_assistant.grocy_client import (
    GrocyClientError,
    diagnose_stock_response,
    parse_stock_response,
)


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


if __name__ == "__main__":
    unittest.main()