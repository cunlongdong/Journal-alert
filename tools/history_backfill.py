"""Bounded historical bibliography backfill; NO push channel is touched.

Looks at papers currently shown in reports/ card wall, fetches full English
abstracts by DOI from OpenAlex/Crossref, then translates using existing cached
translation pipeline. Never re-adds them to WeChat/Bark queues or seen.sqlite.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from jalert.cards import collect
from jalert.reliable_delivery import ReliableDelivery
from jalert.enhanced_delivery import DEFAULTS, _atomic_json
from tools.enrich_bilingual_cards import cache_index, doi_key, title_key

LOG = logging.getLogger("journal-alert-backfill")


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
    def handle_data(self, data):
        self.parts.append(data)


def clean_abstract(value: str) -> str:
    p = _HTMLText()
    p.feed(str(value or ""))
    return " ".join(html.unescape(" ".join(p.parts)).split())


def from_inverted(index: dict | None) -> str:
    if not isinstance(index, dict) or not index:
        return ""
    slots = {}
    for word, indices in index.items():
        if isinstance(indices, list):
            for i in indices:
                if isinstance(i, int) and 0 <= i < 30000:
                    slots[i] = word
    if not slots:
        return ""
    return " ".join(slots.get(i, "") for i in range(max(slots) + 1)).strip()


def _get_json(url: str, *, headers: dict | None = None) -> dict:
    request_headers = {"User-Agent": "journal-alert-history-backfill/1.1 (academic citation alerts)",
                       "Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(req, timeout=18) as response:
        return json.loads(response.read(5 * 1024 * 1024).decode("utf-8"))


def lookup_english_abstract(doi: str) -> tuple[str, str]:
    d = doi_key(doi)
    if not d or "/" not in d:
        return "", ""
    url = "https://api.openalex.org/works/" + urllib.parse.quote("https://doi.org/" + d, safe="")
    try:
        work = _get_json(url)
        full = clean_abstract(from_inverted(work.get("abstract_inverted_index")))
        if full:
            return full, "OpenAlex"
    except (OSError, ValueError, KeyError, RuntimeError) as e:
        LOG.info("OpenAlex DOI fallback for %s (%s)", d[:40], type(e).__name__)
    try:
        work = _get_json("https://api.crossref.org/works/" + urllib.parse.quote(d, safe=""))
        full = clean_abstract(work.get("message", {}).get("abstract", ""))
        if full:
            return full, "Crossref"
    except (OSError, ValueError, KeyError, RuntimeError) as e:
        LOG.info("Crossref abstract unavailable %s (%s)", d[:40], type(e).__name__)
    # Fallback for works whose abstracts are unavailable in OpenAlex/Crossref.
    # Exact DOI validation avoids linking a different paper's abstract to a card.
    if os.getenv("JALERT_SEMANTIC_SCHOLAR_ENABLED", "true").lower() in ("true", "1", "yes"):
        paper_id = urllib.parse.quote("DOI:" + d, safe="")
        endpoint = ("https://api.semanticscholar.org/graph/v1/paper/" + paper_id
                    + "?fields=title,abstract,externalIds")
        key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip()
        extra_headers = {"x-api-key": key} if key else None
        try:
            entry = _get_json(endpoint, headers=extra_headers)
            external = entry.get("externalIds") or {}
            returned_doi = doi_key(external.get("DOI", "")) if isinstance(external, dict) else ""
            abstract = clean_abstract(entry.get("abstract", ""))
            if returned_doi == d and abstract:
                return abstract, "Semantic Scholar"
            LOG.info("Semantic Scholar result without matching DOI/abstract: %s", d[:40])
        except (OSError, ValueError, KeyError, RuntimeError, TypeError) as e:
            LOG.info("Semantic Scholar abstract unavailable for %s (%s)", d[:40], type(e).__name__)
    return "", ""


def backfill(root: Path, max_papers: int = 3, dry_run: bool = False, *, lookup=lookup_english_abstract, log=LOG) -> dict:
    root = Path(root)
    delivery = ReliableDelivery(root, log)
    articles, _reports, _topics = collect(root / "reports")
    by_doi, by_title = cache_index(delivery.cache)
    english_path = root / "state" / "full_english_abstracts.json"
    if english_path.exists():
        english_store = json.loads(english_path.read_text(encoding="utf-8"))
        if not isinstance(english_store, dict) or not isinstance(english_store.get("abstracts"), dict):
            raise ValueError("Malformed full English abstract store; refusing overwrite")
    else:
        english_store = {"version": 1, "abstracts": {}}
    remaining = []
    for paper in articles:
        doi = doi_key(paper.get("doi", ""))
        title = title_key(paper.get("title", ""))
        record = by_doi.get(doi) if doi else by_title.get(title)
        title_hash = hashlib.sha256(str(paper.get("title") or "").strip().encode("utf-8")).hexdigest()
        title_done = bool((delivery.cache.get("titles") or {}).get(title_hash, {}).get("zh_title"))
        english_done = bool(record and record.get("english_abstract") and record.get("summary"))
        if title_done and english_done:
            continue
        remaining.append(paper)
    attempted = translated = missing = failed = 0
    day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    # A missing DOI must not monopolize the first slots on every daily run.
    # The tiny cursor is committed with other state/ files by GitHub Actions.
    cursor_path = root / "state" / "history_backfill_cursor.json"
    cursor = {"version": 1, "next_index": 0}
    if cursor_path.is_file():
        cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
        if cursor.get("version") != 1 or not isinstance(cursor.get("next_index"), int):
            raise ValueError("Malformed historical backfill cursor; refusing to replace")
    start = cursor["next_index"] % len(remaining) if remaining else 0
    ordered = remaining[start:] + remaining[:start]
    for paper in ordered[:max_papers]:
        attempted += 1
        if dry_run:
            continue
        # Persist progress *before* network I/O so an unavailable DOI cannot
        # repeatedly starve the rest after a job error or timeout.
        cursor["next_index"] = (start + attempted) % len(remaining)
        _atomic_json(cursor_path, cursor)
        # A verified title can be translated even when no abstract exists.
        preview = {"uid": "history:" + doi_key(paper.get("doi", "")),
                   "doi": doi_key(paper.get("doi", "")),
                   "title": str(paper.get("title") or ""), "abstract": ""}
        try:
            delivery._translate_title(preview, day)
        except (ValueError, RuntimeError, OSError, KeyError) as title_exc:
            log.warning("Historical title translation pending for %s (%s)",
                        preview["title"][:60], type(title_exc).__name__)
        record = by_doi.get(doi_key(paper.get("doi",""))) if paper.get("doi") else by_title.get(title_key(paper.get("title","")))
        if record and record.get("english_abstract") and record.get("summary"):
            continue  # Older Chinese abstract already exists; update title only.
        norm_doi = doi_key(paper.get("doi", ""))
        if not norm_doi:
            missing += 1
            continue
        existing = english_store["abstracts"].get(norm_doi)
        if isinstance(existing, dict) and existing.get("abstract"):
            english, source = existing["abstract"], existing.get("source", "stored")
        else:
            try:
                english, source = lookup(paper["doi"])
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                failed += 1
                log.warning("Historical source lookup deferred for %s: %s",
                            paper["doi"][:60], type(exc).__name__)
                continue
            if english and len(english) <= DEFAULTS["max_input_chars"]:
                english_store["abstracts"][norm_doi] = {
                    "abstract": english,
                    "source": source,
                    "title": paper.get("title", ""),
                    "retrieved_at": day,
                }
                # Even a failed AI translation must not discard retrieved English.
                _atomic_json(english_path, english_store)
        if not english or len(english) > DEFAULTS["max_input_chars"]:
            missing += 1
            continue
        item = {"uid": "history:" + doi_key(paper["doi"]), "doi": doi_key(paper["doi"]),
                "title": paper["title"], "abstract": english}
        try:
            _ = delivery._get_translation(item, day)
            translated += 1
            log.info("Historical paper translated: %s (%s)", paper["title"][:72], source)
        except (ValueError, RuntimeError, OSError, KeyError) as e:
            failed += 1
            log.warning("Historical DOI translation deferred: %s: %s", paper["doi"][:60], str(e)[:120])
    result = {"cards": len(articles), "full_english_cached": len(english_store["abstracts"]),
              "untranslated_visible_papers": len(remaining),
              "attempted": attempted, "translated": translated, "missing_source_abstract": missing,
              "failed_quality_or_api": failed, "dry_run": dry_run}
    log.info("History backfill (never sends push): %s", json.dumps(result, ensure_ascii=False))
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=3, help="Max historical papers to attempt in this run")
    parser.add_argument("--dry-run", action="store_true")
    ns = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if ns.max < 0 or ns.max > 15:
        parser.error("--max must be 0..15 to bound AI usage")
    backfill(ROOT, ns.max, ns.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
