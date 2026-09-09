"""periodika.lv toolkit — pilna vietnes pārmeklēšana un vecās drukas nolasīšana.

Moduļi:
    config        — vietnes profili un rāpošanas politika
    http_client   — pieklājīgs HTTP klients (robots.txt, rate-limit, kešs, atkārtojumi)
    htmlutil      — bezatkarību HTML parsēšana (saites, meta, teksts)
    alto          — ALTO / METS / TEI parsēšana (rakstu salikšana no OCR blokiem)
    orthography   — vecās ortogrāfijas un Fraktur OCR normalizācija
    extract       — resursa -> ieraksta (Document) izvilkšana
    store         — SQLite krātuve ar pilnteksta meklēšanu
    discovery     — sitemap / OAI-PMH / saišu grafa atklāšana
    crawl         — atsākams pilnas vietnes rāpulis
    search        — vietnes meklēšana ar vecās drukas vaicājumu paplašināšanu
    cli           — komandrindas saskarne
    mcp_server    — MCP (stdio) serveris Claude aģentam
"""

import sys as _sys

__version__ = "0.1.0"


def force_utf8_io() -> None:
    """Nodrošina UTF-8 uz stdin/stdout/stderr arī tad, ja straumes ir novirzītas.

    Uz Windows Python izvēlas kodējumu pēc lokāles (parasti cp1252), tiklīdz
    izvade nav konsole, bet caurule vai fails. Tad jebkurš `ā` gāž programmu ar
    UnicodeEncodeError. MCP serveris darbojas tieši pa novirzītu stdio un visa
    `--json` izvade mēdz tikt novirzīta, tāpēc bez šī uz Windows nestrādā ne
    `periodika mcp`, ne `periodika doctor --json`.
    """
    for stream in (_sys.stdin, _sys.stdout, _sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # jau aizvērta vai nepārkonfigurējama straume
            pass

__all__ = [
    "force_utf8_io",
    "config",
    "http_client",
    "htmlutil",
    "alto",
    "orthography",
    "extract",
    "store",
    "discovery",
    "crawl",
    "search",
]
