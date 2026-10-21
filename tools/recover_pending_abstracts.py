"""Recover full English Abstracts for *existing pending* push papers by DOI.

Never enqueues historical reports, never sends any notification, never invents text.
Each missing source is retried at most once per day; up to 100 DOI lookups/run.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from jalert.enhanced_delivery import DEFAULTS
from jalert.reliable_delivery import ReliableDelivery
from tools.history_backfill import lookup_english_abstract

LOG = logging.getLogger(__name__)


def recover_pending_abstracts(
    delivery: ReliableDelivery, *, limit: int = 100, retry_days: int = 1,
    lookup=lookup_english_abstract,
) -> dict:
    """Recover as many verifiable abstracts as possible before notifying.

    Network lookups run concurrently; durable queue changes are serialized.
    This avoids prematurely sending title-only notices for papers whose official
    publisher pages have an accessible Abstract.
    """
    from concurrent.futures import ThreadPoolExecutor
    if not 0 <= limit <= 200:
        raise ValueError("Pending DOI lookup limit must be 0..200")
    today = date.today()
    checked = filled = missing = skipped = 0
    selected = []
    pending = sorted(
        (v for v in delivery.queue.get("items", {}).values() if v.get("status") == "pending"),
        key=lambda v: (v.get("enqueued_at", ""), v.get("uid", "")),
    )
    for item in pending:
        if str(item.get("abstract") or "").strip():
            continue
        doi = str(item.get("doi") or "").strip()
        if not doi:
            item["abstract_lookup_status"] = "missing_doi"
            skipped += 1
            continue
        previous = item.get("abstract_last_checked")
        if previous:
            try:
                if today - date.fromisoformat(previous) < timedelta(days=retry_days):
                    skipped += 1
                    continue
            except ValueError:
                pass
        if len(selected) >= limit:
            break
        item["abstract_last_checked"] = today.isoformat()
        item["abstract_lookup_status"] = "checking"
        delivery._save_queue()  # Before network, allows safe job interruption.
        selected.append((item, doi))
    checked = len(selected)

    def query(pair):
        item, doi = pair
        try:
            result = lookup(doi)
            if isinstance(result, tuple) and len(result) == 2:
                return result
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            LOG.warning("DOI Abstract lookup failed for %s (%s)", doi[:50], type(exc).__name__)
        return "", ""

    if selected:
        with ThreadPoolExecutor(max_workers=min(6, len(selected))) as pool:
            for (item, _doi), (english, provider) in zip(selected, pool.map(query, selected)):
                if english and len(english) <= DEFAULTS["max_input_chars"]:
                    item["abstract"] = english
                    item["abstract_provider"] = provider
                    item["abstract_lookup_status"] = "recovered"
                    item.pop("last_ai_error", None)
                    item.pop("last_ai_error_at", None)
                    filled += 1
                else:
                    item["abstract_lookup_status"] = "unavailable"
                    missing += 1
                delivery._save_queue()
    return {"attempted": checked, "recovered": filled,
            "unavailable": missing, "skipped": skipped}
