"""Bounded historical bibliography backfill; NO push channel is touched.

Looks at papers currently shown in reports/ card wall, fetches full English
abstracts by DOI from OpenAlex/Crossref, then translates using existing cached
translation pipeline. Never re-adds them to WeChat/Bark queues or seen.sqlite.
"""
from __future__ import annotations

import argparse
import ipaddress
import hashlib
import html
import json
import logging
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
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



class _PublisherAbstractParser(HTMLParser):
    """Read publisher Abstract metadata, ScholarlyArticle JSON-LD and article body."""
    def __init__(self):
        super().__init__()
        self.abstracts = []
        self.doi = ""
        self._jsonld = False
        self._jsonld_parts = []
        self._abstract_depth = 0
        self._abstract_parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        props = dict((k.lower(), v) for k, v in attrs if k)
        if tag == "meta":
            name = (props.get("name") or props.get("property") or "").lower()
            value = (props.get("content") or "").strip()
            if name in ("citation_abstract", "dc.description", "dc:description",
                        "prism.abstract", "article:abstract", "description.abstract") and value:
                self.abstracts.append(value)
            if name in ("citation_doi", "dc.identifier") and value.startswith("10."):
                self.doi = doi_key(value)
        if tag == "script":
            self._jsonld = props.get("type", "").lower() == "application/ld+json"
            if self._jsonld:
                self._jsonld_parts = []
            return
        if self._abstract_depth:
            self._abstract_depth += 1
            if tag in ("p", "div", "br"):
                self._abstract_parts.append(" ")
            if tag in ("script", "style"):
                self._skip_depth += 1
        elif tag in ("section", "div", "article"):
            classes = (props.get("class") or "").lower().split()
            element_id = (props.get("id") or "").lower()
            if ("abstract" in classes or "abstract-content" in classes or
                    "abstract-section" in classes or "abstract-text" in classes or
                    element_id in ("abstract", "abstract-section", "abstract-content")):
                self._abstract_depth = 1
                self._abstract_parts = []

    def handle_data(self, data):
        if self._jsonld:
            self._jsonld_parts.append(data)
        elif self._abstract_depth and not self._skip_depth:
            self._abstract_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._jsonld:
            self._jsonld = False
            raw = "".join(self._jsonld_parts)
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                return
            def walk(node):
                if isinstance(node, list):
                    for item in node:
                        walk(item)
                elif isinstance(node, dict):
                    node_type = node.get("@type") or ""
                    if isinstance(node_type, list):
                        paper = any(str(t).lower() in ("scholarlyarticle", "article") for t in node_type)
                    else:
                        paper = str(node_type).lower() in ("scholarlyarticle", "article")
                    if paper and isinstance(node.get("abstract"), str):
                        self.abstracts.append(node["abstract"])
                    for k in ("@graph", "mainEntity"):
                        if k in node:
                            walk(node[k])
            walk(payload)
        if self._abstract_depth and tag != "script":
            if self._skip_depth and tag in ("script", "style"):
                self._skip_depth -= 1
            self._abstract_depth -= 1
            if not self._abstract_depth:
                self.abstracts.append(" ".join(self._abstract_parts))
                self._abstract_parts = []


def _safe_publisher_url(url: str) -> bool:
    """Reject non-public URLs on both initial request and HTTP redirects."""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return False
    if parsed.port not in (None, 443):
        return False
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        if not ipaddress.ip_address(host).is_global:
            return False
    except ValueError:
        pass
    return True


class _SafePublisherRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _safe_publisher_url(newurl):
            raise ValueError("Publisher redirect rejected: non-public HTTPS destination")
        return super().redirect_request(req, fp, code, msg, headers, newurl)



# Public publisher page URLs confirmed against DOI, for cases where doi.org
# redirects are blocked by anti-bot systems. Add only verified exact mappings.
PUBLISHER_ARTICLE_URLS = {
    "10.1016/j.jmmm.2026.174589":
        "https://www.sciencedirect.com/science/article/pii/S030488532600781X",
    "10.1016/j.jmmm.2026.174575":
        "https://www.sciencedirect.com/science/article/pii/S0304885326007675",
    "10.1103/jlhg-yy14":
        "https://journals.aps.org/prapplied/abstract/10.1103/jlhg-yy14",
}

# Journal DOI -> (arXiv ID, paper title, first author), independently
# checked against the public publisher record and arXiv metadata.
ARXIV_MATCHES = {
    "10.1016/j.jmmm.2026.174589":
        ("2605.21287",
         "Designer Quantum States in Magnetic Topological Insulator Multilayers",
         "Deyi Zhuo"),
    "10.1016/j.jmmm.2026.174575":
        ("2607.12740",
         "Antiphase boundary-driven large exchange bias and negative magnetoresistance in NiCo2O4",
         "Biswanath Pramanik"),
}


def _normalized_scientific_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^a-z0-9]", "", normalized)


