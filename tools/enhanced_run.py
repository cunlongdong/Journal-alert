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
    parser.add_argument("--test-api", action="store_true")
    own, forwarded = parser.parse_known_args(argv)
    log = logging.getLogger("journal-alert-enhanced")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    delivery = Delivery(ROOT, log)

    if own.test_api:
        import os
        base_url = os.getenv("CUSTOM_AI_BASE_URL", "").strip()
        model = os.getenv("CUSTOM_AI_MODEL", "").strip()
        secret = os.getenv("CUSTOM_AI_API_KEY", "")
        if not base_url or not model:
            print("Missing CUSTOM_AI_BASE_URL or CUSTOM_AI_MODEL in GitHub Actions variables")
            return 2
        sample = {
            "title": "Exchange bias and spin transport in magnetic multilayers",
            "abstract": (
                "This example study examines exchange bias and spin transport "
                "in magnetic multilayers by comparing magnetic hysteresis "
                "and electrical resistance measurements. The study discusses "
                "interfacial coupling and magnetic ordering as factors that "
                "may influence the measured signals. This paragraph is a "
                "synthetic connectivity test and is not a real paper."
            ),
        }
        try:
            result = delivery._call_model("custom", base_url, model, secret, sample)
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            print(f"Custom model probe FAILED: {type(exc).__name__}: {exc}")
            return 1
        print("Custom model probe OK (synthetic example, no WeChat send):")
        print(result[:400])
        return 0
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
            # Write the durable outbox BEFORE marking papers seen in SQLite.
            # If a DB/save crash occurs, entries may be safely reprocessed later;
            # enqueue() is idempotent by uid, and seen.sqlite stays untouched.
            delivery.enqueue(entries)
            inserted = super().save(entries)
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
