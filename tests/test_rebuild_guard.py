"""The first review of a freshly reset database never sends Bark accidentally."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import reliable_push, review_rebuild
from jalert.reliable_delivery import ReliableDelivery


class RebuildGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "state").mkdir()
        (self.root / "state" / "rebuild_review_hold.json").write_text(
            json.dumps({"version": 1, "requires_review": True, "purpose": "test"}),
            encoding="utf-8")

    def test_manual_flush_is_blocked_before_any_notification(self):
        with patch.object(reliable_push, "ROOT", self.root), \
             patch.object(ReliableDelivery, "_bark", side_effect=AssertionError("should not send")):
            self.assertEqual(reliable_push.main(["--flush"]), 3)
            self.assertEqual(reliable_push.main(["--flush", "--defer-if-held"]), 0)
            self.assertEqual(reliable_push.main(["--status"]), 0)

    def test_direct_library_call_cannot_bypass_hold(self):
        with patch("os.environ", {"JALERT_BARK_ENABLED": "true"}):
            d = ReliableDelivery(self.root)
            with self.assertRaisesRegex(RuntimeError, "paused"):
                d.flush_both(send_func=lambda *args: self.fail("Bark must not send"))

    def test_only_explicit_approval_unlocks_without_sending(self):
        self.assertTrue(review_rebuild.approve(self.root))
        self.assertFalse((self.root / "state" / "rebuild_review_hold.json").exists())
        self.assertFalse(review_rebuild.approve(self.root))

    def test_malformed_hold_is_not_removed(self):
        p = self.root / "state" / "rebuild_review_hold.json"
        p.write_text('{"version":1,"requires_review":false}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Invalid"):
            review_rebuild.approve(self.root)
        self.assertTrue(p.is_file())

    def test_workflow_has_safe_unlock_mode(self):
        workflow=(Path(__file__).resolve().parents[1] /
                  ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8")
        self.assertIn("approve_rebuild:", workflow)
        self.assertIn("python tools/review_rebuild.py --approve", workflow)
        self.assertIn("state/rebuild_review_hold.json", workflow)
        self.assertIn("!inputs.approve_rebuild", workflow)


if __name__ == "__main__":
    unittest.main()
