from datetime import date, timedelta
import unittest

from foodbrain_assistant.models import StockItem
from foodbrain_assistant.scoring import rank_ingredients_by_urgency, score_stock_item


class ScoringTest(unittest.TestCase):
    def test_score_overdue_item_is_maximum_urgency(self) -> None:
        today = date(2026, 6, 2)
        item = StockItem("1", "Spinach", 1, "bag", today - timedelta(days=1))

        urgency = score_stock_item(item, today=today)

        self.assertEqual(urgency.urgency_score, 1.0)
        self.assertEqual(urgency.days_until_expiry, -1)
        self.assertEqual(urgency.reason, "Overdue")

    def test_score_item_inside_expiry_window(self) -> None:
        today = date(2026, 6, 2)
        item = StockItem("1", "Yogurt", 1, "tub", today + timedelta(days=2))

        urgency = score_stock_item(item, today=today, expiry_window_days=7)

        self.assertGreater(urgency.urgency_score, 0.6)
        self.assertEqual(urgency.reason, "Expires in 2 days")

    def test_rank_ingredients_filters_empty_stock_and_limits_results(self) -> None:
        today = date(2026, 6, 2)
        items = [
            StockItem("1", "Rice", 1, "kg", None),
            StockItem("2", "Spinach", 0, "bag", today),
            StockItem("3", "Milk", 1, "carton", today),
        ]

        ranked = rank_ingredients_by_urgency(items, today=today, limit=1)

        self.assertEqual([entry.item.name for entry in ranked], ["Milk"])


if __name__ == "__main__":
    unittest.main()
