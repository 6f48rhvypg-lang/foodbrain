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
from foodbrain_assistant.models import StockEntry, StockItem


TODAY = date(2026, 6, 6)


def _settings(**overrides) -> Settings:
    base = dict(
        grocy_base_url=None,
        grocy_api_key=None,
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
        self.consumed = []
        self.redated = []
        self._next_id = 100

    def get_quantity_units(self):
        return [{"id": "1", "name": "Piece"}, {"id": "2", "name": "Liter"}]

    def get_locations(self):
        return [{"id": "1", "name": "Fridge"}, {"id": "2", "name": "Pantry"}]

    def get_products(self):
        # Master list seen by intake_commit's duplicate-name guard. Starts empty;
        # products created during the commit get registered in-memory by the API.
        return [{"id": str(p["id"]), "name": p["name"]} for p in self.created]

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

    def consume_product(self, product_id, amount=1.0, *, spoiled=False):
        self.consumed.append(
            {"product_id": product_id, "amount": amount, "spoiled": spoiled}
        )
        return [{"transaction_id": f"cx-{product_id}"}]

    def get_product_entries(self, product_id):
        # Plenty in stock so consume/toss clamping (min(requested, live)) leaves
        # explicit voice quantities untouched; clamping itself is covered in
        # test_grocy_writeback / test_api.
        return [
            StockEntry(
                stock_entry_id=f"entry-{product_id}",
                product_id=product_id,
                amount=10.0,
                best_before_date=None,
                opened=False,
            )
        ]

    def set_entry_due_date(self, stock_entry_id, best_before_date):
        self.redated.append(
            {"stock_entry_id": stock_entry_id, "best_before_date": best_before_date}
        )
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

        def fake_understander(*, transcript, catalog, answers, mode="add"):
            seen["transcript"] = transcript
            seen["catalog"] = catalog
            seen["answers"] = answers
            seen["mode"] = mode
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

        def fake_understander(*, transcript, catalog, answers, mode="add"):
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

    def test_repeated_name_reuses_product_instead_of_duplicate(self) -> None:
        # The voice dump named two "Sonnenblumenöl" (full + half-open). Grocy
        # rejects a duplicate product name, so the second add must reuse the
        # product created for the first and just book a second stock entry.
        client = _FakeWriteClient()
        out = _api(client).intake_commit(
            [
                {"name": "Sonnenblumenöl", "unit": "bottle", "quantity": 1},
                {"name": "Sonnenblumenöl", "unit": "bottle", "quantity": 0.5, "opened": True},
            ]
        )
        self.assertEqual(out["created_products"], 1)   # created once
        self.assertEqual(out["added"], 2)              # but stocked twice
        self.assertEqual(len(client.created), 1)
        self.assertEqual(len(client.added), 2)
        self.assertEqual(client.added[0]["product_id"], client.added[1]["product_id"])

    def test_add_reuses_existing_grocy_product_by_name(self) -> None:
        # A "new" item whose name already exists in Grocy adds stock to it
        # rather than creating a duplicate.
        client = _FakeWriteClient()
        client.created.append({"id": "55", "name": "Couscous", "qu": "1", "location": "1"})
        out = _api(client).intake_commit([{"name": "couscous", "quantity": 0.5}])
        self.assertEqual(out["created_products"], 0)
        self.assertEqual(out["added"], 1)
        self.assertEqual(client.added[0]["product_id"], "55")

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

    def test_zero_amount_reported_as_failed_not_raised(self) -> None:
        # A bad item is reported in 'failed' so it doesn't sink the rest of a
        # batch, rather than aborting the whole commit.
        out = _api(_FakeWriteClient()).intake_commit(
            [{"name": "Milk", "matched_product_id": "10", "quantity": 0}]
        )
        self.assertEqual(out["added"], 0)
        self.assertEqual(len(out["failed"]), 1)
        self.assertEqual(out["failed"][0]["name"], "Milk")

    def test_one_bad_item_does_not_block_the_rest(self) -> None:
        client = _FakeWriteClient()
        out = _api(client).intake_commit(
            [
                {"name": "Milk", "matched_product_id": "10", "quantity": 0},  # bad
                {"name": "Sourdough", "unit": "Piece", "quantity": 1},        # good
            ]
        )
        self.assertEqual(out["added"], 1)
        self.assertEqual(len(out["failed"]), 1)
        self.assertEqual(client.created[0]["name"], "Sourdough")


class EditModeUnderstandTest(unittest.TestCase):
    def test_edit_mode_passes_mode_and_uses_edit_prompt(self) -> None:
        seen = {}

        def fake_understander(*, transcript, catalog, answers, mode="add"):
            seen["mode"] = mode
            return IntakeResult(items=[IntakeItem(name="Milk", action="consume")])

        api = _api(_FakeWriteClient(), intake_understander=fake_understander)
        out = api.intake_understand("finished the milk", mode="edit")
        self.assertEqual(seen["mode"], "edit")
        # The consume action survives reconciliation and reaches the SPA.
        self.assertEqual(out["items"][0]["action"], "consume")
        self.assertEqual(out["items"][0]["matched_product_id"], "10")

    def test_edit_system_prompt_is_sent_to_model(self) -> None:
        captured = {}

        def transport(url, headers, body, timeout):
            captured["body"] = json.loads(body.decode("utf-8"))
            return _openrouter_reply({"items": [], "questions": [], "summary": ""})

        understand_transcript(
            "I used half the tomato can",
            settings=_settings(),
            catalog=CATALOG,
            mode="edit",
            transport=transport,
        )
        system = captured["body"]["messages"][0]["content"]
        self.assertIn("CHANGED", system)
        self.assertIn("consume", system)

    def test_action_synonyms_normalize(self) -> None:
        def transport(url, headers, body, timeout):
            return _openrouter_reply(
                {
                    "items": [
                        {"name": "Milk", "action": "finished"},
                        {"name": "Eggs", "action": "threw away"},
                        {"name": "Cheese", "action": "wat"},
                    ],
                    "questions": [],
                    "summary": "",
                }
            )

        result = understand_transcript(
            "stuff", settings=_settings(), catalog=[], mode="edit", transport=transport
        )
        self.assertEqual([i.action for i in result.items], ["consume", "toss", "add"])


class EditModeCommitTest(unittest.TestCase):
    def test_consume_books_usage_and_is_undoable(self) -> None:
        client = _FakeWriteClient()
        out = _api(client).intake_commit(
            [{"name": "Milk", "matched_product_id": "10", "action": "consume", "quantity": 0.5}]
        )
        self.assertEqual(out["changed"], 1)
        self.assertEqual(out["added"], 0)
        self.assertEqual(client.consumed[0], {"product_id": "10", "amount": 0.5, "spoiled": False})
        self.assertEqual(out["results"][0]["transaction_id"], "cx-10")

    def test_toss_waste_removes(self) -> None:
        client = _FakeWriteClient()
        out = _api(client).intake_commit(
            [{"name": "Eggs", "matched_product_id": "10", "action": "toss", "quantity": 3}]
        )
        self.assertEqual(out["changed"], 1)
        self.assertTrue(client.consumed[0]["spoiled"])
        self.assertEqual(client.consumed[0]["amount"], 3)

    def test_set_date_updates_first_entry(self) -> None:
        client = _FakeWriteClient()
        out = _api(client).intake_commit(
            [{"name": "Milk", "matched_product_id": "10", "action": "set_date",
              "best_before_date": "2026-06-20"}]
        )
        self.assertEqual(out["changed"], 1)
        self.assertEqual(client.redated[0]["stock_entry_id"], "entry-10")
        self.assertEqual(client.redated[0]["best_before_date"], date(2026, 6, 20))

    def test_edit_without_match_is_reported_as_failed(self) -> None:
        out = _api(_FakeWriteClient()).intake_commit(
            [{"name": "Ketchup", "action": "consume"}]
        )
        self.assertEqual(out["changed"], 0)
        self.assertEqual(len(out["failed"]), 1)
        self.assertEqual(out["failed"][0]["action"], "consume")

    def test_set_date_without_date_is_reported_as_failed(self) -> None:
        out = _api(_FakeWriteClient()).intake_commit(
            [{"name": "Milk", "matched_product_id": "10", "action": "set_date"}]
        )
        self.assertEqual(out["changed"], 0)
        self.assertEqual(len(out["failed"]), 1)

    def test_mixed_add_and_edit_in_one_batch(self) -> None:
        client = _FakeWriteClient()
        out = _api(client).intake_commit(
            [
                {"name": "Milk", "matched_product_id": "10", "action": "consume", "quantity": 1},
                {"name": "Sourdough", "unit": "Piece", "location": "Pantry", "action": "add"},
            ]
        )
        self.assertEqual(out["added"], 1)
        self.assertEqual(out["changed"], 1)
        self.assertEqual(out["created_products"], 1)


if __name__ == "__main__":
    unittest.main()
