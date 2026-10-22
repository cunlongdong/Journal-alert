"""Offline invariants for workflow jobs and pending Bark acknowledgments."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import reliable_push

ROOT = Path(__file__).resolve().parents[1]


class WorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / ".github" / "workflows" / "daily.yml").read_text("utf-8")
        cls.parts = cls.source.split("\n  deploy_pages:\n")

    def test_daily_09_beijing_and_staggered_catch_up(self):
        import re
        crons = re.findall(r'cron: "([^"]+)"', self.source)
        self.assertEqual(crons, ["0 1 * * *", "23 1 * * *", "37 3 * * *"])
        self.assertIn("Show trigger and schedule diagnostics", self.source)
        self.assertIn("github.event.schedule", self.source)
        self.assertIn("TZ=Asia/Shanghai date", self.source)
        self.assertIn("concurrency:\n  group: journal-alert", self.source)
        self.assertIn("cancel-in-progress: false", self.source)

    def test_fallback_run_does_not_replay_bark_ack(self):
        from jalert.reliable_delivery import ReliableDelivery
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper = SimpleNamespace(
                uid="doi:10.1000/parallel-schedule",
                doi="10.1000/parallel-schedule", title="Spin logic paper",
                authors=["Scientist A"], journal="PRB", date="2026-10-22",
                url="", abstract="Measurements demonstrate 10 mT switching.")
            score = SimpleNamespace(labels=["spin logic"], score=10)
            first = ReliableDelivery(root)
            self.assertEqual(first.enqueue([(paper, score)]), 1)
            with patch.object(ReliableDelivery, "_translate_title", return_value="自旋逻辑论文"), \
                 patch.object(ReliableDelivery, "_get_translation", return_value="测量证明存在10 mT的磁化翻转。"):
                one = first.flush_both(send_func=lambda *args: True)
                self.assertEqual(one["bark_articles_sent_this_run"], 1)
                # A new runner at the fallback time loads persisted states.
                second = ReliableDelivery(root)
                self.assertEqual(second.enqueue([(paper, score)]), 0)
                two = second.flush_both(
                    send_func=lambda *args: self.fail("already acknowledged Bark"))
                self.assertEqual(two["bark_articles_sent_this_run"], 0)
                self.assertEqual(two["bark_papers_acknowledged"], 1)

    def test_processing_not_pages_environment_gated(self):
        self.assertEqual(len(self.parts), 2)
        self.assertIn("\n  run:\n", self.parts[0])
        self.assertNotIn("\n    environment:\n", self.parts[0])
        self.assertIn("git fetch --quiet origin main", self.parts[0])
        self.assertIn("stale commit", self.parts[0])
        self.assertIn("tools/reliable_push.py --flush --defer-if-held", self.parts[0])

    def test_pages_separate_dependent_job(self):
        deployment = self.parts[1]
        self.assertIn("    needs: run\n", deployment)
        self.assertIn("      name: github-pages\n", deployment)
        self.assertIn("uses: actions/deploy-pages@v4", deployment)
        self.assertEqual(self.source.count("name: github-pages"), 1)
        self.assertIn("uses: actions/upload-pages-artifact@v4", self.parts[0])
        self.assertNotIn("uses: actions/deploy-pages@v4", self.parts[0])

    def test_failed_run_preserves_state_artifact(self):
        self.assertIn("name: recoverable-state-" + chr(36) + "{{ github.run_id }}", self.source)
        self.assertIn("      - name: Preserve runtime state after failure\n        if: failure()", self.source)
        self.assertIn("            state/\n            reports/\n            docs/index.html", self.source)

    def test_bark_hold_prevents_network_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "bark_recovery_required.json").write_text(
                json.dumps({"run_id": 39617398380, "unconfirmed_bark_acceptances": 33}),
                encoding="utf-8")
            with patch.object(reliable_push, "ROOT", Path(tmp)):
                from jalert.reliable_delivery import ReliableDelivery
                with patch.object(ReliableDelivery, "flush_both",
                                  side_effect=AssertionError("must not send")):
                    self.assertEqual(reliable_push.main(["--flush"]), 3)
                    self.assertEqual(reliable_push.main(["--flush", "--defer-if-held"]), 0)
                    self.assertEqual(reliable_push.main(["--status"]), 0)


if __name__ == "__main__":
    unittest.main()
