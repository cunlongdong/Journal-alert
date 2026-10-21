"""Entry point for bilingual notification delivery with separate Bark/WeChat acks."""
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
    report = delivery.status() if ns.status else delivery.flush_both()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
