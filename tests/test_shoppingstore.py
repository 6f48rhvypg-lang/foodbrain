import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from foodbrain_assistant import shoppingstore


class ShoppingStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "sub" / "shopping.json"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_missing_file_returns_skeleton(self) -> None:
        data = shoppingstore.load(self.path)
        self.assertEqual(
            data,
            {"overlay": {}, "habits": {}, "diet_focus": {"chips": [], "freetext": "", "updated_ts": None}},
        )

    def test_atomic_write_leaves_no_tmp(self) -> None:
        shoppingstore.set_overlay(self.path, "1", source="manual")
        self.assertTrue(self.path.exists())
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_corrupt_file_is_backed_up_not_overwritten(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        data = shoppingstore.load(self.path)
        self.assertEqual(data, shoppingstore._skeleton())
        backups = list(self.path.parent.glob("*.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertFalse(self.path.exists())

    def test_partial_file_is_normalized(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"overlay": {"5": {"source": "manual"}}}), encoding="utf-8")
        data = shoppingstore.load(self.path)
        self.assertEqual(data["overlay"]["5"]["source"], "manual")
        self.assertEqual(data["overlay"]["5"]["reason"], "")
        self.assertEqual(data["habits"], {})

    # --- overlay ---

    def test_overlay_set_get_remove_round_trip(self) -> None:
        entry = shoppingstore.set_overlay(self.path, "42", source="low_qty", reason="nur noch 2 übrig")
        self.assertEqual(entry["source"], "low_qty")
        self.assertEqual(shoppingstore.get_overlay(self.path), {"42": entry})
        shoppingstore.remove_overlay(self.path, "42")
        self.assertEqual(shoppingstore.get_overlay(self.path), {})

    def test_prune_overlay_drops_stale_entries(self) -> None:
        shoppingstore.set_overlay(self.path, "1", source="manual")
        shoppingstore.set_overlay(self.path, "2", source="depleted")
        kept = shoppingstore.prune_overlay(self.path, valid_ids=["2"])
        self.assertEqual(set(kept), {"2"})
        self.assertEqual(set(shoppingstore.get_overlay(self.path)), {"2"})

    def test_prune_overlay_noop_when_nothing_stale(self) -> None:
        shoppingstore.set_overlay(self.path, "1", source="manual")
        shoppingstore.prune_overlay(self.path, valid_ids=["1", "2"])
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.assertEqual(set(shoppingstore.get_overlay(self.path)), {"1"})

    # --- habits: buys / removals ---

    def test_record_buy_and_removal_keyed_lowercase(self) -> None:
        shoppingstore.record_buy(self.path, name="Milch", product_id="9", amount=1.0)
        shoppingstore.record_removal(self.path, name="MILCH", product_id="9")
        habit = shoppingstore.get_habit(self.path, "milch")
        self.assertEqual(habit["product_id"], "9")
        self.assertEqual(len(habit["buys"]), 1)
        self.assertEqual(len(habit["removals"]), 1)
        self.assertEqual(habit["buys"][0]["amount"], 1.0)

    def test_events_capped_at_max(self) -> None:
        for i in range(shoppingstore._MAX_EVENTS + 10):
            shoppingstore.record_buy(self.path, name="Eier", amount=1.0)
        habit = shoppingstore.get_habit(self.path, "eier")
        self.assertEqual(len(habit["buys"]), shoppingstore._MAX_EVENTS)

    def test_unknown_habit_returns_none(self) -> None:
        self.assertIsNone(shoppingstore.get_habit(self.path, "nichts"))

    def test_habits_returns_all_keyed_lower(self) -> None:
        shoppingstore.record_buy(self.path, name="Joghurt", amount=1)
        shoppingstore.record_buy(self.path, name="Butter", amount=1)
        self.assertEqual(set(shoppingstore.habits(self.path)), {"joghurt", "butter"})

    def test_concurrent_record_buy_all_land(self) -> None:
        import threading

        names = [f"Produkt {i}" for i in range(20)]
        barrier = threading.Barrier(len(names))

        def worker(name: str) -> None:
            barrier.wait()
            shoppingstore.record_buy(self.path, name=name, amount=1)

        threads = [threading.Thread(target=worker, args=(n,)) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stored = set(shoppingstore.habits(self.path))
        self.assertEqual(stored, {n.lower() for n in names})

    # --- mode ---

    def test_set_mode_valid_values(self) -> None:
        for mode in ("auto", "suggest", "off"):
            habit = shoppingstore.set_mode(self.path, name="Kaffee", mode=mode)
            self.assertEqual(habit["mode"], mode)
        habit = shoppingstore.set_mode(self.path, name="Kaffee", mode=None)
        self.assertIsNone(habit["mode"])

    def test_set_mode_rejects_unknown_value(self) -> None:
        with self.assertRaises(ValueError):
            shoppingstore.set_mode(self.path, name="Kaffee", mode="bogus")

    def test_set_mode_requires_name(self) -> None:
        with self.assertRaises(ValueError):
            shoppingstore.set_mode(self.path, name="", mode="auto")

    def test_set_mode_preserves_existing_buys(self) -> None:
        shoppingstore.record_buy(self.path, name="Reis", amount=1)
        shoppingstore.set_mode(self.path, name="Reis", mode="auto")
        habit = shoppingstore.get_habit(self.path, "reis")
        self.assertEqual(len(habit["buys"]), 1)
        self.assertEqual(habit["mode"], "auto")

    # --- habit_stats (pure) ---

    def test_habit_stats_none_habit_degrades(self) -> None:
        stats = shoppingstore.habit_stats(None)
        self.assertIsNone(stats["typical_amount"])
        self.assertIsNone(stats["median_interval_days"])
        self.assertIsNone(stats["last_buy_ts"])
        self.assertFalse(stats["is_staple"])

    def test_habit_stats_typical_amount_is_median(self) -> None:
        habit = {"buys": [{"amount": 1, "ts": None}, {"amount": 3, "ts": None}, {"amount": 2, "ts": None}]}
        stats = shoppingstore.habit_stats(habit)
        self.assertEqual(stats["typical_amount"], 2)

    def test_habit_stats_median_interval_days(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        buys = [
            {"amount": 1, "ts": (base + timedelta(days=0)).isoformat()},
            {"amount": 1, "ts": (base + timedelta(days=10)).isoformat()},
            {"amount": 1, "ts": (base + timedelta(days=16)).isoformat()},
        ]
        stats = shoppingstore.habit_stats({"buys": buys})
        # gaps: 10, 6 -> median 8
        self.assertAlmostEqual(stats["median_interval_days"], 8.0)
        self.assertIsNotNone(stats["last_buy_ts"])

    def test_habit_stats_is_staple_by_buy_count_or_auto_mode(self) -> None:
        many_buys = {"buys": [{"amount": 1, "ts": None}] * 3}
        self.assertTrue(shoppingstore.habit_stats(many_buys)["is_staple"])
        pinned = {"buys": [], "mode": "auto"}
        self.assertTrue(shoppingstore.habit_stats(pinned)["is_staple"])
        neither = {"buys": [{"amount": 1, "ts": None}]}
        self.assertFalse(shoppingstore.habit_stats(neither)["is_staple"])

    def test_habits_normalize_on_partial_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"habits": {"milch": {"mode": "bogus"}}}), encoding="utf-8")
        habit = shoppingstore.load(self.path)["habits"]["milch"]
        self.assertIsNone(habit["mode"])  # invalid mode dropped, not crashed
        self.assertEqual(habit["buys"], [])

    # --- diet focus (sticky) ---

    def test_diet_focus_defaults_empty(self) -> None:
        focus = shoppingstore.get_diet_focus(self.path)
        self.assertEqual(focus, {"chips": [], "freetext": "", "updated_ts": None})

    def test_diet_focus_set_then_get_round_trips(self) -> None:
        written = shoppingstore.set_diet_focus(
            self.path, chips=["Proteinreich", "Mehr Gemüse"], freetext="weniger Zucker"
        )
        self.assertIsNotNone(written["updated_ts"])
        read = shoppingstore.get_diet_focus(self.path)
        self.assertEqual(read["chips"], ["Proteinreich", "Mehr Gemüse"])
        self.assertEqual(read["freetext"], "weniger Zucker")
        self.assertEqual(read["updated_ts"], written["updated_ts"])

    def test_diet_focus_set_drops_blank_chips(self) -> None:
        written = shoppingstore.set_diet_focus(self.path, chips=["", "  ", "Low-Carb"], freetext="")
        self.assertEqual(written["chips"], ["Low-Carb"])

    def test_diet_focus_set_replaces_not_merges(self) -> None:
        shoppingstore.set_diet_focus(self.path, chips=["Proteinreich"], freetext="alt")
        second = shoppingstore.set_diet_focus(self.path, chips=["Low-Carb"], freetext="neu")
        self.assertEqual(second["chips"], ["Low-Carb"])
        self.assertEqual(second["freetext"], "neu")

    def test_partial_file_normalizes_bogus_diet_focus(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"diet_focus": {"chips": "not-a-list"}}), encoding="utf-8")
        focus = shoppingstore.load(self.path)["diet_focus"]
        self.assertEqual(focus["chips"], [])
        self.assertEqual(focus["freetext"], "")


if __name__ == "__main__":
    unittest.main()
