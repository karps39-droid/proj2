"""Lapas atvēršanas testi: SPA nolasīšana un datu galapunktu atklāšana.

Pret dzīvo vietni tests nekad neiet — tā vietā vietējais HTTP serveris pasniedz
sintētisku skatītāju (``tests/fixtures/spa``), kas uzvedas tāpat kā
``periodika2-viewer``: teksta HTML avotā nav, tas ienāk ar JavaScript, un dati
tiek ielādēti ar XHR. Ja pārlūks nav pieejams, testi tiek izlaisti — tīras
funkcijas (URL vispārināšana) tiek pārbaudītas vienmēr.
"""

import functools
import http.server
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path

from periodika.browser import (
    BrowserReader,
    BrowserUnavailable,
    browser_available,
    find_chromium,
    generalize_urls,
)
from periodika.config import Config, CrawlPolicy, SiteProfile
from periodika.crawl import CrawlLimits, Crawler
from periodika.search import local_search

SPA_DIR = Path(__file__).parent / "fixtures" / "spa"
BROWSER = browser_available()


class LocalSite:
    """Vietējs HTTP serveris ar sintētisko skatītāju."""

    def __init__(self, directory: Path):
        handler = functools.partial(_QuietHandler, directory=str(directory))
        self.httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def viewer_url(self, issue: str = "p_001_bw1899n01", page: int = 1) -> str:
        return f"{self.base}/index.html#panel:pa|issue:/{issue}|page:{page}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: A003 - klusējam testos
        pass


class TestUrlGeneralization(unittest.TestCase):
    """Šie testi strādā vienmēr — pārlūks nav vajadzīgs."""

    def test_issue_id_and_page_number_become_placeholders(self):
        urls = [
            "https://periodika.lndb.lv/periodika2-data/p_001_bw1899n01/mets.xml",
            "https://periodika.lndb.lv/periodika2-data/p_001_bw1899n01/alto/00000003.xml",
        ]
        templates = {t["kind"]: t["template"] for t in generalize_urls(urls, "p_001_bw1899n01")}
        self.assertEqual(
            templates["mets"], "https://periodika.lndb.lv/periodika2-data/{issue}/mets.xml"
        )
        self.assertEqual(
            templates["alto"],
            "https://periodika.lndb.lv/periodika2-data/{issue}/alto/{n:08d}.xml",
        )

    def test_zero_padding_is_preserved(self):
        out = generalize_urls(["https://x.lv/i/p_1/alto/007.xml"], "p_1")
        self.assertIn("{n:03d}", out[0]["template"])

    def test_unpadded_numbers_stay_plain(self):
        out = generalize_urls(["https://x.lv/api/issue/p_1/page/7/text"], "p_1")
        self.assertIn("/page/{n}/text", out[0]["template"])

    def test_host_and_port_are_never_rewritten(self):
        out = generalize_urls(["http://127.0.0.1:36987/data/p_1/mets.xml"], "p_1")
        self.assertIn("127.0.0.1:36987", out[0]["template"])
        self.assertNotIn("{n}", out[0]["template"])

    def test_kinds_are_detected_and_ordered(self):
        urls = [
            "https://x.lv/i/p_1/alto/1.xml",
            "https://x.lv/i/p_1/mets.xml",
            "https://x.lv/i/p_1/tei.xml",
        ]
        kinds = [t["kind"] for t in generalize_urls(urls, "p_1")]
        self.assertEqual(kinds, ["mets", "alto", "tei"])

    def test_duplicates_collapse_into_one_template(self):
        urls = [f"https://x.lv/i/p_1/alto/{i:08d}.xml" for i in range(1, 20)]
        self.assertEqual(len(generalize_urls(urls, "p_1")), 1)


class TestBrowserDiscovery(unittest.TestCase):
    def test_reports_what_is_available(self):
        report = browser_available()
        self.assertIn("playwright", report)
        self.assertIn("chromium", report)
        if not report["pieejams"]:
            self.assertTrue(report["norāde"])

    def test_find_chromium_returns_a_path_or_nothing(self):
        found = find_chromium()
        self.assertTrue(found == "" or Path(found).exists())

    @unittest.skipIf(BROWSER["pieejams"], "pārlūks ir pieejams")
    def test_missing_browser_raises_actionable_error(self):
        with self.assertRaises(BrowserUnavailable) as ctx:
            with BrowserReader():
                pass
        self.assertIn("playwright install", str(ctx.exception))


