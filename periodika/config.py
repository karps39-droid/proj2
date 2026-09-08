"""Konfigurācija: rāpošanas politika un vietnes profili.

periodika.lv saskarnes (meklēšanas API, OAI-PMH, IIIF, METS/ALTO ceļi) laika gaitā
mainās, tāpēc tās NAV iekodētas cieti. Šeit ir *kandidātu* saraksts, ko
``periodika probe`` pārbauda pret dzīvo vietni un saglabā atrasto profilu
``~/.config/periodika/profile.json``. Visi rāpuļa un izvilkšanas moduļi lasa
profilu no šejienes.
"""

from __future__ import annotations

import json
import os
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://periodika.lndb.lv"

#: Saimniekdatori, ko rāpulis uzskata par "periodika.lv" (LNB infrastruktūra).
DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = (
    "periodika.lndb.lv",
    "www.periodika.lndb.lv",
    "periodika.lv",
    "www.periodika.lv",
)


def _ascii_header(value: str) -> str:
    """HTTP galvenes ir latin-1; diakritiku pārrakstām, lai nekas neuzsprāgtu.

    Bez šī jebkurš kontakts ar garumzīmi (vai latviskais noklusējums) nogalinātu
    katru pieprasījumu ar UnicodeEncodeError jau pirms tīkla.
    """
    normalized = unicodedata.normalize("NFKD", value)
    return normalized.encode("ascii", "ignore").decode("ascii").strip()


def default_config_dir() -> Path:
    env = os.environ.get("PERIODIKA_HOME")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "periodika"


@dataclass
class CrawlPolicy:
    """Pieklājīgas rāpošanas iestatījumi.

    Noklusējumi ir apzināti konservatīvi: viens pieprasījums sekundē, viens
    darbinieks. Bibliotēkas serveris ir kopīgs resurss — palielini apzināti.
    """

    #: HTTP galvenes kodē latin-1, tāpēc User-Agent jābūt ASCII.
    user_agent: str = (
        "periodika-agent/0.1 (+research full-text crawler; contact via --contact)"
    )
    contact: str = ""
    requests_per_second: float = 1.0
    burst: int = 2
    workers: int = 1
    timeout: float = 60.0
    max_retries: int = 5
    backoff_base: float = 2.0
    backoff_cap: float = 60.0
    obey_robots: bool = True
    #: Korporatīvais starpniekserveris (vai HTTPS_PROXY vides mainīgais).
    proxy: str = ""
    #: Sertifikātu fails, ja starpniekserveris pārtver TLS.
    ca_bundle: str = ""
    #: "nekad" | "atkāpjoties" (tikai pēc 403/429) | "vienmēr" — sk. browser.py.
    via_browser: str = "nekad"
    #: Maksimālais lejupielādējamā resursa izmērs baitos (0 = bez ierobežojuma).
    max_bytes: int = 64 * 1024 * 1024
    #: Cik ilgi (sekundēs) HTTP kešs uzskata atbildi par svaigu bez revalidācijas.
    cache_ttl: float = 7 * 24 * 3600.0

    def effective_user_agent(self) -> str:
        value = f"{self.user_agent} contact: {self.contact}" if self.contact else self.user_agent
        return _ascii_header(value)


