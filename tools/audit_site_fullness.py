"""Read-only: inspect completeness of English/Chinese Abstracts on published card wall."""
from __future__ import annotations
import argparse
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
PATTERN = re.compile(r'<script type="application/json" id="DATA">(.*?)</script>', re.S)


def inspect(path: Path) -> dict:
    doc = path.read_text(encoding='utf-8')
    found = PATTERN.search(doc)
    if not found:
        raise ValueError('No JSON DATA payload found in docs/index.html')
    data = json.loads(found.group(1))
    papers = data['articles']
    en = sum(bool(p.get('enAbstract')) for p in papers)
    zh = sum(bool(p.get('zhAbstract')) for p in papers)
    both = sum(bool(p.get('enAbstract')) and bool(p.get('zhAbstract')) for p in papers)
    return {
        'card_count': len(papers),
        'full_english_source_present': en,
        'full_chinese_translation_present': zh,
        'both_full': both,
        'english_excerpt_only': sum(not p.get('enAbstract') and bool(p.get('abstract')) for p in papers),
        'no_english_abstract': sum(not p.get('enAbstract') and not p.get('abstract') for p in papers),
        'pending_chinese_count': len(papers) - zh,
        'sample_missing': [{'title': p.get('title','')[:75], 'doi': p.get('doi', '')}
                           for p in papers if not p.get('enAbstract') or not p.get('zhAbstract')][:10],
    }


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--cards',type=Path,default=HERE/'docs/index.html')
    args=parser.parse_args(argv)
    print(json.dumps(inspect(args.cards),ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
