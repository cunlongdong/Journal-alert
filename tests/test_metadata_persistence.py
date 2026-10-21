"""End-to-end offline checks for permanent source metadata and reset readiness."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jalert.metadata import merge_many, merge_verified_abstract, load_metadata
from jalert.enhanced_delivery import Delivery
from tools.enrich_bilingual_cards import build_bilingual_html, DATA_PATTERN
from tools.history_backfill import backfill
from test_bilingual_cards import example_html

ROOT = Path(__file__).resolve().parents[1]


class PersistentMetadataTests(unittest.TestCase):
    def test_richer_sources_upgrade_without_losing_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            short = {"uid":"doi:10.1000/abc","doi":"10.1000/abc","title":"Exchange bias study",
                     "abstract":"First API abstract.", "authors":["A"],"source":"crossref"}
            self.assertEqual(merge_many(root, [short])["updated"], 1)
            better = dict(short, abstract="Much longer measured magnetic abstract. "*8,
                          authors=["A","B","C"],source="openalex")
            merge_many(root, [better])
            merge_many(root, [dict(short, abstract="", authors=[])])
            record = load_metadata(root)["articles"]["doi:10.1000/abc"]
            self.assertEqual(record["abstract"], better["abstract"].strip())
            self.assertEqual(record["abstract_source"], "openalex")
            self.assertEqual(record["authors"], ["A","B","C"])
            self.assertEqual(len(load_metadata(root)["articles"]), 1)

    def test_invalid_ledger_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)/"state"
            state.mkdir()
            file = state/"article_metadata.json"
            file.write_text('{"version": 5, "articles": []}',encoding="utf-8")
            with self.assertRaises(ValueError):
                merge_many(Path(td), [{"doi":"10.1000/test","title":"Test"}])
            self.assertIn('"version": 5',file.read_text("utf-8"))

    def test_verified_abstract_updates_cards_and_authors_without_fake_translation(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            full="This original measured exchange bias abstract includes magnetic field 35 mT. "*8
            merge_many(root,[{"uid":"doi:10.1000/abc","doi":"10.1000/abc",
                "title":"Exchange bias study","abstract":full,"source":"crossref",
                "authors":["A","B"]}])
            new, n = build_bilingual_html(example_html(),{"summaries":{}},
                       {"abstracts":{}},load_metadata(root))
            data=json.loads(DATA_PATTERN.search(new).group(2))["articles"][0]
            self.assertEqual(n,0)
            self.assertEqual(data["enAbstract"],full.strip())
            self.assertEqual(data["enAbstractSource"],"crossref")
            self.assertEqual(data["authors"],["A","B"])
            self.assertNotIn("zhAbstract",data)

    def test_do_not_mix_old_chinese_translation_with_updated_english_abstract(self):
        old="Old source abstract from publisher."
        newer="Longer source abstract verified through a DOI. "*12
        cache={"summaries":{"sig":{"mode":"literal-full-translation","title":"Exchange bias study",
                "doi":"10.1000/abc","summary":"旧的中文摘要","english_abstract":old}}}
        source={"articles":{"doi:10.1000/abc":{"abstract":newer,"abstract_source":"openalex"}}}
        page,n=build_bilingual_html(example_html(),cache,metadata_store=source)
        item=json.loads(DATA_PATTERN.search(page).group(2))["articles"][0]
        self.assertEqual(item["enAbstract"],newer)
        self.assertNotIn("zhAbstract",item)
        self.assertEqual(n,0)

    def test_pending_queue_is_enriched_without_replaying_accepted_paper(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            delivery=Delivery(root)
            score=SimpleNamespace(score=9,labels=["SOT"])
            items=[]
            for number in (1,2):
                items.append((SimpleNamespace(uid=f"doi:10.1000/{number}",
                    doi=f"10.1000/{number}",title="Test paper",authors=["A"],
                    journal="PRB",url="",date="2026-10-21",abstract="brief"),score))
            delivery.enqueue(items)
            delivery.queue["items"][items[1][0].uid]["status"]="sent"
            delivery.queue["items"][items[1][0].uid]["bark_done"]=True
            better={"version":1,"articles":{
                f"doi:10.1000/{number}":{"abstract":"Long reliable Abstract."*8,
                              "authors":["A","B"]} for number in (1,2)}}
            self.assertEqual(delivery.refresh_pending(better),2)
            updated=Delivery(root)
            self.assertTrue(len(updated.queue["items"][items[0][0].uid]["abstract"])>60)
            self.assertEqual(updated.queue["items"][items[0][0].uid]["authors"],["A","B"])
            self.assertEqual(updated.queue["items"][items[1][0].uid]["abstract"],"brief")
            self.assertTrue(updated.queue["items"][items[1][0].uid]["bark_done"])

    def test_backfill_prefers_persisted_english_over_duplicate_network_lookup(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            full="Measurements establish a verifiable interface magnetoresistance response. "*6
            merge_verified_abstract(root,"10.1000/abc","Exchange bias study",full,"crossref")
            paper=[{"doi":"10.1000/abc","title":"Exchange bias study"}]
            with patch("tools.history_backfill.collect",return_value=(paper,[],[])), \
                 patch("tools.history_backfill.ReliableDelivery._translate_title",return_value="交换偏置研究"), \
                 patch("tools.history_backfill.ReliableDelivery._get_translation",return_value="测量证明磁阻响应显著"):
                result=backfill(root,max_papers=1,lookup=lambda doi:self.fail("should not re-query"))
            self.assertEqual(result["translated"],1)

    def test_daily_json_is_tracked_and_workflow_stages_metadata(self):
        gitignore=(ROOT/".gitignore").read_text("utf-8")
        workflow=(ROOT/".github"/"workflows"/"daily.yml").read_text("utf-8")
        source=(ROOT/"run.py").read_text("utf-8")
        self.assertNotIn("\nreports/*.json\n",gitignore)
        self.assertIn('state/article_metadata.json',workflow)
        self.assertIn('load_snapshot(reports_dir / f"{day}.json")',source)


if __name__=="__main__":
    unittest.main()
