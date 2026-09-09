"""RDF/ORE kataloga lasīšana (periodika.lndb.lv faktiskais uzskaitīšanas ceļš).

Paraugi `tests/fixtures/rdf/` ir saīsinātas dzīvās vietnes atbildes (2026-09-09).
Pret tīklu netiek iets — failus pasniedz vietējs serveris.
"""

import http.server
import socketserver
import threading
import unittest
from pathlib import Path

from periodika.config import CrawlPolicy, SiteProfile
from periodika.discovery import iter_rdf_aggregates
from periodika.http_client import HttpClient

FIXTURES = Path(__file__).parent / "fixtures" / "rdf"


class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FIXTURES), **kwargs)

    def log_message(self, *args):  # noqa: A003
        pass


class TestRdfAggregates(unittest.TestCase):
    def setUp(self):
        self.httpd = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.client = HttpClient(CrawlPolicy(obey_robots=False, max_retries=0))

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def test_sitemap_lists_periodicals(self):
        urls = list(iter_rdf_aggregates(self.client, f"{self.base}/nested/deeper/sitemap.rdf"))
        self.assertEqual(len(urls), 3)
        self.assertIn(f"{self.base}/rdf/periodics/221", urls)

    def test_relative_refs_resolve_against_site_root_not_request_path(self):
        # Paraugs tiek pasniegts no /nested/deeper/, bet atsauces ir pret sakni.
        # Ja tās saliktu pret pieprasīto ceļu, sanāktu /nested/deeper/rdf/periodics/221.
        urls = list(iter_rdf_aggregates(self.client, f"{self.base}/nested/deeper/sitemap.rdf"))
        self.assertIn(f"{self.base}/rdf/periodics/221", urls)
        self.assertFalse(any("nested" in u for u in urls), urls)
        self.assertFalse(any("null" in u for u in urls), urls)

    def test_only_filter_selects_articles(self):
        urls = list(iter_rdf_aggregates(self.client, f"{self.base}/issue.rdf", only="article"))
        self.assertEqual(len(urls), 2)
        self.assertTrue(all("/article/" in u for u in urls), urls)

    def test_pdf_resource_is_listed(self):
        urls = list(iter_rdf_aggregates(self.client, f"{self.base}/issue.rdf", only="set=PDF"))
        self.assertEqual(len(urls), 1)
        self.assertIn("id=l_676700", urls[0])

    def test_missing_document_yields_nothing(self):
        self.assertEqual(list(iter_rdf_aggregates(self.client, f"{self.base}/nav.rdf")), [])


class TestProfileCarriesRdfTemplates(unittest.TestCase):
    def test_new_fields_survive_roundtrip(self):
        profile = SiteProfile()
        profile.rdf_sitemap_url = "https://periodika.lndb.lv/rdf/periodika/sitemap"
        profile.pdf_url_template = "https://periodika.lndb.lv/resource?set=PDF&id=l_{issue}"
        restored = SiteProfile.from_json(profile.to_json())
        self.assertEqual(restored.rdf_sitemap_url, profile.rdf_sitemap_url)
        self.assertEqual(restored.pdf_url_template, profile.pdf_url_template)


if __name__ == "__main__":
    unittest.main()


class TestContentTypeAcceptance(unittest.TestCase):
    """`application/rdf+xml` ir derīgs atradums — agrāk probe to nometa."""

    def test_suffixed_xml_and_json_types_are_useful(self):
        from periodika.discovery import _looks_useful

        class _Resp:
            def __init__(self, ct):
                self.content_type = ct
                self.body = b"<rdf:RDF xmlns:rdf='#'></rdf:RDF>"

        for ct in ("application/rdf+xml", "application/atom+xml", "application/ld+json"):
            self.assertTrue(_looks_useful(_Resp(ct)), ct)
        self.assertFalse(_looks_useful(_Resp("image/jpeg")))
