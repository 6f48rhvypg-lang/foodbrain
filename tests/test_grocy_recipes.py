import unittest

from foodbrain_assistant.recipes import (
    RecipesError,
    diagnose_grocy_recipes,
    parse_grocy_recipes_response,
)


RECIPES = [
    {"id": 1, "name": "Omelette", "type": "normal"},
    {"id": 2, "name": "2026-06-02", "type": "mealplan-day"},
    {"id": 3, "name": "Empty Recipe", "type": "normal"},
]
POSITIONS = [
    {"id": 10, "recipe_id": 1, "product_id": 100, "amount": 3, "qu_id": 5},
    {"id": 11, "recipe_id": 1, "product_id": 101, "amount": 1, "qu_id": 6},
    {"id": 12, "recipe_id": 1, "product_id": 999, "amount": 1, "qu_id": 6},  # unknown
    {"id": 13, "recipe_id": 3, "product_id": 999, "amount": 1, "qu_id": 6},  # unknown
]
PRODUCTS = [
    {"id": 100, "name": "Eggs"},
    {"id": 101, "name": "Spinach"},
]
QUANTITY_UNITS = [
    {"id": 5, "name": "piece"},
    {"id": 6, "name": "bag"},
]


class ParseGrocyRecipesTest(unittest.TestCase):
    def test_joins_recipes_positions_and_products(self) -> None:
        recipes = parse_grocy_recipes_response(
            RECIPES, POSITIONS, PRODUCTS, QUANTITY_UNITS
        )

        self.assertEqual([r.name for r in recipes], ["Omelette"])
        omelette = recipes[0]
        self.assertEqual(omelette.source, "grocy")
        self.assertEqual([i.name for i in omelette.ingredients], ["eggs", "spinach"])
        self.assertEqual(omelette.ingredients[0].quantity, 3.0)
        self.assertEqual(omelette.ingredients[0].unit, "piece")

    def test_skips_mealplan_recipes(self) -> None:
        recipes = parse_grocy_recipes_response(RECIPES, POSITIONS, PRODUCTS)
        self.assertNotIn("2026-06-02", [r.name for r in recipes])

    def test_skips_recipe_with_no_resolvable_ingredients(self) -> None:
        recipes = parse_grocy_recipes_response(RECIPES, POSITIONS, PRODUCTS)
        self.assertNotIn("Empty Recipe", [r.name for r in recipes])

    def test_works_without_quantity_units(self) -> None:
        recipes = parse_grocy_recipes_response(RECIPES, POSITIONS, PRODUCTS)
        self.assertIsNone(recipes[0].ingredients[0].unit)

    def test_rejects_non_list_payload(self) -> None:
        with self.assertRaises(RecipesError):
            parse_grocy_recipes_response({}, POSITIONS, PRODUCTS)


class DiagnoseGrocyRecipesTest(unittest.TestCase):
    def test_reports_counts_and_unresolved_products(self) -> None:
        diagnostics = diagnose_grocy_recipes(
            RECIPES, POSITIONS, PRODUCTS, QUANTITY_UNITS
        )

        self.assertEqual(diagnostics["recipe_count"], 3)
        self.assertEqual(diagnostics["normal_recipe_count"], 2)
        self.assertEqual(diagnostics["parsed_recipe_count"], 1)
        self.assertEqual(diagnostics["skipped_empty_recipe_count"], 1)
        self.assertEqual(diagnostics["unresolved_product_count"], 2)
        self.assertEqual(diagnostics["errors"], [])
        self.assertTrue(diagnostics["warnings"])

    def test_reports_error_for_bad_shape(self) -> None:
        diagnostics = diagnose_grocy_recipes("nope", POSITIONS, PRODUCTS)
        self.assertTrue(diagnostics["errors"])


if __name__ == "__main__":
    unittest.main()
