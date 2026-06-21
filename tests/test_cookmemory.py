import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from foodbrain_assistant import cookmemory


class CookMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "sub" / "cookmemory.json"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_missing_file_returns_skeleton(self) -> None:
        data = cookmemory.load(self.path)
        self.assertEqual(data["taste"], {"likes": [], "dislikes": [], "notes": ""})
        self.assertEqual(data["twists"], [])
        self.assertEqual(data["book"], [])

    def test_round_trip_creates_dir(self) -> None:
        cookmemory.add_cooked(self.path, dish="Linsensuppe")
        self.assertTrue(self.path.exists())
        data = cookmemory.load(self.path)
        self.assertEqual(data["cooked"][0]["dish"], "Linsensuppe")

    def test_atomic_write_leaves_no_tmp(self) -> None:
        cookmemory.add_cooked(self.path, dish="Curry")
        leftovers = list(self.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_corrupt_file_recovers_to_skeleton(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        data = cookmemory.load(self.path)
        self.assertEqual(data, cookmemory._skeleton())

    def test_partial_file_is_normalized(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"taste": {"likes": ["Chili"]}}), encoding="utf-8")
        data = cookmemory.load(self.path)
        self.assertEqual(data["taste"]["likes"], ["Chili"])
        self.assertEqual(data["taste"]["dislikes"], [])
        self.assertEqual(data["book"], [])

    def test_add_twist_merges_taste_tags_dedup(self) -> None:
        cookmemory.add_twist(
            self.path,
            dish="Pasta",
            change="mehr Knoblauch",
            tags={"likes": ["Knoblauch", "Chili"], "dislikes": ["Sahne"]},
        )
        cookmemory.add_twist(
            self.path,
            dish="Pasta",
            change="noch mehr Knoblauch",
            tags={"likes": ["knoblauch"]},  # dup, different case
        )
        taste = cookmemory.taste_summary(self.path)
        self.assertEqual(taste["likes"], ["Knoblauch", "Chili"])
        self.assertEqual(taste["dislikes"], ["Sahne"])
        self.assertEqual(len(cookmemory.load(self.path)["twists"]), 2)

    def test_add_to_book_returns_entry_with_id(self) -> None:
        entry = cookmemory.add_to_book(
            self.path, title="Ofengemüse", guidance=["Gemüse schneiden", "Backen"], buy=["Feta"]
        )
        self.assertTrue(entry["id"])
        self.assertEqual(entry["title"], "Ofengemüse")
        booked = cookmemory.book(self.path)
        self.assertEqual(booked[0]["id"], entry["id"])

    def test_upsert_book_replaces_in_place_keeping_id(self) -> None:
        original = cookmemory.add_to_book(self.path, title="Pasta", guidance=["alt"])
        updated = cookmemory.upsert_book(
            self.path, match_title="pasta", title="Pasta", guidance=["neu"], twist="mehr Chili"
        )
        booked = cookmemory.book(self.path)
        self.assertEqual(len(booked), 1)  # replaced, not appended
        self.assertEqual(updated["id"], original["id"])
        self.assertEqual(booked[0]["guidance"], ["neu"])
        self.assertEqual(booked[0]["twist"], "mehr Chili")

    def test_upsert_book_appends_when_no_match(self) -> None:
        cookmemory.add_to_book(self.path, title="Pasta", guidance=["x"])
        entry = cookmemory.upsert_book(
            self.path, match_title="Risotto", title="Risotto", guidance=["y"]
        )
        self.assertTrue(entry["id"])
        self.assertEqual(len(cookmemory.book(self.path)), 2)

    def test_recent_cooked_excludes_old_and_dedupes(self) -> None:
        cookmemory.add_cooked(self.path, dish="Risotto")
        cookmemory.add_cooked(self.path, dish="Risotto")  # dup
        # Inject an old entry directly.
        data = cookmemory.load(self.path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        data["cooked"].append({"dish": "Gulasch", "ts": old_ts})
        cookmemory.save(self.path, data)

        recent = cookmemory.recent_cooked(self.path, days=21)
        self.assertIn("Risotto", recent)
        self.assertNotIn("Gulasch", recent)
        self.assertEqual(recent.count("Risotto"), 1)


    def test_sessions_round_trip_newest_first(self) -> None:
        cookmemory.add_session(
            self.path, dish="Curry",
            lines=[{"name": "Linsen", "product_id": "5", "amount": 1, "unit": "Tasse",
                    "transaction_id": "tx1", "depleted": False, "kind": "consume"}],
        )
        second = cookmemory.add_session(self.path, dish="Risotto", lines=[])
        sessions = cookmemory.sessions(self.path)
        self.assertEqual(sessions[0]["id"], second["id"])  # newest first
        self.assertEqual(sessions[1]["dish"], "Curry")
        line = sessions[1]["lines"][0]
        self.assertEqual(line["product_id"], "5")
        self.assertEqual(line["transaction_id"], "tx1")
        self.assertFalse(line["depleted"])

    def test_update_session_line_patches_fields(self) -> None:
        entry = cookmemory.add_session(
            self.path, dish="Curry",
            lines=[{"name": "Linsen", "product_id": "5", "amount": 1,
                    "transaction_id": "tx1", "depleted": False, "kind": "consume"}],
        )
        cookmemory.update_session_line(
            self.path, entry["id"], 0, amount=0.5, transaction_id="tx2", depleted=True
        )
        line = cookmemory.sessions(self.path)[0]["lines"][0]
        self.assertEqual(line["amount"], 0.5)
        self.assertEqual(line["transaction_id"], "tx2")
        self.assertTrue(line["depleted"])

    def test_update_session_line_unknown_session_returns_none(self) -> None:
        self.assertIsNone(cookmemory.update_session_line(self.path, "nope", 0, amount=1))

    def test_sessions_normalize_on_partial_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"taste": {}}), encoding="utf-8")
        self.assertEqual(cookmemory.load(self.path)["sessions"], [])


if __name__ == "__main__":
    unittest.main()
