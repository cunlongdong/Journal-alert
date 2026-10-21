"""Recover full English Abstracts for *existing pending* push papers by DOI.

Never enqueues historical reports, never sends any notification, never invents text.
Each missing source is retried at most once every day, max 20 DOI lookups/run.
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
    delivery: ReliableDelivery, *, limit: int = 20, retry_days: int = 1,
    lookup=lookup_english_abstract,
) -> dict:
    if not 0 <= limit <= 100:
        raise ValueError('Pending DOI lookup limit must be 0..100')
    today = date.today()
    checked = filled = missing = skipped = 0
    pending = sorted(
        (v for v in delivery.queue.get('items', {}).values() if v.get('status') == 'pending'),
        key=lambda v: (v.get('enqueued_at', ''), v.get('uid', '')),
    )
    for item in pending:
        if item.get('abstract', '').strip():
            continue
        doi = str(item.get('doi') or '').strip()
        if not doi:
            item['abstract_lookup_status'] = 'missing_doi'
            skipped += 1
            continue
        previous = item.get('abstract_last_checked')
        if previous:
            try:
                prev = date.fromisoformat(previous)
                if today - prev < timedelta(days=retry_days):
                    skipped += 1
                    continue
            except ValueError:
                pass
        if checked >= limit:
            break
        # Persist attempt BEFORE network I/O so a timed-out job does not re-query
        # the same unresponsive DOI repeatedly after a restart.
        item['abstract_last_checked'] = today.isoformat()
        item['abstract_lookup_status'] = 'checking'
        delivery._save_queue()
        checked += 1
        try:
            english, provider = lookup(doi)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            LOG.warning('Pending paper English lookup deferred for DOI %s: %s', doi[:45], type(exc).__name__)
            english, provider = '', ''
        if english and len(english) <= DEFAULTS['max_input_chars']:
            item['abstract'] = english
            item['abstract_provider'] = provider
            item['abstract_lookup_status'] = 'recovered'
            item.pop('last_ai_error', None)
            item.pop('last_ai_error_at', None)
            filled += 1
        else:
            item['abstract_lookup_status'] = 'unavailable'
            missing += 1
        delivery._save_queue()
    return {'attempted': checked, 'recovered': filled, 'unavailable': missing, 'skipped': skipped}
