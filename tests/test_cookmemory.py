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


if __name__ == "__main__":
    unittest.main()
