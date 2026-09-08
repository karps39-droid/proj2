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

__version__ = "0.1.0"

__all__ = [
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
