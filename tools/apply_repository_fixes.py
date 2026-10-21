"""Targeted, idempotent source patches for Journal-alert (2026-10-21).

Preview by default. Does NOT modify reports, state, secrets, SQLite or caches.
Recommended usage on a TEST BRANCH:
    python tools/apply_repository_fixes.py
    python tools/apply_repository_fixes.py --apply
"""
from __future__ import annotations

import argparse
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

OPENALEX = '''def fetch_openalex(journal: dict, since: str, per_page: int, log, timeout: int = 45, retries: int = 1) -> list[Item]:
    """JALERT_PAGINATION_V1: fetch every bounded page, not just page one."""
    issn = journal.get("issn", "")
    if not issn:
        return []
    limit = max(1, int(per_page))
    items: list[Item] = []
    page = 1
    max_pages = min(10, (limit + 99) // 100 + 2)
    while len(items) < limit and page <= max_pages:
        take = min(100, limit - len(items))
        params = {
            "filter": f"primary_location.source.issn:{issn},from_publication_date:{since},type:article",
            "per-page": str(take),
            "page": str(page),
            "sort": "publication_date:desc",
            "mailto": MAILTO,
        }
        url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
        status, raw = http_request(url, timeout=timeout, retries=retries)
        if status != 200:
            raise FetchError(f"OpenAlex HTTP {status} (page {page})")
        payload = json.loads(_decode(raw))
        works = payload.get("results", [])
        if not isinstance(works, list):
            raise FetchError("OpenAlex results has invalid format")
        for work in works:
            title = _clean(work.get("title") or work.get("display_name") or "")
            if not title:
                continue
            doi = (work.get("doi") or "").replace("https://doi.org/", "").strip().lower()
            location = work.get("primary_location") or {}
            landing = location.get("landing_page_url") or (work.get("doi") or "")
            authors = [
                _clean((a.get("author") or {}).get("display_name"))
                for a in (work.get("authorships") or [])[:12]
            ]
            items.append(Item(
                uid=make_uid(doi, landing, title), title=title, url=landing or "",
                doi=doi, journal=journal["name"], issn=issn,
                date=_parse_date(work.get("publication_date")),
                abstract=_clean(_inverted_abstract(work.get("abstract_inverted_index"))),
                authors=[a for a in authors if a], source="openalex",
            ))
        if not works or len(works) < take:
            break
        page += 1
    return items[:limit]
'''

CROSSREF = '''def fetch_crossref(journal: dict, since: str, rows: int, log, timeout: int = 30, retries: int = 1) -> list[Item]:
    """JALERT_PAGINATION_V1: request bounded Crossref pages with offset."""
    issn = journal.get("issn", "")
    if not issn:
        return []
    limit = max(1, int(rows))
    items: list[Item] = []
    offset = 0
    page_count = 0
    max_pages = min(10, (limit + 99) // 100 + 2)
    while len(items) < limit and page_count < max_pages:
        page_count += 1
        take = min(100, limit - len(items))
        params = {
            "filter": f"from-pub-date:{since},type:journal-article",
            "rows": str(take), "offset": str(offset),
            "sort": "published", "order": "desc", "mailto": MAILTO,
        }
        url = f"https://api.crossref.org/journals/{issn}/works?" + urllib.parse.urlencode(params)
        status, raw = http_request(url, timeout=timeout, retries=retries)
        if status != 200:
            raise FetchError(f"Crossref HTTP {status} (offset {offset})")
        payload = json.loads(_decode(raw))
        works = payload.get("message", {}).get("items", [])
        if not isinstance(works, list):
            raise FetchError("Crossref items has invalid format")
        for work in works:
            titles = work.get("title") or []
            title = _clean(titles[0]) if titles else ""
            if not title:
                continue
            doi = (work.get("DOI") or "").lower()
            url_value = work.get("URL") or (f"https://doi.org/{doi}" if doi else "")
            published = (
                work.get("published-online") or work.get("published") or
                work.get("published-print") or work.get("issued") or {}
            )
            parts = list((published.get("date-parts") or [[None]])[0] or [])
            date_value = ""
            if parts and parts[0]:
                year = int(parts[0]); month = int(parts[1]) if len(parts) > 1 and parts[1] else 1
                day = int(parts[2]) if len(parts) > 2 and parts[2] else 1
                try:
                    date_value = date(year, month, day).isoformat()
                except ValueError:
                    date_value = ""
            authors = []
            for author in (work.get("author") or [])[:12]:
                name = " ".join(x for x in (author.get("given"), author.get("family")) if x)
                if name:
                    authors.append(_clean(name))
            items.append(Item(
                uid=make_uid(doi, url_value, title), title=title, url=url_value,
                doi=doi, journal=journal["name"], issn=issn, date=date_value,
                abstract=_clean(work.get("abstract")), authors=authors, source="crossref",
            ))
        if not works or len(works) < take:
            break
        offset += len(works)
    return items[:limit]
'''


