"""Safe wrapper: preserve upstream run.py and existing state/seen.sqlite.

Normal runs execute upstream run.py with --no-push, intercept only the
upstream Store.save(new_items) call, then drain the persistent WeChat outbox.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jalert.enhanced_delivery import Delivery  # noqa: E402
import run as upstream  # noqa: E402


def main(args: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if args is None else args)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--flush-only", action="store_true")
    parser.add_argument("--delivery-status", action="store_true")
    own, forwarded = parser.parse_known_args(argv)
    log = logging.getLogger("journal-alert-enhanced")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    delivery = Delivery(ROOT, log)

    if own.delivery_status:
        print(f"Pending Chinese WeChat articles: {delivery.pending_count()}")
        return 0
    if own.flush_only:
        delivery.flush()
        return 0

    # Non-delivery diagnostic tasks preserve upstream behavior.
    passthrough = {"--doctor", "--check", "--cards", "--rerender", "--test-push", "--dry-run", "--help"}
    if any(flag in forwarded for flag in passthrough):
        return upstream.main(forwarded)

    no_push_requested = "--no-push" in forwarded
    original_store = upstream.Store

    class OutboxStore(original_store):
        def save(self, entries):
            entries = list(entries)
            inserted = super().save(entries)
            # Upstream calls save() only with *new* literature; not historic IDs.
            # Never alter seen.sqlite schema, rows, or existing scoring behavior.
            delivery.enqueue(entries)
            return inserted

    upstream.Store = OutboxStore
    try:
        status = upstream.main(forwarded if no_push_requested else forwarded + ["--no-push"])
    finally:
        upstream.Store = original_store
    if status != 0:
        return status
    if no_push_requested:
        log.info("--no-push requested: new articles queued without delivery")
        return 0
    if not upstream.load_config(None).get("push", {}).get("enabled", False):
        log.info("push.enabled=false in existing configuration: queue retained")
        return 0
    delivery.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
