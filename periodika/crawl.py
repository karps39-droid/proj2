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
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from .config import Config
from .discovery import iter_oai_identifiers, iter_rdf_aggregates, iter_sitemap_urls
from .extract import build_document, documents_from_response, ids_from_url
from .http_client import FetchError, HttpClient
from .store import Document, Store

__all__ = ["Crawler", "CrawlLimits", "CrawlResult", "normalize_url"]

#: Cik daudz uzzīmēta teksta pietiek, lai lapu uzskatītu par saturu.
_MIN_RENDERED_TEXT = 40

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


class _Renderer:
    """Pārlūks katram darbinieka pavedienam (Playwright sinhronā API nav koplietojama)."""

    def __init__(self, timeout: float, user_agent: str, save_data_to=None) -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self.save_data_to = save_data_to
        self._local = threading.local()
        self._all: list = []
        self._lock = threading.Lock()

    def reader(self):
        reader = getattr(self._local, "reader", None)
        if reader is None:
            from .browser import BrowserReader

            reader = BrowserReader(timeout=self.timeout, user_agent=self.user_agent).__enter__()
            self._local.reader = reader
            with self._lock:
                self._all.append(reader)
        return reader

    def read(self, url: str):
        return self.reader().read(url, save_data_to=self.save_data_to)

    def close(self) -> None:
        with self._lock:
            readers, self._all = self._all, []
        for reader in readers:
            try:
                reader.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass


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
        render: bool = False,
        render_save_data_to: str | None = None,
        via_browser: str = "",
    ) -> None:
        self.config = config
        self.profile = config.profile
        self.store = store or Store(config.db_path())
        self.client = client or HttpClient(config.policy, cache_path=config.cache_path())
        self.on_event = on_event or (lambda kind, data: None)
        self._deny = [re.compile(p, re.I) for p in self.profile.deny_patterns]
        self._prefer = [re.compile(p, re.I) for p in self.profile.prefer_patterns]
        #: Ja ieslēgts, lapas bez teksta HTML avotā tiek atvērtas īstā pārlūkā.
        self.render = render or self.profile.requires_javascript
        self._renderer = (
            _Renderer(
                timeout=self.config.policy.timeout,
                user_agent=self.config.policy.effective_user_agent(),
                save_data_to=render_save_data_to,
            )
            if self.render
            else None
        )
        #: Kad vietne atsaka klientam, pieprasījumu var izdarīt caur īstu pārlūku.
        self.via_browser = via_browser or self.config.policy.via_browser
        self._transport = None
        if self.via_browser in ("atkāpjoties", "vienmēr"):
            from .browser import BrowserTransport

            self.config.policy.via_browser = self.via_browser
            self._transport = BrowserTransport(
                warm_up_url=self.profile.base_url,
                timeout=self.config.policy.timeout,
                user_agent="",
            )
            self.client.fallback_transport = self._transport.fetch

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
    def seed(self, extra_seeds: Sequence[str] = (), *, time_budget: float = 0.0) -> int:
        """Sagatavo fronti. ``time_budget`` ierobežo RDF kataloga uzskaitīšanu.

        Bez tā pirmā palaišana pret periodika.lndb.lv 1820 izdevumus pie 1 pieprasījuma
        sekundē uzskaitītu ~pusstundu, pirms vispār sāktos rāpošana. Uzskaitīšana ir
        atsākama: aizpildītā fronte glabājas SQLite, un nākamā palaišana turpina.
        """
        started = time.time()

        def out_of_time() -> bool:
            return bool(time_budget) and (time.time() - started) > time_budget

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

        # RDF/ORE katalogs: periodika.lndb.lv "sitemap" ir RDF, nevis <urlset>,
        # tāpēc to uzskaita atsevišķi — izdevumi -> laidieni. Rakstu līmenī
        # neejam: raksta teksts prasa sesiju, un laidiena ieraksts jau satur
        # visu, kas vajadzīgs, ieskaitot PDF ar OCR slāni.
        if self.profile.rdf_sitemap_url:
            titles = 0
            issues = 0
            for periodic_url in iter_rdf_aggregates(
                self.client, self.profile.rdf_sitemap_url, only="/rdf/periodics/"
            ):
                if out_of_time():
                    self.on_event("rdf-daļēji", {"uzskaitīti_izdevumi": titles})
                    break
                titles += 1
                added += self.enqueue([periodic_url], depth=1,
                                      source=self.profile.rdf_sitemap_url)
                batch = list(iter_rdf_aggregates(self.client, periodic_url, only="/issue/"))
                issues += len(batch)
                added += self.enqueue(batch, depth=2, source=periodic_url)
            self.on_event("rdf", {"izdevumi": titles, "laidieni": issues, "kopā": added})

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
        if self._renderer is not None:
            self._renderer.close()
        if self._transport is not None:
            self._transport.close()
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
        if not docs and self._renderer is not None and resp.content_type.startswith("text/html"):
            rendered_docs, rendered_links = self._render(url)
            docs.extend(rendered_docs)
            links.extend(rendered_links)
        for doc in docs:
            self.store.upsert_document(doc)
        n_new = self.enqueue(links, depth=depth + 1, source=url)
        return len(docs), n_new

    def _render(self, url: str) -> tuple[list[Document], list[str]]:
        """Atver lapu pārlūkā: nolasa uzzīmēto tekstu un pieraksta datu pieprasījumus."""
        from .browser import BrowserUnavailable

        try:
            page = self._renderer.read(url)  # type: ignore[union-attr]
        except BrowserUnavailable as exc:
            self.on_event("render", {"url": url, "kļūda": str(exc)})
            self._renderer = None  # vairs nemēģinām
            return [], []
        except Exception as exc:  # noqa: BLE001
            self.on_event("render", {"url": url, "kļūda": repr(exc)})
            return [], []

        ids = ids_from_url(url, self.profile)
        docs: list[Document] = []
        # Slieksnis šeit ir zemāks nekā HTML ceļā: ja lapu apzināti atvērām
        # pārlūkā, pat īss uzzīmēts fragments ir saturs, nevis navigācija.
        if len(page.text) >= _MIN_RENDERED_TEXT:
            docs.append(
                build_document(
                    url=url,
                    kind="web",
                    source_type="browser",
                    text=page.text,
                    title=page.title,
                    profile=self.profile,
                    issue_id=ids["issue_id"],
                    article_id=ids["article_id"],
                    page_number=int(ids["page"]) if ids["page"].isdigit() else None,
                    metadata={"uzzīmēts_pārlūkā": True,
                              "datu_pieprasījumi": [c.url for c in page.data_calls()][:20]},
                )
            )
        # Datu slāņa URL ir vērtīgāki par HTML saitēm — tos liekam frontē pirmos.
        links = [c.url for c in page.data_calls()] + list(page.links)
        self.on_event("render", {"url": url, "teksts": len(page.text),
                                 "datu_pieprasījumi": len(page.data_calls())})
        return docs, links

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
