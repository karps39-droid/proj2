"""Meklēšana: vietnē (ar vecās drukas vaicājumu paplašināšanu) un lokāli.

Galvenā doma: 19. gs. avīzes OCR indeksā glabājas *vecajā* rakstībā. Meklējot
``sabiedrība``, vietne neatradīs neko — jāmeklē arī ``sabeedriba``,
``ſabeedriba``, ``sabeedrihba`` utt. ``site_search`` to izdara automātiski un
apvieno rezultātus, atzīmējot, kurš vaicājuma variants trāpīja.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable

from .config import SiteProfile
from .extract import ids_from_url
from .htmlutil import parse_html
from .http_client import FetchError, HttpClient
from .orthography import expand_query, fold
from .store import Document, Store

__all__ = ["SearchHit", "site_search", "local_search"]


@dataclass
class SearchHit:
    url: str
    title: str = ""
    snippet: str = ""
    issue_id: str = ""
    article_id: str = ""
    page: str = ""
    matched_query: str = ""
    source: str = "site"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "virsraksts": self.title,
            "fragments": self.snippet,
            "laidiens": self.issue_id,
            "raksts": self.article_id,
            "lappuse": self.page,
            "trāpīja_vaicājums": self.matched_query,
            "avots": self.source,
            **({"papildus": self.extra} if self.extra else {}),
        }


def site_search(
    client: HttpClient,
    profile: SiteProfile,
    query: str,
    *,
    expand_old_orthography: bool = True,
    max_variants: int = 6,
    limit: int = 25,
    page: int = 1,
) -> list[SearchHit]:
    """Meklē dzīvajā vietnē, izmēģinot arī vecās rakstības variantus."""
    if not profile.search_url_template:
        raise RuntimeError(
            "Profilā nav meklēšanas veidnes. Palaid `periodika probe`, "
            "vai norādi to manuāli konfigurācijā (search_url_template)."
        )
    queries = expand_query(query, max_queries=max_variants) if expand_old_orthography else [query]
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for q in queries:
        url = profile.search_url_template.format(q=urllib.parse.quote(q), page=page)
        try:
            resp = client.get(url)
        except FetchError:
            continue
        for hit in _parse_search_response(resp, profile, q):
            key = hit.url or (hit.issue_id + hit.article_id)
            if key and key not in seen:
                seen.add(key)
                hits.append(hit)
            if len(hits) >= limit:
                return hits
    return hits


def _parse_search_response(resp, profile: SiteProfile, matched_query: str) -> list[SearchHit]:
    body = resp.body.lstrip()[:1]
    if body in (b"{", b"["):
        return _parse_json_hits(resp.text(), resp.url, profile, matched_query)
    doc = parse_html(resp.text(), resp.url)
    out: list[SearchHit] = []
    for link in doc.links:
        ids = ids_from_url(link, profile)
        if not (ids["issue_id"] or ids["article_id"]):
            continue
        out.append(
            SearchHit(
                url=link,
                title=doc.link_texts.get(link, ""),
                issue_id=ids["issue_id"],
                article_id=ids["article_id"],
                page=ids["page"],
                matched_query=matched_query,
            )
        )
    return out


def _parse_json_hits(payload: str, base_url: str, profile: SiteProfile, matched_query: str):
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return []
    rows: list[dict] = []

    def collect(node: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, list):
            for item in node:
                collect(item, depth + 1)
        elif isinstance(node, dict):
            keys = {k.lower() for k in node}
            if keys & {"url", "href", "id", "identifier"} and keys & {
                "title", "label", "name", "snippet", "text", "heading"
            }:
                rows.append(node)
            for value in node.values():
                collect(value, depth + 1)

    collect(data)
    out: list[SearchHit] = []
    for row in rows:
        raw_url = str(row.get("url") or row.get("href") or row.get("id") or "")
        url = urllib.parse.urljoin(base_url, raw_url) if raw_url else ""
        ids = ids_from_url(url or json.dumps(row, ensure_ascii=False), profile)
        out.append(
            SearchHit(
                url=url,
                title=str(row.get("title") or row.get("label") or row.get("name") or ""),
                snippet=str(row.get("snippet") or row.get("text") or "")[:400],
                issue_id=ids["issue_id"],
                article_id=ids["article_id"],
                page=ids["page"],
                matched_query=matched_query,
            )
        )
    return out


def local_search(
    store: Store, query: str, *, limit: int = 20, expand_old_orthography: bool = True
) -> list[SearchHit]:
    """Meklē jau savāktajā lokālajā indeksā (ātri, bez tīkla)."""
    results = store.search(query, limit=limit, fold_query=expand_old_orthography)
    out: list[SearchHit] = []
    for doc, snippet in results:
        out.append(
            SearchHit(
                url=doc.viewer_url or doc.url,
                title=doc.title or f"{doc.publication} {doc.date}".strip(),
                snippet=snippet,
                issue_id=doc.issue_id,
                article_id=doc.article_id,
                page=str(doc.page_number or ""),
                matched_query=query,
                source="local",
                extra={
                    "dokumenta_id": doc.id,
                    "ortogrāfija": doc.orthography,
                    "datums": doc.date,
                },
            )
        )
    return out
