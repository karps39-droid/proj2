"""Cauruļvada testi: izvilkšana, krātuve, rāpulis un MCP — bez tīkla.

Vietnes vietā lieto ``FakeSite``, kas atdod iepriekš sagatavotas atbildes,
tāpēc testi ir ātri un atkārtojami.
"""

import io
import json
import tempfile
import unittest
from pathlib import Path

from periodika.config import Config, CrawlPolicy, SiteProfile
from periodika.crawl import CrawlLimits, Crawler, normalize_url
from periodika.extract import documents_from_response, identify, ids_from_url
from periodika.http_client import FetchError, Response
from periodika.mcp_server import build_tools
from periodika.search import local_search
from periodika.store import Document, Store

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://periodika.lndb.lv"


class FakeSite:
    """Minimāls HttpClient aizstājējs ar iepriekš sagatavotu saturu."""

    def __init__(self, pages: dict[str, tuple[str, bytes]]):
        self.pages = pages
        self.requested: list[str] = []
        self.stats = {"requests": 0}

    def get(self, url: str, **kwargs) -> Response:
        self.requested.append(url)
        self.stats["requests"] += 1
        if url not in self.pages:
            raise FetchError(url, "HTTP 404", status=404)
        content_type, body = self.pages[url]
        return Response(url=url, status=200, headers={"content-type": content_type}, body=body)

    def sitemaps_from_robots(self, base_url: str):
        return []

    def allowed(self, url: str) -> bool:
        return True


def make_site() -> FakeSite:
    mets = (FIXTURES / "mets_issue.xml").read_bytes()
    alto = (FIXTURES / "alto_page1.xml").read_bytes()
    index_html = f"""<html lang="lv"><head><title>Periodika</title></head><body>
        <nav><a href="/par">Par mums</a></nav>
        <main>
          <a href="/periodika2-data/p_001_bw1899n01/mets.xml">Baltijas Wehſtneſis 1899</a>
        </main></body></html>""".encode()
    return FakeSite(
        {
            f"{BASE}/": ("text/html", index_html),
            f"{BASE}/par": ("text/html", b"<html><body><p>" + b"Par portalu. " * 30 + b"</p></body></html>"),
            f"{BASE}/periodika2-data/p_001_bw1899n01/mets.xml": ("text/xml", mets),
            f"{BASE}/periodika2-data/p_001_bw1899n01/alto/0001.xml": ("text/xml", alto),
        }
    )


def make_config(tmp: Path) -> Config:
    profile = SiteProfile(
        base_url=BASE,
        mets_url_template=BASE + "/periodika2-data/{issue}/mets.xml",
    )
    return Config(policy=CrawlPolicy(requests_per_second=1000), profile=profile, data_dir=tmp)


class TestIdentify(unittest.TestCase):
    def _resp(self, body: bytes, ct: str = "") -> Response:
        return Response(url="x", status=200, headers={"content-type": ct}, body=body)

    def test_detects_formats_by_content(self):
        self.assertEqual(identify(self._resp(b'<?xml version="1.0"?><alto xmlns="x">')), "alto")
        self.assertEqual(identify(self._resp(b"<mets:mets xmlns:mets='x'>")), "mets")
        self.assertEqual(identify(self._resp(b"<TEI xmlns='x'>")), "tei")
        self.assertEqual(identify(self._resp(b'{"a":1}')), "json")
        self.assertEqual(identify(self._resp(b"<!DOCTYPE html><html>")), "html")
        self.assertEqual(identify(self._resp(b"vienkarss teksts", "text/plain")), "text")


class TestIdsFromUrl(unittest.TestCase):
    def test_hash_route_of_lnb_viewer(self):
        url = (BASE + "/periodika2-viewer/?lang=lv#panel:pa|issue:/p_001_bw1899n01"
               "|article:DIVL75|page:3")
        ids = ids_from_url(url, SiteProfile())
        self.assertEqual(ids["issue_id"], "p_001_bw1899n01")
        self.assertEqual(ids["article_id"], "DIVL75")
        self.assertEqual(ids["page"], "3")

    def test_percent_encoded_fragment(self):
        url = BASE + "/v/?x#panel:pa%7Cissue:/p_002_mw1885n07"
        self.assertEqual(ids_from_url(url, SiteProfile())["issue_id"], "p_002_mw1885n07")