@dataclass
class SiteProfile:
    """Konkrētas periodika.lv instalācijas "karte".

    Lauki, kas ir tukši, nozīmē "vēl nav noskaidrots" — ``probe`` tos aizpilda.
    Veidnēs lieto ``{q}``, ``{page}``, ``{id}``, ``{issue}``, ``{n}``.
    """

    name: str = "periodika.lndb.lv"
    base_url: str = DEFAULT_BASE_URL
    allowed_hosts: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_HOSTS))

    # --- satura atklāšana -------------------------------------------------
    sitemap_urls: list[str] = field(default_factory=list)
    oai_endpoints: list[str] = field(default_factory=list)
    oai_metadata_prefix: str = "oai_dc"
    seeds: list[str] = field(default_factory=list)

    # --- meklēšana --------------------------------------------------------
    #: HTML vai JSON meklēšanas veidne, piem. "https://periodika.lv/search?q={q}&page={page}"
    search_url_template: str = ""
    search_result_kind: str = "html"  # "html" | "json"
    #: JSON ceļš līdz rezultātu sarakstam, piem. "response.docs" (tikai json)
    search_results_path: str = ""

    # --- vienību resursi --------------------------------------------------
    #: METS/ALTO/TEI veidnes; {issue} = laidiena ID, {n} = lappuses numurs.
    mets_url_template: str = ""
    alto_url_template: str = ""
    tei_url_template: str = ""
    plaintext_url_template: str = ""
    iiif_manifest_template: str = ""
    viewer_url_template: str = ""

    # --- URL atpazīšana ---------------------------------------------------
    #: Regex, kas URL/fragmentā atrod laidiena ID (grupa "id").
    issue_id_patterns: list[str] = field(
        default_factory=lambda: [
            r"issue:/?(?P<id>[A-Za-z0-9_\-.]+)",
            r"/issue/(?P<id>[A-Za-z0-9_\-.]+)",
            r"[?&]issue=(?P<id>[A-Za-z0-9_\-.]+)",
            # LNB laidiena ID formā p_001_xxxx1899n01 — der arī datu slāņa ceļos
            r"(?P<id>p_\d{3}_[A-Za-z0-9]+\d{4}[a-z]?n\d+)",
            r"/periodika2-data/(?P<id>[A-Za-z0-9_\-.]+)/",
            r"/data/(?P<id>[A-Za-z0-9_\-.]+)/(?:mets|alto|tei|text)",
        ]
    )
    #: Regex, kas atrod raksta ID (LNB tradicionāli lieto DIVL/DIVR/MODSMD ids).
    article_id_patterns: list[str] = field(
        default_factory=lambda: [
            r"article:(?P<id>[A-Za-z0-9_\-.]+)",
            r"/article/(?P<id>[A-Za-z0-9_\-.]+)",
            r"[?&]article=(?P<id>[A-Za-z0-9_\-.]+)",
        ]
    )
    page_id_patterns: list[str] = field(
        default_factory=lambda: [
            r"page:(?P<id>\d+)",
            r"[?&]page=(?P<id>\d+)",
        ]
    )

    #: URL ceļa fragmenti, ko rāpulis nekad neapmeklē (bezgalīgi kalendāri u.tml.).
    deny_patterns: list[str] = field(
        default_factory=lambda: [
            r"/logout",
            r"/login",
            r"[?&]print=",
            r"[?&]download=",
            r"/cgi-bin/",
            r"\.(zip|tar|gz|7z|rar|mp3|mp4|avi|mov|ttf|woff2?|eot)(\?|$)",
        ]
    )
    #: URL, kas jāapmeklē prioritāri (lielāka informācijas blīvuma lapas).
    prefer_patterns: list[str] = field(
        default_factory=lambda: [
            r"issue",
            r"article",
            r"alto",
            r"mets",
            r"tei",
            r"fulltext",
            r"sitemap",
        ]
    )

    #: Vai lapas ir SPA, kam vajag headless pārlūku (probe to nosaka).
    requires_javascript: bool = True
    #: Hash-maršruta veidne (SPA skatītājs), no kuras iegūst cilvēkam rādāmo saiti.
    hash_route_template: str = (
        "{base}/periodika2-viewer/?lang=lv#panel:pa|issue:/{issue}|article:{article}|page:{n}"
    )
    notes: str = (
        "LNB periodika2-viewer ir vienas lapas lietotne ar hash-maršrutu "
        "(#panel:..|issue:/..|article:DIVL..|page:N). Pilnteksts nav HTML lapā, "
        "tāpēc rāpulis strādā ar datu slāni (METS/ALTO/TEI) vai, ja tāda nav, "
        "ar headless renderētu DOM. Palaid `periodika probe`, lai apstiprinātu ceļus."
    )

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SiteProfile":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