def arxiv_preprint_abstract(doi: str) -> str:
    """Official arXiv Atom API, with *both* exact title and author verification.

    These selected journal DOI mappings are grounded in public papers. A
    preprint Abstract may differ from the final journal Abstract and is always
    labeled as a preprint-derived version in the recorded source.
    """
    match = ARXIV_MATCHES.get(doi_key(doi))
    if not match:
        return ""
    preprint_id, expected_title, expected_author = match
    endpoint = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode({
        "id_list": preprint_id,
        "max_results": "1",
    })
    req = urllib.request.Request(endpoint, headers={
        "User-Agent": "JournalAlert/1.2 (academic abstract recovery)",
        "Accept": "application/atom+xml",
    })
    with urllib.request.urlopen(req, timeout=13) as response:
        content = response.read(300000)
    root = ET.fromstring(content)
    namespace = "{http://www.w3.org/2005/Atom}"
    for entry in root.findall(namespace + "entry"):
        title = " ".join("".join(entry.find(namespace + "title").itertext()).split()) if entry.find(namespace + "title") is not None else ""
        author = entry.find(namespace + "author")
        first_author = author.find(namespace + "name") if author is not None else None
        first_name = first_author.text.strip() if first_author is not None and first_author.text else ""
        id_node = entry.find(namespace + "id")
        entry_id = (id_node.text or "") if id_node is not None else ""
        if not entry_id.rstrip("/").split("/")[-1].split("v")[0] == preprint_id:
            continue
        if _normalized_scientific_title(title) != _normalized_scientific_title(expected_title):
            continue
        if _normalized_scientific_title(first_name) != _normalized_scientific_title(expected_author):
            continue
        summary = entry.find(namespace + "summary")
        if summary is not None:
            abstract = clean_abstract(" ".join(summary.itertext()))
            if 65 <= len(abstract) <= DEFAULTS["max_input_chars"]:
                return abstract
    return ""



def publisher_abstract(doi: str) -> str:
    """Read only explicit Abstract metadata from official journal pages.

    Try the DOI redirect first; journal-specific confirmed direct page URLs
    are useful when doi.org redirects fail under GitHub-hosted runners.
    """
    d = doi_key(doi)
    if not d or "/" not in d:
        return ""
    urls = ["https://doi.org/" + urllib.parse.quote(d, safe="/")]
    if d in PUBLISHER_ARTICLE_URLS:
        urls.append(PUBLISHER_ARTICLE_URLS[d])
    opener = urllib.request.build_opener(_SafePublisherRedirect())
    for url in urls:
        if not _safe_publisher_url(url):
            continue
        req = urllib.request.Request(url, headers={
            "User-Agent": "JournalAlert/1.2 academic literature monitor",
            "Accept": "text/html,application/xhtml+xml;q=0.9",
        })
        try:
            with opener.open(req, timeout=12) as response:
                final_url = response.geturl()
                if not _safe_publisher_url(final_url):
                    continue
                if "html" not in response.headers.get("Content-Type", "").lower():
                    continue
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    continue
        except (OSError, ValueError, RuntimeError, TypeError):
            continue
        parser = _PublisherAbstractParser()
        parser.feed(raw.decode("utf-8", errors="replace"))
        if parser.doi and parser.doi != d:
            LOG.warning("Publisher metadata DOI mismatch for %s", d[:55])
            continue
        for candidate in sorted(parser.abstracts, key=len, reverse=True):
            full = clean_abstract(candidate)
            if 65 <= len(full) <= DEFAULTS["max_input_chars"]:
                return full
    return ""



def europe_pmc_abstract(doi: str) -> str:
    """Free Europe PMC DOI search: require an exact returned DOI, no fuzzy title."""
    d = doi_key(doi)
    if not d or "/" not in d:
        return ""
    params = urllib.parse.urlencode({
        "query": 'DOI:"' + d.replace('"', '') + '"',
        "resultType": "core",  # lite/idlist responses do NOT have abstracts
        "format": "json",
        "pageSize": "5",
    })
    payload = _get_json("https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + params)
    for entry in payload.get("resultList", {}).get("result", []):
        if doi_key(entry.get("doi", "")) != d:
            continue
        full = clean_abstract(entry.get("abstractText", ""))
        if 65 <= len(full) <= DEFAULTS["max_input_chars"]:
            return full
    return ""


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
    # An additional free source can provide abstracts for interdisciplinary
    # material-science papers indexed via PubMed / Europe PMC.
    try:
        full = europe_pmc_abstract(d)
        if full:
            return full, "Europe PMC"
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        LOG.info("Europe PMC abstract unavailable for %s (%s)", d[:45], type(exc).__name__)
    # Official publisher landing pages can expose abstracts omitted by APIs.
    try:
        full = publisher_abstract(d)
        if full:
            return full, "Publisher DOI page"
    except (OSError, RuntimeError, ValueError, TypeError, UnicodeError) as exc:
        LOG.info("Publisher DOI abstract unavailable for %s (%s)", d[:45], type(exc).__name__)
    # Last resort: confirmed journal DOI <> arXiv mapping with exact title
    # and first-author agreement. Source notes MUST state preprint provenance.
    try:
        preprint = arxiv_preprint_abstract(d)
        if preprint:
            return preprint, "arXiv preprint (title and first author verified)"
    except (OSError, RuntimeError, ValueError, ET.ParseError, TypeError) as exc:
        LOG.info("arXiv DOI fallback unavailable for %s (%s)", d[:45], type(exc).__name__)
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
