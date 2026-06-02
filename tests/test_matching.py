from datetime import date, timedelta
import unittest

from foodbrain_assistant.matching import rank_recipes
from foodbrain_assistant.models import Recipe, RecipeIngredient, StockItem


def _recipe(name: str, *ingredient_names: str) -> Recipe:
    return Recipe(
        name=name,
        ingredients=[
            RecipeIngredient(raw=n, name=n) for n in ingredient_names
        ],
    )


class MatchingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.today = date(2026, 6, 2)

    def test_full_coverage_recipe_outranks_partial(self) -> None:
        stock = [
            StockItem("1", "spinach", 1, "bag", None),
            StockItem("2", "eggs", 6, "pieces", None),
        ]
        recipes = [
            _recipe("Omelette", "eggs", "spinach"),
            _recipe("Risotto", "rice", "eggs"),
        ]

        ranked = rank_recipes(recipes, stock, today=self.today)

        self.assertEqual(ranked[0].recipe.name, "Omelette")
        self.assertEqual(ranked[0].coverage, 1.0)
        self.assertEqual([i.name for i in ranked[1].missing], ["rice"])

    def test_matches_more_specific_stock_name(self) -> None:
        stock = [StockItem("1", "Greek yogurt", 1, "tub", None)]
        recipes = [_recipe("Bowl", "yogurt")]

        ranked = rank_recipes(recipes, stock, today=self.today)

        self.assertEqual(ranked[0].coverage, 1.0)
        self.assertEqual(ranked[0].missing, [])

    def test_matches_singular_and_plural(self) -> None:
        stock = [StockItem("1", "carrot", 3, "pieces", None)]
        recipes = [_recipe("Soup", "carrots")]

        ranked = rank_recipes(recipes, stock, today=self.today)

        self.assertEqual(ranked[0].coverage, 1.0)

    def test_expiry_usefulness_breaks_coverage_ties(self) -> None:
        stock = [
            StockItem("1", "spinach", 1, "bag", self.today),  # expires today
            StockItem("2", "rice", 1, "kg", None),  # no expiry
        ]
        recipes = [
            _recipe("Use Spinach", "spinach"),
            _recipe("Use Rice", "rice"),
        ]

        ranked = rank_recipes(recipes, stock, today=self.today)

        self.assertEqual(ranked[0].recipe.name, "Use Spinach")
        self.assertGreater(
            ranked[0].expiry_usefulness, ranked[1].expiry_usefulness
        )

    def test_zero_stock_item_does_not_match(self) -> None:
        stock = [StockItem("1", "spinach", 0, "bag", None)]
        recipes = [_recipe("Salad", "spinach")]

        ranked = rank_recipes(recipes, stock, today=self.today)

        self.assertEqual(ranked[0].coverage, 0.0)

    def test_limit_caps_results(self) -> None:
        stock = [StockItem("1", "rice", 1, "kg", None)]
        recipes = [_recipe(f"Recipe {n}", "rice") for n in range(5)]

        ranked = rank_recipes(recipes, stock, today=self.today, limit=2)

        self.assertEqual(len(ranked), 2)


if __name__ == "__main__":
    unittest.main()
