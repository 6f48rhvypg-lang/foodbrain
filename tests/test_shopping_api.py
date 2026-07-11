"""Shopping list API: Grocy-overlay join, manual/diet add, commit-bought
(buy->stock), depletion hook, and the reasoned suggestion engine."""

import json
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from foodbrain_assistant import shoppingstore
from foodbrain_assistant.api import ApiError, FoodBrainAPI
from foodbrain_assistant.config import Settings
from foodbrain_assistant.grocy_client import GrocyClientError
from foodbrain_assistant.models import StockEntry, StockItem
from foodbrain_assistant.server import make_handler


TODAY = date(2026, 7, 11)


def _settings(**overrides) -> Settings:
    base = dict(grocy_base_url=None, grocy_api_key=None, openrouter_api_key="test-key")
    base.update(overrides)
    return Settings(**base)


class FakeGrocy:
    """Mutating Grocy stand-in with stock + a native shopping_list table."""

    def __init__(self, products=None, stock=None, shopping_list=None) -> None:
        self.products = list(products or [])
        self.stock = {str(k): float(v) for k, v in (stock or {}).items()}
        self.shopping_rows = {str(r["id"]): dict(r) for r in (shopping_list or [])}
        self.txns: dict = {}
        self._tx = 0
        self._pid = 100
        self._sid = 100
        self.calls: list = []

    # master data
    def get_products(self):
        return [dict(p) for p in self.products]

    def get_quantity_units(self):
        return [{"id": "1", "name": "Stück"}]

    def get_locations(self):
        return [{"id": "1", "name": "Kühlschrank"}]

    def create_product(self, name, qu_id_stock=None, location_id=None):
        self._pid += 1
        pid = str(self._pid)
        self.products.append({"id": pid, "name": name})
        self.stock.setdefault(pid, 0.0)
        self.calls.append(("create", name, pid))
        return pid

    # stock
    def add_stock(self, product_id, amount=1.0, *, best_before_date=None, location_id=None):
        pid = str(product_id)
        self.stock[pid] = self.stock.get(pid, 0.0) + amount
        tx = self._new_tx(pid, +amount)
        self.calls.append(("add", pid, amount))
        return [{"transaction_id": tx}]

    def consume_product(self, product_id, amount=1.0, *, spoiled=False):
        pid = str(product_id)
        self.stock[pid] -= amount
        tx = self._new_tx(pid, -amount)
        self.calls.append(("consume", pid, amount, spoiled))
        return [{"transaction_id": tx}]

    def get_product_entries(self, product_id):
        pid = str(product_id)
        amt = self.stock.get(pid, 0.0)
        return [StockEntry("e-" + pid, pid, amt, None)] if amt > 1e-9 else []

    def undo_transaction(self, transaction_id):
        pid, delta = self.txns.get(transaction_id, (None, 0.0))
        if pid is not None:
            self.stock[pid] -= delta
        self.calls.append(("undo", transaction_id))

    def _new_tx(self, pid, delta):
        self._tx += 1
        tx = f"tx{self._tx}"
        self.txns[tx] = (pid, delta)
        return tx

    # shopping list
    def get_shopping_list(self):
        return [dict(r) for r in self.shopping_rows.values()]

    def add_shopping_item(self, *, product_id=None, note=None, amount=1.0, qu_id=None, shopping_list_id="1"):
        self._sid += 1
        sid = str(self._sid)
        self.shopping_rows[sid] = {
            "id": sid,
            "product_id": product_id,
            "note": note,
            "amount": amount,
            "qu_id": qu_id,
            "done": "0",
        }
        self.calls.append(("shop-add", sid, product_id, note))
        return sid

    def update_shopping_item(self, item_id, changes):
        row = self.shopping_rows.get(str(item_id))
        if row is None:
            raise GrocyClientError("no such shopping list row")
        row.update(changes)
        self.calls.append(("shop-update", item_id, changes))

    def remove_shopping_item(self, item_id):
        self.shopping_rows.pop(str(item_id), None)
        self.calls.append(("shop-remove", item_id))


def _stock() -> list:
    return [
        StockItem("1", "Milch", 1, "Liter", None),
        StockItem("2", "Zucchini", 2, "Stück", TODAY + timedelta(days=3)),
    ]


class ShoppingApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.store = Path(self._dir.name) / "shopping.json"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _api(self, client=None, **overrides) -> FoodBrainAPI:
        params = dict(
            settings=_settings(),
            stock_provider=_stock,
            today_provider=lambda: TODAY,
            shopping_store_path=self.store,
            source="test",
        )
        if client is not None:
            params["write_client_factory"] = lambda: client
            params["product_catalog_provider"] = client.get_products
        params.update(overrides)
        return FoodBrainAPI(**params)

    # --- list: join + rev ---

    def test_list_joins_grocy_rows_with_overlay(self) -> None:
        client = FakeGrocy(
            products=[{"id": "1", "name": "Milch"}],
            shopping_list=[{"id": "10", "product_id": "1", "note": None, "amount": 1, "done": "0"}],
        )
        api = self._api(client)
        shoppingstore.set_overlay(self.store, "10", source="manual", reason="")
        out = api.shopping_list()
        self.assertEqual(len(out["items"]), 1)
        row = out["items"][0]
        self.assertEqual(row["name"], "Milch")
        self.assertEqual(row["source"], "manual")
        self.assertFalse(row["done"])
        self.assertIn("rev", out)

    def test_list_free_text_row_uses_note(self) -> None:
        client = FakeGrocy(
            shopping_list=[{"id": "10", "product_id": None, "note": "Blumen", "amount": 1, "done": "0"}]
        )
        out = self._api(client).shopping_list()
        self.assertEqual(out["items"][0]["name"], "Blumen")

    def test_list_prunes_stale_overlay_entries(self) -> None:
        client = FakeGrocy(shopping_list=[])
        shoppingstore.set_overlay(self.store, "999", source="manual")
        self._api(client).shopping_list()
        self.assertEqual(shoppingstore.get_overlay(self.store), {})

    def test_rev_changes_when_grocy_rows_change(self) -> None:
        client = FakeGrocy(shopping_list=[{"id": "1", "product_id": None, "note": "A", "amount": 1, "done": "0"}])
        api = self._api(client)
        rev1 = api.shopping_list()["rev"]
        client.shopping_rows["1"]["done"] = "1"
        rev2 = api.shopping_list()["rev"]
        self.assertNotEqual(rev1, rev2)

    def test_rev_stable_when_nothing_changes(self) -> None:
        client = FakeGrocy(shopping_list=[{"id": "1", "product_id": None, "note": "A", "amount": 1, "done": "0"}])
        api = self._api(client)
        self.assertEqual(api.shopping_list()["rev"], api.shopping_list()["rev"])

    # --- add / update / remove ---

    def test_add_free_text_item(self) -> None:
        client = FakeGrocy()
        out = self._api(client).shopping_add(name="Blumen", source="manual")
        self.assertTrue(out["ok"])
        self.assertEqual(client.shopping_rows[out["id"]]["note"], "Blumen")
        self.assertEqual(shoppingstore.get_overlay(self.store)[out["id"]]["source"], "manual")

    def test_add_resolves_known_product_by_name(self) -> None:
        client = FakeGrocy(products=[{"id": "5", "name": "Kaffee"}])
        out = self._api(client).shopping_add(name="Kaffee")
        self.assertEqual(out["product_id"], "5")
        self.assertEqual(client.shopping_rows[out["id"]]["product_id"], "5")

    def test_add_requires_name_or_product_id(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self._api(FakeGrocy()).shopping_add()
        self.assertEqual(ctx.exception.status, 400)

    def test_update_toggles_done(self) -> None:
        client = FakeGrocy(shopping_list=[{"id": "1", "product_id": None, "note": "A", "amount": 1, "done": "0"}])
        self._api(client).shopping_update("1", done=True)
        self.assertEqual(client.shopping_rows["1"]["done"], "1")

    def test_update_requires_a_change(self) -> None:
        client = FakeGrocy(shopping_list=[{"id": "1", "product_id": None, "note": "A", "amount": 1, "done": "0"}])
        with self.assertRaises(ApiError):
            self._api(client).shopping_update("1")

    def test_remove_drops_grocy_row_and_overlay(self) -> None:
        client = FakeGrocy(shopping_list=[{"id": "1", "product_id": None, "note": "A", "amount": 1, "done": "0"}])
        shoppingstore.set_overlay(self.store, "1", source="manual")
        self._api(client).shopping_remove("1")
        self.assertNotIn("1", client.shopping_rows)
        self.assertEqual(shoppingstore.get_overlay(self.store), {})

    # --- staple mode ---

    def test_staple_sets_and_validates_mode(self) -> None:
        out = self._api(FakeGrocy(), write_client_factory=None).shopping_staple("Kaffee", mode="auto")
        self.assertEqual(out["mode"], "auto")
        with self.assertRaises(ApiError):
            self._api(FakeGrocy()).shopping_staple("Kaffee", mode="bogus")

    # --- commit bought ---

    def test_commit_bought_adds_stock_records_buy_and_clears_list(self) -> None:
        client = FakeGrocy(
            products=[{"id": "5", "name": "Kaffee"}],
            stock={"5": 0.0},
            shopping_list=[{"id": "1", "product_id": "5", "note": None, "amount": 1, "done": "0"}],
        )
        shoppingstore.set_overlay(self.store, "1", source="manual")
        out = self._api(client).shopping_commit_bought(
            [{"item_id": "1", "name": "Kaffee", "product_id": "5", "amount": 1}]
        )
        self.assertEqual(out["added"][0]["product_id"], "5")
        self.assertEqual(client.stock["5"], 1.0)
        self.assertNotIn("1", client.shopping_rows)
        self.assertEqual(shoppingstore.get_overlay(self.store), {})
        habit = shoppingstore.get_habit(self.store, "Kaffee")
        self.assertEqual(len(habit["buys"]), 1)

    def test_commit_bought_creates_new_product_when_unmatched(self) -> None:
        client = FakeGrocy()
        out = self._api(client).shopping_commit_bought([{"name": "Senf", "amount": 1}])
        self.assertTrue(out["added"][0]["ok"])
        self.assertEqual(len(client.products), 1)

    def test_commit_bought_isolates_failures(self) -> None:
        client = FakeGrocy()
        out = self._api(client).shopping_commit_bought([{"name": ""}, {"name": "Senf", "amount": 1}])
        self.assertEqual(len(out["failed"]), 1)
        self.assertEqual(len(out["added"]), 1)

    def test_commit_bought_requires_items(self) -> None:
        with self.assertRaises(ApiError):
            self._api(FakeGrocy()).shopping_commit_bought([])

    # --- diet suggestions ---

    def test_diet_uses_injected_suggester(self) -> None:
        captured = {}

        def suggester(**kwargs):
            captured.update(kwargs)
            return {"focus": kwargs["focus"], "items": [{"name": "Linsen", "reason": "Protein"}]}

        out = self._api(FakeGrocy(), diet_suggester=suggester, write_client_factory=None).shopping_diet(
            "proteinreich"
        )
        self.assertEqual(out["items"][0]["name"], "Linsen")
        self.assertEqual(captured["focus"], "proteinreich")

    def test_diet_requires_focus(self) -> None:
        with self.assertRaises(ApiError):
            self._api(FakeGrocy(), diet_suggester=lambda **k: {}).shopping_diet("")

    def test_diet_without_llm_configured_is_503(self) -> None:
        api = self._api(FakeGrocy(), settings=_settings(openrouter_api_key=None))
        with self.assertRaises(ApiError) as ctx:
            api.shopping_diet("proteinreich")
        self.assertEqual(ctx.exception.status, 503)

    # --- depletion hook ---

    def test_consume_to_zero_records_removal(self) -> None:
        client = FakeGrocy(products=[{"id": "1", "name": "Milch"}], stock={"1": 1.0})
        self._api(client).consume("1", 1.0)
        habit = shoppingstore.get_habit(self.store, "Milch")
        self.assertEqual(len(habit["removals"]), 1)

    def test_consume_leaving_stock_does_not_record_removal(self) -> None:
        client = FakeGrocy(products=[{"id": "1", "name": "Milch"}], stock={"1": 2.0})
        self._api(client).consume("1", 1.0)
        self.assertIsNone(shoppingstore.get_habit(self.store, "Milch"))

    def test_depletion_of_auto_staple_adds_to_shopping_list(self) -> None:
        client = FakeGrocy(products=[{"id": "1", "name": "Milch"}], stock={"1": 1.0})
        # Seed enough buy history to count as a staple, then pin it to auto.
        for _ in range(3):
            shoppingstore.record_buy(self.store, name="Milch", product_id="1", amount=1)
        shoppingstore.set_mode(self.store, name="Milch", product_id="1", mode="auto")
        self._api(client).consume("1", 1.0)
        rows = client.get_shopping_list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["product_id"], "1")

    def test_toss_confirmed_also_records_removal(self) -> None:
        client = FakeGrocy(products=[{"id": "1", "name": "Milch"}], stock={"1": 1.0})
        self._api(client).toss("1", 1.0, confirm=True)
        habit = shoppingstore.get_habit(self.store, "Milch")
        self.assertEqual(len(habit["removals"]), 1)

    # --- suggestion engine ---

    def test_low_qty_signal_reason(self) -> None:
        client = FakeGrocy(products=[{"id": "1", "name": "Milch"}])
        for amt in (5, 5, 5):
            shoppingstore.record_buy(self.store, name="Milch", product_id="1", amount=amt)
        # current stock (1 Liter, well below typical 5) triggers low_qty.
        out = self._api(client).shopping_list()
        low = next(s for s in out["suggestions"] if s["name"] == "Milch")
        self.assertEqual(low["signal"], "low_qty")
        self.assertIn("übrig", low["reason"])

    def test_depleted_signal_when_removed_and_not_in_stock(self) -> None:
        client = FakeGrocy(products=[{"id": "9", "name": "Senf"}])
        for _ in range(3):
            shoppingstore.record_buy(self.store, name="Senf", product_id="9", amount=1)
        shoppingstore.record_removal(self.store, name="Senf", product_id="9")
        out = self._api(client).shopping_list()
        row = next(s for s in out["suggestions"] if s["name"] == "Senf")
        self.assertEqual(row["signal"], "depleted")

    def test_interval_signal_when_overdue(self) -> None:
        client = FakeGrocy(products=[{"id": "9", "name": "Reis"}], stock={"9": 5.0})
        oldest = datetime.now(timezone.utc) - timedelta(days=40)
        mid = datetime.now(timezone.utc) - timedelta(days=30)
        recent = datetime.now(timezone.utc) - timedelta(days=20)
        shoppingstore.record_buy(self.store, name="Reis", product_id="9", amount=5)
        # Directly seed three buys with 10-day gaps (median interval = 10),
        # last one 20 days ago -> overdue, without also being low on stock
        # (typical amount == current amount).
        data = shoppingstore.load(self.store)
        data["habits"]["reis"]["buys"] = [
            {"amount": 5.0, "ts": oldest.isoformat()},
            {"amount": 5.0, "ts": mid.isoformat()},
            {"amount": 5.0, "ts": recent.isoformat()},
        ]
        shoppingstore.save(self.store, data)
        out = self._api(client).shopping_list()
        row = next((s for s in out["suggestions"] if s["name"] == "Reis"), None)
        self.assertIsNotNone(row)
        self.assertEqual(row["signal"], "interval")
        self.assertIn("Tage", row["reason"])

    def test_off_mode_is_never_suggested(self) -> None:
        client = FakeGrocy(products=[{"id": "9", "name": "Senf"}])
        for _ in range(3):
            shoppingstore.record_buy(self.store, name="Senf", product_id="9", amount=1)
        shoppingstore.record_removal(self.store, name="Senf", product_id="9")
        shoppingstore.set_mode(self.store, name="Senf", product_id="9", mode="off")
        out = self._api(client).shopping_list()
        self.assertFalse(any(s["name"] == "Senf" for s in out["suggestions"]))

    def test_items_already_on_list_are_not_suggested(self) -> None:
        client = FakeGrocy(
            products=[{"id": "1", "name": "Milch"}],
            shopping_list=[{"id": "5", "product_id": "1", "note": None, "amount": 1, "done": "0"}],
        )
        for _ in range(3):
            shoppingstore.record_buy(self.store, name="Milch", product_id="1", amount=1)
        shoppingstore.record_removal(self.store, name="Milch", product_id="1")
        out = self._api(client).shopping_list()
        self.assertFalse(any(s["name"] == "Milch" for s in out["suggestions"]))

    def test_auto_staple_suggestion_gets_added_on_list_read(self) -> None:
        client = FakeGrocy(products=[{"id": "9", "name": "Senf"}])
        for _ in range(3):
            shoppingstore.record_buy(self.store, name="Senf", product_id="9", amount=1)
        shoppingstore.record_removal(self.store, name="Senf", product_id="9")
        shoppingstore.set_mode(self.store, name="Senf", product_id="9", mode="auto")
        out = self._api(client).shopping_list()
        # Auto-added, so it should no longer show up as a pending suggestion...
        self.assertFalse(any(s["name"] == "Senf" for s in out["suggestions"]))
        # ...but should now be a real Grocy row.
        self.assertTrue(any(i["name"] == "Senf" for i in out["items"]))

    def test_auto_add_is_deduped_against_existing_row(self) -> None:
        client = FakeGrocy(
            products=[{"id": "9", "name": "Senf"}],
            shopping_list=[{"id": "5", "product_id": "9", "note": None, "amount": 1, "done": "0"}],
        )
        for _ in range(3):
            shoppingstore.record_buy(self.store, name="Senf", product_id="9", amount=1)
        shoppingstore.record_removal(self.store, name="Senf", product_id="9")
        shoppingstore.set_mode(self.store, name="Senf", product_id="9", mode="auto")
        self._api(client).shopping_list()
        self.assertEqual(len(client.shopping_rows), 1)  # not duplicated


