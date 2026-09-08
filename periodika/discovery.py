"""Satura atklāšana: robots.txt, sitemap, OAI-PMH un vietnes "izzināšana".

Mērķis ir dabūt *pilnu* URL kopu, nevis tikai to, kas atrodama, klikšķinot pa
saitēm. Prioritāšu secība:

1. ``sitemap.xml`` (arī sitemap indeksi un ``.gz``) — lētākais pilnais saraksts;
2. OAI-PMH ``ListIdentifiers`` — bibliotēku standarts, dod visus laidienus ar
   metadatiem un atbalsta atsākšanu ar ``resumptionToken``;
3. saišu grafa apstaigāšana (``crawl`` modulī) — rezerve, ja abu iepriekšējo nav.

``probe_site`` pārbauda ``config.PROBE_CANDIDATES`` pret dzīvo vietni un
atgriež aizpildītu ``SiteProfile``, ko var saglabāt un lietot turpmāk.
"""

from __future__ import annotations

import gzip
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from .alto import _iter_local, _local, _parse_xml
from .config import ISSUE_RESOURCE_CANDIDATES, PROBE_CANDIDATES, SiteProfile
from .http_client import FetchError, HttpClient
from .htmlutil import parse_html

__all__ = [
    "iter_sitemap_urls",
    "iter_oai_identifiers",
    "oai_records",
    "probe_site",
    "ProbeReport",
]


