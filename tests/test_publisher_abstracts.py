"""Offline integration tests for publisher abstracts, author persistence and Bark."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jalert.enhanced_delivery import Delivery
from tools.history_backfill import (
    _PublisherAbstractParser, _safe_publisher_url,
    lookup_english_abstract, publisher_abstract,
)


class PublisherAbstractTests(unittest.TestCase):
    def test_citation_meta_and_valid_doi(self):
        class Page:
            headers = {"Content-Type": "text/html; charset=utf-8"}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return None
            def geturl(self):
                return "https://publisher.example.org/articles/my-study"
            def read(self, size):
                abstract = ("Spin-orbit torque drives magnetic switching in the sample. " * 5)
                return (f'<html><head><meta name="citation_doi" content="10.1234/exact">'
                        f'<meta name="citation_abstract" content="{abstract}"></head></html>').encode()
        class Opener:
            def open(self, req, timeout):
                return Page()
        with patch("tools.history_backfill.urllib.request.build_opener", return_value=Opener()):
            result = publisher_abstract("10.1234/exact")
        self.assertIn("Spin-orbit torque", result)

    def test_mismatched_doi_is_rejected(self):
        class Page:
            headers = {"Content-Type": "text/html"}
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def geturl(self): return "https://publisher.example.org/article"
            def read(self, size):
                return (b'<meta name="citation_doi" content="10.1234/other">'
                        b'<meta name="citation_abstract" content="' +
                        b'Full experimentally verified magnetic switching Abstract. ' * 5 + b'">')
        class Opener:
            def open(self, req, timeout): return Page()
        with patch("tools.history_backfill.urllib.request.build_opener", return_value=Opener()):
            self.assertEqual(publisher_abstract("10.1234/exact"), "")

    def test_article_body_fallback(self):
        p = _PublisherAbstractParser()
        p.feed('<section class="abstract"><h2>Abstract</h2><p>'
               + "Magnetic switching was experimentally measured and analyzed. " * 5
               + '</p></section>')
        self.assertEqual(len(p.abstracts), 1)
        self.assertIn("Magnetic switching", p.abstracts[0])

    def test_no_generic_seo_snippets(self):
        p = _PublisherAbstractParser()
        p.feed('<meta property="og:description" content="Explore papers here">'
               '<meta name="description" content="Click to read more">')
        self.assertEqual(p.abstracts, [])

    def test_https_redirect_policy(self):
        self.assertTrue(_safe_publisher_url("https://www.aps.org/article"))
        for url in ("http://example.com", "https://127.0.0.1/a",
                    "https://localhost/a", "https://192.168.1.1/",
                    "https://user:secret@publisher.org/"):
            self.assertFalse(_safe_publisher_url(url))

    def test_publisher_is_final_doi_fallback(self):
        with patch("tools.history_backfill._get_json", side_effect=RuntimeError("unavailable")), \
             patch("tools.history_backfill.publisher_abstract",
                   return_value="Experimental Abstract from the verified publisher.") as pub:
            result, provenance = lookup_english_abstract("10.1234/paper")
        self.assertEqual(provenance, "Publisher DOI page")
        self.assertIn("Experimental Abstract", result)
        pub.assert_called_once_with("10.1234/paper")

    def test_authors_persist_from_source_item(self):
        with tempfile.TemporaryDirectory() as root:
            delivery = Delivery(Path(root))
            item = SimpleNamespace(
                uid="doi:10.1234/authors", title="Magnetic switching", journal="PRB",
                date="2026-10-21", doi="10.1234/authors", url="",
                abstract="", authors=["Alice Chen", "Bob Li"],
            )
            score = SimpleNamespace(labels=["magnetism"], score=20)
            delivery.enqueue([(item, score)])
            persisted = Delivery(Path(root))
        self.assertEqual(persisted.queue["items"]["doi:10.1234/authors"]["authors"],
                         ["Alice Chen", "Bob Li"])


if __name__ == "__main__":
    unittest.main()
