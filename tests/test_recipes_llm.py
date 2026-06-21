import json
import unittest

from foodbrain_assistant import recipes_llm
from foodbrain_assistant.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        grocy_base_url=None,
        grocy_api_key=None,
        home_assistant_webhook_url=None,
        openrouter_api_key="test-key",
    )
    base.update(overrides)
    return Settings(**base)


def _reply(payload: dict):
    """A transport that returns an OpenRouter envelope wrapping ``payload``."""
    captured = {}

    def transport(url, headers, body, timeout):
        captured["body"] = json.loads(body.decode("utf-8"))
        return json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]})

    return transport, captured


class GenerateIdeasTest(unittest.TestCase):
    def test_seeds_and_inventory_reach_the_prompt(self) -> None:
        transport, captured = _reply(
            {"ideas": [{"title": "Spinat-Frittata", "hook": "schnell", "uses": "Spinat", "buy": []}]}
        )
        out = recipes_llm.generate_ideas(
            seeds=["Spinat"],
            inventory=["Eier", "Zwiebel"],
            taste={"likes": ["Chili"], "dislikes": [], "notes": ""},
            recent_cooked=["Lasagne"],
            mode="stock",
            preferences={},
            balance=0.7,
            count=8,
            model="google/gemini-3.1-pro",
            settings=_settings(),
            transport=transport,
        )
        self.assertEqual(out["ideas"][0]["title"], "Spinat-Frittata")
        user_msg = captured["body"]["messages"][1]["content"]
        self.assertIn("Spinat", user_msg)
        self.assertIn("Eier", user_msg)
        # recent-cooked anti-repeat list is passed through.
        self.assertIn("Lasagne", user_msg)
        # client model override is honored.
        self.assertEqual(captured["body"]["model"], "google/gemini-3.1-pro")

    def test_stock_mode_forces_empty_buy(self) -> None:
        transport, _ = _reply(
            {"ideas": [{"title": "Curry", "hook": "", "uses": "Linsen", "buy": ["Kokosmilch"]}]}
        )
        out = recipes_llm.generate_ideas(
            seeds=["Linsen"], inventory=[], taste={}, recent_cooked=[], mode="stock",
            preferences={}, balance=0.5, count=5, model="google/gemini-3.1-pro",
            settings=_settings(), transport=transport,
        )
        self.assertEqual(out["ideas"][0]["buy"], [])

    def test_shop_mode_allows_capped_buy(self) -> None:
        transport, _ = _reply(
            {"ideas": [{"title": "Curry", "uses": "Linsen",
                        "buy": ["Kokosmilch", "Koriander", "Limette", "Ingwer"]}]}
        )
        out = recipes_llm.generate_ideas(
            seeds=["Linsen"], inventory=[], taste={}, recent_cooked=[], mode="shop",
            preferences={}, balance=0.5, count=5, model="google/gemini-3.1-pro",
            settings=_settings(), transport=transport,
        )
        self.assertEqual(len(out["ideas"][0]["buy"]), 3)  # capped at 3

    def test_drops_ideas_without_title(self) -> None:
        transport, _ = _reply({"ideas": [{"hook": "no title"}, {"title": "Gut"}]})
        out = recipes_llm.generate_ideas(
            seeds=["X"], inventory=[], taste={}, recent_cooked=[], mode="stock",
            preferences={}, balance=0.5, count=5, model="google/gemini-3.1-pro",
            settings=_settings(), transport=transport,
        )
        self.assertEqual([i["title"] for i in out["ideas"]], ["Gut"])


class GenerateRecipeTest(unittest.TestCase):
    def test_returns_phase_guidance(self) -> None:
        transport, _ = _reply(
            {"title": "Spinat-Frittata", "time": "25 Min", "uses": "Spinat", "buy": [],
             "guidance": ["Spinat anbraten", "Eier verquirlen", "stocken lassen"]}
        )
        out = recipes_llm.generate_recipe(
            idea={"title": "Spinat-Frittata", "uses": "Spinat"}, mode="stock",
            model="google/gemini-3.1-flash-lite", settings=_settings(), transport=transport,
        )
        self.assertEqual(len(out["guidance"]), 3)
        self.assertEqual(out["time"], "25 Min")
        self.assertEqual(out["buy"], [])


