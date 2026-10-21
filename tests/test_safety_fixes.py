"""Additional regressions for non-destructive source fixes and full-text delivery."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from jalert.reliable_delivery import ReliableDelivery
from tools.history_backfill import lookup_english_abstract
from tools.apply_safe_fixes import prepare_changes


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

    def test_source_patcher_preview_is_read_only_and_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/'jalert').mkdir(); (root/'.github/workflows').mkdir(parents=True)
            cards='''def write_cards():
    if not articles:
        if log:
            log.info("cards page skipped: no reports found in %s", reports_dir)
        return None
    result = build_html()
'''
            readme='# 大气环境文献日报（journal-alert）\n\n把「期刊定期推送」变成**按关键词筛选后的微信推送 + 本地 Markdown 每日阅读报**，全自动、每天定时运行。\n'
            flow='name: journal-alert daily\n      - name: Commit reports and safe resumable state\n'
            (root/'jalert/cards.py').write_text(cards)
            (root/'README.md').write_text(readme)
            (root/'.github/workflows/daily.yml').write_text(flow)
            updates=prepare_changes(root)
            self.assertEqual(len(updates),3)
            self.assertEqual((root/'jalert/cards.py').read_text(),cards)
            self.assertEqual((root/'README.md').read_text(),readme)
            for file,body in updates.items():file.write_text(body)
            self.assertEqual(prepare_changes(root),{})
            self.assertIn('cards page rebuilt empty', (root/'jalert/cards.py').read_text())

if __name__=='__main__':unittest.main()
