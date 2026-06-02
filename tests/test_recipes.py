import unittest

from foodbrain_assistant.recipes import (
    RecipesError,
    parse_ingredient_line,
    parse_recipes_response,
)


class IngredientLineTest(unittest.TestCase):
    def test_parses_quantity_unit_and_name(self) -> None:
        ingredient = parse_ingredient_line("2 cups flour")

        self.assertIsNotNone(ingredient)
        assert ingredient is not None
        self.assertEqual(ingredient.quantity, 2.0)
        self.assertEqual(ingredient.unit, "cups")
        self.assertEqual(ingredient.name, "flour")

    def test_parses_fraction_quantity(self) -> None:
        ingredient = parse_ingredient_line("1/2 tsp salt")

        assert ingredient is not None
        self.assertEqual(ingredient.quantity, 0.5)
        self.assertEqual(ingredient.unit, "tsp")
        self.assertEqual(ingredient.name, "salt")

    def test_strips_trailing_qualifier_and_quantity_without_unit(self) -> None:
        ingredient = parse_ingredient_line("2 carrots, diced")

        assert ingredient is not None
        self.assertEqual(ingredient.quantity, 2.0)
        self.assertIsNone(ingredient.unit)
        self.assertEqual(ingredient.name, "carrots")

    def test_handles_to_taste_without_quantity(self) -> None:
        ingredient = parse_ingredient_line("salt to taste")

        assert ingredient is not None
        self.assertIsNone(ingredient.quantity)
        self.assertEqual(ingredient.name, "salt")

    def test_blank_line_is_skipped(self) -> None:
        self.assertIsNone(parse_ingredient_line("   "))


class RecipesResponseTest(unittest.TestCase):
    def test_parses_list_of_recipes(self) -> None:
        recipes = parse_recipes_response(
            [{"name": "Toast", "ingredients": ["2 slices bread"]}]
        )

        self.assertEqual(len(recipes), 1)
        self.assertEqual(recipes[0].name, "Toast")
        self.assertEqual(recipes[0].ingredients[0].name, "bread")
        self.assertEqual(recipes[0].source, "local")

    def test_accepts_recipes_wrapper_object(self) -> None:
        recipes = parse_recipes_response(
            {"recipes": [{"name": "Toast", "ingredients": ["bread"]}]}
        )

        self.assertEqual(len(recipes), 1)

    def test_parses_structured_ingredient_object(self) -> None:
        recipes = parse_recipes_response(
            [
                {
                    "name": "Omelette",
                    "ingredients": [{"name": "Eggs", "quantity": 3, "unit": "pieces"}],
                }
            ]
        )

        ingredient = recipes[0].ingredients[0]
        self.assertEqual(ingredient.name, "eggs")
        self.assertEqual(ingredient.quantity, 3.0)
        self.assertEqual(ingredient.unit, "pieces")

    def test_rejects_non_list_payload(self) -> None:
        with self.assertRaises(RecipesError):
            parse_recipes_response("not a list")

    def test_rejects_recipe_without_ingredients(self) -> None:
        with self.assertRaises(RecipesError):
            parse_recipes_response([{"name": "Empty", "ingredients": []}])

    def test_rejects_recipe_without_name(self) -> None:
        with self.assertRaises(RecipesError):
            parse_recipes_response([{"ingredients": ["bread"]}])


if __name__ == "__main__":
    unittest.main()