def replace_function(text: str, name: str, replacement: str, marker: str) -> str:
    tree = ast.parse(text)
    funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    if len(funcs) != 1:
        raise ValueError(f"Expected exactly one {name} function, found {len(funcs)}")
    node = funcs[0]
    lines = text.splitlines(keepends=True)
    original = "".join(lines[node.lineno-1:node.end_lineno])
    if marker in original:
        return text  # idempotent
    return "".join(lines[:node.lineno-1]) + replacement + "\n" + "".join(lines[node.end_lineno:])


def replace_once(text: str, before: str, after: str, description: str) -> str:
    if after in text:
        return text
    count = text.count(before)
    if count != 1:
        raise ValueError(f"Cannot safely patch {description}: expected 1 anchor, found {count}")
    return text.replace(before, after, 1)


def prepare_changes(root: Path) -> dict[Path, str]:
    root = Path(root)
    changes: dict[Path, str] = {}

    fetch = root / 'jalert/fetch.py'
    src = fetch.read_text('utf-8')
    src = replace_function(src, 'fetch_openalex', OPENALEX, 'JALERT_PAGINATION_V1')
    src = replace_function(src, 'fetch_crossref', CROSSREF, 'JALERT_PAGINATION_V1')
    ast.parse(src)
    if src != fetch.read_text('utf-8'):
        changes[fetch] = src

    cards = root / 'jalert/cards.py'
    src = cards.read_text('utf-8')
    before = '''    if not articles:
        if log:
            log.info("cards page skipped: no reports found in %s", reports_dir)
        return None
'''
    after = '''    if not articles:
        if log:
            log.info("cards page rebuilt empty: no reports found in %s", reports_dir)
        # JALERT_EMPTY_WALL_V1: overwrite stale HTML even when reports are empty.
'''
    if 'cards page rebuilt empty:' not in src:
        src = replace_once(src, before, after, 'empty card wall')
    ast.parse(src)
    if src != cards.read_text('utf-8'):
        changes[cards] = src

    run = root / 'run.py'
    src = run.read_text('utf-8')
    before = '''    log.info("fetched %d unique items (%d source calls failed)", len(items), len(failures))
'''
    after = before + '''    # JALERT_SOURCE_HEALTH_V1: a green job must not conceal total API failure.
    tried = [s for s in statuses if s.get("source") in ("rss", "openalex", "crossref")]
    succeeded = sum(s.get("ok") is True for s in tried)
    failed = sum(s.get("ok") is False for s in tried)
    if failed:
        log.warning("journal source health: %d succeeded, %d failed", succeeded, failed)
        summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_file:
            with open(summary_file, "a", encoding="utf-8") as out:
                out.write(f"\\n### 文献数据源部分失败\\n成功 {succeeded}，失败 {failed}；请检查抓取日志。\\n")
    if not args.check and tried and not succeeded:
        raise RuntimeError("All journal data-source requests failed; aborting before dedupe, report and push")
'''
    if 'source health: %d succeeded' not in src:
        src = replace_once(src, before, after, 'source health')
    ast.parse(src)
    if src != run.read_text('utf-8'):
        changes[run] = src

    reliable = root / 'jalert/reliable_delivery.py'
    src = reliable.read_text('utf-8')
    before = '''        return min((bark_cap if "bark" in channels else 6400), wechat_cap * 2)'''
    after = '''        # JALERT_PUSH_CAP_V1: bytes <= character limit for all-ASCII messages.
        return min((bark_cap if "bark" in channels else wechat_cap), wechat_cap)'''
    if 'return min((bark_cap if "bark" in channels else wechat_cap), wechat_cap)' not in src:
        src = replace_once(src, before, after, 'message size bound')
    ast.parse(src)
    if src != reliable.read_text('utf-8'):
        changes[reliable] = src

    flow = root / '.github/workflows/daily.yml'
    src = flow.read_text('utf-8')
    if '      rebuild_only:' not in src:
        src = replace_once(src,
        '''      cards_only:
        description: "仅重建双语网页，不检索、不推送"
        type: boolean
        default: false
''',
        '''      cards_only:
        description: "仅重建双语网页，不检索、不推送"
        type: boolean
        default: false
      rebuild_only:
        description: "安全重建：重新检索并写日报和去重库，但不入队、不推送微信/Bark"
        type: boolean
        default: false
''', 'rebuild workflow input')
        src = replace_once(src,
        '''          MODE_CARDS: ${{ inputs.cards_only }}
''',
        '''          MODE_CARDS: ${{ inputs.cards_only }}
          MODE_REBUILD: ${{ inputs.rebuild_only }}
''', 'rebuild validation env')
        src = replace_once(src,
        '''          [[ "$MODE_CARDS" == "true" ]] && selected+=(cards_only) || true
''',
        '''          [[ "$MODE_CARDS" == "true" ]] && selected+=(cards_only) || true
          [[ "$MODE_REBUILD" == "true" ]] && selected+=(rebuild_only) || true
''', 'rebuild validation')
        src = replace_once(src,
        '''          EXTRA_CARDS_ONLY: ${{ inputs.cards_only }}
''',
        '''          EXTRA_CARDS_ONLY: ${{ inputs.cards_only }}
          EXTRA_REBUILD_ONLY: ${{ inputs.rebuild_only }}
''', 'rebuild execution env')
        src = replace_once(src,
        '''          elif [[ "$EXTRA_FLUSH_ONLY" == "true" ]]; then
''',
        '''          elif [[ "$EXTRA_REBUILD_ONLY" == "true" ]]; then
            # JALERT_REBUILD_NO_QUEUE_V1: upstream run.py never touches the dual-channel queue.
            rebuild_args=(--once --no-push)
            if [[ -n "$EXTRA_DAYS" ]]; then rebuild_args+=(--days "$EXTRA_DAYS"); fi
            if [[ -n "$EXTRA_SOURCES" ]]; then rebuild_args+=(--sources "$EXTRA_SOURCES"); fi
            python run.py "${rebuild_args[@]}"
          elif [[ "$EXTRA_FLUSH_ONLY" == "true" ]]; then
''', 'safe no-queue rebuild')
    if src != flow.read_text('utf-8'):
        changes[flow] = src

    readme = root / 'README.md'
    src = readme.read_text('utf-8')
    old_heading = '# 大气环境文献日报（journal-alert）'
    marker = '<!-- JALERT_SPINTRONICS_README_V1 -->'
    if marker not in src:
        if not src.startswith(old_heading):
            raise ValueError('README header changed unexpectedly; no automatic rewrite')
        lead = '''# 自旋电子学文献日报（Journal-alert）

<!-- JALERT_SPINTRONICS_README_V1 -->

**当前云端版本（2026-10）**：52 本期刊，面向 MTJ/TMR、STT/SOT-MRAM、交换偏置、反铁磁、斯格明子、磁振子及自旋存算。
根据公开的韩秀峰、于国强、赵巍胜等研究方向设计学术关键词。

- 自动抓取 OpenAlex / Crossref，保存 `state/seen.sqlite` 并按 DOI/标题去重。
- GitHub Actions 每天运行；微信（Server酱）与 Bark 使用同一份中文全文直译分页消息，分别记录进度。
- GitHub Pages 显示中英文摘要；如学术数据源缺少原始英文摘要，网页会明确显示“原文摘要缺失”，不会编造。
- **仅重建不推送也不入队：Actions 中单选 `rebuild_only`。** 旧的 `no_push` 会入队但不立即发送，不能用于清空历史后的安全重建。
- 工作流云端密钥以 GitHub Actions Secrets/Variables 为准。`config.json` 中的早期 PushPlus/Server酱/Bark 配置保留供旧本地入口使用。
- 不建议直接删除 `state/seen.sqlite`、`state/enhanced_wechat_outbox.json` 和 `state/dual_channel_delivery.json`；先备份再单独设计清理流程。

---

## 历史版使用说明（以下内容保留兼容，部分旧期刊/气象示例不适用于当前项目）

'''
        changes[readme] = lead + src
    return changes


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description='Preview/apply safe Journal-alert fixes to checkout')
    p.add_argument('--apply', action='store_true')
    p.add_argument('--root', type=Path, default=ROOT)
    args = p.parse_args(argv)
    changes = prepare_changes(args.root)
    if not changes:
        print('All targeted fixes are already applied.')
        return 0
    for path, content in changes.items():
        print(('APPLY ' if args.apply else 'PREVIEW ') + str(path.relative_to(args.root)))
        if args.apply:
            path.write_text(content, encoding='utf-8', newline='\n')
    if not args.apply:
        print('Read-only preview: no files changed. Add --apply to modify source files.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
