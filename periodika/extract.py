"""No ielādēta resursa uz ``Document`` ierakstiem.

Šeit satiekas visi pārējie moduļi: resursa tips tiek noteikts pēc satura (nevis
tikai pēc paplašinājuma), teksts izvilkts ar attiecīgo parseri, un beigās
vienmēr izlaists caur ``orthography.normalize_article_text`` — tā katram
dokumentam ir gan oriģinālais teksts, gan mūsdienu rakstības versija.
"""

from __future__ import annotations

import hashlib
import re
import urllib.parse
from typing import Iterable

from . import alto as alto_mod
from .config import SiteProfile
from .htmlutil import parse_html
from .http_client import FetchError, HttpClient, Response
from .latvian import analyze_article
from .store import Document

__all__ = ["identify", "ids_from_url", "documents_from_response", "build_document"]

_DATE_RE = re.compile(r"(1[5-9]\d{2}|20\d{2})[-./](\d{1,2})[-./](\d{1,2})")
_YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20[0-2]\d)\b")


def identify(resp: Response) -> str:
    """Nosaka resursa tipu pēc satura pirmajiem baitiem un content-type."""
    head = resp.body[:2048].lstrip()
    ct = resp.content_type
    if head[:1] == b"<":
        low = head.lower()
        if b"<alto" in low:
            return "alto"
        if b"<mets" in low or b"mets/" in low:
            return "mets"
        if b"<tei" in low:
            return "tei"
        if b"urlset" in low or b"sitemapindex" in low:
            return "sitemap"
        if b"oai-pmh" in low:
            return "oai"
        if b"<html" in low or b"<!doctype html" in low or ct.startswith("text/html"):
            return "html"
        return "xml"
    if head[:1] in (b"{", b"["):
        return "json"
    if ct.startswith("text/html"):
        return "html"
    if ct.startswith("text/") or not ct:
        return "text"
    return "binary"


def ids_from_url(url: str, profile: SiteProfile) -> dict[str, str]:
    """Izvelk laidiena / raksta / lappuses ID arī no hash-maršruta.

    LNB skatītāja saite izskatās šādi:
    ``…/periodika2-viewer/?lang=lv#panel:pa|issue:/p_001_abcd1899n01|article:DIVL75|page:3``
    """
    decoded = urllib.parse.unquote(url)
    out = {"issue_id": "", "article_id": "", "page": ""}
    for pattern in profile.issue_id_patterns:
        m = re.search(pattern, decoded)
        if m:
            out["issue_id"] = m.group("id").strip("/")
            break
    for pattern in profile.article_id_patterns:
        m = re.search(pattern, decoded)
        if m:
            out["article_id"] = m.group("id")
            break
    for pattern in profile.page_id_patterns:
        m = re.search(pattern, decoded)
        if m:
            out["page"] = m.group("id")
            break
    return out


def _guess_date(*candidates: str) -> str:
    for value in candidates:
        if not value:
            continue
        m = _DATE_RE.search(value)
        if m:
            y, mo, d = m.groups()
            return f"{y}-{int(mo):02d}-{int(d):02d}"
    for value in candidates:
        if not value:
            continue
        m = _YEAR_RE.search(value)
        if m:
            return m.group(1)
    return ""


def _doc_id(*parts: str) -> str:
    raw = "|".join(p for p in parts if p)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def build_document(
    *,
    url: str,
    kind: str,
    source_type: str,
    text: str,
    title: str = "",
    profile: SiteProfile | None = None,
    issue_id: str = "",
    article_id: str = "",
    page_number: int | None = None,
    publication: str = "",
    date: str = "",
    language: str = "",
    ocr_confidence: float | None = None,
    metadata: dict | None = None,
) -> Document:
    norm = analyze_article(text)
    viewer_url = ""
    if profile and profile.hash_route_template and issue_id:
        viewer_url = profile.hash_route_template.format(
            base=profile.base_url.rstrip("/"),
            issue=issue_id,
            article=article_id or "",
            n=page_number or 1,
        )
    language = language or str(norm["language"])
    if not date and norm.get("date"):
        date = str(norm["date"])
    return Document(
        id=_doc_id(url, issue_id, article_id, str(page_number or ""), title[:60]),
        kind=kind,
        url=url,
        viewer_url=viewer_url,
        issue_id=issue_id,
        article_id=article_id,
        page_number=page_number,
        publication=publication,
        title=title.strip(),
        date=date,
        language=language,
        text_raw=str(norm["text_raw"]),
        text_modern=str(norm["text_modern"]),
        orthography=str(norm["orthography"]),
        old_score=float(norm["old_score"]),  # type: ignore[arg-type]
        ocr_confidence=ocr_confidence,
        source_type=source_type,
        metadata={
            **(metadata or {}),
            "valoda": norm["language_name"],
            "valodas_ticamība": norm["language_confidence"],
            **({"vietvārdi": norm["places"]} if norm["places"] else {}),
            **({"datuma_detaļas": norm["date_details"]} if norm.get("date_details") else {}),
        },
    )


