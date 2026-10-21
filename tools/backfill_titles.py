"""Fill missing Chinese card titles without fetching papers or sending Bark.

Unlike the abstract backfill cursor, this pass only examines absent title
translations and can fill them even when there is no English Abstract.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from jalert.cards import collect
from jalert.enhanced_delivery import _now_date
from jalert.reliable_delivery import ReliableDelivery

LOG = logging.getLogger("journal-alert-titles")


def fill_missing_titles(root: Path, *, limit: int = 80, dry_run: bool = False,
                        translate=None, log=LOG) -> dict:
    if not 0 <= limit <= 200:
        raise ValueError("title translation limit must be 0..200")
    root = Path(root)
    papers, _reports, _topics = collect(root / "reports")
    delivery = ReliableDelivery(root, log)
    titles = delivery.cache.setdefault("titles", {})
    missing = []
    seen = set()
    for paper in papers:
        title = str(paper.get("title") or "").strip()
        if not title:
            continue
        signature = hashlib.sha256(title.encode("utf-8")).hexdigest()
        if signature in seen:
            continue
        seen.add(signature)
        if not (titles.get(signature) or {}).get("zh_title"):
            missing.append(paper)

    attempted = translated = errors = 0
    today = _now_date()
    title_fn = translate or delivery._translate_title
    exhausted = False
    for paper in missing[:limit]:
        attempted += 1
        if dry_run:
            continue
        data = {
            "uid": "title:" + str(paper.get("doi") or paper["title"]),
            "title": str(paper["title"]),
            "doi": str(paper.get("doi") or ""),
            "abstract": "",
        }
        try:
            zh = title_fn(data, today)
            if not zh or not any("\u4e00" <= ch <= "\u9fff" for ch in zh):
                raise ValueError("title translation is not Chinese")
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            errors += 1
            log.warning("Chinese title pending for %s: %s", data["title"][:80], type(exc).__name__)
            if "budget exhausted" in str(exc):
                exhausted = True
                break
            continue
        # Real translator persists internally. A test double may simply return.
        translated += 1

    result = {
        "cards": len(papers),
        "title_cache_entries": len(titles),
        "missing_before": len(missing),
        "attempted": attempted,
        "translated": translated,
        "remaining_estimate": max(0, len(missing) - translated),
        "errors": errors,
        "ai_budget_exhausted": exhausted,
        "dry_run": dry_run,
    }
    log.info("Chinese title backfill: %s", json.dumps(result, ensure_ascii=False))
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Fill missing Chinese titles only; never sends Bark")
    parser.add_argument("--max", type=int, default=80)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    report = fill_missing_titles(ROOT, limit=args.max, dry_run=args.dry_run)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # A transient model failure must not block web deployment; remaining titles
    # are explicitly counted and retried on subsequent workflow runs.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
