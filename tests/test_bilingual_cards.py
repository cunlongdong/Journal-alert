import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bilingual_cards', ROOT / 'tools' / 'enrich_bilingual_cards.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def example_html():
    data = {'articles': [
        {'doi': '10.1000/ABC', 'title': 'Exchange bias study', 'abstract': 'English excerpt', 'authors': [], 'journal':'PRB'},
        {'doi': '', 'title': 'Old article', 'abstract': 'Old English excerpt', 'authors': [], 'journal':'APL'},
    ]}
    return '''<html><head><style>body { color:#000 }\n</style></head><body><script type="application/json" id="DATA">'''+json.dumps(data)+'''</script><script>
    var ab = a.abstract
      ? '<p class="excerpt">'+esc(trunc(a.abstract,200))+'</p>'
      : '<p class="excerpt none">日报未收录摘要'+(a.matchTerm?'（命中词 '+esc(a.matchTerm)+'）':'')+' · 点击看原文</p>';
      +   '<div class="sec-t">摘要</div>'
      +   (a.abstract
            ? '<p class="abs">'+esc(a.abstract)+'</p>'
            : '<p class="abs" style="color:#9d9da8">该条日报未收录摘要。</p>')
    var hay = (a.title+' '+a.abstract+' '+a.authors.join(' ')+' '+a.journal).toLowerCase();
</script></body></html>'''


def cache():
    return {'summaries': {'signature1': {
      'mode':'literal-full-translation', 'title':'Exchange bias study',
      'doi':'10.1000/abc', 'summary':'完整直译：磁性材料中的交换偏置。',
      'english_abstract':'Full English abstract with <safety> & quotes.',
    }}}


class BilingualTests(unittest.TestCase):
    def test_bilingual_data_and_fallback(self):
        new, count = mod.build_bilingual_html(example_html(), cache())
        self.assertEqual(count, 1)
        self.assertIn('JALERT_BILINGUAL_V1', new)
        self.assertIn('中文直译', new)
        m = mod.DATA_PATTERN.search(new)
        d = json.loads(m.group(2))
        self.assertEqual(d['articles'][0]['zhAbstract'], '完整直译：磁性材料中的交换偏置。')
        self.assertEqual(d['articles'][0]['enAbstract'], 'Full English abstract with <safety> & quotes.')
        self.assertNotIn('zhAbstract', d['articles'][1])
        self.assertNotIn('<safety>', m.group(2))  # XSS-safe embedded JSON

    def test_chinese_title_is_exposed_with_full_english_title(self):
        import hashlib
        english = "Exchange bias study"
        signature = hashlib.sha256(english.encode("utf-8")).hexdigest()
        data = cache()
        data["titles"] = {signature: {"title": english, "zh_title": "交换偏置研究"}}
        rendered, _ = mod.build_bilingual_html(example_html(), data)
        payload = json.loads(mod.DATA_PATTERN.search(rendered).group(2))
        self.assertEqual(payload["articles"][0]["zhTitle"], "交换偏置研究")
        self.assertEqual(payload["articles"][0]["title"], english)
        self.assertNotIn("zhTitle", payload["articles"][1])

    def test_idempotence(self):
        once, _ = mod.build_bilingual_html(example_html(), cache())
        twice, _ = mod.build_bilingual_html(once, cache())
        self.assertEqual(once, twice)

    def test_remove_stale_zh_when_cache_removed(self):
        once, _ = mod.build_bilingual_html(example_html(), cache())
        cleared, count = mod.build_bilingual_html(once, {'summaries':{}})
        self.assertEqual(count, 0)
        self.assertNotIn('zhAbstract', json.loads(mod.DATA_PATTERN.search(cleared).group(2))['articles'][0])

    def test_doi_prefix_and_title_mapping(self):
        self.assertEqual(mod.doi_key('https://doi.org/10.1000/ABC'), '10.1000/abc')
        self.assertEqual(mod.title_key(' A   Paper '), 'a paper')

    def test_fail_closed_on_changed_template(self):
        with self.assertRaisesRegex(ValueError,'missing'):
            mod.build_bilingual_html('<html>No script</html>', cache())

    def test_cli_files(self):
        with tempfile.TemporaryDirectory() as td:
            base=pathlib.Path(td)
            (base/'index.html').write_text(example_html(),encoding='utf-8')
            (base/'cache.json').write_text(json.dumps(cache()),encoding='utf-8')
            self.assertEqual(mod.main(['--cards',str(base/'index.html'),'--cache',str(base/'cache.json')]),0)
            self.assertEqual(mod.main(['--cards',str(base/'index.html'),'--cache',str(base/'cache.json')]),0)
            self.assertIn('zhAbstract', (base/'index.html').read_text('utf-8'))

if __name__ == '__main__':
    unittest.main()

class TranslationMetadataTests(unittest.TestCase):
    def test_completed_translation_carries_bilingual_article_metadata(self):
        import os
        from types import SimpleNamespace
        from unittest.mock import patch
        from jalert.enhanced_delivery import Delivery
        with tempfile.TemporaryDirectory() as td:
            delivery=Delivery(pathlib.Path(td))
            item={'uid':'doi:10.1000/x','title':'Exchange bias study',
                  'doi':'10.1000/x','abstract':'Exchange bias changes interfacial magnetic ordering. '
                  'The experiments compare magnetic hysteresis at different temperatures.'}
            summary='研究研究了交换偏置对界面磁序的影响。实验比较了不同温度下的磁滞行为，并保留了原文条件。'
            with patch.dict(os.environ, {'JALERT_AI_PRIMARY':'custom','JALERT_AI_FALLBACK':'none',
                'CUSTOM_AI_BASE_URL':'https://example.edu/v1','CUSTOM_AI_MODEL':'my-model'}):
                with patch.object(delivery,'_call_model',return_value=summary):
                    self.assertEqual(delivery._summary(item,'2026-10-21'),summary)
                entries=list(delivery.cache['summaries'].values())
                completed=[x for x in entries if x.get('mode')=='literal-full-translation']
                self.assertEqual(len(completed),1)
                self.assertEqual(completed[0]['doi'], '10.1000/x')
                self.assertEqual(completed[0]['title'],'Exchange bias study')
                self.assertEqual(completed[0]['english_abstract'],item['abstract'])
                self.assertNotIn('api_key', str(completed[0]).lower())
                with patch.object(delivery,'_call_model',side_effect=AssertionError('unexpected extra model call')):
                    self.assertEqual(delivery._summary(item,'2026-10-21'),summary)

    def test_existing_complete_cache_can_be_augmented_without_regeneration(self):
        import os,hashlib
        from unittest.mock import patch
        from jalert.enhanced_delivery import Delivery, PROMPT_VERSION
        with tempfile.TemporaryDirectory() as td:
            delivery=Delivery(pathlib.Path(td))
            item={'uid':'doi:10.1000/z','doi':'10.1000/z','title':'Old cached zh','abstract':'English source abstract.'}
            sig=hashlib.sha256((PROMPT_VERSION+'\n'+item['title']+'\n'+item['abstract']).encode()).hexdigest()
            delivery.cache['summaries'][sig]={'summary':'旧译文保留','mode':'literal-full-translation'}
            with patch.object(delivery,'_call_model',side_effect=AssertionError('should read old translation')):
                self.assertEqual(delivery._summary(item,'2026-10-21'),'旧译文保留')
            self.assertEqual(delivery.cache['summaries'][sig]['doi'],'10.1000/z')
            self.assertEqual(delivery.cache['summaries'][sig]['english_abstract'],'English source abstract.')
