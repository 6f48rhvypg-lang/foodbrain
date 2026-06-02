import unittest

from foodbrain_assistant.normalization import normalize_ingredient_name


class NormalizationTest(unittest.TestCase):
    def test_normalize_ingredient_name_removes_noise(self) -> None:
        self.assertEqual(
            normalize_ingredient_name("  Greek   Yogurt (opened) "), "greek yogurt"
        )

    def test_normalize_ingredient_name_expands_ampersand(self) -> None:
        self.assertEqual(normalize_ingredient_name("Salt & Pepper"), "salt and pepper")


if __name__ == "__main__":
    unittest.main()
