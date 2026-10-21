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

    def test_long_translation_is_sent_in_parts_never_120_char_summary(self):
        self.delivery.enqueue([(self.item, self.score)])
        original = "完整译文：" + ("这是英文摘要对应的中文完整译文段落，包括研究背景和方法及结果。" * 125) + "全篇结束。"
        sent = []
        with patch.dict(os.environ, {'JALERT_WECHAT_MAX_MESSAGES_DAY':'5', 'JALERT_WECHAT_MAX_CHARS':'1200', 'JALERT_AI_PRIMARY':'custom', 'CUSTOM_AI_BASE_URL':'https://model.example/v1', 'CUSTOM_AI_MODEL':'example'}):
            with patch.object(self.delivery, '_call_model', return_value=original):
                result = self.delivery.flush(send_func=lambda title, body: sent.append(body) or True)
        self.assertGreaterEqual(len(sent), 2)
        self.assertTrue(all(len(body)<=1200 for body in sent))
        self.assertEqual(result['pending'], 0)
        self.assertEqual(result['sent'], 1)
        self.assertTrue(any('全篇结束。' in body for body in sent))

    def test_failed_second_part_keeps_translation_cursor(self):
        self.delivery.enqueue([(self.item, self.score)])
        translation = '中文原文：' + ('磁性耦合与自旋输运实验参数按照英文原始语句完整翻译。'*80)
        outputs=[]
        with patch.dict(os.environ, {'JALERT_WECHAT_MAX_MESSAGES_DAY':'4', 'JALERT_WECHAT_MAX_CHARS':'1200', 'JALERT_AI_PRIMARY':'custom', 'CUSTOM_AI_BASE_URL':'https://model.example/v1', 'CUSTOM_AI_MODEL':'example'}):
            with patch.object(self.delivery, '_call_model', return_value=translation):
                r=self.delivery.flush(send_func=lambda t,b: outputs.append(b) or len(outputs)==1)
        self.assertEqual(r['pending'],1)
        self.assertGreater(self.delivery.queue['items'][self.item.uid].get('delivery_offset',0),0)
        self.assertEqual(self.delivery.queue['items'][self.item.uid]['status'],'pending')


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
