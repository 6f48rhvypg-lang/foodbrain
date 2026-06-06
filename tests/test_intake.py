import json
import unittest
from datetime import date

from foodbrain_assistant.api import ApiError, FoodBrainAPI
from foodbrain_assistant.config import Settings
from foodbrain_assistant.intake import (
    IntakeError,
    IntakeItem,
    IntakeNotConfigured,
    IntakeResult,
    reconcile_items,
    understand_transcript,
)
from foodbrain_assistant.models import StockItem


TODAY = date(2026, 6, 6)


def _settings(**overrides) -> Settings:
    base = dict(
        grocy_base_url=None,
        grocy_api_key=None,
        home_assistant_webhook_url=None,
        openrouter_api_key="test-key",
    )
    base.update(overrides)
    return Settings(**base)


def _openrouter_reply(payload: dict) -> str:
    """Wrap a model JSON answer in an OpenRouter chat-completions envelope."""
    return json.dumps(
        {"choices": [{"message": {"content": json.dumps(payload)}}]}
    )


CATALOG = [
    {"id": "10", "name": "Milk"},
    {"id": "11", "name": "Greek Yogurt"},
    {"id": "12", "name": "Carrots"},
]


class UnderstandTest(unittest.TestCase):
    def test_parses_items_questions_summary(self) -> None:
        captured = {}

        def transport(url, headers, body, timeout):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = json.loads(body.decode("utf-8"))
            return _openrouter_reply(
                {
                    "items": [
                        {
                            "name": "Milk",
                            "quantity": 1,
                            "unit": "l",
                            "opened": True,
                            "freshness_days": 4,
                            "location": "fridge",
                            "confidence": 0.9,
                            "note": "half full",
                        }
                    ],
                    "questions": ["How many carrots?"],
                    "summary": "1 item heard",
                }
            )

        result = understand_transcript(
            "half a liter of milk, opened, good for 4 days",
            settings=_settings(),
            catalog=CATALOG,
            transport=transport,
        )
        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        self.assertEqual(item.name, "Milk")
        self.assertTrue(item.opened)
        self.assertEqual(item.freshness_days, 4)
        self.assertEqual(result.questions, ["How many carrots?"])
        self.assertEqual(result.summary, "1 item heard")
        # URL and auth header are built from settings.
        self.assertTrue(captured["url"].endswith("/chat/completions"))
        self.assertEqual(captured["headers"]["Authorization"], "Bearer test-key")
        # Existing product names are sent so the model reuses them.
        user_msg = captured["body"]["messages"][1]["content"]
        self.assertIn("Milk", user_msg)
        self.assertIn("Carrots", user_msg)

    def test_strips_code_fences(self) -> None:
        def transport(url, headers, body, timeout):
            fenced = "```json\n" + json.dumps({"items": [{"name": "Eggs"}]}) + "\n```"
            return json.dumps({"choices": [{"message": {"content": fenced}}]})

        result = understand_transcript(
            "a dozen eggs", settings=_settings(), catalog=[], transport=transport
        )
        self.assertEqual([i.name for i in result.items], ["Eggs"])
        self.assertEqual(result.items[0].quantity, 1.0)  # default applied

    def test_requires_api_key(self) -> None:
        with self.assertRaises(IntakeNotConfigured):
            understand_transcript(
                "milk", settings=_settings(openrouter_api_key=None), catalog=[]
            )

    def test_empty_transcript_rejected(self) -> None:
        with self.assertRaises(IntakeError):
            understand_transcript("   ", settings=_settings(), catalog=[])

    def test_surfaces_openrouter_error(self) -> None:
        def transport(url, headers, body, timeout):
            return json.dumps({"error": {"message": "no credits"}})

        with self.assertRaises(IntakeError) as ctx:
            understand_transcript(
                "milk", settings=_settings(), catalog=[], transport=transport
            )
        self.assertIn("no credits", str(ctx.exception))


class ReconcileTest(unittest.TestCase):
    def test_exact_match_uses_existing_product(self) -> None:
        items = [IntakeItem(name="milk")]
        [resolved] = reconcile_items(items, CATALOG)
        self.assertEqual(resolved.matched_product_id, "10")
        self.assertEqual(resolved.match, "exact")

    def test_alias_match(self) -> None:
        items = [IntakeItem(name="Milch")]
        [resolved] = reconcile_items(items, CATALOG, aliases={"milch": "milk"})
        self.assertEqual(resolved.matched_product_id, "10")

    def test_fuzzy_unique_containment(self) -> None:
        items = [IntakeItem(name="yogurt")]
        [resolved] = reconcile_items(items, CATALOG)
        self.assertEqual(resolved.matched_product_id, "11")
        self.assertEqual(resolved.match, "fuzzy")

    def test_new_product_when_unmatched(self) -> None:
        items = [IntakeItem(name="Sourdough Bread")]
        [resolved] = reconcile_items(items, CATALOG)
        self.assertIsNone(resolved.matched_product_id)
        self.assertEqual(resolved.match, "new")

    def test_ambiguous_containment_is_not_matched(self) -> None:
        catalog = [{"id": "1", "name": "Apple Juice"}, {"id": "2", "name": "Apple Sauce"}]
        items = [IntakeItem(name="apple")]
        [resolved] = reconcile_items(items, catalog)
        self.assertIsNone(resolved.matched_product_id)


