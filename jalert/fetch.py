"""Data acquisition: journal RSS/Atom feeds plus OpenAlex and Crossref fallbacks.

Only the Python standard library is used, because on this host the bundled
Python interpreter is the only client with working outbound TLS.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape

USER_AGENT = os.environ.get("JALERT_USER_AGENT") or "Mozilla/5.0 (compatible; JournalAlert/1.0)"
# OpenAlex/Crossref give the "polite pool" (faster, more reliable) to callers that
# identify a contact address, so set a real one via ``JALERT_MAILTO`` rather than
# shipping a placeholder in a public repo.
MAILTO = os.environ.get("JALERT_MAILTO") or "journal-alert@example.com"
TIMEOUT = 30

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_BAD_AMP_RE = re.compile(r"&(?!(?:[a-zA-Z][a-zA-Z0-9]{1,31}|#\d{1,7}|#x[0-9a-fA-F]{1,6});)")
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>)\]]+")


class FetchError(RuntimeError):
    """Raised when a remote source cannot be retrieved."""


@dataclass
class Item:
    uid: str
    title: str
    url: str = ""
    doi: str = ""
    journal: str = ""
    issn: str = ""
    date: str = ""
    abstract: str = ""
    authors: list[str] = field(default_factory=list)
    source: str = ""

    def as_dict(self) -> dict:
        return {
            "uid": self.uid,
            "title": self.title,
            "url": self.url,
            "doi": self.doi,
            "journal": self.journal,
            "issn": self.issn,
            "date": self.date,
            "abstract": self.abstract,
            "authors": self.authors,
            "source": self.source,
        }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def http_request(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict | None = None,
    method: str | None = None,
    timeout: int = TIMEOUT,
    retries: int = 2,
    backoff: float = 1.5,
) -> tuple[int, bytes]:
    """GET/POST a URL, returning ``(status, body)``; retries transient failures."""
    effective_method = (method or ("POST" if data is not None else "GET")).upper()
    if effective_method not in ("GET", "HEAD", "OPTIONS"):
        retries = 0
    last_error = "unknown error"
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("User-Agent", USER_AGENT)
            req.add_header("Accept", "*/*")
            req.add_header("Accept-Encoding", "gzip, deflate")
            for key, value in (headers or {}).items():
                req.add_header(key, value)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                encoding = (resp.headers.get("Content-Encoding") or "").lower()
                if "gzip" in encoding:
                    raw = gzip.decompress(raw)
                elif "deflate" in encoding:
                    try:
                        raw = zlib.decompress(raw)
                    except zlib.error:
                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                return resp.status, raw
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code} {exc.reason}"
            if exc.code not in (408, 425, 429, 500, 502, 503, 504):
                break
        except Exception as exc:  # noqa: BLE001 - network layer, report and retry
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            time.sleep(backoff * (attempt + 1))
    raise FetchError(f"Request failed ({effective_method}): {last_error}")


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _clean(text: str | None) -> str:
    """Strip markup, entities and collapse whitespace."""
    if not text:
        return ""
    text = unescape(text)
    text = _TAG_RE.sub(" ", text)
    text = text.replace("\u00a0", " ")
    return _WS_RE.sub(" ", text).strip()


# --------------------------------------------------------------------------
# Feed parsing
# --------------------------------------------------------------------------


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()


def _parse_date(value: str | None) -> str:
    """Normalise assorted feed/API date formats to ``YYYY-MM-DD``."""
    if not value:
        return ""
    value = value.strip()
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except Exception:  # noqa: BLE001 - many feeds emit non-RFC dates
        pass
    iso = value.replace("Z", "+00:00")
    for candidate in (iso, iso[:19], iso[:10]):
        try:
            return datetime.fromisoformat(candidate).date().isoformat()
        except Exception:  # noqa: BLE001
            continue
    match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", value)
    if match:
        year, month, day = (int(g) for g in match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return ""
    return ""


def _doi_from(*values: str) -> str:
    for value in values:
        if not value:
            continue
        match = _DOI_RE.search(value)
        if match:
            return match.group(0).rstrip(".").lower()
    return ""


def make_uid(doi: str, url: str, title: str) -> str:
    if doi:
        return "doi:" + doi.lower()
    if url:
        cleaned = urllib.parse.urldefrag(url)[0].strip().lower()
        if cleaned:
            return "url:" + cleaned
    digest = hashlib.sha1(_WS_RE.sub(" ", title.lower()).encode("utf-8")).hexdigest()[:20]
    return "title:" + digest


def parse_feed(raw: bytes, journal: dict) -> list[Item]:
    """Parse RSS 2.0, RSS 1.0/RDF or Atom into :class:`Item` objects."""
    text = _BAD_AMP_RE.sub("&amp;", _decode(raw))
    root = ET.fromstring(text)
    root_name = _localname(root.tag)
    if root_name == "feed":
        entries = [el for el in root if _localname(el.tag) == "entry"]
    elif root_name == "rdf":
        entries = [el for el in root if _localname(el.tag) == "item"]
    else:
        entries = []
        for channel in root:
            if _localname(channel.tag) == "channel":
                entries.extend(el for el in channel if _localname(el.tag) == "item")
        if not entries:
            entries = [el for el in root.iter() if _localname(el.tag) == "item"]

    items: list[Item] = []
    for entry in entries:
        fields: dict[str, list[str]] = {}
        links: list[tuple[str, str]] = []
        for child in entry:
            name = _localname(child.tag)
            if name == "link":
                href = child.get("href")
                rel = (child.get("rel") or "alternate").lower()
                if href:
                    links.append((rel, href.strip()))
                elif child.text and child.text.strip():
                    links.append(("alternate", child.text.strip()))
                continue
            value = "".join(child.itertext())
            if value and value.strip():
                fields.setdefault(name, []).append(value.strip())

        title = _clean(fields.get("title", [""])[0])
        if not title:
            continue
        preferred = [href for rel, href in links if rel == "alternate"] or [href for _, href in links]
        url = preferred[0] if preferred else ""
        summary = ""
        for key in ("encoded", "content", "description", "summary", "subtitle"):
            if fields.get(key):
                summary = _clean(fields[key][0])
                break
        date_value = ""
        for key in ("date", "pubdate", "published", "updated", "publicationdate", "created"):
            if fields.get(key):
                date_value = _parse_date(fields[key][0])
                if date_value:
                    break
        identifier = " ".join(fields.get("identifier", []))
        doi = _doi_from(" ".join(fields.get("doi", [])), identifier, url)
        authors = [_clean(a) for a in fields.get("creator", []) + fields.get("author", [])]
        authors = [a for a in authors if a and len(a) < 120]
        deduped: list[str] = []
        for author in authors:
            if author not in deduped:
                deduped.append(author)
        authors = deduped

        items.append(
            Item(
                uid=make_uid(doi, url, title),
                title=title,
                url=url,
                doi=doi,
                journal=journal["name"],
                issn=journal.get("issn", ""),
                date=date_value,
                abstract=summary,
                authors=authors[:12],
                source="rss",
            )
        )
    return items


# --------------------------------------------------------------------------
# OpenAlex
# --------------------------------------------------------------------------


def _inverted_abstract(index: dict | None) -> str:
    if not index:
        return ""
    positions: dict[int, str] = {}
    for word, offsets in index.items():
        for offset in offsets or []:
            if isinstance(offset, int):
                positions[offset] = word
    if not positions:
        return ""
    return " ".join(positions[key] for key in sorted(positions))


def fetch_openalex(journal: dict, since: str, per_page: int, log, timeout: int = 45, retries: int = 1) -> list[Item]:
    issn = journal.get("issn", "")
    if not issn:
        return []
    params = {
        "filter": f"primary_location.source.issn:{issn},from_publication_date:{since},type:article",
        "per-page": str(per_page),
        "sort": "publication_date:desc",
        "mailto": MAILTO,
    }
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    status, raw = http_request(url, timeout=timeout, retries=retries)
    if status != 200:
        raise FetchError(f"OpenAlex HTTP {status}")
    payload = json.loads(_decode(raw))
    items: list[Item] = []
    for work in payload.get("results", []):
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
        items.append(
            Item(
                uid=make_uid(doi, landing, title),
                title=title,
                url=landing or "",
                doi=doi,
                journal=journal["name"],
                issn=issn,
                date=_parse_date(work.get("publication_date")),
                abstract=_clean(_inverted_abstract(work.get("abstract_inverted_index"))),
                authors=[a for a in authors if a],
                source="openalex",
            )
        )
    return items


# --------------------------------------------------------------------------
# Crossref
# --------------------------------------------------------------------------


def fetch_crossref(journal: dict, since: str, rows: int, log, timeout: int = 30, retries: int = 1) -> list[Item]:
    issn = journal.get("issn", "")
    if not issn:
        return []
    params = {
        "filter": f"from-pub-date:{since},type:journal-article",
        "rows": str(rows),
        "sort": "published",
        "order": "desc",
        "mailto": MAILTO,
    }
    url = f"https://api.crossref.org/journals/{issn}/works?" + urllib.parse.urlencode(params)
    status, raw = http_request(url, timeout=timeout, retries=retries)
    if status != 200:
        raise FetchError(f"Crossref HTTP {status}")
    payload = json.loads(_decode(raw))
    items: list[Item] = []
    for work in payload.get("message", {}).get("items", []):
        titles = work.get("title") or []
        title = _clean(titles[0]) if titles else ""
        if not title:
            continue
        doi = (work.get("DOI") or "").lower()
        url_value = work.get("URL") or (f"https://doi.org/{doi}" if doi else "")
        published = (
            work.get("published-online")
            or work.get("published")
            or work.get("published-print")
            or work.get("issued")
            or {}
        )
        parts = list((published.get("date-parts") or [[None]])[0] or [])
        date_value = ""
        if parts and parts[0]:
            year = int(parts[0])
            month = int(parts[1]) if len(parts) > 1 and parts[1] else 1
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
        items.append(
            Item(
                uid=make_uid(doi, url_value, title),
                title=title,
                url=url_value,
                doi=doi,
                journal=journal["name"],
                issn=issn,
                date=date_value,
                abstract=_clean(work.get("abstract")),
                authors=authors,
                source="crossref",
            )
        )
    return items


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def _fetch_journal(journal: dict, since: str, limit: int, sources: dict, log,
                   timeouts: dict, retries: int) -> tuple[list[Item], list[dict]]:
    """Fetch every enabled source for one journal. Never raises."""
    name = journal["name"]
    items: list[Item] = []
    statuses: list[dict] = []
    for source in ("rss", "openalex", "crossref"):
        if not sources.get(source, True):
            continue
        if source != "rss" and not journal.get(source, True):
            continue
        started = time.time()
        try:
            if source == "rss":
                url = journal.get("rss")
                if not url:
                    # Publisher has no usable feed (e.g. ACS): rely on the APIs silently.
                    log.info("%-44s %-9s skipped (no feed configured)", name, source)
                    continue
                status, raw = http_request(url, timeout=timeouts["rss"], retries=retries)
                if status != 200:
                    raise FetchError(f"HTTP {status}")
                fetched = parse_feed(raw, journal)
            elif source == "openalex":
                fetched = fetch_openalex(journal, since, limit, log, timeout=timeouts["api"], retries=retries)
            else:
                fetched = fetch_crossref(journal, since, limit, log, timeout=timeouts["api"], retries=retries)
            items.extend(fetched)
            statuses.append(
                {"journal": name, "source": source, "ok": True, "items": len(fetched),
                 "seconds": round(time.time() - started, 2), "note": ""}
            )
            log.info("%-44s %-9s %4d items  (%.1fs)", name, source, len(fetched), time.time() - started)
        except Exception as exc:  # noqa: BLE001 - a dead source must not stop the run
            statuses.append(
                {"journal": name, "source": source, "ok": False, "items": 0,
                 "seconds": round(time.time() - started, 2), "note": str(exc)[:220]}
            )
            log.warning("%-44s %-9s FAILED after %.1fs: %s", name, source, time.time() - started, str(exc)[:160])
    return items, statuses


def collect_items(cfg: dict, log, since_days: int | None = None) -> tuple[list[Item], list[dict]]:
    """Fetch every enabled source for every enabled journal, concurrently.

    Returns the merged, de-duplicated, window-filtered item list plus one status
    record per (journal, source) attempt. Sources are queried in parallel because
    a single publisher timeout would otherwise dominate the daily run time.
    """
    window = cfg.get("window", {})
    days = since_days if since_days is not None else int(window.get("days", 3))
    since = (date.today() - timedelta(days=days)).isoformat()
    limit = int(window.get("max_items_per_journal", 60))
    sources = window.get("sources", {})
    retries = int(window.get("retries", 1))
    timeouts = {
        "rss": int(window.get("rss_timeout_seconds", 25)),
        "api": int(window.get("api_timeout_seconds", 45)),
    }
    journals = [j for j in cfg.get("journals", []) if j.get("enabled", True)]
    workers = max(1, min(int(window.get("workers", 6)), len(journals) or 1))
    log.info("fetching %d journals from %s with %d workers (since %s)",
             len(journals), ",".join(s for s, on in sources.items() if on), workers, since)

    per_journal: dict[str, tuple[list[Item], list[dict]]] = {}
    if workers == 1:
        for journal in journals:
            per_journal[journal["name"]] = _fetch_journal(
                journal, since, limit, sources, log, timeouts, retries)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_fetch_journal, journal, since, limit, sources, log, timeouts, retries): journal["name"]
                for journal in journals
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    per_journal[name] = future.result()
                except Exception as exc:  # noqa: BLE001 - defensive; worker already catches
                    log.error("%s worker crashed: %s", name, exc)
                    per_journal[name] = ([], [{"journal": name, "source": "-", "ok": False,
                                               "items": 0, "note": f"worker error: {exc}"}])

    statuses: list[dict] = []
    merged: dict[str, Item] = {}
    for journal in journals:
        journal_items, journal_statuses = per_journal.get(journal["name"], ([], []))
        statuses.extend(journal_statuses)
        for item in journal_items:
            if not item.date or item.date < since:
                continue
            existing = merged.get(item.uid)
            if existing is None:
                merged[item.uid] = item
            else:
                # Prefer an abstract and richer metadata regardless of source order.
                if len(item.abstract) > len(existing.abstract):
                    existing.abstract = item.abstract
                if not existing.url and item.url:
                    existing.url = item.url
                if not existing.doi and item.doi:
                    existing.doi = item.doi
                if not existing.authors and item.authors:
                    existing.authors = item.authors
                if not existing.date and item.date:
                    existing.date = item.date

    items = sorted(merged.values(), key=lambda i: (i.date, i.journal), reverse=True)
    return items, statuses
