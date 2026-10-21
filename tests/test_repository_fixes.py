"""Safe-patch regression tests; no network, no push, no state deletion."""
from __future__ import annotations

import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.generate_curated_config import KEYWORDS, modified_config
from tools.recover_pending_abstracts import recover_pending_abstracts


class RecoverPendingTests(unittest.TestCase):
    def test_fills_missing_english_without_sending(self):
        class Fake:
            def __init__(self):
                self.queue={'items':{'x': {'uid':'x','doi':'10.7/abc','abstract':'','status':'pending','enqueued_at':'2026-10-21'}}}
                self.saves=0
            def _save_queue(self):self.saves+=1
        d=Fake()
        result=recover_pending_abstracts(d, lookup=lambda doi: ('Actual complete English Abstract', 'OpenAlex'))
        self.assertEqual(result['recovered'],1)
        self.assertEqual(d.queue['items']['x']['status'],'pending')
        self.assertEqual(d.queue['items']['x']['abstract'],'Actual complete English Abstract')
        self.assertGreaterEqual(d.saves,2)

    def test_recovered_abstract_survives_bark_queue_compaction(self):
        from jalert.reliable_delivery import ReliableDelivery
        from jalert.metadata import load_metadata
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            delivery = ReliableDelivery(root)
            item = SimpleNamespace(
                uid="doi:10.7/abc", doi="10.7/abc", title="Magnetic tunnel junction",
                authors=["A"], journal="PRB", date="2026-10-21", url="", abstract="")
            score = SimpleNamespace(score=7, labels=["MTJ"])
            delivery.enqueue([(item, score)])
            full = "A verified original abstract describing magnetic switching. " * 5
            found = recover_pending_abstracts(delivery, lookup=lambda doi:(full, "Crossref"))
            self.assertEqual(found["recovered"], 1)
            delivery.queue["items"][item.uid].pop("abstract")
            delivery._save_queue()
            stored = load_metadata(root)["articles"]["doi:10.7/abc"]
            self.assertEqual(stored["abstract"], full.strip())
            self.assertEqual(stored["abstract_source"], "Crossref")

    def test_cooldown_prevents_repeat_requests(self):
        d=SimpleNamespace(queue={'items':{'x':{'uid':'x','doi':'10.7/abc','abstract':'',
              'status':'pending','abstract_last_checked':__import__('datetime').date.today().isoformat()}}},
                          _save_queue=lambda:None)
        called=[]
        result=recover_pending_abstracts(d, lookup=lambda doi: called.append(doi))
        self.assertEqual(called,[])
        self.assertEqual(result['attempted'],0)

    def test_unknown_doi_never_creates_a_fake_abstract(self):
        d=SimpleNamespace(queue={'items':{'x':{'uid':'x','doi':'','abstract':'','status':'pending'}}},
                          _save_queue=lambda:None)
        result=recover_pending_abstracts(d, lookup=lambda doi: (_ for _ in ()).throw(AssertionError()))
        self.assertEqual(result['attempted'],0)
        self.assertEqual(d.queue['items']['x']['abstract'],'')


class KeywordTests(unittest.TestCase):
    def test_curated_groups_are_specific_and_compatible(self):
        self.assertGreaterEqual(len(KEYWORDS),10)
        self.assertEqual(len({g['label'] for g in KEYWORDS}),len(KEYWORDS))
        self.assertTrue(all(g['terms'] and g['weight'] > 0 for g in KEYWORDS))
        allterms={term.lower() for g in KEYWORDS for term in g['terms']}
        self.assertIn('exchange bias',allterms)
        self.assertIn('magnetic tunnel junction',allterms)
        self.assertIn('sot-mram',allterms)
        self.assertIn('magnon transistor',allterms)
        self.assertNotIn('magnetic anisotropy',allterms)
        self.assertNotIn('magnetic heterostructure',allterms)

    def test_modify_config_preserves_journals_and_exclusions(self):
        src={'journals':[{'name':'a'}]*52,'keywords':[{'label':'old'}],
             'window':{'max_items_per_journal':100},'exclude_terms':['retracted'],
             'push':{'enabled': True}}
        fresh=modified_config(src)
        self.assertEqual(len(src['keywords']),1)
        self.assertEqual(len(fresh['journals']),52)
        self.assertEqual(fresh['exclude_terms'],['retracted'])
        self.assertEqual(fresh['push'],src['push'])
        self.assertEqual(fresh['window']['max_items_per_journal'],200)

if __name__ == '__main__':unittest.main()