class _FakeWriteClient:
    """Minimal stand-in for a writable GrocyClient used by intake_commit."""

    def __init__(self) -> None:
        self.created = []
        self.added = []
        self.opened = []
        self._next_id = 100

    def get_quantity_units(self):
        return [{"id": "1", "name": "Piece"}, {"id": "2", "name": "Liter"}]

    def get_locations(self):
        return [{"id": "1", "name": "Fridge"}, {"id": "2", "name": "Pantry"}]

    def create_product(self, name, *, qu_id_stock, location_id, qu_id_purchase=None):
        self._next_id += 1
        new_id = str(self._next_id)
        self.created.append(
            {"id": new_id, "name": name, "qu": qu_id_stock, "location": location_id}
        )
        return new_id

    def add_stock(self, product_id, amount=1.0, *, best_before_date=None, location_id=None):
        self.added.append(
            {
                "product_id": product_id,
                "amount": amount,
                "best_before_date": best_before_date,
                "location_id": location_id,
            }
        )
        return [{"transaction_id": f"txn-{product_id}"}]

    def open_product(self, product_id, amount=1.0):
        self.opened.append({"product_id": product_id, "amount": amount})
        return {}


def _api(client, **overrides) -> FoodBrainAPI:
    params = dict(
        settings=_settings(),
        stock_provider=lambda: [],
        product_catalog_provider=lambda: CATALOG,
        write_client_factory=lambda: client,
        today_provider=lambda: TODAY,
        source="test",
    )
    params.update(overrides)
    return FoodBrainAPI(**params)


class IntakeUnderstandApiTest(unittest.TestCase):
    def test_understand_uses_injected_understander_and_reconciles(self) -> None:
        seen = {}

        def fake_understander(*, transcript, catalog, answers):
            seen["transcript"] = transcript
            seen["catalog"] = catalog
            seen["answers"] = answers
            return IntakeResult(
                items=[IntakeItem(name="milk"), IntakeItem(name="Sourdough")],
                questions=["q?"],
                summary="ok",
            )

        api = _api(_FakeWriteClient(), intake_understander=fake_understander)
        out = api.intake_understand("milk and bread", answers="2 carrots")
        self.assertEqual(seen["transcript"], "milk and bread")
        self.assertEqual(seen["answers"], "2 carrots")
        self.assertEqual(seen["catalog"], CATALOG)
        self.assertEqual(out["items"][0]["matched_product_id"], "10")  # milk
        self.assertEqual(out["items"][1]["match"], "new")  # sourdough
        self.assertEqual(out["questions"], ["q?"])

    def test_catalog_falls_back_to_stock_when_no_provider(self) -> None:
        stock = [StockItem("9", "Butter", 1, "pack", None)]
        captured = {}

        def fake_understander(*, transcript, catalog, answers):
            captured["catalog"] = catalog
            return IntakeResult(items=[IntakeItem(name="butter")])

        api = _api(
            _FakeWriteClient(),
            product_catalog_provider=None,
            stock_provider=lambda: stock,
            intake_understander=fake_understander,
        )
        out = api.intake_understand("butter")
        self.assertEqual(captured["catalog"], [{"id": "9", "name": "Butter"}])
        self.assertEqual(out["items"][0]["matched_product_id"], "9")


class IntakeCommitApiTest(unittest.TestCase):
    def test_adds_to_existing_product(self) -> None:
        client = _FakeWriteClient()
        api = _api(client)
        out = api.intake_commit(
            [
                {
                    "name": "Milk",
                    "matched_product_id": "10",
                    "quantity": 2,
                    "best_before_date": "2026-06-10",
                    "opened": True,
                }
            ]
        )
        self.assertEqual(out["added"], 1)
        self.assertEqual(out["created_products"], 0)
        self.assertEqual(client.added[0]["product_id"], "10")
        self.assertEqual(client.added[0]["amount"], 2)
        self.assertEqual(client.added[0]["best_before_date"], date(2026, 6, 10))
        self.assertEqual(client.opened[0]["product_id"], "10")  # opened booked
        self.assertEqual(out["results"][0]["transaction_id"], "txn-10")
        self.assertEqual(client.created, [])

    def test_creates_new_product_then_adds(self) -> None:
        client = _FakeWriteClient()
        api = _api(client)
        out = api.intake_commit(
            [{"name": "Sourdough", "unit": "Piece", "location": "Pantry", "quantity": 1}]
        )
        self.assertEqual(out["created_products"], 1)
        self.assertEqual(client.created[0]["name"], "Sourdough")
        self.assertEqual(client.created[0]["qu"], "1")  # Piece
        self.assertEqual(client.created[0]["location"], "2")  # Pantry
        # add_stock targets the freshly created id.
        self.assertEqual(client.added[0]["product_id"], client.created[0]["id"])
        self.assertTrue(out["results"][0]["created"])

    def test_unknown_unit_falls_back_to_first(self) -> None:
        client = _FakeWriteClient()
        api = _api(client)
        api.intake_commit([{"name": "Mystery", "unit": "zorp"}])
        self.assertEqual(client.created[0]["qu"], "1")  # first unit

    def test_commit_requires_writer(self) -> None:
        api = _api(_FakeWriteClient(), write_client_factory=None)
        with self.assertRaises(ApiError) as ctx:
            api.intake_commit([{"name": "Milk", "matched_product_id": "10"}])
        self.assertEqual(ctx.exception.status, 403)

    def test_empty_items_rejected(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            _api(_FakeWriteClient()).intake_commit([])
        self.assertEqual(ctx.exception.status, 400)

    def test_zero_amount_rejected(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            _api(_FakeWriteClient()).intake_commit(
                [{"name": "Milk", "matched_product_id": "10", "quantity": 0}]
            )
        self.assertEqual(ctx.exception.status, 400)


if __name__ == "__main__":
    unittest.main()
