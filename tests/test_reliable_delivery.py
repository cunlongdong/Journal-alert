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
        self.assertTrue(all("中文标题：" in body and "作者：" in body and "中文摘要：" in body for _,body in sent))
        self.assertTrue(all("（忠实翻译）" not in body and "英文标题：" not in body for _,body in sent))
        self.assertTrue(all(title.startswith("Exchange bias in structures ") for title,_ in sent))
        self.assertTrue(all(len(body.encode("utf-8")) <= 1500 for _,body in sent))
        restarted = ReliableDelivery(self.root)
        with patch.object(restarted, "_bark", side_effect=AssertionError("duplicate Bark")):
            self.assertEqual(restarted.flush_both()["bark_articles_sent_this_run"], 0)

    def test_no_abstract_still_sends_chinese_title_and_author_information(self):
        self.delivery.enqueue([makeitem(0, "")])
        self.delivery.queue["items"][makeitem(0)[0].uid]["authors"] = ["Alice Chen", "Bob Li"]
        sent = []
        with patch.object(self.delivery, "_translate_title", return_value=CHINESE_TITLE), \
             patch.object(self.delivery, "_get_translation", side_effect=AssertionError("no source")), \
             patch.object(self.delivery, "_bark", side_effect=lambda title,body: sent.append(body) or True):
            result = self.delivery.flush_both()
        self.assertEqual(result["pending_papers"], 0)
        self.assertEqual(result["sent_without_abstract"], 1)
        self.assertEqual(result["bark_articles_sent_this_run"], 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("中文标题：", sent[0])
        self.assertIn("作者：Alice Chen、Bob Li", sent[0])
        self.assertIn("暂未提供可核实的摘要", sent[0])
        self.assertIn("论文链接：", sent[0])

    def test_missing_abstract_waits_for_verified_chinese_title(self):
        self.delivery.enqueue([makeitem(0, "")])
        sent = []
        with patch.object(self.delivery, "_translate_title", side_effect=RuntimeError("AI daily request budget exhausted")), \
             patch.object(self.delivery, "_get_translation", side_effect=AssertionError("no Abstract")), \
             patch.object(self.delivery, "_bark", side_effect=lambda t,b: sent.append(b) or True):
            status = self.delivery.flush_both()
        self.assertEqual(sent, [])
        self.assertEqual(status["pending_papers"], 1)
        self.assertEqual(status["bark_articles_sent_this_run"], 0)
        self.assertIn("budget exhausted", self.delivery.queue["items"][makeitem(0)[0].uid]["last_ai_error"])
        with patch.object(self.delivery, "_translate_title", return_value=CHINESE_TITLE), \
             patch.object(self.delivery, "_bark", side_effect=lambda t,b: sent.append((t,b)) or True):
            result = self.delivery.flush_both()
        self.assertEqual(result["pending_papers"], 0)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][0].startswith("Exchange bias"))
        self.assertIn("中文标题："+CHINESE_TITLE, sent[0][1])
        self.assertIn("中文摘要：", sent[0][1])
        self.assertIn("暂未提供", sent[0][1])

    def test_ai_failure_keeps_original_pending(self):
        self.delivery.enqueue([makeitem(0)])
        with patch.object(self.delivery, "_translate_title", return_value=CHINESE_TITLE), \
             patch.object(self.delivery, "_get_translation", side_effect=RuntimeError("AI daily request budget exhausted")), \
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
        self.assertIn("正文较长", sent[0])
        self.assertIn("双语网页：", sent[0])
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

    def test_title_number_validator_understands_chemical_subscripts(self):
        from jalert.reliable_delivery import _title_numbers
        source = "Ground-state S = 3 2 in Na 3 Cr (PO 4) 2"
        translated = "S=3/2 的 Na₃Cr(PO₄)₂ 中的基态"
        self.assertFalse(_title_numbers(source) - _title_numbers(translated))
        self.assertFalse(_title_numbers("Fe 3 − x GeTe 2") -
                         _title_numbers("Fe₃₋ₓGeTe₂"))
        self.assertEqual(_title_numbers("MnBi2Te4") - _title_numbers("MnBi₂Te"), {"4"})

    def test_he_irradiation_dose_malformed_power_passes_equivalent_translation(self):
        from jalert.reliable_delivery import _normalize_scientific_notation
        original = ("At the lowest fluence 1 × 1 0 14 He + / cm 2, "
                    "the magnetic anisotropy increased by 20%, with domain wall "
                    "velocities of 1.1 mm/s and 0.38 mm/s.")
        chinese = ("在最低的 1×10¹⁴ He⁺/cm² 离子注量下，磁各向异性提高了 20%，"
                   "测得畴壁运动速率分别为 1.1 mm/s 和 0.38 mm/s。"
                   "这些结果说明低剂量辐照影响界面磁性与磁畴动力学。")
        normalized = _normalize_scientific_notation(original)
        self.assertIn("1×10^14 He+/cm²", normalized)
        self.assertNotIn("1 × 1 0 14", normalized)
        self.assertEqual(quality_report(original, chinese)["missing_numbers"], [])
        self.assertTrue(quality_report(original, chinese)["ok"])
        self.assertIn("1 × 1 0 14", _normalize_scientific_notation(
            "1 × 1 0 14 in an unrelated table"))

    def test_superscript_braces_and_unit_notation_are_equivalent(self):
        from jalert.reliable_delivery import _normalize_scientific_notation
        cases = ("1 × 10¹⁴ He⁺/cm²", "1×10^14 He+/cm²",
                 "1 × 10^{14} He + / cm 2", "10^14 He+/cm²")
        for version in cases:
            with self.subTest(version=version):
                self.assertIn("1×10^14", _normalize_scientific_notation(version))
        chinese = "在 1×10¹⁴ He⁺/cm² 下进行了实验，离子辐照引起了明显的磁性变化。"
        for version in cases:
            with self.subTest(english=version):
                self.assertEqual(quality_report(
                    "Irradiation fluence was " + version + ".", chinese
                )["missing_numbers"], [])

    def test_wrong_dose_or_missing_measurement_still_blocked(self):
        original = ("At 1 × 1 0 14 He + / cm 2, magnetization changes by 20%, "
                    "with wall velocity 1.1 mm/s and 0.38 mm/s.")
        correct = ("在 1×10¹⁴ He⁺/cm² 下，磁化变化 20%，"
                   "畴壁速率分别为 1.1 mm/s 和 0.38 mm/s，实验数据符合观测。")
        for wrong in (correct.replace("10¹⁴", "10¹³"),
                      correct.replace("20%", "25%"),
                      correct.replace("0.38", "0.39")):
            with self.subTest(chinese=wrong):
                self.assertFalse(quality_report(original, wrong)["ok"])

    def test_model_receives_normalized_dose_without_rewriting_original(self):
        from jalert.enhanced_delivery import Delivery
        raw = "The dose is 1 × 1 0 14 He + / cm 2 and anisotropy rises by 20%."
        chinese = ("辐照离子注量为 1×10¹⁴ He⁺/cm²，磁各向异性提高了 20%。"
                   "这一变化体现出离子辐照对材料界面磁性的调节作用。")
        item = {"title": "He-ion domain wall experiment", "abstract": raw}
        with patch.object(Delivery, "_call_model", return_value=chinese) as translated:
            output = self.delivery._call_model(
                "custom", "https://offline.example.org", "offline", "", item)
        self.assertEqual(output, chinese)
        self.assertEqual(item["abstract"], raw)
        sent = translated.call_args.args[-1]
        self.assertEqual(sent["abstract"].count("1×10^14"), 1)
        self.assertIn("He+/cm²", sent["abstract"])

    def test_critical_numerals_and_2d_notation(self):
        self.assertNotIn("2", quality_report("2D material with 10 mT.", "二维材料表现出10 mT磁化。")["missing_numbers"])
        self.assertFalse(quality_report("At 10 K and 50 mT.", "在10 K下观测到磁性。")["ok"])

    def test_embedded_chemical_subscripts_do_not_block_valid_abstract(self):
        cases = [
            ("The Mn2PtSn phase showed 2 mT anisotropy.",
             "Mn₂PtSn 相表现出 2 mT 的各向异性，测量结果表明界面效应显著。"),
            ("Irradiation of He+ at 0.1 to 14 ions per nm2.",
             "在每 nm² 有 0.1 至 14 个离子的 He+ 辐照后，观察到有序磁畴变化。"),
            ("The Fe3GeTe2 layer exhibits strong spin-orbit coupling.",
             "Fe₃GeTe₂ 层表现出较强的自旋轨道耦合，这一效应在实验中得到了观察。"),
        ]
        for source, target in cases:
            with self.subTest(source=source):
                self.assertEqual(quality_report(source, target)["missing_numbers"], [])

    def test_missing_actual_measurement_remains_blocking(self):
        report = quality_report(
            "Magnetization switching at 10 K requires a magnetic field of 50 mT.",
            "磁化在 10 K 下发生翻转，实验显示磁化状态发生了变化。"
        )
        self.assertIn("50", report["missing_numbers"])
        self.assertFalse(report["ok"])

    def test_real_title_translation_api_is_cached_without_second_call(self):
        import json
        title = "Exchange bias at 10 mT"
        record = {"uid": "doi:10.2000/test", "title": title, "doi": "10.2000/test",
                  "abstract": ABSTRACT}
        class Reply:
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return None
            def read(self, *args):
                return json.dumps({"choices": [{"finish_reason": "stop",
                    "message": {"content": "10 mT 条件下的交换偏置"}}]}).encode()
        with patch.dict(os.environ, {
            "JALERT_AI_PRIMARY": "custom", "JALERT_AI_FALLBACK": "none",
            "CUSTOM_AI_BASE_URL": "https://example.edu/api/v1",
            "CUSTOM_AI_MODEL": "deepseek-v4.1-flash", "CUSTOM_AI_API_KEY": "offline-test",
        }):
            with patch("jalert.reliable_delivery.urllib.request.urlopen", return_value=Reply()) as request:
                title_zh = self.delivery._translate_title(record, "2026-10-21")
                again = self.delivery._translate_title(record, "2026-10-21")
            self.assertEqual(request.call_count, 1)
        self.assertEqual(title_zh, "10 mT 条件下的交换偏置")
        self.assertEqual(again, title_zh)
        self.assertEqual(len(self.delivery.cache["titles"]), 1)

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
                return ('', '') if doi.endswith('not-found') else (ABSTRACT, 'OpenAlex')
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
