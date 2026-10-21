"""Regression tests: full English text, AI-independent source caching, safe reset."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tools import history_backfill as hist
from tools import enrich_bilingual_cards as cards
from tools.inspect_and_reset_history import inspect_or_reset
from test_bilingual_cards import example_html


class FullAbstractTests(unittest.TestCase):
    def test_english_source_is_preserved_if_model_translation_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'reports').mkdir()
            papers = [{'doi':'10.1000/ABC', 'title':'Exchange bias study', 'abstract':'Truncated English...'}]
            full = 'This is the full original English abstract, including 13.2 mT, 0.4%, and its conclusions.'
            with patch.object(hist, 'collect', return_value=(papers, [], [])), \
                 patch.object(hist.ReliableDelivery, '_get_translation', side_effect=ValueError('missing critical number')):
                result = hist.backfill(root, max_papers=1, lookup=lambda doi:(full, 'OpenAlex'))
            self.assertEqual(result['translated'], 0)
            self.assertEqual(result['failed_quality_or_api'], 1)
            d = json.loads((root/'state/full_english_abstracts.json').read_text())
            self.assertEqual(d['abstracts']['10.1000/abc']['abstract'], full)
            # Retrying a failed translation should not refetch English from provider.
            with patch.object(hist, 'collect', return_value=(papers, [], [])), \
                 patch.object(hist.ReliableDelivery, '_get_translation', side_effect=ValueError('still failed')):
                hist.backfill(root, max_papers=1, lookup=lambda doi:self.fail('duplicate DOI HTTP lookup'))

    def test_complete_english_without_chinese_is_exposed_as_english_only(self):
        en = 'Complete source Abstract: data 1, 2, 3. A final conclusion with original limitations.'
        page, count = cards.build_bilingual_html(example_html(), {'summaries':{}},
                   {'abstracts':{'10.1000/abc':{'abstract':en,'source':'Crossref'}}})
        a = json.loads(cards.DATA_PATTERN.search(page).group(2))['articles'][0]
        self.assertEqual(count, 0)
        self.assertEqual(a['enAbstract'], en)
        self.assertNotIn('zhAbstract', a)
        self.assertIn('Complete source Abstract: data 1, 2, 3.', page)

    def test_sqlite_history_deletion_is_preview_first_and_backup_is_created(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td)/'seen.sqlite'
            with sqlite3.connect(db) as c:
                c.executescript('CREATE TABLE articles (uid TEXT PRIMARY KEY, first_seen TEXT);CREATE TABLE runs(id INTEGER PRIMARY KEY);')
                c.executemany('INSERT INTO articles VALUES (?,?)', [('a','2026-10-10'),('b','2026-10-21')])
            preview = inspect_or_reset(db, before='2026-10-21')
            self.assertEqual(preview['records_selected'], 1)
            with sqlite3.connect(db) as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM articles').fetchone()[0], 2)
            with self.assertRaisesRegex(ValueError,'Refusing deletion'):
                inspect_or_reset(db, before='2026-10-21', apply=True)
            result = inspect_or_reset(db, before='2026-10-21', apply=True,
                                      confirm='DELETE_HISTORY_AFTER_BACKUP')
            self.assertEqual(result['remaining'], 1)
            backup = Path(result['backup'])
            self.assertTrue(backup.is_file())
            with sqlite3.connect(backup) as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM articles').fetchone()[0], 2)

if __name__ == '__main__':
    unittest.main()

class FullnessAuditTests(unittest.TestCase):
    def test_fullness_audit_does_not_treat_excerpt_as_full(self):
        from tools.audit_site_fullness import inspect
        from test_bilingual_cards import example_html
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'index.html'
            path.write_text(example_html(),encoding='utf-8')
            result=inspect(path)
        self.assertEqual(result['card_count'],2)
        self.assertEqual(result['full_english_source_present'],0)
        self.assertEqual(result['english_excerpt_only'],2)