class TestNormalizeUrl(unittest.TestCase):
    def test_keeps_viewer_fragment(self):
        url = BASE + "/v/#issue:/p_001"
        self.assertEqual(normalize_url(url), url)

    def test_drops_tracking_params_and_default_port(self):
        self.assertEqual(
            normalize_url("https://Periodika.LNDB.lv:443/a?utm_source=x&lang=lv"),
            "https://periodika.lndb.lv/a?lang=lv",
        )

    def test_rejects_non_http(self):
        self.assertEqual(normalize_url("ftp://periodika.lndb.lv/a"), "")


class TestExtraction(unittest.TestCase):
    def test_mets_plus_alto_yields_articles_with_both_orthographies(self):
        site = make_site()
        profile = SiteProfile(base_url=BASE)
        resp = site.get(f"{BASE}/periodika2-data/p_001_bw1899n01/mets.xml")
        docs, links = documents_from_response(resp, profile, client=site)
        self.assertEqual(len(docs), 2)
        article = docs[0]
        self.assertEqual(article.kind, "article")
        self.assertEqual(article.title, "Rihgas Latweeschu Beedriba")
        self.assertEqual(article.date, "1899-05-01")
        self.assertEqual(article.publication, "Baltijas Wehſtneſis")
        self.assertIn("Beedriba", article.text_raw)
        self.assertIn("Biedriba", article.text_modern)  # vecā -> mūsdienu rakstība
        self.assertEqual(article.orthography, "veca")
        self.assertIsNotNone(article.ocr_confidence)
        self.assertIn(f"{BASE}/periodika2-data/p_001_bw1899n01/alto/0001.xml", links)

    def test_viewer_url_is_built_for_humans(self):
        site = make_site()
        profile = SiteProfile(base_url=BASE)
        resp = site.get(f"{BASE}/periodika2-data/p_001_bw1899n01/mets.xml")
        docs, _ = documents_from_response(resp, profile, client=site)
        self.assertIn("issue:/", docs[0].viewer_url)
        self.assertIn("article:DIVL2", docs[0].viewer_url)

    def test_html_page_becomes_web_document_with_links(self):
        site = make_site()
        resp = site.get(f"{BASE}/")
        docs, links = documents_from_response(resp, SiteProfile(base_url=BASE), client=site)
        self.assertIn(f"{BASE}/periodika2-data/p_001_bw1899n01/mets.xml", links)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp / "db.sqlite3")

    def _doc(self, doc_id="d1", **kw):
        base = dict(
            id=doc_id, kind="article", title="Wehſtneſis",
            text_raw="Rihgas Latweeschu Beedriba ſchodeen ſapulzejahs.",
            text_modern="Rīgas Latviešu Biedriba šodien sapulcējās.",
            date="1899-05-01", issue_id="p_001_bw1899n01", orthography="veca",
        )
        base.update(kw)
        return Document(**base)

    def test_modern_query_finds_old_text(self):
        self.store.upsert_document(self._doc())
        self.assertTrue(local_search(self.store, "šodien"))
        self.assertTrue(local_search(self.store, "biedrība"))

    def test_old_query_finds_document_too(self):
        self.store.upsert_document(self._doc())
        self.assertTrue(local_search(self.store, "Beedriba"))

    def test_reindex_removes_stale_text(self):
        self.store.upsert_document(self._doc(text_raw="pirmais teksts par mahjahm"))
        self.store.upsert_document(self._doc(text_raw="otrais teksts"))
        self.assertFalse(local_search(self.store, "mahjahm"))
        self.assertTrue(local_search(self.store, "otrais"))

    def test_frontier_claim_is_not_repeated(self):
        self.store.add_urls([f"{BASE}/a", f"{BASE}/b"])
        first = [r["url"] for r in self.store.claim(1)]
        second = [r["url"] for r in self.store.claim(1)]
        self.assertNotEqual(first, second)
        self.assertEqual(len(first) + len(second), 2)

    def test_requeue_active_after_crash(self):
        self.store.add_urls([f"{BASE}/a"])
        self.store.claim(1)
        self.assertEqual(self.store.requeue_active(), 1)
        self.assertEqual(len(self.store.claim(1)), 1)

    def test_export_jsonl(self):
        self.store.upsert_document(self._doc())
        out = self.tmp / "out.jsonl"
        self.assertEqual(self.store.export_jsonl(out), 1)
        row = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["issue_id"], "p_001_bw1899n01")


