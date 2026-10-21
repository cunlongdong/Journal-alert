"""Durable, source-attributed metadata for all matched articles.

Independent of seen.sqlite and Bark. Re-observing a DOI may improve its
abstract/authors without generating a second notification.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def doi_key(raw: str) -> str:
    doi = str(raw or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
    return doi


def article_key(article: dict) -> str:
    doi = doi_key(article.get("doi", ""))
    if doi:
        return "doi:" + doi
    uid = str(article.get("uid") or "").strip()
    if not uid:
        raise ValueError("Article metadata needs a DOI or UID")
    return uid


def load_metadata(root: Path) -> dict:
    path = Path(root) / "state" / "article_metadata.json"
    if not path.is_file():
        return {"version": 1, "articles": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("version") != 1 or
            not isinstance(data.get("articles"), dict)):
        raise ValueError("Invalid metadata ledger; refusing destructive overwrite")
    return data


def _save(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".metadata-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(obj, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def merge_many(root: Path, items: list[object]) -> dict:
    """Preserve richer source metadata without touching Bark/SQLite.

    Prefer nonempty, longer API abstracts and fuller author lists. Metadata
    provenance is retained; a later empty result cannot erase verified text.
    """
    root = Path(root)
    data = load_metadata(root)
    updated = 0
    for item in items:
        current = item if isinstance(item, dict) else {
            name: getattr(item, name, None)
            for name in ("uid", "doi", "title", "journal", "issn", "date",
                         "url", "abstract", "authors", "source")
        }
        key = article_key(current)
        old = data["articles"].get(key, {})
        if not isinstance(old, dict):
            raise ValueError("Malformed article metadata entry")
        entry = dict(old)
        changed = not bool(old)
        for field in ("uid", "doi", "title", "journal", "issn", "date", "url"):
            value = current.get(field)
            if value and not entry.get(field):
                entry[field] = value
                changed = True
        authors = [str(a).strip() for a in (current.get("authors") or []) if str(a).strip()]
        if len(authors) > len(entry.get("authors") or []):
            entry["authors"] = authors[:30]
            changed = True
        abstract = str(current.get("abstract") or "").strip()
        if len(abstract) > len(str(entry.get("abstract") or "")):
            entry["abstract"] = abstract
            entry["abstract_source"] = str(current.get("source") or "publisher metadata")
            changed = True
        if changed:
            entry["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            data["articles"][key] = entry
            updated += 1
    if updated:
        _save(root / "state" / "article_metadata.json", data)
    return {"seen": len(items), "updated": updated, "total": len(data["articles"])}


def merge_verified_abstract(root: Path, doi: str, title: str,
                            abstract: str, source: str) -> dict:
    """Add a DOI-matched full abstract, even when its translation fails."""
    if not doi_key(doi) or not abstract.strip():
        raise ValueError("A verified Abstract requires a DOI and text")
    return merge_many(Path(root), [{
        "uid": "doi:" + doi_key(doi), "doi": doi_key(doi),
        "title": title, "abstract": abstract, "source": source
    }])
