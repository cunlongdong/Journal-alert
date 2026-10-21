"""Offline title backfill tests; never invokes an LLM or sends Bark."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jalert.reliable_delivery import ReliableDelivery
from tools.backfill_titles import fill_missing_titles

PAPERS = [
    {"title": "Spin-orbit torque memory", "doi": "10.1111/one"},
    {"title": "Exchange bias in magnets", "doi": "10.1111/two"},
    {"title": "Spin-orbit torque memory", "doi": "10.1111/one"},
]


class TitleBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_translates_each_missing_title_and_persists_cache(self):
        calls = []
        real = ReliableDelivery._translate_title
        def stub(delivery, item, day):
            calls.append(item["title"])
            import hashlib
            key = hashlib.sha256(item["title"].encode("utf-8")).hexdigest()
            delivery.cache.setdefault("titles", {})[key] = {
                "title": item["title"], "zh_title": "自旋电子学论文标题",
            }
            delivery._save_cache()
            return "自旋电子学论文标题"
        with patch("tools.backfill_titles.collect", return_value=(PAPERS, [], [])), \
             patch.object(ReliableDelivery, "_translate_title", stub):
            result = fill_missing_titles(self.root)
            repeated = fill_missing_titles(self.root)
        self.assertEqual(result["missing_before"], 2)
        self.assertEqual(result["translated"], 2)
        self.assertEqual(result["remaining_estimate"], 0)
        self.assertEqual(repeated["missing_before"], 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(ReliableDelivery(self.root).cache["titles"]), 2)

    def test_dry_run_does_not_call_ai_or_create_state(self):
        with patch("tools.backfill_titles.collect", return_value=(PAPERS, [], [])), \
             patch.object(ReliableDelivery, "_translate_title",
                          side_effect=AssertionError("AI must not run")):
            result = fill_missing_titles(self.root, dry_run=True)
        self.assertEqual(result["missing_before"], 2)
        self.assertEqual(result["attempted"], 2)
        self.assertFalse((self.root / "state" / "enhanced_zh_cache.json").exists())

    def test_budget_exhaustion_keeps_missing_titles_for_next_run(self):
        with patch("tools.backfill_titles.collect", return_value=(PAPERS, [], [])), \
             patch.object(ReliableDelivery, "_translate_title",
                          side_effect=RuntimeError("AI daily request budget exhausted")):
            result = fill_missing_titles(self.root)
        self.assertEqual(result["attempted"], 1)
        self.assertTrue(result["ai_budget_exhausted"])
        self.assertEqual(result["remaining_estimate"], 2)
        self.assertEqual(len(ReliableDelivery(self.root).cache.get("titles", {})), 0)

    def test_scientific_formula_corrections_avoid_failed_model_calls(self):
        import json
        from tools.backfill_titles import ROOT
        glossary = json.loads((ROOT / "research" / "title_corrections.json").read_text("utf-8"))
        self.assertEqual(len(glossary), 4)
        papers = [{"doi": doi, "title": row["title"]} for doi,row in glossary.items()]
        with patch("tools.backfill_titles.collect", return_value=(papers, [], [])), \
             patch.object(ReliableDelivery, "_translate_title",
                          side_effect=AssertionError("curated titles must not invoke AI")):
            first = fill_missing_titles(self.root)
            repeated = fill_missing_titles(self.root)
        self.assertEqual(first["reviewed_glossary_applied"], 4)
        self.assertEqual(first["missing_before"], 0)
        self.assertEqual(repeated["reviewed_glossary_applied"], 0)
        self.assertEqual(len(ReliableDelivery(self.root).cache["titles"]), 4)

    def test_invalid_max_rejected(self):
        for value in (-1, 201):
            with self.assertRaises(ValueError):
                fill_missing_titles(self.root, limit=value)


if __name__ == "__main__":
    unittest.main()
