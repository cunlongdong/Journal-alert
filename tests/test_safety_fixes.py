"""Additional regressions for non-destructive source fixes and full-text delivery."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from jalert.reliable_delivery import ReliableDelivery
from tools.history_backfill import lookup_english_abstract


class SafetyFixTests(unittest.TestCase):
    def test_ack_finalization_does_not_translate_again(self):
        with tempfile.TemporaryDirectory() as td:
            delivery = ReliableDelivery(Path(td))
            item = SimpleNamespace(uid='doi:10.7000/snapshot', title='Exchange bias paper',
                doi='10.7000/snapshot', abstract='English original with 10 K and 15 mT',
                journal='Physical Review B', date='2026-10-21', url='')
            scored = SimpleNamespace(labels=['交换偏置'], score=18)
            delivery.enqueue([(item, scored)])
            zh = '研究在10 K条件下研究了交换偏置，发现15 mT的变化，详细论述了界面耦合及磁性行为。'
            called = []
            def once(_item, _day):
                called.append(1)
                if len(called)>1:
                    raise AssertionError('must not contact AI again after acknowledgements')
                return zh
            with patch.object(delivery, '_get_translation', side_effect=once), \
                 patch.dict('os.environ', {'JALERT_SERVERCHAN_SENDKEY': 'test',
                         'JALERT_BARK_ENABLED':'true','JALERT_BARK_DEVICE_KEY':'test'}):
                result=delivery.flush_both(send_func=lambda *args: True)
            self.assertEqual(result['pending_papers'],0)
            self.assertEqual(len(called),1)

    def test_semantic_scholar_matching_doi_fallback(self):
        def fake_json(url, **kwargs):
            if 'openalex.org' in url: return {'abstract_inverted_index':None}
            if 'crossref.org' in url: return {'message':{}}
            if 'semanticscholar.org' in url:
                return {'externalIds':{'DOI':'10.2000/EXAMPLE'},
                        'abstract':'Actual abstract with 10 K and 20 mT.'}
            raise AssertionError(url)
        with patch('tools.history_backfill._get_json', side_effect=fake_json), \
             patch.dict('os.environ', {'JALERT_SEMANTIC_SCHOLAR_ENABLED':'true'}):
            s, provider=lookup_english_abstract('10.2000/example')
        self.assertEqual(provider,'Semantic Scholar')
        self.assertIn('20 mT',s)

    def test_semantic_scholar_refuses_doi_mismatch(self):
        def fake_json(url, **kwargs):
            if 'crossref.org' in url: return {'message':{}}
            if 'openalex.org' in url: return {'abstract_inverted_index':None}
            return {'externalIds':{'DOI':'10.2000/unrelated'},
                    'abstract':'A scientifically unrelated abstract.'}
        with patch('tools.history_backfill._get_json', side_effect=fake_json), \
             patch.dict('os.environ', {'JALERT_SEMANTIC_SCHOLAR_ENABLED':'true'}):
            s,provider=lookup_english_abstract('10.2000/example')
        self.assertEqual((s,provider),('',''))


if __name__=='__main__':unittest.main()
