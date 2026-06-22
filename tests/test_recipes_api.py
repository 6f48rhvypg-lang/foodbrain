import json
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from foodbrain_assistant import cookmemory
from foodbrain_assistant.api import ApiError, FoodBrainAPI
from foodbrain_assistant.config import Settings
from foodbrain_assistant.models import StockItem
from foodbrain_assistant.server import make_handler


TODAY = date(2026, 6, 4)


def _settings(**overrides) -> Settings:
    base = dict(
        grocy_base_url=None,
        grocy_api_key=None,
        openrouter_api_key="test-key",
    )
    base.update(overrides)
    return Settings(**base)


def _stock() -> list[StockItem]:
    return [
        StockItem("1", "Joghurt", 1, "Becher", TODAY - timedelta(days=1)),  # hot
        StockItem("2", "Zucchini", 2, "Stück", TODAY + timedelta(days=3)),  # warm
        StockItem("3", "Karotten", 4, "Stück", TODAY + timedelta(days=20)),  # cool
        StockItem("4", "Olivenöl", 1, "Flasche", None),  # staple
    ]


class RecipeApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.store = Path(self._dir.name) / "cookmemory.json"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _api(self, **overrides) -> FoodBrainAPI:
        params = dict(
            settings=_settings(),
            stock_provider=_stock,
            today_provider=lambda: TODAY,
            cook_store_path=self.store,
            source="test",
        )
        params.update(overrides)
        return FoodBrainAPI(**params)

    def test_ideas_passes_urgent_seeds_and_supporting_inventory(self) -> None:
        seen = {}

        def gen(**kwargs):
            seen.update(kwargs)
            return {"ideas": [{"title": "Joghurt-Bowl", "uses": "Joghurt", "buy": []}]}

        out = self._api(idea_generator=gen).recipe_ideas(mode="stock")
        # hot+warm become seeds; cool/staple are supporting inventory.
        self.assertIn("Joghurt", seen["seeds"])
        self.assertIn("Zucchini", seen["seeds"])
        self.assertIn("Karotten", seen["inventory"])
        self.assertIn("Olivenöl", seen["inventory"])
        self.assertEqual(out["ideas"][0]["title"], "Joghurt-Bowl")
        self.assertEqual(out["seeds"], seen["seeds"])

    def test_ideas_recent_cooked_passed_for_avoidance(self) -> None:
        cookmemory.add_cooked(self.store, dish="Joghurt-Bowl")
        seen = {}

        def gen(**kwargs):
            seen.update(kwargs)
            return {"ideas": []}

        self._api(idea_generator=gen).recipe_ideas()
        self.assertIn("Joghurt-Bowl", seen["recent_cooked"])

    def test_ideas_rejects_unknown_model(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self._api(idea_generator=lambda **k: {"ideas": []}).recipe_ideas(
                idea_model="evil/model"
            )
        self.assertEqual(ctx.exception.status, 400)

    def test_ideas_requires_api_key_without_injected_generator(self) -> None:
        api = self._api(settings=_settings(openrouter_api_key=None))
        with self.assertRaises(ApiError) as ctx:
            api.recipe_ideas()
        self.assertEqual(ctx.exception.status, 503)

    def test_detail_returns_guidance(self) -> None:
        def gen(**kwargs):
            return {"title": "X", "time": "20 Min", "guidance": ["Phase 1", "Phase 2"], "buy": []}

        out = self._api(recipe_generator=gen).recipe_detail({"title": "X"}, mode="stock")
        self.assertEqual(out["guidance"], ["Phase 1", "Phase 2"])

    def test_detail_requires_title(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self._api(recipe_generator=lambda **k: {}).recipe_detail({}, mode="stock")
        self.assertEqual(ctx.exception.status, 400)

    def test_twist_persists_and_merges_taste(self) -> None:
        def gen(**kwargs):
            return {"change": "mehr Chili", "note": "", "tags": {"likes": ["Chili"], "dislikes": []}}

        out = self._api(twist_extractor=gen).recipe_twist("Pasta", text="mehr chili rein")
        self.assertTrue(out["ok"])
        self.assertIn("Chili", cookmemory.taste_summary(self.store)["likes"])
        self.assertEqual(len(cookmemory.load(self.store)["twists"]), 1)

    def test_revise_rewrites_recipe_and_updates_book_in_place(self) -> None:
        # Seed a saved recipe, then revise it: the book entry is replaced (same
        # id, new guidance), not duplicated, and taste tags are persisted.
        original = self._api().recipe_save("Pasta", ["alt phase"], buy=[])["recipe"]

        def reviser(**kwargs):
            return {"title": "Pasta", "time": "25 Min", "uses": "Sahne",
                    "guidance": ["neue phase"], "buy": []}

        def extract(**kwargs):
            return {"change": "Crème fraîche", "note": "",
                    "tags": {"likes": ["Crème fraîche"], "dislikes": []}}

        api = self._api(recipe_reviser=reviser, twist_extractor=extract)
        out = api.recipe_revise(
            {"title": "Pasta", "guidance": ["alt phase"], "buy": []},
            text="Crème fraîche statt Sahne", mode="stock",
        )
        self.assertEqual(out["recipe"]["guidance"], ["neue phase"])
        self.assertEqual(out["recipe"]["twist"], "Crème fraîche statt Sahne")
        book = api.recipe_book()["recipes"]
        self.assertEqual(len(book), 1)  # replaced, not duplicated
        self.assertEqual(book[0]["id"], original["id"])
        self.assertEqual(book[0]["guidance"], ["neue phase"])
        self.assertIn("Crème fraîche", cookmemory.taste_summary(self.store)["likes"])

    def test_revise_requires_recipe_title(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self._api().recipe_revise({}, text="x")
        self.assertEqual(ctx.exception.status, 400)

    def test_revise_requires_description(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self._api().recipe_revise({"title": "Pasta"}, text="  ")
        self.assertEqual(ctx.exception.status, 400)

    def test_cooked_logs_dish(self) -> None:
        self._api().recipe_cooked("Risotto")
        self.assertIn("Risotto", cookmemory.recent_cooked(self.store))

    def test_save_and_book_round_trip(self) -> None:
        api = self._api()
        api.recipe_save("Ofengemüse", ["Schneiden", "Backen"], buy=["Feta"])
        book = api.recipe_book()["recipes"]
        self.assertEqual(book[0]["title"], "Ofengemüse")


class RecipeHttpRoutesTest(unittest.TestCase):
    """Real socket round-trip to catch route-wiring bugs (param names etc.)."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        store = Path(self._dir.name) / "cookmemory.json"
        api = FoodBrainAPI(
            settings=_settings(),
            stock_provider=_stock,
            today_provider=lambda: TODAY,
            cook_store_path=store,
            idea_generator=lambda **k: {"ideas": [{"title": "T", "uses": "Joghurt", "buy": []}]},
            recipe_generator=lambda **k: {"title": "T", "time": "20 Min",
                                          "guidance": ["a", "b"], "buy": []},
            recipe_reviser=lambda **k: {"title": "T", "time": "25 Min",
                                        "guidance": ["c", "d"], "buy": []},
            twist_extractor=lambda **k: {"change": "mehr Chili", "note": "",
                                         "tags": {"likes": ["Chili"], "dislikes": []}},
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
        req = Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def _get(self, path):
        with urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def test_ideas_route(self) -> None:
        status, body = self._post("/api/recipes/ideas", {"mode": "stock"})
        self.assertEqual(status, 200)
        self.assertEqual(body["ideas"][0]["title"], "T")

    def test_recipe_route(self) -> None:
        status, body = self._post("/api/recipes/recipe", {"idea": {"title": "T"}, "mode": "stock"})
        self.assertEqual(status, 200)
        self.assertEqual(body["guidance"], ["a", "b"])

    def test_twist_then_book_flow(self) -> None:
        s, _ = self._post("/api/recipes/twist", {"dish": "Pasta", "text": "mehr chili"})
        self.assertEqual(s, 200)
        self._post("/api/recipes/save", {"title": "Pasta", "guidance": ["x"]})
        s, body = self._get("/api/recipes/book")
        self.assertEqual(body["recipes"][0]["title"], "Pasta")

    def test_revise_route(self) -> None:
        s, body = self._post(
            "/api/recipes/revise",
            {"recipe": {"title": "T", "guidance": ["a"]}, "text": "mehr chili", "mode": "stock"},
        )
        self.assertEqual(s, 200)
        self.assertEqual(body["recipe"]["guidance"], ["c", "d"])
        self.assertEqual(body["recipe"]["twist"], "mehr chili")

    def test_revise_route_rejects_non_object_recipe(self) -> None:
        req = Request(
            f"http://127.0.0.1:{self.port}/api/recipes/revise",
            data=json.dumps({"recipe": "nope", "text": "x"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(Exception) as ctx:
            urlopen(req)
        self.assertEqual(ctx.exception.code, 400)

    def test_config_route(self) -> None:
        status, body = self._get("/api/recipes/config")
        self.assertEqual(status, 200)
        self.assertTrue(body["models"])
        self.assertIn("idea_model", body)


if __name__ == "__main__":
    unittest.main()
