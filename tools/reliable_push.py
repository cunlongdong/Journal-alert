"""Bark-only Chinese alerts with bounded missing-abstract recovery."""
from __future__ import annotations
import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from jalert.reliable_delivery import ReliableDelivery


def main(argv=None):
    parser = argparse.ArgumentParser()
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument('--status', action='store_true')
    actions.add_argument('--flush', action='store_true')
    actions.add_argument('--test-bark', action='store_true')
    parser.add_argument('--defer-if-held', action='store_true', help='Daily pipeline may skip sending while recovery is held')
    ns = parser.parse_args(argv)
    if ns.defer_if_held and not ns.flush:
        parser.error('--defer-if-held requires --flush')
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    delivery = ReliableDelivery(ROOT)
    # Run #47 accepted 33 Bark articles, but the state push was rejected.
    # Never replay these possible deliveries without an explicit reconciliation.
    hold_path = ROOT / "state" / "bark_recovery_required.json"
    recovery_hold = json.loads(hold_path.read_text("utf-8")) if hold_path.is_file() else None
    review_path = ROOT / "state" / "rebuild_review_hold.json"
    review_hold = json.loads(review_path.read_text("utf-8")) if review_path.exists() else None
    if ns.flush and review_hold is not None:
        logging.warning("Bark paused until reconstructed database has been reviewed")
        print(json.dumps({"rebuild_review_required": review_hold,
                          "pending_papers": delivery.pending_count()}, ensure_ascii=False, indent=2))
        return 0 if ns.defer_if_held else 3
    if ns.flush and recovery_hold is not None:
        logging.warning("Bark paused: review %s before replaying uncommitted notifications", hold_path)
        print(json.dumps({"recovery_required": recovery_hold,
                          "pending_papers": delivery.pending_count()},
                         ensure_ascii=False, indent=2))
        return 0 if ns.defer_if_held else 3
    if ns.test_bark:
        import os
        if os.getenv('JALERT_BARK_ENABLED', 'false').lower() not in ('true','1','yes'):
            print('Bark is not enabled: set JALERT_BARK_ENABLED=true')
            return 2
        try:
            accepted = delivery._bark('Journal-alert Bark 连接测试', '这是一条 Bark 连接测试通知，不包含论文。')
        except (OSError, ValueError, RuntimeError, KeyError) as ex:
            print('Bark test failed:', type(ex).__name__)
            return 1
        print('Bark test accepted by API' if accepted else 'Bark rejected test message')
        return 0 if accepted else 1
    if ns.flush:
        import os
        enabled = os.getenv("JALERT_BARK_ENABLED", "true").lower() in ("true", "1", "yes")
        if not enabled or not os.getenv("JALERT_BARK_DEVICE_KEY", "").strip():
            logging.error("Bark required: enable it and configure BARK_DEVICE_KEY")
            return 2
        # Reuse richer source metadata from previous runs before DOI network retries.
        from jalert.metadata import load_metadata
        updated = delivery.refresh_pending(load_metadata(ROOT))
        logging.info("Pending items updated from durable metadata: %d", updated)
        # JALERT_RECOVER_PENDING_V1: restore actual source abstracts, not invented text.
        from tools.recover_pending_abstracts import recover_pending_abstracts
        recovery = recover_pending_abstracts(delivery)
        logging.info('Pending Abstract recovery: %s', recovery)
        report = delivery.flush_both()
    else:
        report = delivery.status()
    if review_hold is not None:
        report["rebuild_review_required"] = review_hold
    if recovery_hold is not None:
        report['recovery_required'] = recovery_hold
    report['pending_without_english'] = sum(
        bool(x.get('status') == 'pending' and not x.get('abstract', '').strip())
        for x in delivery.queue['items'].values()
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report['pending_without_english']:
        logging.warning('%d pending papers have no original English Abstract; source recheck follows a one-day cooldown', report['pending_without_english'])
    if ns.flush and report.get('bark_articles_waiting', 0):
        logging.warning("%d articles still await verified sources, translation or Bark retry",
                        report['bark_articles_waiting'])
    if ns.flush and report.get('bark_articles_failed_this_run', 0):
        logging.error("Bark transport rejected %d articles", report['bark_articles_failed_this_run'])
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