class ReviseRecipeTest(unittest.TestCase):
    def test_rewrites_guidance_and_passes_change_to_prompt(self) -> None:
        transport, captured = _reply(
            {"title": "Pasta", "time": "25 Min", "uses": "Crème fraîche", "buy": [],
             "guidance": ["Knoblauch anbraten", "Crème fraîche dazu"]}
        )
        out = recipes_llm.revise_recipe(
            recipe={"title": "Pasta", "guidance": ["alt"], "buy": []},
            transcript="Crème fraîche statt Sahne, mehr Knoblauch", mode="stock",
            model="google/gemini-3.1-flash-lite", settings=_settings(), transport=transport,
        )
        self.assertEqual(out["guidance"], ["Knoblauch anbraten", "Crème fraîche dazu"])
        user_msg = captured["body"]["messages"][1]["content"]
        self.assertIn("Crème fraîche statt Sahne", user_msg)
        self.assertIn("alt", user_msg)  # original phases reach the prompt

    def test_falls_back_to_original_guidance(self) -> None:
        transport, _ = _reply({"title": "Pasta", "guidance": []})
        out = recipes_llm.revise_recipe(
            recipe={"title": "Pasta", "guidance": ["original phase"]},
            transcript="weniger Salz", mode="stock",
            model="google/gemini-3.1-flash-lite", settings=_settings(), transport=transport,
        )
        self.assertEqual(out["guidance"], ["original phase"])

    def test_stock_mode_forces_empty_buy(self) -> None:
        transport, _ = _reply({"title": "Pasta", "guidance": ["x"], "buy": ["Feta"]})
        out = recipes_llm.revise_recipe(
            recipe={"title": "Pasta", "guidance": ["x"]},
            transcript="mehr Feta", mode="stock",
            model="google/gemini-3.1-flash-lite", settings=_settings(), transport=transport,
        )
        self.assertEqual(out["buy"], [])


class ExtractTwistTest(unittest.TestCase):
    def test_parses_change_and_tags(self) -> None:
        transport, _ = _reply(
            {"change": "mehr Knoblauch", "note": "",
             "tags": {"likes": ["Knoblauch"], "dislikes": ["Sahne"]}}
        )
        out = recipes_llm.extract_twist(
            transcript="ich hab mehr Knoblauch genommen und keine Sahne",
            dish="Pasta", model="google/gemini-3.1-flash-lite",
            settings=_settings(), transport=transport,
        )
        self.assertEqual(out["change"], "mehr Knoblauch")
        self.assertEqual(out["tags"]["likes"], ["Knoblauch"])
        self.assertEqual(out["tags"]["dislikes"], ["Sahne"])


class EstimateConsumptionTest(unittest.TestCase):
    def test_normalizes_used_and_bought(self) -> None:
        transport, captured = _reply(
            {
                "used": [
                    {"name": "Zwiebel", "amount": 1, "unit": "Stück"},
                    {"name": "", "amount": 2},          # no name -> dropped
                    {"name": "Salz", "amount": 0},      # non-positive -> dropped
                ],
                "bought": [
                    {"name": "Sahne", "pack_amount": 1, "used_amount": 0.5, "unit": "Becher"},
                ],
            }
        )
        out = recipes_llm.estimate_consumption(
            dish="Pasta", guidance=["Anbraten", "Köcheln"], mode="shop",
            candidates=[{"name": "Zwiebel", "amount": 3, "unit": "Stück"}],
            buy=["Sahne"], model="google/gemini-3.1-flash-lite",
            settings=_settings(), transport=transport,
        )
        self.assertEqual(len(out["used"]), 1)
        self.assertEqual(out["used"][0], {"name": "Zwiebel", "amount": 1.0, "unit": "Stück"})
        self.assertEqual(out["bought"][0]["used_amount"], 0.5)
        user_msg = captured["body"]["messages"][1]["content"]
        self.assertIn("Zwiebel", user_msg)
        self.assertIn("Sahne", user_msg)

    def test_stock_mode_forces_empty_bought(self) -> None:
        transport, _ = _reply(
            {"used": [{"name": "Reis", "amount": 1, "unit": "Tasse"}],
             "bought": [{"name": "Sahne", "pack_amount": 1, "used_amount": 1}]}
        )
        out = recipes_llm.estimate_consumption(
            dish="Risotto", guidance=[], mode="stock",
            candidates=[{"name": "Reis", "amount": 2, "unit": "Tasse"}], buy=[],
            model="google/gemini-3.1-flash-lite", settings=_settings(), transport=transport,
        )
        self.assertEqual(out["bought"], [])

    def test_bought_used_clamped_to_pack(self) -> None:
        transport, _ = _reply(
            {"used": [], "bought": [{"name": "Feta", "pack_amount": 1, "used_amount": 5}]}
        )
        out = recipes_llm.estimate_consumption(
            dish="Salat", guidance=[], mode="shop", candidates=[], buy=["Feta"],
            model="google/gemini-3.1-flash-lite", settings=_settings(), transport=transport,
        )
        self.assertEqual(out["bought"][0]["used_amount"], 1.0)

    def test_correction_reaches_prompt(self) -> None:
        transport, captured = _reply({"used": [], "bought": []})
        recipes_llm.estimate_consumption(
            dish="Pasta", guidance=[], mode="stock", candidates=[], buy=[],
            correction="nur die halbe Zwiebel",
            model="google/gemini-3.1-flash-lite", settings=_settings(), transport=transport,
        )
        self.assertIn("nur die halbe Zwiebel", captured["body"]["messages"][1]["content"])


class ModelAllowlistTest(unittest.TestCase):
    def test_known_models_valid(self) -> None:
        self.assertTrue(recipes_llm.is_valid_model("google/gemini-3.1-flash-lite"))

    def test_unknown_model_invalid(self) -> None:
        self.assertFalse(recipes_llm.is_valid_model("evil/model; rm -rf"))


if __name__ == "__main__":
    unittest.main()
