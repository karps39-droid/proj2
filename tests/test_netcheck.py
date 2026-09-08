"""Bloķēšanas diagnostikas un apiešanas testi.

Visi bloķēšanas veidi tiek atdarināti lokāli: aizvērts ports, starpniekserveris,
kas atsaka ar 403, un vietne, kas atsaka *klientam* (pēc User-Agent), bet atbild
īstam pārlūkam. Pret dzīvām vietnēm netiek iets.
"""

import functools
import http.server
import socket
import socketserver
import threading
import unittest
from pathlib import Path

from periodika.browser import browser_available
from periodika.config import CrawlPolicy
from periodika.http_client import FetchError, HttpClient
from periodika.netcheck import _bypasses_proxy, _check_dns, _check_tcp, diagnose

BROWSER = browser_available()
SPA_DIR = Path(__file__).parent / "fixtures" / "spa"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Server:
    """Vietējs HTTP serveris ar norādīto apstrādātāju."""

    def __init__(self, handler_cls):
        self.httpd = socketserver.TCPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class _PlainHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SPA_DIR), **kwargs)

    def log_message(self, *args):  # noqa: A003
        pass


class _BotWallHandler(http.server.BaseHTTPRequestHandler):
    """Atdarina vietni, kas atsaka ne-pārlūka klientiem."""

    def do_GET(self):  # noqa: N802
        agent = self.headers.get("User-Agent", "")
        looks_like_browser = "Mozilla" in agent and "Chrome" in agent
        body = b"<html><body>Rihgas Latweeschu Beedriba</body></html>"
        self.send_response(200 if looks_like_browser else 403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if looks_like_browser:
            self.wfile.write(body)

    def log_message(self, *args):  # noqa: A003
        pass


class _ProxyDenyHandler(http.server.BaseHTTPRequestHandler):
    """Atdarina izejas starpniekserveri, kas neatļauj saimniekdatoru."""

    protocol_version = "HTTP/1.1"

    def do_CONNECT(self):  # noqa: N802
        self.send_response(403, "Forbidden")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # noqa: A003
        pass


class TestLayerChecks(unittest.TestCase):
    def test_dns_failure_is_named(self):
        layer = _check_dns("nav-tadas-vietnes.periodika-tests.invalid")
        self.assertIs(layer.ok, False)
        self.assertIn("DNS", layer.name)
        self.assertTrue(layer.remedy)

    def test_closed_port_is_reported_as_tcp(self):
        layer = _check_tcp("127.0.0.1", _free_port(), 2.0)
        self.assertIs(layer.ok, False)
        self.assertIn("proxy", layer.remedy.lower())

    def test_no_proxy_list_is_honoured(self):
        import os
        from unittest import mock

        # abi mainīgie vienlaikus: vides jau uzstādītais nedrīkst aizēnot mūsējo
        with mock.patch.dict(os.environ, {"NO_PROXY": "example.com,.lndb.lv",
                                          "no_proxy": "localhost"}, clear=False):
            self.assertTrue(_bypasses_proxy("periodika.lndb.lv"))
            self.assertTrue(_bypasses_proxy("example.com"))
            self.assertTrue(_bypasses_proxy("localhost"))
            self.assertFalse(_bypasses_proxy("periodika.lv"))


class TestDiagnosisAgainstLocalSite(unittest.TestCase):
    def setUp(self):
        self.site = _Server(_PlainHandler)
        self.addCleanup(self.site.close)

    def test_working_site_has_no_failing_layer(self):
        result = diagnose(f"{self.site.base}/index.html", check_browser=False)
        self.assertEqual([l.name for l in result.layers if l.ok is False], [])
        self.assertIn("kārtībā", result.verdict)

    def test_unreachable_port_produces_remedy(self):
        result = diagnose(f"http://127.0.0.1:{_free_port()}/", check_browser=False)
        self.assertTrue(any(l.ok is False for l in result.layers))
        self.assertTrue(result.remedies)

    def test_report_serialises_to_json_and_text(self):
        result = diagnose(f"{self.site.base}/index.html", check_browser=False)
        payload = result.to_json()
        self.assertIn("slāņi", payload)
        self.assertIn("periodika netcheck", result.as_text())


class TestProxyDenial(unittest.TestCase):
    def test_proxy_403_is_named_as_policy_not_site(self):
        proxy = _Server(_ProxyDenyHandler)
        self.addCleanup(proxy.close)
        result = diagnose(
            "https://periodika.lndb.lv/",
            proxy=f"http://127.0.0.1:{proxy.port}",
            check_browser=False,
        )
        layer = next(l for l in result.layers if l.name == "Starpniekserveris")
        self.assertIs(layer.ok, False)
        self.assertIn("403", layer.detail)
        self.assertIn("politikas", layer.remedy)
        self.assertIn("neapiet", result.verdict)


class TestBotWall(unittest.TestCase):
    def setUp(self):
        self.site = _Server(_BotWallHandler)
        self.addCleanup(self.site.close)

    def test_plain_client_is_refused(self):
        client = HttpClient(CrawlPolicy(max_retries=0, obey_robots=False))
        with self.assertRaises(FetchError) as ctx:
            client.get(self.site.base + "/lapa", check_robots=False)
        self.assertEqual(ctx.exception.status, 403)

    def test_http_403_gets_a_client_side_remedy(self):
        result = diagnose(self.site.base + "/lapa", check_browser=False)
        layer = next(l for l in result.layers if l.name == "HTTP")
        self.assertIs(layer.ok, False)
        self.assertIn("via-browser", layer.remedy)

    @unittest.skipUnless(BROWSER["pieejams"], "nav headless pārlūka")
    def test_browser_fallback_rescues_the_request(self):
        from periodika.browser import BrowserTransport

        client = HttpClient(CrawlPolicy(max_retries=0, obey_robots=False))
        with BrowserTransport(timeout=30) as transport:
            client.fallback_transport = transport.fetch
            resp = client.get(self.site.base + "/lapa", check_robots=False, use_cache=False)
        self.assertEqual(resp.status, 200)
        self.assertIn("Beedriba", resp.text())
        self.assertEqual(client.stats["browser_fallbacks"], 1)

    @unittest.skipUnless(BROWSER["pieejams"], "nav headless pārlūka")
    def test_diagnosis_says_the_site_blocks_the_client_not_the_network(self):
        result = diagnose(self.site.base + "/lapa", check_browser=True)
        browser = next(l for l in result.layers if l.name == "Pārlūks")
        self.assertIs(browser.ok, True)
        self.assertIn("pati vietne", result.verdict)


class TestHeaderSafety(unittest.TestCase):
    def test_latvian_contact_does_not_break_requests(self):
        site = _Server(_PlainHandler)
        self.addCleanup(site.close)
        policy = CrawlPolicy(max_retries=0, obey_robots=False, contact="Jānis Ozoliņš")
        client = HttpClient(policy)
        resp = client.get(site.base + "/index.html", check_robots=False, use_cache=False)
        self.assertEqual(resp.status, 200)

    def test_user_agent_is_latin1_encodable(self):
        CrawlPolicy(contact="Ā-Ē-Ī").effective_user_agent().encode("latin-1")


if __name__ == "__main__":
    unittest.main()
