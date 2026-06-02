from datetime import date
import unittest

from foodbrain_assistant.models import IngredientUrgency, StockItem
from foodbrain_assistant.pairing import PairingError, load_pairings, suggest_pairings


def _urgency(name: str, score: float = 0.5) -> IngredientUrgency:
    return IngredientUrgency(
        item=StockItem("1", name, 1, None, None),
        days_until_expiry=None,
        urgency_score=score,
        reason="test",
    )


class LoadPairingsTest(unittest.TestCase):
    def test_pairs_are_symmetric(self) -> None:
        graph = load_pairings({"pairs": [{"a": "tomato", "b": "basil", "score": 0.9}]})

        self.assertEqual(graph.partners_for("tomato"), [("basil", 0.9)])
        self.assertEqual(graph.partners_for("basil"), [("tomato", 0.9)])

    def test_accepts_bare_list(self) -> None:
        graph = load_pairings([{"a": "rice", "b": "peas", "score": 0.7}])

        self.assertEqual(graph.partners_for("rice"), [("peas", 0.7)])

    def test_partners_sorted_by_score_then_name(self) -> None:
        graph = load_pairings(
            {
                "pairs": [
                    {"a": "rice", "b": "peas", "score": 0.7},
                    {"a": "rice", "b": "egg", "score": 0.9},
                    {"a": "rice", "b": "ginger", "score": 0.7},
                ]
            }
        )

        self.assertEqual(
            graph.partners_for("rice"),
            [("egg", 0.9), ("ginger", 0.7), ("peas", 0.7)],
        )

    def test_duplicate_pair_keeps_highest_score(self) -> None:
        graph = load_pairings(
            {
                "pairs": [
                    {"a": "rice", "b": "egg", "score": 0.5},
                    {"a": "egg", "b": "rice", "score": 0.8},
                ]
            }
        )

        self.assertEqual(graph.partners_for("rice"), [("egg", 0.8)])

    def test_missing_score_defaults_to_one(self) -> None:
        graph = load_pairings({"pairs": [{"a": "rice", "b": "egg"}]})

        self.assertEqual(graph.partners_for("rice"), [("egg", 1.0)])

    def test_self_pair_is_ignored(self) -> None:
        graph = load_pairings({"pairs": [{"a": "rice", "b": "Rice", "score": 0.9}]})

        self.assertEqual(len(graph), 0)

    def test_token_containment_lookup(self) -> None:
        graph = load_pairings({"pairs": [{"a": "yogurt", "b": "honey", "score": 0.8}]})

        self.assertEqual(graph.partners_for("Greek yogurt"), [("honey", 0.8)])

    def test_bad_payload_raises(self) -> None:
        with self.assertRaises(PairingError):
            load_pairings("nope")

    def test_missing_ingredient_raises(self) -> None:
        with self.assertRaises(PairingError):
            load_pairings({"pairs": [{"a": "rice", "score": 0.5}]})

    def test_non_numeric_score_raises(self) -> None:
        with self.assertRaises(PairingError):
            load_pairings({"pairs": [{"a": "rice", "b": "egg", "score": "lots"}]})


class SuggestPairingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.today = date(2026, 6, 2)
        self.graph = load_pairings(
            {
                "pairs": [
                    {"a": "spinach", "b": "garlic", "score": 0.84},
                    {"a": "spinach", "b": "rice", "score": 0.61},
                ]
            }
        )

    def test_flags_partner_in_stock(self) -> None:
        stock = [
            StockItem("1", "Spinach", 1, "bag", None),
            StockItem("2", "Rice", 1, "kg", None),
        ]

        suggestions = suggest_pairings(self.graph, [_urgency("Spinach")], stock)

        self.assertEqual(suggestions[0].ingredient, "Spinach")
        by_name = {p.name: p for p in suggestions[0].partners}
        self.assertFalse(by_name["garlic"].in_stock)
        self.assertTrue(by_name["rice"].in_stock)

    def test_zero_stock_partner_not_counted_in_stock(self) -> None:
        stock = [StockItem("2", "Rice", 0, "kg", None)]

        suggestions = suggest_pairings(self.graph, [_urgency("Spinach")], stock)

        by_name = {p.name: p for p in suggestions[0].partners}
        self.assertFalse(by_name["rice"].in_stock)

    def test_ingredient_without_pairings_is_skipped(self) -> None:
        suggestions = suggest_pairings(
            self.graph, [_urgency("Quinoa")], [StockItem("1", "Quinoa", 1, None, None)]
        )

        self.assertEqual(suggestions, [])

    def test_ingredient_limit_caps_suggestions(self) -> None:
        urgencies = [_urgency("Spinach", 0.9), _urgency("Rice", 0.5)]
        graph = load_pairings(
            {
                "pairs": [
                    {"a": "spinach", "b": "garlic", "score": 0.8},
                    {"a": "rice", "b": "peas", "score": 0.7},
                ]
            }
        )

        suggestions = suggest_pairings(graph, urgencies, [], ingredient_limit=1)

        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0].ingredient, "Spinach")

    def test_partner_limit_caps_partners(self) -> None:
        graph = load_pairings(
            {
                "pairs": [
                    {"a": "rice", "b": "egg", "score": 0.9},
                    {"a": "rice", "b": "peas", "score": 0.8},
                    {"a": "rice", "b": "ginger", "score": 0.7},
                ]
            }
        )

        suggestions = suggest_pairings(
            graph, [_urgency("Rice")], [], partner_limit=2
        )

        self.assertEqual([p.name for p in suggestions[0].partners], ["egg", "peas"])


if __name__ == "__main__":
    unittest.main()
