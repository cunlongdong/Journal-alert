"""Regression tests for verified Chinese-only Bark notifications."""
from __future__ import annotations
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jalert.reliable_delivery import ReliableDelivery, quality_report
from tools import history_backfill

ABSTRACT = "Exchange bias was measured at 10 K and 20 K, producing a response of 15 mT."
CHINESE = "交换偏置在10 K和20 K下测得，其响应为15 mT。研究表明这一结果与界面磁性有关。"
CHINESE_TITLE = "磁性异质结构中的交换偏置"
def makeitem(n, abstract=ABSTRACT):
    paper = SimpleNamespace(
        uid=f"doi:10.2000/test{n}", title=f"Exchange bias in structures {n}",
        journal="Physical Review B", date="2026-10-21", doi=f"10.2000/test{n}",
        url="", abstract=abstract,
    )
    return (paper, SimpleNamespace(labels=["交换偏置"], score=18))


class BarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.delivery = ReliableDelivery(self.root)
        self.env = patch.dict(os.environ, {
            "JALERT_BARK_ENABLED": "true",
            "JALERT_BARK_DEVICE_KEY": "example",
            "JALERT_BARK_BODY_BYTES": "1500",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_23_articles_each_have_one_chinese_bark(self):
        for n in range(23):
            self.delivery.enqueue([makeitem(n)])
        sent = []
        with patch.object(self.delivery, "_get_translation", return_value=CHINESE), \
             patch.object(self.delivery, "_translate_title", return_value=CHINESE_TITLE), \
             patch.object(self.delivery, "_bark", side_effect=lambda title, body: sent.append((title, body)) or True):
            result = self.delivery.flush_both()
        self.assertEqual(len(sent), 23)
        self.assertEqual(result["pending_papers"], 0)
        self.assertEqual(result["bark_articles_sent_this_run"], 23)
        self.assertTrue(all("中文标题：" in body and "英文标题：" in body and "中文摘要" in body for _,body in sent))
        self.assertTrue(all("中文摘要（全文直译）：" in body for _,body in sent))
        self.assertTrue(all(len(body.encode("utf-8")) <= 1500 for _,body in sent))
        restarted = ReliableDelivery(self.root)
        with patch.object(restarted, "_bark", side_effect=AssertionError("duplicate Bark")):
            self.assertEqual(restarted.flush_both()["bark_articles_sent_this_run"], 0)

    def test_missing_english_never_sends_fake_summary(self):
        self.delivery.enqueue([makeitem(0, "")])
        with patch.object(self.delivery, "_bark", side_effect=AssertionError("must not push")), \
             patch.object(self.delivery, "_translate_title", side_effect=AssertionError("must not translate a missing source")):
            result = self.delivery.flush_both()
        self.assertEqual(result["pending_papers"], 1)
        self.assertEqual(result["pending_without_english"], 1)
        self.assertEqual(result["bark_articles_sent_this_run"], 0)

    def test_ai_failure_keeps_original_pending(self):
        self.delivery.enqueue([makeitem(0)])
        with patch.object(self.delivery, "_get_translation", side_effect=RuntimeError("AI daily request budget exhausted")), \
             patch.object(self.delivery, "_bark", side_effect=AssertionError("must not push")):
            result = self.delivery.flush_both()
        self.assertEqual(result["pending_papers"], 1)
        self.assertEqual(result["bark_articles_sent_this_run"], 0)
        self.assertIn("budget exhausted", self.delivery.queue["items"][makeitem(0)[0].uid]["last_ai_error"])

    def test_failed_bark_retries_only_unacknowledged_article(self):
        for n in range(3):
            self.delivery.enqueue([makeitem(n)])
        with patch.object(self.delivery, "_get_translation", return_value=CHINESE), \
             patch.object(self.delivery, "_translate_title", return_value=CHINESE_TITLE), \
             patch.object(self.delivery, "_bark", side_effect=[True, False, True]):
            first = self.delivery.flush_both()
        self.assertEqual(first["bark_articles_sent_this_run"], 2)
        self.assertEqual(first["pending_papers"], 1)
        restarted = ReliableDelivery(self.root)
        seen=[]
        with patch.object(restarted, "_get_translation", return_value=CHINESE), \
             patch.object(restarted, "_translate_title", return_value=CHINESE_TITLE), \
             patch.object(restarted, "_bark", side_effect=lambda t,b: seen.append(t) or True):
            second = restarted.flush_both()
        self.assertEqual(len(seen), 1)
        self.assertEqual(second["pending_papers"], 0)

    def test_oversized_translation_is_explicitly_linked_not_silently_cropped(self):
        self.delivery.enqueue([makeitem(0)])
        sent = []
        with patch.object(self.delivery, "_get_translation", return_value=CHINESE * 80), \
             patch.object(self.delivery, "_translate_title", return_value=CHINESE_TITLE), \
             patch.object(self.delivery, "_bark", side_effect=lambda t,b: sent.append(b) or True):
            self.delivery.flush_both()
        self.assertEqual(len(sent), 1)
        self.assertIn("摘要较长", sent[0])
        self.assertIn("完整中英文摘要：", sent[0])
        self.assertLessEqual(len(sent[0].encode("utf-8")), 1500)

    def test_legacy_bark_ack_not_resend_after_removing_wechat(self):
        self.delivery.enqueue([makeitem(0)])
        paper = self.delivery.queue["items"][makeitem(0)[0].uid]
        paper["bark_done"] = True
        self.delivery._save_queue()
        other = ReliableDelivery(self.root)
        self.assertEqual(other.pending_count(), 0)
        with patch.object(other, "_bark", side_effect=AssertionError("old acknowledged article")):
            self.assertEqual(other.flush_both()["bark_articles_sent_this_run"], 0)

    def test_critical_numerals_and_2d_notation(self):
        self.assertNotIn("2", quality_report("2D material with 10 mT.", "二维材料表现出10 mT磁化。")["missing_numbers"])
        self.assertFalse(quality_report("At 10 K and 50 mT.", "在10 K下观测到磁性。")["ok"])

    def test_bark_json_uses_correct_chinese_title(self):
        with patch("jalert.reliable_delivery._http_json", return_value={"code":200}) as sender:
            self.assertTrue(self.delivery._bark("中文标题", "中文摘要全文"))
        url, payload = sender.call_args.args
        self.assertEqual(url, "https://api.day.app/push")
        self.assertEqual(payload["title"], "中文标题")
        self.assertEqual(payload["body"], "中文摘要全文")

    def test_history_backfill_does_not_send_notifications(self):
        records = [{"doi":"10.1000/historic","title":"Historic exchange bias","abstract":"TRUNCATED ENGLISH"}]
        with patch.object(history_backfill, "collect", return_value=(records,[],[])), \
             patch.object(history_backfill.ReliableDelivery, "_get_translation", return_value=CHINESE):
            info = history_backfill.backfill(self.root, max_papers=1, lookup=lambda d:(ABSTRACT,"OpenAlex"))
        self.assertEqual(info["translated"], 1)
        self.assertEqual(ReliableDelivery(self.root).pending_count(),0)


class CleanupRegressionTests(unittest.TestCase):
    def test_history_cursor_advances_after_missing_doi_abstract(self):
        papers = [
            {'doi': '10.1000/not-found', 'title': 'No source abstract'},
            {'doi': '10.1000/available', 'title': 'Available abstract'},
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            called = []
            def lookup(doi):
                called.append(doi)
                return ('', '') if doi.endswith('not-found') else (ORIGINAL, 'OpenAlex')
            with patch.object(history_backfill, 'collect', return_value=(papers, [], [])):
                first = history_backfill.backfill(root, max_papers=1, lookup=lookup)
                self.assertEqual(first['missing_source_abstract'], 1)
                # The next execution must progress to another paper, not retry
                # the same unavailable DOI forever.
                with patch.object(history_backfill.ReliableDelivery, '_get_translation', return_value=CHINESE):
                    second = history_backfill.backfill(root, max_papers=1, lookup=lookup)
            self.assertEqual(second['translated'], 1)
            self.assertEqual(called, ['10.1000/not-found', '10.1000/available'])
            cursor = root / 'state' / 'history_backfill_cursor.json'
            self.assertEqual(__import__('json').loads(cursor.read_text('utf-8'))['next_index'], 0)

    def test_history_backfill_dry_run_does_not_write_cursor(self):
        papers = [{'doi': '10.1000/one', 'title': 'One article'}]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(history_backfill, 'collect', return_value=(papers, [], [])):
                info = history_backfill.backfill(root, max_papers=1, dry_run=True,
                                                lookup=lambda doi: self.fail('dry-run requested'))
            self.assertEqual(info['attempted'], 1)
            self.assertFalse((root / 'state' / 'history_backfill_cursor.json').exists())


if __name__=='__main__':
    unittest.main()

if __name__ == '__main__':
    unittest.main()
