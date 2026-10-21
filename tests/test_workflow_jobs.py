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

    def test_processing_not_pages_environment_gated(self):
        self.assertEqual(len(self.parts), 2)
        self.assertIn("\n  run:\n", self.parts[0])
        self.assertNotIn("\n    environment:\n", self.parts[0])
        self.assertIn("git fetch --quiet origin main", self.parts[0])
        self.assertIn("stale commit", self.parts[0])

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
                    self.assertEqual(reliable_push.main(["--status"]), 0)


if __name__ == "__main__":
    unittest.main()
