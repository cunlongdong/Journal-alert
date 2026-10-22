"""Offline regression tests for the original Journal-alert acquisition pipeline."""
import logging
import threading
import unittest
from unittest.mock import patch
from jalert.fetch import Item, _fetch_journal


class ParallelFetchTests(unittest.TestCase):
    def test_apis_start_together_and_results_keep_source_order(self):
        both_started=threading.Barrier(2, timeout=3)
        journal={"name":"Magnetic Journal", "issn":"0304-8853"}
        def openalex(*args, **kwargs):
            both_started.wait()
            return [Item(uid="doi:10.1/openalex", title="OpenAlex paper", source="openalex")]
        def crossref(*args, **kwargs):
            both_started.wait()
            return [Item(uid="doi:10.1/crossref", title="Crossref paper", source="crossref")]
        with patch("jalert.fetch.fetch_openalex", side_effect=openalex), \
             patch("jalert.fetch.fetch_crossref", side_effect=crossref):
            papers, status = _fetch_journal(
                journal,"2026-10-21",100,
                {"rss":False,"openalex":True,"crossref":True},
                logging.getLogger("test"),{"rss":5,"api":5},0)
        self.assertEqual([x.source for x in papers], ["openalex","crossref"])
        self.assertEqual([x["source"] for x in status], ["openalex","crossref"])
        self.assertTrue(all(x["ok"] for x in status))

    def test_individual_source_failure_does_not_lose_other_source(self):
        journal={"name":"Magnetic Journal","issn":"0304-8853"}
        with patch("jalert.fetch.fetch_openalex", side_effect=RuntimeError("timeout")), \
             patch("jalert.fetch.fetch_crossref", return_value=[
                Item(uid="doi:10.1/ok",title="Crossref paper")]):
            items, records=_fetch_journal(journal,"2026-10-21",100,
                      {"rss":False,"openalex":True,"crossref":True},
                      logging.getLogger("test"),{"rss":5,"api":5},0)
        self.assertEqual(len(items),1)
        self.assertFalse(records[0]["ok"])
        self.assertTrue(records[1]["ok"])
        self.assertEqual(records[1]["items"],1)

    def test_disabled_source_never_called(self):
        with patch("jalert.fetch.fetch_openalex",side_effect=AssertionError("disabled")), \
             patch("jalert.fetch.fetch_crossref",return_value=[]):
            items, records=_fetch_journal({"name":"Journal","issn":"0304-8853"},
                       "2026-10-21",100,{"rss":False,"openalex":False,"crossref":True},
                       logging.getLogger("test"),{"rss":5,"api":5},0)
        self.assertEqual(items,[])
        self.assertEqual([r["source"] for r in records],["crossref"])

if __name__ == "__main__":
    unittest.main()
