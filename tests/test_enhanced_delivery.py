import importlib.util
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jalert.enhanced_delivery import Delivery, QUEUE_NAME, CACHE_NAME


class EnhancedDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.delivery = Delivery(self.root, logging.getLogger('test'))
        self.item = SimpleNamespace(uid='doi:abcd', title='Exchange bias at interfaces', journal='PRB', date='2026-10-21', doi='10.1103/example', url='', abstract='Exchange bias is investigated experimentally by measuring several samples. ' * 6)
        self.score = SimpleNamespace(score=22, labels=['交换偏置'])

    def test_queue_idempotent_and_database_untouched(self):
        self.assertEqual(self.delivery.enqueue([(self.item, self.score)]), 1)
        self.assertEqual(self.delivery.enqueue([(self.item, self.score)]), 0)
        self.assertEqual(self.delivery.pending_count(), 1)
        self.assertFalse((self.root / 'state' / 'seen.sqlite').exists())
        self.assertTrue((self.root / 'state' / QUEUE_NAME).exists())

    def test_accepted_batch_and_cache_survive_restarts(self):
        self.delivery.enqueue([(self.item, self.score)])
        sent = []
        with patch.dict(os.environ, {'JALERT_SERVERCHAN_SENDKEY': 'fake-for-tests', 'DEEPSEEK_API_KEY': 'fake-for-tests', 'JALERT_WECHAT_MAX_MESSAGES_DAY': '4'}):
            with patch.object(self.delivery, '_call_model', return_value='研究通过实验检测磁性异质结的交换偏置效应，比较不同参数对界面耦合和磁滞特征的影响，并依据摘要中的结果解释主要趋势。'):
                result = self.delivery.flush(send_func=lambda title, body: sent.append((title, body)) or True)
        self.assertEqual(result['sent'], 1)
        self.assertIn('中文摘要', sent[0][1])
        self.assertEqual(Delivery(self.root).pending_count(), 0)
        self.assertTrue((self.root / 'state' / CACHE_NAME).exists())

    def test_failed_push_keeps_queue_and_counts_quota(self):
        self.delivery.enqueue([(self.item, self.score)])
        with patch.dict(os.environ, {'JALERT_WECHAT_MAX_MESSAGES_DAY': '1', 'DEEPSEEK_API_KEY': 'fake-for-tests'}):
            with patch.object(self.delivery, '_call_model', return_value='这篇论文比较磁性界面中的交换偏置现象，通过实验手段研究磁滞回线和样品变化，展示主要结果及相关物理过程。'):
                result = self.delivery.flush(send_func=lambda title, body: False)
        self.assertEqual(result['pending'], 1)
        self.assertEqual(result['wechat_attempts_today'], 1)
        with patch.dict(os.environ, {'JALERT_WECHAT_MAX_MESSAGES_DAY': '1'}):
            self.assertEqual(Delivery(self.root).flush(send_func=lambda t, b: True)['sent'], 0)

    def test_missing_abstract_is_not_invented(self):
        self.item.abstract = ''
        self.delivery.enqueue([(self.item, self.score)])
        with patch.dict(os.environ, {'JALERT_WECHAT_MAX_MESSAGES_DAY': '1', 'DEEPSEEK_API_KEY': 'fake-for-tests'}):
            with patch.object(self.delivery, '_call_model', side_effect=AssertionError('should not call AI')):
                sent=[]
                result=self.delivery.flush(send_func=lambda t, b: sent.append(b) or True)
        self.assertEqual(result['sent'], 1)
        self.assertIn('无法可靠生成中文摘要', sent[0])

    def test_daily_wechat_cap_and_backlog(self):
        for i in range(16):
            item=SimpleNamespace(**vars(self.item))
            item.uid=f'uid-{i}'
            item.title=f'Article {i}'
            item.abstract=''
            self.delivery.enqueue([(item, self.score)])
        sent=[]
        with patch.dict(os.environ, {'JALERT_WECHAT_MAX_MESSAGES_DAY': '1', 'JALERT_WECHAT_MAX_CHARS': '1000'}):
            result=self.delivery.flush(send_func=lambda t, b: sent.append(b) or True)
        self.assertEqual(result['wechat_attempts_today'], 1)
        self.assertGreater(result['pending'], 0)
        self.assertEqual(len(sent), 1)

    def test_corrupt_state_refused(self):
        p=self.root/'state'/QUEUE_NAME
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('not json',encoding='utf-8')
        with self.assertRaises(json.JSONDecodeError):
            Delivery(self.root)