#: Kandidāti, ko ``probe`` pārbauda. Pirmais, kas atbild ar 200 un derīgu saturu,
#: nonāk saglabātajā profilā. Saraksts ir apzināti plašs — LNB gadu gaitā ir
#: lietojusi vairākas platformas (periodika2-viewer, DOM/LNDB, jaunais portāls).
PROBE_CANDIDATES: dict[str, list[str]] = {
    "robots": ["{base}/robots.txt"],
    "sitemap": [
        "{base}/sitemap.xml",
        "{base}/sitemap_index.xml",
        "{base}/sitemap-index.xml",
        "{base}/sitemaps/sitemap.xml",
        "{base}/sitemap.xml.gz",
    ],
    "oai": [
        "{base}/oai",
        "{base}/oai-pmh",
        "{base}/oai/request",
        "{base}/OAI-PMH",
        "{base}/api/oai",
    ],
    "api": [
        "{base}/api",
        "{base}/api/v1",
        "{base}/rest/v1",
        "{base}/api/search",
        "{base}/solr/select",
    ],
    "search": [
        "{base}/search?q={q}",
        "{base}/meklet?q={q}",
        "{base}/?s={q}",
        "{base}/api/search?q={q}",
        "{base}/api/v1/search?query={q}",
        "{base}/find/?q={q}",
        "{base}/periodika2-viewer/?lang=lv#panel:pa|query:{q}",
    ],
    "iiif": [
        "{base}/iiif",
        "{base}/iiif/presentation",
        "{base}/i/iiif",
    ],
    "viewer": [
        "{base}/periodika2-viewer/",
        "{base}/viewer/",
        "{base}/view/",
    ],
    "browse": [
        "{base}/browse",
        "{base}/find",
        "{base}/titles",
        "{base}/periodika2-viewer/",
    ],
}

#: Zināmie LNB METS/ALTO izkārtojumi, ko probe pārbauda pret paraugu laidienu.
ISSUE_RESOURCE_CANDIDATES: dict[str, list[str]] = {
    "mets": [
        "{base}/periodika2-data/{issue}/mets.xml",
        "{base}/periodika2-data/{issue}/METS.xml",
        "{base}/periodika2-viewer/data/{issue}/mets.xml",
        "{base}/data/{issue}/mets.xml",
        "{base}/objects/{issue}/mets.xml",
        "{base}/api/issue/{issue}/mets",
    ],
    "alto": [
        "{base}/periodika2-data/{issue}/alto/{n}.xml",
        "{base}/periodika2-data/{issue}/alto/{n:08d}.xml",
        "{base}/periodika2-viewer/data/{issue}/alto/{n}.xml",
        "{base}/data/{issue}/alto/{n:08d}.xml",
        "{base}/objects/{issue}/ALTO/{n}.xml",
        "{base}/api/issue/{issue}/page/{n}/alto",
    ],
    "tei": [
        "{base}/periodika2-data/{issue}/tei.xml",
        "{base}/periodika2-data/{issue}/{issue}.tei.xml",
        "{base}/periodika2-viewer/data/{issue}/tei.xml",
        "{base}/data/{issue}/tei/{n}.xml",
        "{base}/api/issue/{issue}/tei",
    ],
    "plaintext": [
        "{base}/api/issue/{issue}/page/{n}/text",
        "{base}/data/{issue}/text/{n}.txt",
    ],
}


@dataclass
class Config:
    policy: CrawlPolicy = field(default_factory=CrawlPolicy)
    profile: SiteProfile = field(default_factory=SiteProfile)
    data_dir: Path = field(default_factory=lambda: default_config_dir() / "data")

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        p = Path(path) if path else default_config_dir() / "config.json"
        cfg = cls()
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if "policy" in raw:
                known = {f for f in CrawlPolicy.__dataclass_fields__}  # type: ignore[attr-defined]
                cfg.policy = CrawlPolicy(
                    **{k: v for k, v in raw["policy"].items() if k in known}
                )
            if "profile" in raw:
                cfg.profile = SiteProfile.from_json(raw["profile"])
            if raw.get("data_dir"):
                cfg.data_dir = Path(raw["data_dir"]).expanduser()
        return cfg

    def save(self, path: Path | str | None = None) -> Path:
        p = Path(path) if path else default_config_dir() / "config.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "policy": asdict(self.policy),
            "profile": self.profile.to_json(),
            "data_dir": str(self.data_dir),
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    def db_path(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir / "periodika.sqlite3"

    def cache_path(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir / "http-cache.sqlite3"
