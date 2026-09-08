"""Atsākams pilnas vietnes rāpulis.

Stāvoklis dzīvo SQLite frontē, tāpēc procesu var nogalināt un palaist no jauna
bez darba zaudēšanas. Rāpulis apvieno visus atklāšanas ceļus:

    sitemap / OAI-PMH  ->  fronte  ->  ielāde  ->  izvilkšana  ->  jaunas saites

Papildus vispārīgajai apstaigāšanai ir ``crawl_issue`` — mērķtiecīga viena
laidiena ielāde pa datu slāni (METS -> ALTO -> raksti), kas ir vienīgais veids,
kā dabūt tīri sagrieztus rakstus, nevis lappušu tekstu.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from .config import Config
from .discovery import iter_oai_identifiers, iter_sitemap_urls
from .extract import documents_from_response, ids_from_url
from .http_client import FetchError, HttpClient
from .store import Document, Store

__all__ = ["Crawler", "CrawlLimits", "CrawlResult", "normalize_url"]

_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}


def normalize_url(url: str, *, keep_fragment: bool = True) -> str:
    """Kanoniskā URL forma.

    Fragmentu (``#panel:…|issue:/…``) NEDRĪKST mest prom: LNB skatītājā tieši
    tas identificē laidienu un rakstu.
    """
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https"):
        return ""
    netloc = parts.netloc.lower()
    if netloc.endswith(":80") and parts.scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and parts.scheme == "https":
        netloc = netloc[:-4]
    query = urllib.parse.urlencode(
        [
            (k, v)
            for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS
        ]
    )
    path = re.sub(r"/{2,}", "/", parts.path) or "/"
    fragment = parts.fragment if keep_fragment else ""
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, query, fragment))


@dataclass
class CrawlLimits:
    max_urls: int = 0          # 0 = bez ierobežojuma (pilna vietne)
    max_documents: int = 0
    max_depth: int = 12
    time_budget: float = 0.0   # sekundes; 0 = bez ierobežojuma
    max_pages_per_issue: int = 0


@dataclass
class CrawlResult:
    fetched: int = 0
    documents: int = 0
    errors: int = 0
    skipped: int = 0
    started_at: float = field(default_factory=time.time)

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    def as_dict(self) -> dict:
        return {
            "ielādēti": self.fetched,
            "dokumenti": self.documents,
            "kļūdas": self.errors,
            "izlaisti": self.skipped,
            "sekundes": round(self.elapsed, 1),
        }


class Crawler:
    def __init__(
        self,
        config: Config,
        store: Store | None = None,
        client: HttpClient | None = None,
        *,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.config = config
        self.profile = config.profile
        self.store = store or Store(config.db_path())
        self.client = client or HttpClient(config.policy, cache_path=config.cache_path())
        self.on_event = on_event or (lambda kind, data: None)
        self._deny = [re.compile(p, re.I) for p in self.profile.deny_patterns]
        self._prefer = [re.compile(p, re.I) for p in self.profile.prefer_patterns]

    # -- URL politika ---------------------------------------------------
    def in_scope(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        host = parts.netloc.lower().split(":")[0]
        if host not in {h.lower() for h in self.profile.allowed_hosts}:
            return False
        return not any(p.search(url) for p in self._deny)

    def priority(self, url: str) -> int:
        score = 0
        for pattern in self._prefer:
            if pattern.search(url):
                score += 10
        if url.endswith(".xml"):
            score += 5
        return score

    def enqueue(self, urls: Iterable[str], depth: int = 0, source: str = "") -> int:
        good: list[str] = []
        for raw in urls:
            url = normalize_url(raw)
            if url and self.in_scope(url):
                good.append(url)
        added = 0
        for url in dict.fromkeys(good):
            added += self.store.add_urls([url], depth=depth, priority=self.priority(url),
                                         discovered_from=source)
        return added

    # -- sēklas ---------------------------------------------------------
    def seed(self, extra_seeds: Sequence[str] = ()) -> int:
        base = self.profile.base_url.rstrip("/")
        seeds = [base + "/", *self.profile.seeds, *extra_seeds]
        added = self.enqueue(seeds, depth=0, source="seed")

        for sitemap in self.profile.sitemap_urls:
            batch: list[str] = []
            for url in iter_sitemap_urls(self.client, sitemap):
                batch.append(url)
                if len(batch) >= 1000:
                    added += self.enqueue(batch, depth=1, source=sitemap)
                    batch.clear()
            added += self.enqueue(batch, depth=1, source=sitemap)
            self.on_event("sitemap", {"url": sitemap, "kopā": added})

        for endpoint in self.profile.oai_endpoints:
            n = 0
            for record in iter_oai_identifiers(
                self.client, endpoint, self.profile.oai_metadata_prefix
            ):
                issue_id = record["identifier"].rsplit(":", 1)[-1]
                added += self.enqueue(self._issue_urls(issue_id), depth=1, source=endpoint)
                n += 1
            self.on_event("oai", {"endpoint": endpoint, "ieraksti": n})
        return added

    def _issue_urls(self, issue_id: str) -> list[str]:
        base = self.profile.base_url.rstrip("/")
        urls: list[str] = []
        for tpl in (
            self.profile.mets_url_template,
            self.profile.tei_url_template,
            self.profile.iiif_manifest_template,
        ):
            if tpl:
                try:
                    urls.append(tpl.format(base=base, issue=issue_id, n=1))
                except (KeyError, IndexError, ValueError):
                    continue
        if not urls and self.profile.hash_route_template:
            urls.append(
                self.profile.hash_route_template.format(
                    base=base, issue=issue_id, article="", n=1
                )
            )
        return urls

    # -- galvenā cilpa ---------------------------------------------------
    def run(self, limits: CrawlLimits | None = None) -> CrawlResult:
        limits = limits or CrawlLimits()
        result = CrawlResult()
        self.store.requeue_active()
        workers = max(1, self.config.policy.workers)

        def budget_exhausted() -> bool:
            if limits.time_budget and result.elapsed > limits.time_budget:
                return True
            if limits.max_urls and result.fetched >= limits.max_urls:
                return True
            if limits.max_documents and result.documents >= limits.max_documents:
                return True
            return False

        with ThreadPoolExecutor(max_workers=workers) as pool:
            while not budget_exhausted():
                rows = self.store.claim(workers * 4)
                if not rows:
                    break
                futures = {
                    pool.submit(self._process, r["url"], r["depth"], limits): r["url"]
                    for r in rows
                    if r["depth"] <= limits.max_depth
                }
                for r in rows:
                    if r["depth"] > limits.max_depth:
                        self.store.finish(r["url"], "skipped")
                        result.skipped += 1
                for future in as_completed(futures):
                    url = futures[future]
                    try:
                        n_docs, n_links = future.result()
                        result.fetched += 1
                        result.documents += n_docs
                        self.store.finish(url, "done")
                        self.on_event(
                            "fetched",
                            {"url": url, "dokumenti": n_docs, "saites": n_links,
                             "kopā": result.fetched},
                        )
                    except FetchError as exc:
                        result.errors += 1
                        self.store.record_resource(url, exc.status, error=exc.message)
                        self.store.finish(url, "failed")
                        self.on_event("error", {"url": url, "kļūda": exc.message})
                    except Exception as exc:  # noqa: BLE001
                        result.errors += 1
                        self.store.record_resource(url, None, error=repr(exc))
                        self.store.finish(url, "failed")
                        self.on_event("error", {"url": url, "kļūda": repr(exc)})
                    if budget_exhausted():
                        break
        self.store.set_meta("last_crawl", str(time.time()))
        return result

    def _process(self, url: str, depth: int, limits: CrawlLimits) -> tuple[int, int]:
        resp = self.client.get(url)
        self.store.record_resource(
            url, resp.status, resp.content_type, resp.sha256()
        )
        docs, links = documents_from_response(
            resp,
            self.profile,
            client=self.client,
            max_pages_per_issue=limits.max_pages_per_issue,
        )
        for doc in docs:
            self.store.upsert_document(doc)
        n_new = self.enqueue(links, depth=depth + 1, source=url)
        return len(docs), n_new

    # -- mērķtiecīga viena laidiena ielāde -------------------------------
    def crawl_issue(self, issue_id: str, *, max_pages: int = 0) -> list[Document]:
        """Ielādē vienu laidienu pa datu slāni un atgriež tā rakstus."""
        docs: list[Document] = []
        for url in self._issue_urls(issue_id):
            try:
                resp = self.client.get(url)
            except FetchError:
                continue
            found, links = documents_from_response(
                resp,
                self.profile,
                client=self.client,
                max_pages_per_issue=max_pages,
            )
            for doc in found:
                if not doc.issue_id:
                    doc.issue_id = issue_id
                self.store.upsert_document(doc)
            docs.extend(found)
            self.enqueue(links, depth=1, source=url)
            if docs:
                break
        return docs

    def status(self) -> dict:
        return {
            "fronte": self.store.frontier_counts(),
            "krātuve": self.store.stats(),
            "http": dict(self.client.stats),
        }
