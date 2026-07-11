import json
import unittest

from foodbrain_assistant import shopping_llm
from foodbrain_assistant.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        grocy_base_url=None,
        grocy_api_key=None,
        openrouter_api_key="test-key",
    )
    base.update(overrides)
    return Settings(**base)


def _reply(payload: dict):
    captured = {}

    def transport(url, headers, body, timeout):
        captured["body"] = json.loads(body.decode("utf-8"))
        return json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]})

    return transport, captured


class SuggestDietItemsTest(unittest.TestCase):
    def test_focus_and_inventory_reach_the_prompt(self) -> None:
        transport, captured = _reply(
            {"items": [{"name": "Linsen", "amount": 500, "unit": "g", "reason": "proteinreich"}]}
        )
        out = shopping_llm.suggest_diet_items(
            focus="proteinreich",
            inventory_lines=["Reis — 1 kg (Vorrat)"],
            taste={"likes": ["Chili"], "dislikes": []},
            model="google/gemini-3.1-flash-lite",
            settings=_settings(),
            transport=transport,
        )
        self.assertEqual(out["items"][0]["name"], "Linsen")
        self.assertEqual(out["items"][0]["reason"], "proteinreich")
        user_msg = captured["body"]["messages"][1]["content"]
        self.assertIn("proteinreich", user_msg)
        self.assertIn("Reis", user_msg)

    def test_item_without_reason_is_dropped(self) -> None:
        transport, _ = _reply(
            {"items": [{"name": "Tofu", "reason": ""}, {"name": "Kichererbsen", "reason": "Ballaststoffe"}]}
        )
        out = shopping_llm.suggest_diet_items(
            focus="mehr Gemüse",
            inventory_lines=[],
            taste={},
            model="google/gemini-3.1-flash-lite",
            settings=_settings(),
            transport=transport,
        )
        names = [item["name"] for item in out["items"]]
        self.assertEqual(names, ["Kichererbsen"])

    def test_invalid_amount_degrades_to_none(self) -> None:
        transport, _ = _reply(
            {"items": [{"name": "Haferflocken", "amount": "viel", "reason": "Ballaststoffe"}]}
        )
        out = shopping_llm.suggest_diet_items(
            focus="Vorrat auffüllen",
            inventory_lines=[],
            taste={},
            model="google/gemini-3.1-flash-lite",
            settings=_settings(),
            transport=transport,
        )
        self.assertIsNone(out["items"][0]["amount"])

    def test_caps_at_eight_items(self) -> None:
        rows = [{"name": f"Item {i}", "reason": "x"} for i in range(12)]
        transport, _ = _reply({"items": rows})
        out = shopping_llm.suggest_diet_items(
            focus="x",
            inventory_lines=[],
            taste={},
            model="google/gemini-3.1-flash-lite",
            settings=_settings(),
            transport=transport,
        )
        self.assertEqual(len(out["items"]), 8)


if __name__ == "__main__":
    unittest.main()