# ------------------------------------------------------------------ sitemap
def _maybe_gunzip(body: bytes) -> bytes:
    if body[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(body)
        except OSError:
            return body
    return body


def iter_sitemap_urls(
    client: HttpClient, sitemap_url: str, *, max_depth: int = 3, seen: set[str] | None = None
) -> Iterator[str]:
    """Rekursīvi izlasa sitemap (indeksu) un atdod visus ``<loc>`` URL."""
    seen = seen if seen is not None else set()
    if sitemap_url in seen or max_depth < 0:
        return
    seen.add(sitemap_url)
    try:
        resp = client.get(sitemap_url)
    except FetchError:
        return
    body = _maybe_gunzip(resp.body)
    try:
        root = _parse_xml(body)
    except ET.ParseError:
        return
    tag = _local(root.tag)
    if tag == "sitemapindex":
        for loc in _iter_local(root, "loc"):
            if loc.text:
                yield from iter_sitemap_urls(
                    client, loc.text.strip(), max_depth=max_depth - 1, seen=seen
                )
        return
    for url_el in _iter_local(root, "url"):
        loc = next((c for c in url_el if _local(c.tag) == "loc"), None)
        if loc is not None and loc.text:
            yield loc.text.strip()


# ------------------------------------------------------------------ OAI-PMH
OAI_NS_HINT = "http://www.openarchives.org/OAI/2.0/"


def _oai_request(client: HttpClient, endpoint: str, params: dict[str, str]):
    url = endpoint + ("&" if "?" in endpoint else "?") + urllib.parse.urlencode(params)
    resp = client.get(url)
    return _parse_xml(resp.body)


def iter_oai_identifiers(
    client: HttpClient,
    endpoint: str,
    metadata_prefix: str = "oai_dc",
    set_spec: str = "",
    from_date: str = "",
    until_date: str = "",
    max_records: int = 0,
) -> Iterator[dict[str, str]]:
    """Iziet cauri visiem OAI-PMH ierakstiem, ievērojot ``resumptionToken``."""
    params: dict[str, str] = {"verb": "ListIdentifiers", "metadataPrefix": metadata_prefix}
    if set_spec:
        params["set"] = set_spec
    if from_date:
        params["from"] = from_date
    if until_date:
        params["until"] = until_date
    count = 0
    while True:
        try:
            root = _oai_request(client, endpoint, params)
        except (FetchError, ET.ParseError):
            return
        error = next(_iter_local(root, "error"), None)
        if error is not None:
            return
        for header in _iter_local(root, "header"):
            identifier = next((e.text for e in header if _local(e.tag) == "identifier"), "")
            datestamp = next((e.text for e in header if _local(e.tag) == "datestamp"), "")
            sets = [e.text or "" for e in header if _local(e.tag) == "setSpec"]
            if identifier:
                yield {
                    "identifier": identifier.strip(),
                    "datestamp": (datestamp or "").strip(),
                    "sets": ",".join(s.strip() for s in sets),
                }
                count += 1
                if max_records and count >= max_records:
                    return
        token_el = next(_iter_local(root, "resumptionToken"), None)
        token = (token_el.text or "").strip() if token_el is not None else ""
        if not token:
            return
        params = {"verb": "ListIdentifiers", "resumptionToken": token}


def oai_records(
    client: HttpClient, endpoint: str, identifier: str, metadata_prefix: str = "oai_dc"
) -> dict[str, list[str]]:
    """``GetRecord`` -> vienkāršs Dublin Core vārdnīcas attēlojums."""
    try:
        root = _oai_request(
            client,
            endpoint,
            {"verb": "GetRecord", "identifier": identifier, "metadataPrefix": metadata_prefix},
        )
    except (FetchError, ET.ParseError):
        return {}
    out: dict[str, list[str]] = {}
    meta = next(_iter_local(root, "metadata"), None)
    if meta is None:
        return out
    for el in meta.iter():
        name = _local(el.tag)
        text = (el.text or "").strip()
        if text and name not in ("metadata", "dc"):
            out.setdefault(name, []).append(text)
    return out


# ------------------------------------------------------------------- probe
@dataclass
class ProbeReport:
    base_url: str
    found: dict[str, list[str]] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    sample_issue: str = ""

    def as_text(self) -> str:
        lines = [f"periodika probe: {self.base_url}", ""]
        for key, urls in self.found.items():
            lines.append(f"  [+] {key}:")
            lines.extend(f"        {u}" for u in urls)
        if self.missing:
            lines.append(f"  [-] neatradās: {', '.join(self.missing)}")
        for note in self.notes:
            lines.append(f"  ! {note}")
        return "\n".join(lines)


_JSONISH = re.compile(rb"^\s*[\[{]")


def _looks_useful(resp) -> bool:
    if not resp.body:
        return False
    ct = resp.content_type
    if ct in ("application/xml", "text/xml", "application/json"):
        return True
    if _JSONISH.match(resp.body[:64]):
        return True
    if ct.startswith("text/html") and len(resp.body) > 500:
        return True
    return ct.startswith("text/")


def probe_site(
    client: HttpClient,
    profile: SiteProfile,
    *,
    sample_query: str = "Rīga",
    sample_issue: str = "",
) -> tuple[SiteProfile, ProbeReport]:
    """Noskaidro, kuri periodika galapunkti šai instalācijai tiešām strādā.

    Neko nepieņem par pašsaprotamu: katrs kandidāts tiek ielādēts, un profilā
    nonāk tikai tas, kas atbild ar derīgu saturu.
    """
    base = profile.base_url.rstrip("/")
    report = ProbeReport(base_url=base, sample_issue=sample_issue)
    q = urllib.parse.quote(sample_query)

    def try_urls(kind: str, templates: Iterable[str], **fmt) -> list[str]:
        hits: list[str] = []
        for tpl in templates:
            url = tpl.format(base=base, q=q, page=1, **fmt)
            if "{" in url:  # neaizpildīta veidne
                continue
            try:
                resp = client.get(url, check_robots=False)
            except FetchError:
                continue
            except Exception:
                continue
            if 200 <= resp.status < 300 and _looks_useful(resp):
                hits.append(resp.url)
        if hits:
            report.found[kind] = hits
        else:
            report.missing.append(kind)
        return hits

    # robots + sitemap
    robots_sitemaps = client.sitemaps_from_robots(base)
    if robots_sitemaps:
        report.found["robots-sitemap"] = list(robots_sitemaps)
        profile.sitemap_urls = list(dict.fromkeys(profile.sitemap_urls + list(robots_sitemaps)))
    sitemaps = try_urls("sitemap", PROBE_CANDIDATES["sitemap"])
    if sitemaps:
        profile.sitemap_urls = list(dict.fromkeys(profile.sitemap_urls + sitemaps))

    # OAI-PMH
    oai_hits: list[str] = []
    for tpl in PROBE_CANDIDATES["oai"]:
        url = tpl.format(base=base)
        try:
            resp = client.get(url + "?verb=Identify", check_robots=False)
        except Exception:
            continue
        if b"OAI-PMH" in resp.body[:4000] or OAI_NS_HINT.encode() in resp.body[:4000]:
            oai_hits.append(url)
    if oai_hits:
        report.found["oai"] = oai_hits
        profile.oai_endpoints = list(dict.fromkeys(profile.oai_endpoints + oai_hits))
    else:
        report.missing.append("oai")

    # meklēšana
    search_hits = try_urls("search", PROBE_CANDIDATES["search"])
    if search_hits and not profile.search_url_template:
        for tpl in PROBE_CANDIDATES["search"]:
            candidate = tpl.format(base=base, q=q, page=1)
            if candidate in search_hits or any(h.startswith(candidate.split("?")[0]) for h in search_hits):
                profile.search_url_template = tpl.format(base=base, q="{q}", page="{page}")
                break

    # API / IIIF / skatītājs
    try_urls("api", PROBE_CANDIDATES["api"])
    iiif = try_urls("iiif", PROBE_CANDIDATES["iiif"])
    if iiif and not profile.iiif_manifest_template:
        profile.iiif_manifest_template = iiif[0].rstrip("/") + "/{issue}/manifest.json"
    viewer = try_urls("viewer", PROBE_CANDIDATES["viewer"])
    if viewer:
        profile.viewer_url_template = viewer[0]
        profile.seeds = list(dict.fromkeys(profile.seeds + viewer))
    try_urls("browse", PROBE_CANDIDATES["browse"])

    # SPA pazīmes: vai sākumlapā vispār ir saites?
    try:
        home = client.get(base + "/", check_robots=False)
        doc = parse_html(home.text(), home.url)
        profile.requires_javascript = len(doc.links) < 5 and "app" in home.text().lower()
        if profile.requires_javascript:
            report.notes.append(
                "Sākumlapā gandrīz nav HTML saišu — vietne ir SPA. "
                "Lieto datu slāni (METS/ALTO) vai --render (headless)."
            )
        if doc.links:
            profile.seeds = list(dict.fromkeys(profile.seeds + doc.links[:50]))
    except Exception as exc:  # pragma: no cover - tīkla atkarīgs
        report.notes.append(f"sākumlapu neizdevās ielādēt: {exc}")

    # laidiena datu slānis
    if sample_issue:
        for kind, templates in ISSUE_RESOURCE_CANDIDATES.items():
            hits = []
            for tpl in templates:
                try:
                    url = tpl.format(base=base, issue=sample_issue, n=1)
                except (KeyError, ValueError):
                    continue
                try:
                    resp = client.get(url, check_robots=False)
                except Exception:
                    continue
                if 200 <= resp.status < 300 and resp.body.lstrip()[:1] == b"<":
                    hits.append(tpl)
            if hits:
                report.found[f"issue-{kind}"] = hits
                setattr(profile, f"{kind}_url_template", hits[0])
            else:
                report.missing.append(f"issue-{kind}")
    else:
        report.notes.append(
            "Bez --sample-issue netika pārbaudīti METS/ALTO ceļi. "
            "Padod viena laidiena ID (piem. no skatītāja URL fragmenta issue:/…)."
        )

    if not profile.sitemap_urls and not profile.oai_endpoints:
        report.notes.append(
            "Nav ne sitemap, ne OAI-PMH: pilna pārmeklēšana notiks pa saišu grafu, "
            "kas ir lēnāk un var nepārklāt visu. Apsver LNB datu kopu pieprasījumu."
        )
    return profile, report
