"""Safe-patch regression tests; no network, no push, no state deletion."""
from __future__ import annotations

import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.apply_repository_fixes import prepare_changes
from tools.generate_curated_config import KEYWORDS, modified_config
from tools.recover_pending_abstracts import recover_pending_abstracts


FLOW = '''name: journal-alert daily
  workflow_dispatch:
    inputs:
      cards_only:
        description: "仅重建双语网页，不检索、不推送"
        type: boolean
        default: false
      test_bark:
        type: boolean
  steps:
          MODE_CARDS: ${{ inputs.cards_only }}
          [[ "$MODE_CARDS" == "true" ]] && selected+=(cards_only) || true
          EXTRA_CARDS_ONLY: ${{ inputs.cards_only }}
          elif [[ "$EXTRA_FLUSH_ONLY" == "true" ]]; then
'''
FETCH = '''def fetch_openalex(journal: dict, since: str, per_page: int, log, timeout: int = 45, retries: int = 1) -> list[Item]:
    return []


def fetch_crossref(journal: dict, since: str, rows: int, log, timeout: int = 30, retries: int = 1) -> list[Item]:
    return []
'''
CARDS = '''def write_cards():
    if not articles:
        if log:
            log.info("cards page skipped: no reports found in %s", reports_dir)
        return None
    result = build_html()
'''
RUN = '''def main(args):
    log.info("fetched %d unique items (%d source calls failed)", len(items), len(failures))
    if args.check:
        return 0
'''
RELIABLE = '''def cap():
        return min((bark_cap if "bark" in channels else 6400), wechat_cap * 2)
'''


class SourcePatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, content in {
            'jalert/fetch.py': FETCH, 'jalert/cards.py': CARDS, 'run.py': RUN,
            'jalert/reliable_delivery.py': RELIABLE, '.github/workflows/daily.yml': FLOW,
            'README.md': '# 大气环境文献日报（journal-alert）\n旧版说明\n',
        }.items():
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding='utf-8')

    def test_preview_does_not_write_any_source(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        changes = prepare_changes(self.root)
        self.assertEqual(len(changes), 6)
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_patch_is_idempotent(self):
        changes = prepare_changes(self.root)
        for path, code in changes.items():
            path.write_text(code)
        self.assertEqual(prepare_changes(self.root), {})

    def test_workflow_rebuild_does_not_call_queue(self):
        flow = prepare_changes(self.root)[self.root / '.github/workflows/daily.yml']
        snippet = flow.split('elif [[ "$EXTRA_REBUILD_ONLY" == "true" ]]', 1)[1].split('elif [[ "$EXTRA_FLUSH_ONLY"', 1)[0]
        self.assertIn('python run.py', snippet)
        self.assertIn('--no-push', snippet)
        self.assertNotIn('reliable_push.py', snippet)
        self.assertNotIn('enhanced_run.py', snippet)

    def test_source_health_all_failed_is_visible(self):
        run = prepare_changes(self.root)[self.root / 'run.py']
        self.assertIn('not succeeded', run)
        self.assertIn('raise RuntimeError', run)

    def test_fetch_both_apis_paginate_200_records(self):
        from dataclasses import dataclass
        from datetime import date
        import urllib
        import json as json_lib
        data = prepare_changes(self.root)[self.root / 'jalert/fetch.py']
        class Item:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)
        calls = []
        def fake_http(url, **kwargs):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            take = int(query.get('per-page', query.get('rows'))[0])
            page = int(query.get('page', ['1'])[0])
            offset = int(query.get('offset', ['0'])[0])
            calls.append(url)
            if 'openalex.org' in url:
                works = [{'title': f'Paper {n}', 'doi': f'https://doi.org/10.7/{n}',
                          'publication_date': '2026-10-21'}
                         for n in range((page-1)*take, (page-1)*take + take)]
                return 200, json_lib.dumps({'results': works}).encode()
            works = [{'title': [f'Paper {n}'], 'DOI': f'10.7/{n}',
                      'issued': {'date-parts': [[2026, 10, 21]]}}
                     for n in range(offset, offset+take)]
            return 200, json_lib.dumps({'message': {'items': works}}).encode()
        scope = {
            'Item': Item, 'MAILTO': 'test@example.org', 'date': date,
            'http_request': fake_http, 'FetchError': RuntimeError,
            'json': json_lib, 'urllib': urllib,
            '_decode': lambda x:x.decode(), '_clean': lambda x:x or '',
            '_inverted_abstract': lambda x:'', '_parse_date': lambda x:x,
            'make_uid': lambda doi,url,title:doi or title,
        }
        exec(compile(data, 'fetch_patch_test.py', 'exec'), scope)
        journal={'issn':'1234-5678','name':'Test Journal'}
        a=scope['fetch_openalex'](journal,'2026-10-19',200,None)
        b=scope['fetch_crossref'](journal,'2026-10-19',200,None)
        self.assertEqual(len(a), 200)
        self.assertEqual(len(b), 200)
        self.assertEqual(len(calls), 4)
        self.assertIn('page=2', calls[1]);self.assertIn('offset=100', calls[3])


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