def documents_from_response(
    resp: Response,
    profile: SiteProfile,
    *,
    client: HttpClient | None = None,
    fetch_linked_alto: bool = True,
    max_pages_per_issue: int = 0,
) -> tuple[list[Document], list[str]]:
    """Atgriež (dokumenti, jaunatklātie URL).

    METS gadījumā, ja padots ``client``, tiek ielādēti arī tajā norādītie ALTO
    faili, lai rakstus varētu salikt kopā — tas ir vienīgais ceļš, kā dabūt
    *rakstu*, nevis lappusi.
    """
    kind = identify(resp)
    ids = ids_from_url(resp.url, profile)
    docs: list[Document] = []
    links: list[str] = []

    if kind == "alto":
        page = alto_mod.parse_alto(resp.body, resp.url)
        text = page.text(reading_order="columns")
        if text.strip():
            docs.append(
                build_document(
                    url=resp.url,
                    kind="page",
                    source_type="alto",
                    text=text,
                    profile=profile,
                    issue_id=ids["issue_id"],
                    page_number=page.number or (int(ids["page"]) if ids["page"].isdigit() else None),
                    language=page.language,
                    ocr_confidence=page.confidence,
                    metadata={"alto_page_id": page.id, "blocks": len(page.blocks)},
                )
            )
        return docs, links

    if kind == "mets":
        mets = alto_mod.parse_mets(resp.body, resp.url)
        meta = mets.metadata
        issue_id = ids["issue_id"] or meta.get("identifier", "")
        date = _guess_date(meta.get("date", ""), meta.get("dateIssued", ""), resp.url)
        publication = meta.get("publication") or meta.get("title", "")
        pages: dict[str, alto_mod.AltoPage] = {}
        alto_files = mets.alto_files()
        if max_pages_per_issue:
            alto_files = alto_files[:max_pages_per_issue]
        for f in alto_files:
            target = urllib.parse.urljoin(resp.url, f.href)
            links.append(target)
            if not (fetch_linked_alto and client):
                continue
            try:
                page_resp = client.get(target)
            except FetchError:
                continue
            try:
                pages[f.id] = alto_mod.parse_alto(page_resp.body, target)
            except Exception:
                continue
        articles = alto_mod.assemble_articles(mets, pages)
        for art in articles:
            docs.append(
                build_document(
                    url=resp.url,
                    kind="article" if art.type.upper() != "PAGE" else "page",
                    source_type="mets+alto",
                    text=art.text,
                    title=art.title,
                    profile=profile,
                    issue_id=issue_id,
                    article_id=art.id,
                    page_number=art.page_numbers[0] if art.page_numbers else None,
                    publication=publication,
                    date=date,
                    language=meta.get("language", ""),
                    ocr_confidence=art.confidence,
                    metadata={
                        "mets_type": art.type,
                        "pages": art.page_numbers,
                        "blocks": len(art.block_ids),
                        **{k: v for k, v in meta.items() if k not in ("title",)},
                    },
                )
            )
        if not articles and not pages:
            # vismaz saglabājam laidiena metadatus, lai zinām, ka tas apstrādāts
            docs.append(
                build_document(
                    url=resp.url,
                    kind="issue",
                    source_type="mets",
                    text="",
                    title=meta.get("title", ""),
                    profile=profile,
                    issue_id=issue_id,
                    publication=publication,
                    date=date,
                    metadata=dict(meta),
                )
            )
        return docs, links

    if kind == "tei":
        for art in alto_mod.parse_tei(resp.body, resp.url):
            docs.append(
                build_document(
                    url=resp.url,
                    kind="article",
                    source_type="tei",
                    text=art.text,
                    title=art.title,
                    profile=profile,
                    issue_id=ids["issue_id"],
                    article_id=art.id,
                    date=_guess_date(art.metadata.get("date", ""), resp.url),
                    publication=art.metadata.get("title", ""),
                    metadata=art.metadata,
                )
            )
        return docs, links

    if kind in ("html", "xml"):
        doc = parse_html(resp.text(), resp.url)
        links = list(doc.links)
        text = doc.main_text
        if len(text) >= 200 or doc.meta_get("citation_title", "og:title"):
            docs.append(
                build_document(
                    url=resp.url,
                    kind="web",
                    source_type="html",
                    text=text,
                    title=doc.meta_get("citation_title", "og:title", "dc.title") or doc.title,
                    profile=profile,
                    issue_id=ids["issue_id"],
                    article_id=ids["article_id"],
                    page_number=int(ids["page"]) if ids["page"].isdigit() else None,
                    publication=doc.meta_get("citation_journal_title", "og:site_name"),
                    date=_guess_date(
                        doc.meta_get("citation_date", "citation_publication_date", "dc.date"),
                        doc.title,
                        resp.url,
                    ),
                    language=doc.lang,
                    metadata={"meta": doc.meta, "json_ld": doc.json_ld[:3]},
                )
            )
        return docs, links

    if kind == "text":
        text = resp.text()
        if text.strip():
            docs.append(
                build_document(
                    url=resp.url,
                    kind="page",
                    source_type="text",
                    text=text,
                    profile=profile,
                    issue_id=ids["issue_id"],
                    page_number=int(ids["page"]) if ids["page"].isdigit() else None,
                )
            )
        return docs, links

    if kind == "json":
        links = _urls_in_json(resp.text(), resp.url)
        return docs, links

    return docs, links


_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>\\]+")


def _urls_in_json(payload: str, base_url: str) -> list[str]:
    urls = set(_URL_IN_TEXT.findall(payload))
    # relatīvie ceļi JSON laukos ("href": "/issue/123")
    for m in re.finditer(r'"(?:url|href|link|id)"\s*:\s*"(/[^"]{2,200})"', payload):
        urls.add(urllib.parse.urljoin(base_url, m.group(1)))
    return sorted(urls)
