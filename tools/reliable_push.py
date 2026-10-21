"""Reliable dual-channel alerts, with bounded missing-abstract recovery."""
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
    ns = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    delivery = ReliableDelivery(ROOT)
    if ns.test_bark:
        import os
        if os.getenv('JALERT_BARK_ENABLED', 'false').lower() not in ('true','1','yes'):
            print('Bark is not enabled: set JALERT_BARK_ENABLED=true')
            return 2
        try:
            accepted = delivery._bark('Journal-alert Bark 连接测试', '这是一条连接测试通知，不包含论文，也不会占用 Server酱额度。')
        except (OSError, ValueError, RuntimeError, KeyError) as ex:
            print('Bark test failed:', type(ex).__name__)
            return 1
        print('Bark test accepted by API' if accepted else 'Bark rejected test message')
        return 0 if accepted else 1
    if ns.flush:
        # JALERT_RECOVER_PENDING_V1: restore actual source abstracts, not invented text.
        from tools.recover_pending_abstracts import recover_pending_abstracts
        recovery = recover_pending_abstracts(delivery)
        logging.info('Pending Abstract recovery: %s', recovery)
        report = delivery.flush_both()
    else:
        report = delivery.status()
    report['pending_without_english'] = sum(
        bool(x.get('status') == 'pending' and not x.get('abstract', '').strip())
        for x in delivery.queue['items'].values()
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report['pending_without_english']:
        logging.warning('%d pending papers have no original English Abstract; retry is scheduled with a 7-day cooldown', report['pending_without_english'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