if __name__ == '__main__':
    unittest.main()

# These additional tests use no network, no API credits, and no GitHub changes.
class IntegrationContractTests(unittest.TestCase):
    def test_ai_primary_and_fallback_resolution(self):
        with tempfile.TemporaryDirectory() as temp:
            d = Delivery(Path(temp))
            with patch.dict(os.environ, {
                'JALERT_AI_PRIMARY': 'custom',
                'JALERT_AI_FALLBACK': 'deepseek',
                'CUSTOM_AI_BASE_URL': 'https://example.org/v1',
                'CUSTOM_AI_MODEL': 'my-magnetic-model',
                'CUSTOM_AI_API_KEY': 'test-secret',
                'DEEPSEEK_API_KEY': 'test-deepseek',
            }):
                endpoints = d._model_endpoints()
                self.assertEqual([e[0] for e in endpoints], ['custom', 'deepseek'])
                self.assertEqual(endpoints[0][1], 'https://example.org/v1')

    def test_wrapper_preserves_upstream_db_save_and_queues_only_new(self):
        import sys
        from unittest.mock import patch
        from types import ModuleType
        fake = ModuleType('run')
        class FakeStore:
            def __init__(self, *args, **kwargs):
                pass
            def save(self, entries):
                return len(entries)
        fake.Store = FakeStore
        fake.load_config = lambda *_: {"push": {"enabled": True}}
        fake.main = lambda argv: (
            fake.Store('placeholder').save([(
                SimpleNamespace(uid='fresh-doi', title='Magnetic paper', journal='PRB', date='2026-10-21', doi='10.1/test', url='', abstract=''),
                SimpleNamespace(score=20, labels=['反铁磁']),
            )]), 0)[1]
        path = Path(__file__).resolve().parents[1] / 'tools' / 'enhanced_run.py'
        spec = importlib.util.spec_from_file_location('_test_enhanced_run', path)
        with patch.dict(sys.modules, {'run': fake}):
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with tempfile.TemporaryDirectory() as temp:
                original = module.ROOT
                module.ROOT = Path(temp)
                try:
                    self.assertEqual(module.main(['--once', '--no-push']), 0)
                    q = json.loads((Path(temp) / 'state' / QUEUE_NAME).read_text(encoding='utf-8'))
                    self.assertIn('fresh-doi', q['items'])
                    self.assertEqual(q['items']['fresh-doi']['status'], 'pending')
                    self.assertIs(fake.Store, FakeStore)  # monkeypatch is restored
                finally:
                    module.ROOT = original

    def test_publisher_deduplication_status_survives_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            d=Delivery(Path(temp))
            item=SimpleNamespace(uid='x',title='No abstract',journal='PRB',date='2026-10-21',doi='',url='https://doi.org/10.1/x',abstract='')
            score=SimpleNamespace(score=20,labels=['自旋电子学'])
            d.enqueue([(item,score)])
            with patch.dict(os.environ, {'JALERT_WECHAT_MAX_MESSAGES_DAY': '1'}):
                d.flush(send_func=lambda a,b:True)
                d2=Delivery(Path(temp))
                self.assertEqual(d2.enqueue([(item,score)]),0)
                self.assertEqual(d2.pending_count(),0)
