"""Research-backed spintronics keyword regression tests, offline only."""
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from jalert.score import Scorer, _pattern
from tools.generate_curated_config import modified_config

ROOT = Path(__file__).resolve().parents[1]


class ResearchKeywordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        cls.scorer = Scorer(cls.config["keywords"], cls.config["exclude_terms"],
                            cls.config.get("exclude_doi_prefixes", []))

    def score(self, title, abstract=""):
        item = SimpleNamespace(title=title, abstract=abstract,
                               doi="10.1234/test", authors=[])
        return self.scorer.score(item)

    def test_existing_research_topics_are_preserved(self):
        self.assertGreaterEqual(len(self.config["keywords"]), 14)
        self.assertEqual(len({g["label"] for g in self.config["keywords"]}),
                         len(self.config["keywords"]))
        for label in ("交换偏置与界面交换耦合", "反铁磁与补偿磁体",
                      "自旋轨道力矩SOT", "磁隧道结与磁电阻"):
            self.assertIn(label, [g["label"] for g in self.config["keywords"]])

    def test_representative_publications_remain_discoverable(self):
        titles = [
            "Voltage-Controlled Magnon Transistor via Tuning Interfacial Exchange Coupling",
            "Restricted Boltzmann Machines Implemented by Spin–Orbit Torque Magnetic Tunnel Junctions",
            "Giant Tunneling Magnetoresistance in Spin-Filter Magnetic Tunnel Junctions Based on CrSBr",
            "Large spin-orbit-torque efficiency and low critical-current density in β-W/X multilayers",
            "The magnetic immunity of perpendicular STT-MRAM",
            "High-performance orbital-torque magnetic memory on the 300-mm platform",
            "Spin-Torque Memristors Based on Perpendicular Magnetic Tunnel Junctions for Neuromorphic Computing",
        ]
        for title in titles:
            with self.subTest(title=title):
                self.assertGreater(self.score(title).score, 0)

    def test_unicode_hyphen_and_whitespace_forms(self):
        pattern = _pattern("spin-orbit torque")
        for phrase in ("spin-orbit torque", "spin–orbit torque",
                       "spin‐orbit-torque", "Spin Orbit Torque",
                       "spin‑orbit‑torque"):
            with self.subTest(text=phrase):
                self.assertIsNotNone(pattern.search(phrase))
        self.assertIsNone(_pattern("MRAM").search("unrelated-MRAMXYZ"))

    def test_unrelated_generic_topics_do_not_trigger_alert(self):
        for title in ("A fast transformer for generative AI",
                      "A new analog compute-in-memory accelerator",
                      "Efficient scheduling of 300 mm wafer fabrication"):
            with self.subTest(title=title):
                self.assertEqual(self.score(title).score, 0)

    def test_optional_curator_does_not_erase_user_groups_or_lower_fetch_cap(self):
        updated = modified_config(self.config)
        self.assertEqual(updated["keywords"], self.config["keywords"])
        self.assertEqual(updated["window"]["max_items_per_journal"],
                         self.config["window"]["max_items_per_journal"])

    def test_rss_requires_per_journal_valid_feeds(self):
        self.assertFalse(self.config["window"]["sources"]["rss"])
        self.assertFalse(any(j.get("rss") for j in self.config["journals"]))
        # Only verified publisher RSS URLs may be installed and enabled:
        # enabling an empty feed field would not provide additional Abstracts.


if __name__ == "__main__":
    unittest.main()
