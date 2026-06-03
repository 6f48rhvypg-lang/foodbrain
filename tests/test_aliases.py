import unittest

from foodbrain_assistant.aliases import AliasError, load_aliases, merge_aliases
from foodbrain_assistant.normalization import normalize_ingredient_name


class LoadAliasesTest(unittest.TestCase):
    def test_normalizes_keys_and_values(self) -> None:
        aliases = load_aliases({"  Milch ": "Milk", "Eier": "egg"})

        self.assertEqual(aliases, {"milch": "milk", "eier": "egg"})

    def test_rejects_non_object(self) -> None:
        with self.assertRaises(AliasError):
            load_aliases(["milch", "milk"])

    def test_rejects_non_string_value(self) -> None:
        with self.assertRaises(AliasError):
            load_aliases({"milch": 1})

    def test_rejects_empty_after_normalization(self) -> None:
        with self.assertRaises(AliasError):
            load_aliases({"   ": "milk"})

    def test_merge_layers_override_over_base(self) -> None:
        base = {"milch": "milk", "eier": "egg"}
        override = {"eier": "eggs", "reis": "rice"}

        merged = merge_aliases(base, override)

        self.assertEqual(merged, {"milch": "milk", "eier": "eggs", "reis": "rice"})
        # base is not mutated
        self.assertEqual(base, {"milch": "milk", "eier": "egg"})


class NormalizeWithAliasesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.aliases = load_aliases({"milch": "milk", "bio": "organic"})

    def test_no_aliases_unchanged(self) -> None:
        self.assertEqual(normalize_ingredient_name("Milch"), "milch")

    def test_whole_name_aliased(self) -> None:
        self.assertEqual(normalize_ingredient_name("Milch", self.aliases), "milk")

    def test_per_token_aliased(self) -> None:
        self.assertEqual(
            normalize_ingredient_name("Bio Milch", self.aliases), "organic milk"
        )

    def test_unknown_name_passes_through(self) -> None:
        self.assertEqual(normalize_ingredient_name("Quark", self.aliases), "quark")


if __name__ == "__main__":
    unittest.main()
