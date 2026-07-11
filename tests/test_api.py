import json
import threading
import unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from foodbrain_assistant.api import ApiError, FoodBrainAPI, band_for
from foodbrain_assistant.config import Settings
from foodbrain_assistant.grocy_client import GrocyClientError
from foodbrain_assistant.intake import IntakeResult
from foodbrain_assistant.models import Recipe, RecipeIngredient, StockEntry, StockItem
from foodbrain_assistant.pairing import load_pairings
from foodbrain_assistant.server import make_handler


TODAY = date(2026, 6, 4)


def _settings(**overrides) -> Settings:
    base = dict(
        grocy_base_url=None,
        grocy_api_key=None,
        expiry_window_days=7,
        top_ingredient_limit=8,
        top_recipe_limit=5,
        top_pairing_limit=5,
        pairing_partner_limit=4,
    )
    base.update(overrides)
    return Settings(**base)


def _stock() -> list[StockItem]:
    return [
        StockItem("1", "Joghurt", 1, "Becher", TODAY - timedelta(days=1)),
        StockItem("2", "Zucchini", 2, "Stück", TODAY + timedelta(days=3)),
        StockItem("3", "Feta", 1, "Pack", TODAY + timedelta(days=5)),
        StockItem("4", "Karotten", 4, "Stück", TODAY + timedelta(days=10)),
        StockItem("5", "Olivenöl", 1, "Flasche", None),
        StockItem("6", "Empty", 0, "Pack", TODAY),  # filtered out (amount 0)
    ]


def _api(**overrides) -> FoodBrainAPI:
    params = dict(
        settings=_settings(),
        stock_provider=_stock,
        today_provider=lambda: TODAY,
        source="sample",
    )
    params.update(overrides)
    return FoodBrainAPI(**params)


class BandTest(unittest.TestCase):
    def test_band_thresholds(self) -> None:
        self.assertEqual(band_for(None, 7), "staple")
        self.assertEqual(band_for(-1, 7), "hot")
        self.assertEqual(band_for(0, 7), "hot")
        self.assertEqual(band_for(3, 7), "warm")
        self.assertEqual(band_for(7, 7), "warm")
        self.assertEqual(band_for(8, 7), "cool")


class StockWithScoresTest(unittest.TestCase):
    def test_scores_and_bands(self) -> None:
        payload = _api().stock_with_scores()
        self.assertEqual(payload["source"], "sample")
        self.assertEqual(payload["as_of"], "2026-06-04")

        # Empty (amount 0) item is filtered out.
        self.assertEqual(payload["summary"]["total"], 5)

        bands = {row["name"]: row["band"] for row in payload["items"]}
        self.assertEqual(bands["Joghurt"], "hot")
        self.assertEqual(bands["Zucchini"], "warm")
        self.assertEqual(bands["Karotten"], "cool")
        self.assertEqual(bands["Olivenöl"], "staple")

        # Sorted most-urgent first.
        self.assertEqual(payload["items"][0]["name"], "Joghurt")
        self.assertEqual(payload["summary"]["band_counts"]["hot"], 1)
        self.assertEqual(payload["summary"]["urgent"], 3)  # hot + warm (Joghurt, Zucchini, Feta)

    def test_serializable(self) -> None:
        json.dumps(_api().stock_with_scores())  # must not raise


class ConnectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pairings = load_pairings(
            {
                "pairs": [
                    {"a": "zucchini", "b": "feta", "score": 0.9},
                    {"a": "zucchini", "b": "garlic", "score": 0.7},
                ]
            }
        )
        self.recipes = [
            Recipe("Zucchini Bake", [
                RecipeIngredient("2 zucchini", "zucchini"),
                RecipeIngredient("100g feta", "feta"),
            ]),
            Recipe("Carrot Soup", [RecipeIngredient("4 carrots", "carrots")]),
        ]

    def test_connect_returns_pairings_and_unlocked_recipes(self) -> None:
        api = _api(pairings=self.pairings, recipes=self.recipes)
        result = api.connect(["2", "3"])  # Zucchini, Feta
        self.assertEqual(result["selection"], ["Zucchini", "Feta"])

        ingredients = {p["ingredient"] for p in result["pairings"]}
        self.assertIn("Zucchini", ingredients)
        zucchini = next(p for p in result["pairings"] if p["ingredient"] == "Zucchini")
        partner_names = {pp["name"] for pp in zucchini["partners"]}
        self.assertIn("feta", partner_names)
        # Feta is in stock, so it is flagged.
        feta_partner = next(pp for pp in zucchini["partners"] if pp["name"] == "feta")
        self.assertTrue(feta_partner["in_stock"])

        # Only the recipe that uses the selection is returned.
        recipe_names = {r["name"] for r in result["recipes"]}
        self.assertEqual(recipe_names, {"Zucchini Bake"})

    def test_connect_empty_selection_400(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            _api().connect(["999"])
        self.assertEqual(ctx.exception.status, 400)

    def test_connect_without_engines_is_empty(self) -> None:
        result = _api().connect(["2"])
        self.assertEqual(result["pairings"], [])
        self.assertEqual(result["recipes"], [])


class BuildPromptTest(unittest.TestCase):
    def test_prompt_lists_selection(self) -> None:
        result = _api().build_prompt(["2", "3"])
        self.assertEqual(result["selection"], ["Zucchini", "Feta"])
        self.assertIn("2 Stück Zucchini", result["prompt"])
        self.assertIn("1 Pack Feta", result["prompt"])
        # German by default; no mood -> 3 dishes, no cuisine word.
        self.assertIn("Schlag mir 3 Gerichte vor", result["prompt"])

    def test_prompt_weaves_in_mood(self) -> None:
        result = _api().build_prompt(
            ["2"],
            {"cuisine": "asiatisch", "style": "schnell", "needs": ["scharf", "warm"]},
        )
        prompt = result["prompt"]
        self.assertIn("asiatische Gerichte", prompt)
        self.assertIn("30 Minuten", prompt)
        self.assertIn("scharf gewürzt und warm", prompt)

    def test_prompt_egal_widens_variety(self) -> None:
        result = _api().build_prompt(["2"], {"cuisine": "egal"})
        self.assertIn("Schlag mir 8 Gerichte vor", result["prompt"])
        self.assertIn("quer durch verschiedene Küchen", result["prompt"])

    def test_prompt_empty_selection_400(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            _api().build_prompt([])
        self.assertEqual(ctx.exception.status, 400)


class _FakeClient:
    """Records writeback calls so the API write proxies can be tested in isolation.

    Models live stock per product so the consume/toss clamp is exercised: like
    real Grocy, ``consume_product`` rejects an amount greater than what's in
    stock. ``stock`` seeds product_id -> current amount (default 5 if unseeded).
    """

    def __init__(self, stock=None) -> None:
        self.calls = []
        self.stock = dict(stock or {})

    def _amount_for(self, product_id):
        return self.stock.get(str(product_id), 5.0)

    def get_product_entries(self, product_id):
        amount = self._amount_for(product_id)
        if amount <= 0:
            return []
        return [StockEntry("e-" + str(product_id), str(product_id), amount, None)]

    def consume_product(self, product_id, amount=1.0, *, spoiled=False):
        if amount > self._amount_for(product_id):
            # Mirror Grocy: consuming more than current stock is an HTTP 400.
            raise GrocyClientError("Grocy request failed with HTTP 400")
        self.calls.append(("consume", product_id, amount, spoiled))
        return [{"transaction_id": "tx-1"}]

    def set_entry_due_date(self, stock_entry_id, best_before_date):
        self.calls.append(("set_due", stock_entry_id, best_before_date))
        return {}

    def undo_transaction(self, transaction_id):
        self.calls.append(("undo", transaction_id))
        return None


class WriteProxyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _FakeClient()
        self.api = _api(write_client_factory=lambda: self.client)

    def test_consume_returns_undoable_outcome(self) -> None:
        out = self.api.consume("2", 1.0)
        self.assertEqual(out["action"], "consume")
        self.assertTrue(out["undoable"])
        self.assertEqual(out["transaction_id"], "tx-1")
        self.assertEqual(self.client.calls[0], ("consume", "2", 1.0, False))

    def test_toss_without_confirm_409(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self.api.toss("2", 1.0, confirm=False)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.client.calls, [])  # nothing was sent

    def test_toss_with_confirm_marks_spoiled(self) -> None:
        out = self.api.toss("2", 1.0, confirm=True)
        self.assertEqual(out["action"], "toss")
        self.assertEqual(self.client.calls[0], ("consume", "2", 1.0, True))

    def test_set_due_date_parses_iso(self) -> None:
        out = self.api.set_due_date("7", "2026-07-01", product_id="2")
        self.assertEqual(out["action"], "set_due_date")
        self.assertEqual(self.client.calls[0], ("set_due", "7", date(2026, 7, 1)))

    def test_set_due_date_bad_date_400(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self.api.set_due_date("7", "nope")
        self.assertEqual(ctx.exception.status, 400)

    def test_undo(self) -> None:
        out = self.api.undo("tx-1")
        self.assertTrue(out["ok"])
        self.assertEqual(self.client.calls[0], ("undo", "tx-1"))

    def test_writes_disabled_when_no_factory_403(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            _api().consume("2")
        self.assertEqual(ctx.exception.status, 403)

    # --- regression: stale cached amount must not break delete -----------
    # The UI books an item's amount cached at page load. When that exceeds the
    # real stock (stale page / fractional drift) Grocy rejects it with HTTP 400.
    # The server clamps to live stock so removing the whole item always works.

    def test_consume_clamps_to_live_stock(self) -> None:
        client = _FakeClient(stock={"9": 0.5})
        api = _api(write_client_factory=lambda: client)
        out = api.consume("9", 1.0)  # stale cached amount, higher than stock
        self.assertEqual(out["action"], "consume")
        self.assertEqual(out["amount"], 0.5)  # booked live stock, not 1.0
        self.assertEqual(client.calls[0], ("consume", "9", 0.5, False))

    def test_toss_clamps_to_live_stock(self) -> None:
        client = _FakeClient(stock={"9": 0.5})
        api = _api(write_client_factory=lambda: client)
        out = api.toss("9", 1.0, confirm=True)
        self.assertEqual(out["amount"], 0.5)
        self.assertEqual(client.calls[0], ("consume", "9", 0.5, True))

    def test_consume_already_empty_is_noop(self) -> None:
        client = _FakeClient(stock={"9": 0})
        api = _api(write_client_factory=lambda: client)
        out = api.consume("9", 1.0)
        self.assertEqual(out["amount"], 0.0)
        self.assertFalse(out["undoable"])  # nothing booked, nothing to undo
        self.assertEqual(client.calls, [])  # Grocy not called


class ProductEntriesTest(unittest.TestCase):
    def test_entries_from_reader(self) -> None:
        class Reader:
            def get_product_entries(self, product_id):
                return [StockEntry("7", product_id, 1.5, date(2026, 6, 10))]

        api = _api(write_client_factory=lambda: Reader())
        result = api.product_entries("2")
        self.assertEqual(result["entries"][0]["stock_entry_id"], "7")
        self.assertEqual(result["entries"][0]["best_before_date"], "2026-06-10")


class NextDueEntryTest(unittest.TestCase):
    """Grocy returns entries opened-first, not earliest-first; the re-date flow
    must target the entry whose date the stock tile shows (the earliest)."""

    def test_picks_earliest_not_first(self) -> None:
        from foodbrain_assistant.api import _next_due_entry

        # Mirrors live ginger: an opened later-dated entry is returned first,
        # while the tile shows the earlier unopened one.
        entries = [
            StockEntry("78", "17", 100, date(2026, 7, 18), opened=True),
            StockEntry("124", "17", 1, date(2026, 7, 3)),
            StockEntry("316", "17", 100, date(2026, 7, 18)),
        ]
        self.assertEqual(_next_due_entry(entries).stock_entry_id, "124")

    def test_dateless_entries_sort_last(self) -> None:
        from foodbrain_assistant.api import _next_due_entry

        entries = [
            StockEntry("a", "1", 1, None),
            StockEntry("b", "1", 1, date(2026, 8, 1)),
        ]
        self.assertEqual(_next_due_entry(entries).stock_entry_id, "b")


class HttpSmokeTest(unittest.TestCase):
    """A real socket round-trip over the http.server handler."""

    def setUp(self) -> None:
        self.client = _FakeClient()
        # A stub understander so the voice-intake endpoint can be smoke-tested
        # without a live LLM. It echoes a summary and produces no items.
        self.understood = []

        def understander(*, transcript, catalog, answers, mode):
            self.understood.append((transcript, answers, mode))
            return IntakeResult(items=[], questions=[], summary=f"verstanden: {transcript}")

        api = _api(
            write_client_factory=lambda: self.client,
            intake_understander=understander,
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

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

    def test_health(self) -> None:
        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_stock_endpoint(self) -> None:
        status, body = self._get("/api/stock")
        self.assertEqual(status, 200)
        self.assertEqual(body["summary"]["total"], 5)

    def test_connect_endpoint(self) -> None:
        status, body = self._post("/api/connect", {"selection": ["2", "3"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["selection"], ["Zucchini", "Feta"])

    def test_toss_without_confirm_returns_409(self) -> None:
        with self.assertRaises(HTTPError) as ctx:
            self._post("/api/toss", {"product_id": "2"})
        self.assertEqual(ctx.exception.code, 409)

    def test_consume_endpoint(self) -> None:
        # ✓ Verbraucht button.
        status, body = self._post("/api/consume", {"product_id": "2", "amount": 1})
        self.assertEqual(status, 200)
        self.assertEqual(body["action"], "consume")

    def test_consume_stale_amount_still_succeeds(self) -> None:
        # ✓ Verbraucht with a cached amount higher than live stock (the bug):
        # the server clamps to stock (5.0 here) instead of returning HTTP 400.
        status, body = self._post("/api/consume", {"product_id": "2", "amount": 999})
        self.assertEqual(status, 200)
        self.assertEqual(body["amount"], 5.0)

    def test_toss_with_confirm_endpoint(self) -> None:
        # 🗑 Wegwerfen button (confirmed).
        status, body = self._post(
            "/api/toss", {"product_id": "2", "amount": 1, "confirm": True}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["action"], "toss")
        self.assertTrue(body["undoable"])

    def test_undo_endpoint(self) -> None:
        # Snackbar "Rückgängig" button.
        status, body = self._post("/api/undo", {"transaction_id": "tx-1"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self.client.calls[-1], ("undo", "tx-1"))

    def test_product_entries_endpoint(self) -> None:
        # Edit-date popover first reads the product's stock entries.
        status, body = self._get("/api/product-entries?product_id=2")
        self.assertEqual(status, 200)
        self.assertTrue(body["entries"])  # _FakeClient returns one entry

    def test_set_due_date_endpoint(self) -> None:
        # Edit-date chips / date picker.
        status, body = self._post(
            "/api/set-due-date",
            {"stock_entry_id": "e-2", "best_before_date": "2026-07-01", "product_id": "2"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["action"], "set_due_date")

    def test_build_prompt_endpoint(self) -> None:
        # Ask-AI prompt builder.
        status, body = self._post("/api/build-prompt", {"selection": ["2", "3"]})
        self.assertEqual(status, 200)
        self.assertIn("Zucchini", body["prompt"])

    def test_intake_understand_endpoint(self) -> None:
        # Voice intake — "verstehen" (speech -> reviewable items).
        status, body = self._post(
            "/api/intake/understand", {"transcript": "zwei Zucchini", "mode": "add"}
        )
        self.assertEqual(status, 200)
        self.assertIn("verstanden", body["summary"])
        self.assertEqual(self.understood[-1], ("zwei Zucchini", "", "add"))

    def test_intake_commit_consume_endpoint(self) -> None:
        # Voice intake — commit an edit (consume) back to Grocy.
        status, body = self._post(
            "/api/intake/commit",
            {"items": [{"name": "Zucchini", "product_id": "2", "action": "consume", "amount": 1}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["changed"], 1)
        self.assertEqual(body["results"][0]["action"], "consume")

    def test_unknown_route_404(self) -> None:
        with self.assertRaises(HTTPError) as ctx:
            self._get("/api/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_ui_routes_404_without_bundle(self) -> None:
        # This server was built without a UI bundle, so / and /ui are 404.
        for path in ("/", "/ui"):
            with self.assertRaises(HTTPError) as ctx:
                self._get(path)
            self.assertEqual(ctx.exception.code, 404)


class UiServingTest(unittest.TestCase):
    """The SPA bundle is served at / and /ui when make_handler gets ui_html."""

    UI = b"<!doctype html><title>FoodBrain SPA</title>"

    def setUp(self) -> None:
        api = _api(write_client_factory=lambda: _FakeClient())
        self.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(api, self.UI)
        )
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def _fetch(self, path: str):
        with urlopen(f"http://127.0.0.1:{self.port}{path}") as response:
            return response.status, response.headers.get("Content-Type"), response.read()

    def test_root_serves_spa(self) -> None:
        status, ctype, body = self._fetch("/")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "text/html; charset=utf-8")
        self.assertEqual(body, self.UI)

    def test_ui_serves_spa(self) -> None:
        status, _ctype, body = self._fetch("/ui")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.UI)

    def test_api_still_works_with_ui(self) -> None:
        status, _ctype, body = self._fetch("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body.decode("utf-8"))["ok"])

    def test_ui_is_no_cache(self) -> None:
        # The SPA must not be cached, so a deploy shows up on the next load
        # without a ?v=N bust or clearing Website Data on the phone.
        with urlopen(f"http://127.0.0.1:{self.port}/ui") as response:
            self.assertIn("no-cache", response.headers.get("Cache-Control", ""))


if __name__ == "__main__":
    unittest.main()
