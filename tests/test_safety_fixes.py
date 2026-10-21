"""Additional regressions for non-destructive source fixes and full-text delivery."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from jalert.reliable_delivery import ReliableDelivery
from tools.history_backfill import lookup_english_abstract


class SafetyFixTests(unittest.TestCase):
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