@unittest.skipUnless(BROWSER["pieejams"], "nav headless pārlūka")
class TestReadingASpa(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = LocalSite(SPA_DIR)
        cls.tmp = Path(tempfile.mkdtemp())
        with BrowserReader(timeout=30) as reader:
            cls.page = reader.read(
                cls.site.viewer_url(),
                save_data_to=cls.tmp / "dati",
                screenshot_to=cls.tmp / "lapa.png",
            )

    @classmethod
    def tearDownClass(cls):
        cls.site.close()

    def test_text_is_not_in_the_html_source(self):
        source = (SPA_DIR / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("Latweeschu", source)

    def test_but_the_browser_reads_it(self):
        self.assertIn("Latweeschu", self.page.text)
        self.assertIn("Baltijas Wehstnesis", self.page.text)

    def test_late_rendered_content_is_waited_for(self):
        # rakstu tekstu lietotne uzzīmē ar 600 ms nokavēšanos pēc networkidle
        self.assertIn("Rihgas Latweeschu Beedriba", self.page.text)

    def test_app_generated_links_are_collected(self):
        self.assertTrue(any("page:2" in link for link in self.page.links))

    def test_data_layer_requests_are_logged(self):
        urls = [call.url for call in self.page.data_calls()]
        self.assertTrue(any(u.endswith("mets.xml") for u in urls))
        self.assertTrue(any("/alto/" in u for u in urls))

    def test_design_files_are_not_counted_as_data(self):
        for call in self.page.data_calls():
            self.assertFalse(call.url.endswith((".js", ".css")))

    def test_intercepted_bodies_are_saved_and_parseable(self):
        from periodika.alto import parse_alto

        alto_file = next(
            Path(p) for p in self.page.saved.values() if "0000000" in Path(p).name
        )
        page = parse_alto(alto_file.read_bytes())
        self.assertIn("Beedriba", page.text())

    def test_screenshot_is_written(self):
        self.assertTrue(Path(self.page.screenshot).exists())
        self.assertGreater(Path(self.page.screenshot).stat().st_size, 1000)

    def test_templates_derived_from_a_real_visit(self):
        urls = [c.url for c in self.page.data_calls()]
        kinds = {t["kind"]: t["template"] for t in generalize_urls(urls, "p_001_bw1899n01")}
        self.assertIn("{issue}", kinds["mets"])
        self.assertIn("{n:08d}", kinds["alto"])


@unittest.skipUnless(BROWSER["pieejams"], "nav headless pārlūka")
class TestCrawlerWithRendering(unittest.TestCase):
    def test_spa_pages_yield_documents_only_with_render(self):
        site = LocalSite(SPA_DIR)
        try:
            url = site.viewer_url()

            def make_config() -> Config:
                # katram rāpulim sava krātuve: atsākams rāpulis apstrādātu URL
                # otrreiz neņemtu, un salīdzinājums nebūtu godīgs
                return Config(
                    policy=CrawlPolicy(requests_per_second=1000, obey_robots=False),
                    # requires_javascript=False, lai pārbaudītu tieši --render ietekmi
                    profile=SiteProfile(
                        base_url=site.base, allowed_hosts=["127.0.0.1"],
                        requires_javascript=False,
                    ),
                    data_dir=Path(tempfile.mkdtemp()),
                )

            plain = Crawler(make_config(), render=False)
            plain.enqueue([url])
            plain.run(CrawlLimits(max_depth=1))
            self.assertEqual(plain.store.stats()["dokumenti"], 0)

            rendered = Crawler(make_config(), render=True)
            rendered.enqueue([url])
            rendered.run(CrawlLimits(max_depth=1))
            stats = rendered.store.stats()
            self.assertGreaterEqual(stats["dokumenti"], 1)
            # uzzīmētais teksts ir vecajā ortogrāfijā -> atrodams mūsdienu vaicājumā
            self.assertTrue(local_search(rendered.store, "biedrība"))
        finally:
            site.close()

    def test_profile_flag_enables_rendering_without_the_switch(self):
        site = LocalSite(SPA_DIR)
        try:
            config = Config(
                policy=CrawlPolicy(requests_per_second=1000, obey_robots=False),
                # noklusējuma profils zina, ka periodika2-viewer ir SPA
                profile=SiteProfile(base_url=site.base, allowed_hosts=["127.0.0.1"]),
                data_dir=Path(tempfile.mkdtemp()),
            )
            crawler = Crawler(config)          # bez render=True
            self.assertTrue(crawler.render)
            crawler.enqueue([site.viewer_url()])
            crawler.run(CrawlLimits(max_depth=1))
            self.assertGreaterEqual(crawler.store.stats()["dokumenti"], 1)
        finally:
            site.close()

    def test_data_urls_are_queued_for_the_fast_path(self):
        site = LocalSite(SPA_DIR)
        try:
            tmp = Path(tempfile.mkdtemp())
            config = Config(
                policy=CrawlPolicy(requests_per_second=1000, obey_robots=False),
                profile=SiteProfile(base_url=site.base, allowed_hosts=["127.0.0.1"]),
                data_dir=tmp,
            )
            crawler = Crawler(config, render=True)
            crawler.enqueue([site.viewer_url()])
            crawler.run(CrawlLimits(max_depth=2))
            rows = crawler.store._conn().execute(
                "SELECT url FROM frontier WHERE url LIKE '%mets.xml'"
            ).fetchall()
            self.assertTrue(rows, "METS URL netika ievietots frontē")
        finally:
            site.close()


if __name__ == "__main__":
    unittest.main()