class ShoppingHttpSmokeTest(unittest.TestCase):
    """A real socket round-trip over the http.server handler for the new routes."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.store = Path(self._dir.name) / "shopping.json"
        self.client = FakeGrocy(
            products=[{"id": "1", "name": "Milch"}],
            shopping_list=[{"id": "10", "product_id": "1", "note": None, "amount": 1, "done": "0"}],
        )
        api = FoodBrainAPI(
            settings=_settings(),
            stock_provider=_stock,
            today_provider=lambda: TODAY,
            shopping_store_path=self.store,
            write_client_factory=lambda: self.client,
            product_catalog_provider=self.client.get_products,
            source="test",
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self._dir.cleanup()

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def _get(self, path: str):
        with urlopen(self._url(path)) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def _post(self, path: str, body: dict):
        request = Request(
            self._url(path),
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_list_endpoint(self) -> None:
        status, body = self._get("/api/shopping/list")
        self.assertEqual(status, 200)
        self.assertEqual(body["items"][0]["name"], "Milch")
        self.assertIn("rev", body)

    def test_add_update_remove_round_trip(self) -> None:
        status, body = self._post("/api/shopping/add", {"name": "Blumen"})
        self.assertEqual(status, 200)
        item_id = body["id"]

        status, _ = self._post("/api/shopping/update", {"item_id": item_id, "done": True})
        self.assertEqual(status, 200)
        self.assertEqual(self.client.shopping_rows[item_id]["done"], "1")

        status, _ = self._post("/api/shopping/remove", {"item_id": item_id})
        self.assertEqual(status, 200)
        self.assertNotIn(item_id, self.client.shopping_rows)

    def test_staple_endpoint(self) -> None:
        status, body = self._post("/api/shopping/staple", {"name": "Kaffee", "mode": "auto"})
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "auto")

    def test_staple_endpoint_rejects_bad_mode(self) -> None:
        with self.assertRaises(HTTPError) as ctx:
            self._post("/api/shopping/staple", {"name": "Kaffee", "mode": "bogus"})
        self.assertEqual(ctx.exception.code, 400)

    def test_commit_bought_endpoint(self) -> None:
        status, body = self._post(
            "/api/shopping/commit-bought",
            {"items": [{"item_id": "10", "name": "Milch", "product_id": "1", "amount": 1}]},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self.client.stock["1"], 1.0)


if __name__ == "__main__":
    unittest.main()
