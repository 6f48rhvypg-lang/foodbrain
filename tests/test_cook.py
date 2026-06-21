"""Cook -> consumption tracking: estimate, commit (add+deduct, depleted,
failure isolation), Verlauf history + adjust (undo + reconsume)."""

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from foodbrain_assistant import cookmemory
from foodbrain_assistant.api import ApiError, FoodBrainAPI
from foodbrain_assistant.config import Settings
from foodbrain_assistant.grocy_client import GrocyClientError
from foodbrain_assistant.models import StockEntry, StockItem


TODAY = date(2026, 6, 4)


def _settings(**overrides) -> Settings:
    base = dict(
        grocy_base_url=None,
        grocy_api_key=None,
        home_assistant_webhook_url=None,
        openrouter_api_key="test-key",
    )
    base.update(overrides)
    return Settings(**base)


def _stock() -> list[StockItem]:
    return [
        StockItem("1", "Joghurt", 1, "Becher", TODAY - timedelta(days=1)),
        StockItem("2", "Zucchini", 2, "Stück", TODAY + timedelta(days=3)),
        StockItem("3", "Karotten", 4, "Stück", TODAY + timedelta(days=20)),
    ]


class FakeGrocy:
    """A mutating Grocy stand-in: add/consume change live stock, undo reverses
    a recorded transaction (so the adjust undo+reconsume path is exercised)."""

    def __init__(self, products=None, stock=None) -> None:
        self.products = list(products or [])
        self.stock = {str(k): float(v) for k, v in (stock or {}).items()}
        self.txns: dict = {}
        self._tx = 0
        self._pid = 100
        self.calls: list = []

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

    def add_stock(self, product_id, amount=1.0, *, best_before_date=None, location_id=None):
        pid = str(product_id)
        self.stock[pid] = self.stock.get(pid, 0.0) + amount
        tx = self._new_tx(pid, +amount)
        self.calls.append(("add", pid, amount))
        return [{"transaction_id": tx}]

    def consume_product(self, product_id, amount=1.0, *, spoiled=False):
        pid = str(product_id)
        if amount > self.stock.get(pid, 0.0) + 1e-9:
            raise GrocyClientError("Grocy request failed with HTTP 400")
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
            self.stock[pid] -= delta  # reverse the booking
        self.calls.append(("undo", transaction_id))

    def _new_tx(self, pid, delta):
        self._tx += 1
        tx = f"tx{self._tx}"
        self.txns[tx] = (pid, delta)
        return tx


class CookTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.store = Path(self._dir.name) / "cookmemory.json"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _api(self, client=None, **overrides) -> FoodBrainAPI:
        params = dict(
            settings=_settings(),
            stock_provider=_stock,
            today_provider=lambda: TODAY,
            cook_store_path=self.store,
            source="test",
        )
        if client is not None:
            params["write_client_factory"] = lambda: client
            params["product_catalog_provider"] = client.get_products
        params.update(overrides)
        return FoodBrainAPI(**params)

    # --- estimate ---

    def test_estimate_reconciles_used_names_to_products(self) -> None:
        client = FakeGrocy(products=[{"id": "2", "name": "Zucchini"}], stock={"2": 2})

        def est(**kwargs):
            return {"used": [{"name": "Zucchini", "amount": 1, "unit": "Stück"}], "bought": []}

        out = self._api(client, consumption_estimator=est).recipe_cook_estimate(
            "Zucchini-Pasta", guidance=["braten"], mode="stock"
        )
        row = out["items"][0]
        self.assertEqual(row["kind"], "consume")
        self.assertEqual(row["matched_product_id"], "2")
        self.assertEqual(row["amount"], 1)
        # current stock is attached so the review can show "X of Y -> Z left".
        self.assertEqual(row["stock_amount"], 2)
        self.assertEqual(row["stock_unit"], "Stück")

    def test_estimate_unmatched_row_has_null_stock(self) -> None:
        client = FakeGrocy(products=[{"id": "2", "name": "Zucchini"}], stock={"2": 2})

        def est(**kwargs):
            return {"used": [{"name": "Einhornfleisch", "amount": 1}], "bought": []}

        out = self._api(client, consumption_estimator=est).recipe_cook_estimate(
            "Fantasie", mode="stock"
        )
        row = out["items"][0]
        self.assertIsNone(row["matched_product_id"])
        self.assertIsNone(row["stock_amount"])

    def test_estimate_bought_rows_only_in_shop_mode(self) -> None:
        client = FakeGrocy()

        def est(**kwargs):
            return {"used": [], "bought": [{"name": "Sahne", "pack_amount": 1,
                                            "used_amount": 0.5, "unit": "Becher"}]}

        out = self._api(client, consumption_estimator=est).recipe_cook_estimate(
            "Pasta", buy=["Sahne"], mode="shop"
        )
        bought = [r for r in out["items"] if r["kind"] == "bought"]
        self.assertEqual(bought[0]["name"], "Sahne")
        self.assertEqual(bought[0]["used_amount"], 0.5)

    def test_estimate_requires_dish(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self._api(FakeGrocy(), consumption_estimator=lambda **k: {}).recipe_cook_estimate("")
        self.assertEqual(ctx.exception.status, 400)

    # --- commit ---

    def test_commit_consume_books_and_flags_depleted(self) -> None:
        client = FakeGrocy(stock={"1": 1.0, "2": 2.0})
        api = self._api(client, consumption_estimator=lambda **k: {})
        out = api.recipe_cook_commit(
            "Bowl",
            [
                {"kind": "consume", "name": "Joghurt", "matched_product_id": "1", "amount": 1},
                {"kind": "consume", "name": "Zucchini", "matched_product_id": "2", "amount": 1},
            ],
        )
        self.assertEqual(out["consumed"], 2)
        lines = {l["name"]: l for l in out["lines"]}
        self.assertTrue(lines["Joghurt"]["depleted"])      # 1 -> 0
        self.assertFalse(lines["Zucchini"]["depleted"])    # 2 -> 1
        self.assertEqual(client.stock["1"], 0.0)
        self.assertEqual(client.stock["2"], 1.0)
        # session + anti-repeat persisted
        self.assertTrue(out["session_id"])
        self.assertIn("Bowl", cookmemory.recent_cooked(self.store))

    def test_commit_bought_adds_pack_then_deducts_leaving_leftover(self) -> None:
        client = FakeGrocy()
        api = self._api(client, consumption_estimator=lambda **k: {})
        out = api.recipe_cook_commit(
            "Pasta",
            [{"kind": "bought", "name": "Sahne", "pack_amount": 1, "used_amount": 0.5, "unit": "Becher"}],
        )
        self.assertEqual(out["added"], 1)
        self.assertEqual(out["consumed"], 1)
        line = out["lines"][0]
        pid = line["product_id"]
        self.assertAlmostEqual(client.stock[pid], 0.5)  # leftover stays
        self.assertFalse(line["depleted"])

    def test_commit_reuses_existing_product_by_name(self) -> None:
        client = FakeGrocy(products=[{"id": "9", "name": "Sahne"}], stock={"9": 0})
        api = self._api(client, consumption_estimator=lambda **k: {})
        out = api.recipe_cook_commit(
            "Pasta",
            [{"kind": "bought", "name": "Sahne", "pack_amount": 1, "used_amount": 1}],
        )
        self.assertEqual(out["lines"][0]["product_id"], "9")
        self.assertNotIn("create", [c[0] for c in client.calls])

    def test_commit_isolates_failures(self) -> None:
        client = FakeGrocy(stock={"1": 0.0, "2": 2.0})  # product 1 already empty

        # consuming from empty stock clamps to 0 -> no failure; force a real
        # failure with a missing product id instead.
        api = self._api(client, consumption_estimator=lambda **k: {})
        out = api.recipe_cook_commit(
            "Mix",
            [
                {"kind": "consume", "name": "Geist", "matched_product_id": "", "amount": 1},
                {"kind": "consume", "name": "Zucchini", "matched_product_id": "2", "amount": 1},
            ],
        )
        self.assertEqual(out["consumed"], 1)
        self.assertEqual(len(out["failed"]), 1)
        self.assertEqual(out["failed"][0]["name"], "Geist")

    def test_commit_requires_items(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self._api(FakeGrocy()).recipe_cook_commit("Pasta", [])
        self.assertEqual(ctx.exception.status, 400)

    # --- history + adjust ---

    def test_history_newest_first(self) -> None:
        client = FakeGrocy(stock={"2": 5.0})
        api = self._api(client, consumption_estimator=lambda **k: {})
        api.recipe_cook_commit("Erst", [{"kind": "consume", "name": "Z", "matched_product_id": "2", "amount": 1}])
        api.recipe_cook_commit("Zweit", [{"kind": "consume", "name": "Z", "matched_product_id": "2", "amount": 1}])
        hist = api.cook_history()["sessions"]
        self.assertEqual(hist[0]["dish"], "Zweit")
        self.assertEqual(hist[1]["dish"], "Erst")

    def test_adjust_undoes_and_reconsumes(self) -> None:
        client = FakeGrocy(stock={"2": 3.0})
        api = self._api(client, consumption_estimator=lambda **k: {})
        out = api.recipe_cook_commit(
            "Pasta", [{"kind": "consume", "name": "Zucchini", "matched_product_id": "2", "amount": 1}]
        )
        self.assertEqual(client.stock["2"], 2.0)  # 3 - 1
        res = api.cook_adjust(out["session_id"], 0, 2.0)
        # undo restores to 3, then consume 2 -> 1
        self.assertEqual(client.stock["2"], 1.0)
        self.assertEqual(res["amount"], 2.0)
        self.assertFalse(res["depleted"])
        line = cookmemory.sessions(self.store)[0]["lines"][0]
        self.assertEqual(line["amount"], 2.0)

    def test_adjust_to_zero_only_undoes(self) -> None:
        client = FakeGrocy(stock={"2": 3.0})
        api = self._api(client, consumption_estimator=lambda **k: {})
        out = api.recipe_cook_commit(
            "Pasta", [{"kind": "consume", "name": "Zucchini", "matched_product_id": "2", "amount": 1}]
        )
        api.cook_adjust(out["session_id"], 0, 0.0)
        self.assertEqual(client.stock["2"], 3.0)  # fully restored
        line = cookmemory.sessions(self.store)[0]["lines"][0]
        self.assertEqual(line["amount"], 0.0)
        self.assertIsNone(line["transaction_id"])

    def test_adjust_unknown_session_404(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self._api(FakeGrocy()).cook_adjust("nope", 0, 1.0)
        self.assertEqual(ctx.exception.status, 404)


class CookHttpRoutesTest(unittest.TestCase):
    """Real socket round-trip to catch cook route wiring (param names)."""

    def setUp(self) -> None:
        import json
        import threading
        from http.server import ThreadingHTTPServer
        from foodbrain_assistant.server import make_handler

        self._json = json
        self._dir = tempfile.TemporaryDirectory()
        store = Path(self._dir.name) / "cookmemory.json"
        self.client = FakeGrocy(products=[{"id": "2", "name": "Zucchini"}], stock={"2": 3.0})
        api = FoodBrainAPI(
            settings=_settings(),
            stock_provider=_stock,
            today_provider=lambda: TODAY,
            cook_store_path=store,
            write_client_factory=lambda: self.client,
            product_catalog_provider=self.client.get_products,
            consumption_estimator=lambda **k: {
                "used": [{"name": "Zucchini", "amount": 1, "unit": "Stück"}], "bought": []},
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

    def _post(self, path, body):
        from urllib.request import Request, urlopen
        req = Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=self._json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req) as r:
            return r.status, self._json.loads(r.read().decode("utf-8"))

    def _get(self, path):
        from urllib.request import urlopen
        with urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
            return r.status, self._json.loads(r.read().decode("utf-8"))

    def test_full_cook_flow_over_http(self) -> None:
        status, est = self._post("/api/recipes/cook-estimate",
                                 {"dish": "Pasta", "guidance": ["a"], "mode": "stock"})
        self.assertEqual(status, 200)
        self.assertEqual(est["items"][0]["matched_product_id"], "2")

        status, commit = self._post("/api/recipes/cook-commit",
                                    {"dish": "Pasta", "items": est["items"]})
        self.assertEqual(status, 200)
        self.assertEqual(commit["consumed"], 1)
        sid = commit["session_id"]

        status, hist = self._get("/api/recipes/cook-history")
        self.assertEqual(status, 200)
        self.assertEqual(hist["sessions"][0]["dish"], "Pasta")

        status, adj = self._post("/api/recipes/cook-adjust",
                                 {"session_id": sid, "line_index": 0, "new_amount": 2})
        self.assertEqual(status, 200)
        self.assertEqual(adj["amount"], 2.0)


if __name__ == "__main__":
    unittest.main()
