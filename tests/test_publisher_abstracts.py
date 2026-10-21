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

    def test_europe_pmc_requires_exact_doi_and_core_abstract(self):
        from tools.history_backfill import europe_pmc_abstract
        entries = {"resultList": {"result": [
            {"doi": "10.1234/other", "abstractText": "Irrelevant study. " * 20},
            {"doi": "10.1234/target", "abstractText": "<p>Spin-current conversion measured.</p> " * 6}
        ]}}
        with patch("tools.history_backfill._get_json", return_value=entries) as request:
            result = europe_pmc_abstract("10.1234/target")
        self.assertIn("Spin-current conversion", result)
        self.assertNotIn("<p>", result)
        self.assertIn("resultType=core", request.call_args.args[0])
        self.assertIn("DOI%3A", request.call_args.args[0])

    def test_europe_pmc_doi_mismatch_cannot_forge_abstract(self):
        from tools.history_backfill import europe_pmc_abstract
        with patch("tools.history_backfill._get_json", return_value={
            "resultList": {"result": [
                {"doi": "10.1234/not-the-same", "abstractText": "Unrelated " * 30}
            ]}
        }):
            self.assertEqual(europe_pmc_abstract("10.1234/target"), "")

    def test_europe_pmc_is_preferred_to_publisher_after_other_sources_fail(self):
        def fake_json(url, **kwargs):
            if "europepmc" in url:
                return {"resultList": {"result": [{
                    "doi": "10.1234/target",
                    "abstractText": "An experimentally measured interfacial magnetic response. " * 5,
                }]}}
            raise RuntimeError("No abstract from first three services")
        with patch("tools.history_backfill._get_json", side_effect=fake_json), \
             patch("tools.history_backfill.publisher_abstract",
                   side_effect=AssertionError("Europe PMC already provides it")):
            text, source = lookup_english_abstract("10.1234/target")
        self.assertEqual(source, "Europe PMC")
        self.assertIn("interfacial magnetic response", text)

    def test_publisher_is_final_doi_fallback(self):
        with patch("tools.history_backfill._get_json", side_effect=RuntimeError("unavailable")), \
             patch("tools.history_backfill.publisher_abstract",
                   return_value="Experimental Abstract from the verified publisher.") as pub:
            result, provenance = lookup_english_abstract("10.1234/paper")
        self.assertEqual(provenance, "Publisher DOI page")
        self.assertIn("Experimental Abstract", result)
        pub.assert_called_once_with("10.1234/paper")

    def test_verified_arxiv_preprint_abstract_for_both_jmmm_dois(self):
        from tools.history_backfill import arxiv_preprint_abstract, ARXIV_MATCHES
        for doi, (arxiv_id, title, author) in ARXIV_MATCHES.items():
            with self.subTest(doi=doi):
                xml = (f'<feed xmlns="http://www.w3.org/2005/Atom">'
                       f'<entry><id>https://arxiv.org/abs/{arxiv_id}v2</id>'
                       f'<title>{title}</title>'
                       f'<author><name>{author}</name></author>'
                       f'<summary>{"A genuine experimentally verified magnetic heterostructure abstract. " * 4}</summary>'
                       '</entry></feed>')
                class Response:
                    def __enter__(self): return self
                    def __exit__(self, *args): return None
                    def read(self, *args): return xml.encode()
                with patch("tools.history_backfill.urllib.request.urlopen",
                           return_value=Response()):
                    result = arxiv_preprint_abstract(doi)
                self.assertIn("experimentally verified", result)

    def test_arxiv_different_author_or_title_is_rejected(self):
        from tools.history_backfill import arxiv_preprint_abstract
        xml = ('<feed xmlns="http://www.w3.org/2005/Atom">'
               '<entry><id>https://arxiv.org/abs/2605.21287</id>'
               '<title>Different unrelated paper</title>'
               '<author><name>Somebody Else</name></author>'
               '<summary>Arbitrary made up Abstract. </summary></entry></feed>')
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self, *args): return xml.encode()
        with patch("tools.history_backfill.urllib.request.urlopen", return_value=Response()):
            self.assertEqual(arxiv_preprint_abstract("10.1016/j.jmmm.2026.174589"), "")

    def test_sciencedirect_direct_page_fallback_when_doi_redirect_blocked(self):
        import urllib.error
        from tools.history_backfill import PUBLISHER_ARTICLE_URLS
        target = "10.1016/j.jmmm.2026.174575"
        urls = []
        class Page:
            headers = {"Content-Type": "text/html; charset=utf-8"}
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def geturl(self): return PUBLISHER_ARTICLE_URLS[target]
            def read(self, size):
                return (f'<meta name="citation_doi" content="{target}">'
                        '<section id="abstracts"><h2>Abstract</h2><p>'
                        + 'Spinel oxide spin transport and exchange bias are measured experimentally. ' * 5
                        + '</p></section>').encode()
        class Opener:
            def open(self, req, timeout):
                urls.append(req.full_url)
                if "doi.org" in req.full_url:
                    raise urllib.error.HTTPError(req.full_url, 403, "blocked", None, None)
                return Page()
        with patch("tools.history_backfill.urllib.request.build_opener", return_value=Opener()):
            value = publisher_abstract(target)
        self.assertIn("Spinel oxide spin transport", value)
        self.assertEqual(len(urls), 2)
        self.assertIn("sciencedirect.com", urls[1])

    def test_unmapped_aps_article_does_not_fabricate_preprint_abstract(self):
        from tools.history_backfill import ARXIV_MATCHES, PUBLISHER_ARTICLE_URLS, arxiv_preprint_abstract
        doi = "10.1103/jlhg-yy14"
        self.assertIn(doi, PUBLISHER_ARTICLE_URLS)
        self.assertNotIn(doi, ARXIV_MATCHES)
        self.assertEqual(arxiv_preprint_abstract(doi), "")

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


class TargetedBackfillTests(unittest.TestCase):
    def test_only_specified_doi_is_recovered_without_push_or_cursor_changes(self):
        from tools import history_backfill
        papers = [
            {"title": "Designer Quantum States in Magnetic Topological Insulator Multilayers",
             "doi": "10.1016/j.jmmm.2026.174589"},
            {"title": "Unrelated journal paper", "doi": "10.9999/not-requested"},
        ]
        calls = []
        def lookup(doi):
            calls.append(doi)
            return ("Magnetic topological multilayer experiments are demonstrated. " * 4,
                    "arXiv preprint (title and first author verified)")
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            with patch.object(history_backfill, "collect", return_value=(papers, [], [])), \
                 patch.object(history_backfill.ReliableDelivery, "_translate_title",
                              return_value="磁性拓扑绝缘体多层结构中的设计量子态"), \
                 patch.object(history_backfill.ReliableDelivery, "_get_translation",
                              return_value="经核对的完整中文摘要"):
                result = history_backfill.backfill(
                    base, max_papers=15,
                    target_dois={"10.1016/j.jmmm.2026.174589"}, lookup=lookup)
            self.assertEqual(result["attempted"], 1)
            self.assertEqual(result["translated"], 1)
            self.assertEqual(result["cards"], 1)
            self.assertEqual(calls, ["10.1016/j.jmmm.2026.174589"])
            self.assertFalse((base / "state" / "history_backfill_cursor.json").exists())
            self.assertFalse((base / "state" / "seen.sqlite").exists())
            self.assertFalse((base / "state" / "enhanced_wechat_outbox.json").is_file() and
                             bool(__import__("json").loads(
                                (base / "state" / "enhanced_wechat_outbox.json").read_text())["items"]))



if __name__ == "__main__":
    unittest.main()
