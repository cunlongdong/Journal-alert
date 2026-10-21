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





    def test_corrupt_state_refused(self):
        p=self.root/'state'/QUEUE_NAME
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('not json',encoding='utf-8')
        with self.assertRaises(json.JSONDecodeError):
            Delivery(self.root)


    def test_split_original_text_is_not_omitted(self):
        original = ("The measurement confirms an interfacial magnetic response under applied bias. " * 60).strip()
        pieces = self.delivery._split_abstract(original, max_chars=520)
        self.assertGreater(len(pieces), 1)
        self.assertEqual(" ".join(pieces), original)

    def test_translation_chunks_cached_without_rerunning_model(self):
        self.item.abstract = ("The magnetization switching changes with exchange coupling. " * 80).strip()
        self.delivery.enqueue([(self.item, self.score)])
        item = self.delivery.queue['items'][self.item.uid]
        expected_calls = len(self.delivery._split_abstract(self.item.abstract))
        fake_translation = "本研究测试不同磁性界面的耦合机制，通过对比实验观察材料响应，保留所有主要条件和测量方法。" * 5
        with patch.dict(os.environ, {'JALERT_AI_PRIMARY':'custom', 'CUSTOM_AI_BASE_URL':'https://model.example/v1', 'CUSTOM_AI_MODEL':'example', 'JALERT_AI_MAX_CALLS_DAY':'25'}):
            with patch.object(self.delivery, '_call_model', return_value=fake_translation) as mock:
                full = self.delivery._summary(item, '2026-10-21')
                self.assertEqual(mock.call_count, expected_calls)
                self.assertEqual(full.count(fake_translation), expected_calls)
                restored = Delivery(self.root)
                with patch.object(restored, '_call_model', side_effect=AssertionError('should use cache')):
                    self.assertEqual(restored._summary(item, '2026-10-21'), full)




if __name__ == '__main__':
    unittest.main()

# These additional tests use no network, no API credits, and no GitHub changes.

    def test_quality_error_retried_once_with_separate_budget_charge(self):
        attempts = []
        def fake_model(name, base, model, key, item):
            attempts.append(bool(item.get("_translation_retry_warning")))
            if not item.get("_translation_retry_warning"):
                raise ValueError("translation quality check failed: ['14']")
            return "在 0.1 至 14 个离子的照射后，研究者观察到磁畴结构明显改变。"
        with patch.dict(os.environ, {
            "JALERT_AI_PRIMARY": "custom", "JALERT_AI_FALLBACK": "none",
            "CUSTOM_AI_BASE_URL": "https://offline.example.org/api",
            "CUSTOM_AI_MODEL": "offline", "JALERT_AI_MAX_CALLS_DAY": "3",
        }), patch.object(self.delivery, "_call_model", side_effect=fake_model):
            result = self.delivery._summary({
                "uid": "doi:10.1234/retry", "doi": "10.1234/retry",
                "title": "Irradiation", "abstract": "Irradiation from 0.1 to 14 ions was measured.",
            }, "2026-10-21")
        self.assertIn("14", result)
        self.assertEqual(attempts, [False, True])
        self.assertEqual(self.delivery._usage("2026-10-21")["ai_attempts"], 2)

    def test_repeated_quality_failure_remains_pending_without_full_cache(self):
        with patch.dict(os.environ, {
            "JALERT_AI_PRIMARY": "custom", "JALERT_AI_FALLBACK": "none",
            "CUSTOM_AI_BASE_URL": "https://offline.example.org/api",
            "CUSTOM_AI_MODEL": "offline", "JALERT_AI_MAX_CALLS_DAY": "3",
        }), patch.object(self.delivery, "_call_model",
                         side_effect=ValueError("translation quality check failed: ['50']")) as model:
            with self.assertRaises(RuntimeError):
                self.delivery._summary({
                    "uid": "doi:10.1234/test", "doi": "10.1234/test",
                    "title": "Test", "abstract": "Magnetic switching at 50 mT.",
                }, "2026-10-21")
        self.assertEqual(model.call_count, 2)
        self.assertFalse(any(rec.get("mode") == "literal-full-translation"
                             for rec in self.delivery.cache["summaries"].values()))


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

