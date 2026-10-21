"""Offline regression tests against the live modules, not patch templates."""
import json
import logging
import tempfile
import unittest
import urllib.parse
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jalert import fetch
from jalert.cards import write_cards
from jalert.reliable_delivery import ReliableDelivery
import run as daily


class PaginationTests(unittest.TestCase):
    def test_both_apis_retrieve_two_pages(self):
        calls = []

        def fake_http(url, **kwargs):
            calls.append(url)
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            take = int(query.get("per-page", query.get("rows", ["100"]))[0])
            if "openalex.org" in url:
                start = (int(query.get("page", ["1"])[0]) - 1) * 100
                payload = {"results": [
                    {"title": f"Work {i}", "doi": f"https://doi.org/10.9000/{i}",
                     "publication_date": "2026-10-21"}
                    for i in range(start, start + take)
                ]}
            else:
                start = int(query.get("offset", ["0"])[0])
                payload = {"message": {"items": [
                    {"title": [f"Work {i}"], "DOI": f"10.9000/{i}",
                     "issued": {"date-parts": [[2026, 10, 21]]}}
                    for i in range(start, start + take)
                ]}}
            return 200, json.dumps(payload).encode("utf-8")

        journal = {"name": "Example", "issn": "1234-5678"}
        with patch.object(fetch, "http_request", side_effect=fake_http):
            a = fetch.fetch_openalex(journal, "2026-10-19", 200, logging.getLogger("test"))
            b = fetch.fetch_crossref(journal, "2026-10-19", 200, logging.getLogger("test"))
        self.assertEqual(len(a), 200)
        self.assertEqual(len(b), 200)
        self.assertEqual(len(calls), 4)
        self.assertIn("page=2", calls[1])
        self.assertIn("offset=100", calls[3])

    def test_second_page_failure_is_not_silent(self):
        def fake_http(url, **kwargs):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            if int(query.get("page", ["1"])[0]) == 2:
                raise fetch.FetchError("second page timeout")
            payload = {"results": [
                {"title": f"Work {i}", "doi": f"https://doi.org/10.9000/{i}",
                 "publication_date": "2026-10-21"} for i in range(100)
            ]}
            return 200, json.dumps(payload).encode()

        with patch.object(fetch, "http_request", side_effect=fake_http):
            with self.assertRaisesRegex(fetch.FetchError, "second page"):
                fetch.fetch_openalex({"name": "Example", "issn": "1234-5678"},
                                    "2026-10-19", 200, logging.getLogger("test"))


class SafetyTests(unittest.TestCase):
    def test_empty_reports_clear_old_webpage(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / "docs" / "index.html"
            out.parent.mkdir()
            out.write_text("STALE_ARTICLES", encoding="utf-8")
            result = write_cards(Path(root) / "reports", out)
            self.assertEqual(result, out)
            self.assertNotIn("STALE_ARTICLES", out.read_text(encoding="utf-8"))

    def test_wechat_body_cap_not_doubled(self):
        with tempfile.TemporaryDirectory() as root:
            delivery = ReliableDelivery(Path(root))
            with patch.dict("os.environ", {"JALERT_WECHAT_MAX_CHARS": "300"}):
                self.assertLessEqual(delivery._cap(("wechat",)), 300)
                self.assertLessEqual(delivery._cap(("wechat", "bark")), 300)

    def test_total_source_outage_never_creates_seen_database(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = {
                "_root": root, "reports_dir": str(Path(root) / "reports"),
                "log_dir": str(Path(root) / "logs"),
                "state_db": str(Path(root) / "state" / "seen.sqlite"),
                "project": {"name": "Test"}, "window": {"days": 1},
                "tiers": {"min_score": 3}, "output": {},
            }
            args = SimpleNamespace(
                config=None, days=None, sources=None, date="2026-10-21",
                test_push=False, doctor=False, rerender=False,
                cards=False, check=False, strict_check=False,
            )
            with ExitStack() as stack:
                stack.enter_context(patch.object(daily, "load_config", return_value=cfg))
                stack.enter_context(patch.object(daily, "setup_logging", return_value=logging.getLogger("test")))
                stack.enter_context(patch.object(daily, "effective_window_days", return_value=1))
                stack.enter_context(patch.object(daily, "collect_items", return_value=([], [
                    {"journal": "Example", "source": "openalex", "ok": False,
                     "items": 0, "note": "timeout"}
                ])))
                with self.assertRaisesRegex(RuntimeError, "data-source"):
                    daily.run(args)
            self.assertFalse(Path(cfg["state_db"]).exists())


if __name__ == "__main__":
    unittest.main()
