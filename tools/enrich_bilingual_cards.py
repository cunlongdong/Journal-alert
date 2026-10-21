"""Post-process journal-alert static card wall with cached English/Chinese abstracts.

No network, no model calls. Works on already-generated docs/index.html.
Safe to rerun; cached translations only appear for articles translated since this
feature was installed. Historical un-translated articles remain English-first.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = "JALERT_BILINGUAL_V1"
DATA_PATTERN = re.compile(r'(<script type="application/json" id="DATA">)(.*?)(</script>)', re.S)

# The upstream template may use older CSS for masonry; the existing equal-card
# override is left intact. These additional rules fit bilingual previews inside it.
CSS = r"""
/* JALERT_BILINGUAL_V1: English / Chinese bilingual paper previews */
.bilingual-preview { margin-top: 6px; min-width: 0; }
.bilingual-preview .language { display:block; color:#667085; font-size:10.5px;
  font-weight:750; letter-spacing:.15px; margin-bottom:2px; }
.bilingual-preview .excerpt { display:-webkit-box; -webkit-line-clamp:2;
  -webkit-box-orient:vertical; overflow:hidden; margin:0;
  font-size:11.5px; line-height:1.45; color:var(--ink2); }
.bilingual-preview.absent .excerpt { color:var(--ink3); font-style:italic; }
.bilingual-section .sec-t { margin-top:15px; }
.bilingual-section .abs { overflow-wrap:anywhere; }
.english-title { display:block; font-size:0.76em; font-weight:500; color:var(--ink3); padding-top:3px; }
"""

# Match template verbatim (as checked against 2026-10-21 upstream cards.py).
OLD_CARD = """    var ab = a.abstract
      ? '<p class="excerpt">'+esc(trunc(a.abstract,200))+'</p>'
      : '<p class="excerpt none">日报未收录摘要'+(a.matchTerm?'（命中词 '+esc(a.matchTerm)+'）':'')+' · 点击看原文</p>';"""
NEW_CARD = """    var chinese = a.zhAbstract || '';
    var english = a.enAbstract || a.abstract || '';
    var ab = chinese
      ? '<div class="bilingual-preview"><span class="language">中文直译</span><p class="excerpt">'+esc(trunc(chinese,110))+'</p></div>'
      : '<div class="bilingual-preview absent"><span class="language">中文直译</span><p class="excerpt">译文暂缺；历史文献可后续补译</p></div>';
    ab += english
      ? '<div class="bilingual-preview"><span class="language">English Abstract</span><p class="excerpt">'+esc(trunc(english,125))+'</p></div>'
      : '<div class="bilingual-preview absent"><span class="language">English Abstract</span><p class="excerpt">英文摘要暂缺 · 点击原文</p></div>';"""
OLD_MODAL = """      +   '<div class="sec-t">摘要</div>'
      +   (a.abstract
            ? '<p class="abs">'+esc(a.abstract)+'</p>'
            : '<p class="abs" style="color:#9d9da8">该条日报未收录摘要。</p>')"""
NEW_MODAL = """      +   '<div class="bilingual-section"><div class="sec-t">中文摘要（忠实直译）</div>'
      +   (a.zhAbstract
            ? '<p class="abs">'+esc(a.zhAbstract)+'</p>'
            : '<p class="abs" style="color:#9d9da8">尚未生成中文译文。仅对升级后新发现的论文自动翻译；历史论文可另行补译。</p>')
      +   '<div class="sec-t">English Abstract '+(a.enAbstract?'（原始全文）':'（日报摘录，可能不完整）')+'</div>'
      +   ((a.enAbstract || a.abstract)
            ? '<p class="abs">'+esc(a.enAbstract || a.abstract)+'</p>'
            : '<p class="abs" style="color:#9d9da8">原数据源未提供英文摘要。</p>')
      +   '</div>'"""
OLD_SEARCH = "a.title+' '+a.abstract+' '+a.authors.join(' ')"
NEW_SEARCH = "(a.zhTitle||'')+' '+a.title+' '+a.abstract+' '+(a.enAbstract||'')+' '+(a.zhAbstract||'')+' '+a.authors.join(' ')"


def doi_key(doi: str) -> str:
    d = str(doi or '').strip().lower()
    d = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', d)
    return d.removeprefix('doi:').strip()


def title_key(title: str) -> str:
    return re.sub(r'\s+', ' ', str(title or '')).strip().casefold()


def cache_index(cache: dict) -> tuple[dict, dict]:
    by_doi, by_title = {}, {}
    for record in (cache.get('summaries') or {}).values():
        if not isinstance(record, dict) or record.get('mode') != 'literal-full-translation':
            continue
        if not isinstance(record.get('summary'), str) or not record['summary'].strip():
            continue
        if not record.get('title'):
            continue
        key = doi_key(record.get('doi', ''))
        if key:
            by_doi[key] = record
        t = title_key(record['title'])
        if t:
            by_title[t] = record
    return by_doi, by_title


def _replace_once(page: str, old: str, new: str, label: str) -> str:
    count = page.count(old)
    if count != 1:
        raise ValueError(f'Unable to patch {label}: expected one occurrence, found {count}. '
                         'Upstream cards.py may have changed. No file was modified.')
    return page.replace(old, new, 1)


def build_bilingual_html(page: str, cache: dict, english_store: dict | None = None,
                         metadata_store: dict | None = None) -> tuple[str, int]:
    found = DATA_PATTERN.search(page)
    if not found:
        raise ValueError('Card wall is missing the required DATA script; no file modified')
    data = json.loads(found.group(2))
    articles = data.get('articles')
    if not isinstance(articles, list):
        raise ValueError('Unexpected card DATA schema; no file modified')
    doi_map, title_map = cache_index(cache)
    translated_count = 0
    full_english = (english_store or {}).get("abstracts", {})
    if not isinstance(full_english, dict):
        raise ValueError("Unexpected full English abstract cache format")
    all_metadata = (metadata_store or {}).get("articles", {})
    if not isinstance(all_metadata, dict):
        raise ValueError("Unexpected metadata ledger schema")
    for paper in articles:
        paper_doi = doi_key(paper.get('doi', ''))
        rec = doi_map.get(paper_doi) if paper_doi else None
        if not rec:
            candidate = title_map.get(title_key(paper.get('title', '')))
            # Do not attach another DOI's translation based on a similar title.
            if candidate and (not paper_doi or not doi_key(candidate.get('doi')) or
                              paper_doi == doi_key(candidate.get('doi'))):
                rec = candidate
        source_record = full_english.get(paper_doi) if paper_doi else None
        complete_en = (rec.get('english_abstract') if rec else None) or (
            source_record.get('abstract') if isinstance(source_record, dict) else None)
        # The API metadata ledger is durable and can contain a better source
        # abstract found during a later run. Do not use report excerpts as full.
        observed = all_metadata.get("doi:" + paper_doi) if paper_doi else None
        observed_en = str(observed.get("abstract") or "") if isinstance(observed, dict) else ""
        if len(observed_en) > len(str(complete_en or "")):
            complete_en = observed_en
            paper["enAbstractSource"] = observed.get("abstract_source") or "source API"
        elif source_record and isinstance(source_record, dict) and complete_en:
            paper["enAbstractSource"] = source_record.get("source") or "verified DOI"
        elif rec and complete_en:
            paper["enAbstractSource"] = rec.get("provider") or "translated source"
        if complete_en:
            paper['enAbstract'] = complete_en
        else:
            paper.pop('enAbstract', None)
            paper.pop("enAbstractSource", None)
        if isinstance(observed, dict) and len(observed.get("authors") or []) > len(paper.get("authors") or []):
            paper["authors"] = observed["authors"]
        # Titles have a separate verified cache, even without an abstract.
        title_hash = hashlib.sha256(str(paper.get("title", "")).strip().encode("utf-8")).hexdigest()
        title_record = (cache.get("titles") or {}).get(title_hash, {})
        zh_title = (rec.get("zh_title") if rec else None) or title_record.get("zh_title")
        if zh_title:
            paper["zhTitle"] = zh_title
        else:
            paper.pop("zhTitle", None)
        if rec and (not complete_en or
                    str(rec.get("english_abstract") or "").strip() == str(complete_en).strip()):
            paper['zhAbstract'] = rec['summary']
            translated_count += 1
        else:
            # Never display a translation of a different/obsolete Abstract
            # beside a newly recovered original English source.
            paper.pop('zhAbstract', None)
    content = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    content = content.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    page = page[:found.start(2)] + content + page[found.end(2):]
    if MARKER not in page:
        page = _replace_once(page, OLD_CARD, NEW_CARD, 'bilingual card previews')
        page = _replace_once(page, OLD_MODAL, NEW_MODAL, 'bilingual modal')
        page = _replace_once(page, OLD_SEARCH, NEW_SEARCH, 'Chinese search')
        page = _replace_once(page, '</style>', CSS + '\n</style>', 'bilingual CSS')
    return page, translated_count


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description='Enrich generated journal-alert cards with both languages')
    p.add_argument('--cards', type=Path, default=ROOT / 'docs' / 'index.html')
    p.add_argument('--cache', type=Path, default=ROOT / 'state' / 'enhanced_zh_cache.json')
    p.add_argument('--english-cache', type=Path, default=ROOT / 'state' / 'full_english_abstracts.json')
    p.add_argument('--metadata', type=Path, default=ROOT / 'state' / 'article_metadata.json')
    args = p.parse_args(argv)
    if not args.cards.is_file():
        print(f'No generated card wall at {args.cards}; nothing to enrich')
        return 0
    cache = json.loads(args.cache.read_text('utf-8')) if args.cache.is_file() else {'summaries': {}}
    current = args.cards.read_text('utf-8')
    english_store = json.loads(args.english_cache.read_text('utf-8')) if args.english_cache.is_file() else {'abstracts': {}}
    metadata_store = json.loads(args.metadata.read_text('utf-8')) if args.metadata.is_file() else {'articles': {}}
    updated, translated = build_bilingual_html(current, cache, english_store, metadata_store)
    if updated != current:
        args.cards.write_text(updated, encoding='utf-8', newline='\n')
    print(f'Bilingual wall ready: {translated} cards with Chinese translations; '
          'other cards retain available English and show translation pending.')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
