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
        'chinese_title_present': sum(bool(p.get('zhTitle')) for p in papers),
        'chinese_title_pending': sum(not p.get('zhTitle') for p in papers),
        'missing_chinese_titles': [{'title': p.get('title',''), 'doi': p.get('doi','')}
                                   for p in papers if not p.get('zhTitle')][:30],
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
    result = inspect(args.cards)
    # Include the durable originals and Bark backlog, not only rendered cards.
    metadata_path = HERE / "state" / "article_metadata.json"
    queue_path = HERE / "state" / "enhanced_wechat_outbox.json"
    if metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        originals = metadata.get("articles", {})
        result["durable_article_count"] = len(originals)
        result["durable_original_abstracts"] = sum(
            bool(a.get("abstract")) for a in originals.values() if isinstance(a, dict))
    if queue_path.is_file():
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        pending = [x for x in queue.get("items", {}).values()
                   if isinstance(x, dict) and x.get("status") == "pending"]
        result["bark_pending"] = len(pending)
        result["bark_pending_with_english"] = sum(bool(x.get("abstract")) for x in pending)
        result["bark_pending_details"] = [
            {"doi":x.get("doi",""),"reason":str(x.get("last_ai_error") or
                  x.get("abstract_lookup_status") or "translation pending")[:125]}
            for x in pending[:20]]
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