class TestCrawler(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = make_config(self.tmp)
        self.site = make_site()
        self.crawler = Crawler(self.config, client=self.site)

    def test_full_crawl_collects_articles(self):
        self.crawler.seed()
        result = self.crawler.run(CrawlLimits(max_depth=3))
        self.assertGreaterEqual(result.documents, 2)
        self.assertEqual(result.errors, 0)
        hits = local_search(self.crawler.store, "biedrība")
        self.assertTrue(hits)
        self.assertEqual(self.crawler.store.frontier_counts().get("pending", 0), 0)

    def test_crawl_is_resumable(self):
        self.crawler.seed()
        self.crawler.run(CrawlLimits(max_urls=1, max_depth=3))
        first = self.crawler.store.stats()["dokumenti"]
        self.crawler.run(CrawlLimits(max_depth=3))
        self.assertGreaterEqual(self.crawler.store.stats()["dokumenti"], first)

    def test_out_of_scope_urls_are_not_queued(self):
        self.assertEqual(self.crawler.enqueue(["https://example.com/x"]), 0)
        self.assertFalse(self.crawler.in_scope("https://example.com/x"))

    def test_deny_patterns_are_respected(self):
        self.assertFalse(self.crawler.in_scope(BASE + "/a.zip"))
        self.assertTrue(self.crawler.in_scope(BASE + "/a.xml"))

    def test_crawl_issue_targets_data_layer(self):
        docs = self.crawler.crawl_issue("p_001_bw1899n01")
        self.assertEqual(len(docs), 2)
        self.assertTrue(all(d.issue_id for d in docs))


class TestMcpTools(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = make_config(self.tmp)
        self.tools = build_tools(self.config)

    def call(self, name, args):
        return self.tools[name][1](args)

    def test_every_tool_has_a_schema(self):
        for name, (schema, _) in self.tools.items():
            self.assertIn("description", schema, name)
            self.assertEqual(schema["inputSchema"]["type"], "object", name)

    def test_normalize_tool(self):
        out = self.call("periodika_normalize_text", {"text": "Wehſtneſis ſchodeen"})
        self.assertEqual(out["text_modern"], "Vēstnesis šodien")

    def test_detect_tool(self):
        self.assertEqual(
            self.call("periodika_detect_orthography", {"text": "Šodien Rīgā"})["secinājums"],
            "mūsdienu rakstība",
        )

    def test_search_tool_reports_tried_variants(self):
        out = self.call("periodika_search", {"query": "sabiedrība"})
        self.assertIn("sabeedriba", out["izmēģinātie_varianti"])
        self.assertEqual(out["rezultāti"], [])

    def test_read_tool_explains_missing_document(self):
        self.assertIn("kļūda", self.call("periodika_read_article", {"document_id": "nav"}))


class TestMcpProtocol(unittest.TestCase):
    def test_stdio_session(self):
        from periodika.mcp_server import serve

        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "periodika_expand_query", "arguments": {"query": "skola"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "nav_taada", "arguments": {}}},
        ]
        stdin = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
        stdout = io.StringIO()
        serve(make_config(Path(tempfile.mkdtemp())), stdin=stdin, stdout=stdout)
        messages = [json.loads(line) for line in stdout.getvalue().splitlines()]
        by_id = {m.get("id"): m for m in messages}
        self.assertEqual(by_id[1]["result"]["serverInfo"]["name"], "periodika")
        self.assertTrue(by_id[2]["result"]["tools"])
        payload = json.loads(by_id[3]["result"]["content"][0]["text"])
        self.assertIn("ſkola", payload["varianti"])
        self.assertIn("error", by_id[4])   # nezināms rīks -> kļūda, nevis avārija


if __name__ == "__main__":
    unittest.main()
