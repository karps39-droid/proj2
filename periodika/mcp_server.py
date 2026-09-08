"""MCP serveris (stdio) — periodika rīki Claude aģentam.

Bez ārējām atkarībām: MCP stdio transports ir rindu-dalīts JSON-RPC 2.0, ko var
apkalpot ar stdlib. Pievieno Claude Code konfigurācijā:

    {
      "mcpServers": {
        "periodika": {
          "command": "python3",
          "args": ["-m", "periodika.mcp_server"],
          "env": {"PERIODIKA_HOME": "~/.config/periodika"}
        }
      }
    }

Rīki apzināti atgriež gan oriģinālo tekstu, gan mūsdienu rakstībā pārrakstīto
versiju — aģents pats izlemj, kuru lasīt, un citātam vienmēr pieejams oriģināls.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Callable

from .config import Config
from .crawl import CrawlLimits, Crawler
from .discovery import probe_site
from .http_client import HttpClient
from .orthography import expand_query, looks_old, normalize_article_text
from .search import local_search, site_search
from .store import Store

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "periodika"
SERVER_VERSION = "0.1.0"

__all__ = ["serve", "build_tools"]


def _text_result(payload: Any) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}]}


def build_tools(config: Config) -> dict[str, tuple[dict, Callable[[dict], Any]]]:
    """Rīku reģistrs: nosaukums -> (shēma, izpildītājs)."""
    state: dict[str, Any] = {}

    def store() -> Store:
        if "store" not in state:
            state["store"] = Store(config.db_path())
        return state["store"]

    def client() -> HttpClient:
        if "client" not in state:
            state["client"] = HttpClient(config.policy, cache_path=config.cache_path())
        return state["client"]

    def crawler() -> Crawler:
        if "crawler" not in state:
            state["crawler"] = Crawler(config, store=store(), client=client())
        return state["crawler"]

    # -- rīku realizācijas ---------------------------------------------
    def t_search(args: dict) -> Any:
        query = str(args.get("query", "")).strip()
        if not query:
            return {"kļūda": "tukšs vaicājums"}
        limit = int(args.get("limit", 20))
        expand = bool(args.get("expand_old_orthography", True))
        source = args.get("source", "local")
        if source == "site":
            hits = site_search(
                client(), config.profile, query,
                expand_old_orthography=expand, limit=limit,
            )
        else:
            hits = local_search(store(), query, limit=limit, expand_old_orthography=expand)
        return {
            "vaicājums": query,
            "izmēģinātie_varianti": expand_query(query) if expand else [query],
            "rezultāti": [h.to_json() for h in hits],
        }

    def t_read(args: dict) -> Any:
        doc_id = str(args.get("document_id", "")).strip()
        doc = store().get_document(doc_id) if doc_id else None
        if doc is None and args.get("issue_id"):
            found = store().find_documents(issue_id=str(args["issue_id"]), limit=1)
            doc = found[0] if found else None
        if doc is None:
            return {"kļūda": "dokuments nav lokālajā krātuvē; "
                             "vispirms izmanto periodika_fetch_issue vai periodika_crawl"}
        limit = int(args.get("max_chars", 20000))
        return {
            "id": doc.id,
            "virsraksts": doc.title,
            "izdevums": doc.publication,
            "datums": doc.date,
            "lappuse": doc.page_number,
            "laidiens": doc.issue_id,
            "ortogrāfija": doc.orthography,
            "vecuma_novērtējums": doc.old_score,
            "ocr_ticamība": doc.ocr_confidence,
            "saite": doc.viewer_url or doc.url,
            "teksts_oriģinālā": doc.text_raw[:limit],
            "teksts_mūsdienu_rakstībā": doc.text_modern[:limit],
            "saīsināts": len(doc.text_raw) > limit,
        }

    def t_fetch_issue(args: dict) -> Any:
        issue_id = str(args.get("issue_id", "")).strip()
        if not issue_id:
            return {"kļūda": "jānorāda issue_id"}
        docs = crawler().crawl_issue(issue_id, max_pages=int(args.get("max_pages", 0)))
        return {
            "laidiens": issue_id,
            "atrasti_raksti": len(docs),
            "raksti": [
                {
                    "id": d.id,
                    "virsraksts": d.title,
                    "lappuse": d.page_number,
                    "ortogrāfija": d.orthography,
                    "sākums": d.text_modern[:300],
                }
                for d in docs
            ],
        }

    def t_normalize(args: dict) -> Any:
        text = str(args.get("text", ""))
        if not text:
            return {"kļūda": "tukšs teksts"}
        result = normalize_article_text(text)
        result["vecuma_novērtējums"] = result.pop("old_score")
        return result

    def t_expand(args: dict) -> Any:
        query = str(args.get("query", ""))
        return {"vaicājums": query, "varianti": expand_query(query, max_queries=int(args.get("limit", 12)))}

    def t_crawl(args: dict) -> Any:
        limits = CrawlLimits(
            max_urls=int(args.get("max_urls", 200)),
            max_documents=int(args.get("max_documents", 0)),
            max_depth=int(args.get("max_depth", 12)),
            time_budget=float(args.get("time_budget_seconds", 300)),
        )
        c = crawler()
        seeded = 0
        if args.get("seed", True):
            seeded = c.seed(args.get("extra_seeds", []) or [])
        result = c.run(limits)
        return {"pievienotas_sēklas": seeded, "rezultāts": result.as_dict(),
                "statuss": c.status()}

    def t_status(args: dict) -> Any:
        return crawler().status()

    def t_probe(args: dict) -> Any:
        profile, report = probe_site(
            client(), config.profile,
            sample_issue=str(args.get("sample_issue", "")),
            sample_query=str(args.get("sample_query", "Rīga")),
        )
        config.profile = profile
        if args.get("save", True):
            config.save()
        return {"atrasts": report.found, "trūkst": report.missing,
                "piezīmes": report.notes, "profils": profile.to_json()}

    def t_detect(args: dict) -> Any:
        text = str(args.get("text", ""))
        score = looks_old(text)
        return {
            "vecuma_novērtējums": round(score, 3),
            "secinājums": "veca ortogrāfija" if score >= 0.35
            else ("jaukta" if score >= 0.12 else "mūsdienu rakstība"),
        }

    return {
        "periodika_search": (
            {
                "description": (
                    "Meklē periodika.lndb.lv rakstus. Vaicājumu automātiski papildina ar "
                    "vecās ortogrāfijas variantiem (sabiedrība -> sabeedriba, ſabeedriba), "
                    "tāpēc mūsdienu vārds atrod arī 19. gs. tekstus. source='local' meklē "
                    "jau savāktajā indeksā (ātri), source='site' — dzīvajā vietnē."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "meklējamais vārds vai frāze"},
                        "source": {"type": "string", "enum": ["local", "site"], "default": "local"},
                        "limit": {"type": "integer", "default": 20},
                        "expand_old_orthography": {"type": "boolean", "default": True},
                    },
                    "required": ["query"],
                },
            },
            t_search,
        ),
        "periodika_read_article": (
            {
                "description": (
                    "Nolasa saglabātu rakstu pilnā apjomā: atgriež gan oriģinālo OCR tekstu, "
                    "gan mūsdienu rakstībā pārrakstītu versiju, gan metadatus (izdevums, "
                    "datums, lappuse, OCR ticamība). Citātiem lieto oriģinālo tekstu."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string"},
                        "issue_id": {"type": "string", "description": "rezerve, ja nav dokumenta ID"},
                        "max_chars": {"type": "integer", "default": 20000},
                    },
                },
            },
            t_read,
        ),
        "periodika_fetch_issue": (
            {
                "description": (
                    "Ielādē vienu laidienu no vietnes pa datu slāni (METS -> ALTO) un sagriež "
                    "to atsevišķos rakstos. Lieto, kad meklēšana atdevusi laidiena ID "
                    "(piem. p_001_xxxx1899n01)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "issue_id": {"type": "string"},
                        "max_pages": {"type": "integer", "default": 0},
                    },
                    "required": ["issue_id"],
                },
            },
            t_fetch_issue,
        ),
        "periodika_normalize_text": (
            {
                "description": (
                    "Pārraksta vecās ortogrāfijas / Fraktur OCR tekstu mūsdienu latviešu "
                    "rakstībā un notīra OCR artefaktus (garais ſ, vārdu pārnesumi, "
                    "izretinājums). Lieto, kad ielīmēts teksts no attēla vai cita avota."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
            t_normalize,
        ),
        "periodika_expand_query": (
            {
                "description": "Parāda, kādos vecās rakstības variantos vārds meklējams.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 12},
                    },
                    "required": ["query"],
                },
            },
            t_expand,
        ),
        "periodika_crawl": (
            {
                "description": (
                    "Palaiž ierobežotu rāpošanas ciklu (pēc noklusējuma 200 URL / 5 min) un "
                    "papildina lokālo indeksu. Atsākams: izsauc atkārtoti, lai turpinātu."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "max_urls": {"type": "integer", "default": 200},
                        "max_documents": {"type": "integer", "default": 0},
                        "max_depth": {"type": "integer", "default": 12},
                        "time_budget_seconds": {"type": "number", "default": 300},
                        "seed": {"type": "boolean", "default": True},
                        "extra_seeds": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            t_crawl,
        ),
        "periodika_status": (
            {
                "description": "Rāpuļa frontes, krātuves un HTTP statistika.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            t_status,
        ),
        "periodika_probe": (
            {
                "description": (
                    "Noskaidro, kuri vietnes galapunkti (sitemap, OAI-PMH, meklēšana, "
                    "METS/ALTO ceļi) tiešām strādā, un saglabā profilu. Palaid vienreiz "
                    "pirms pirmās rāpošanas."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "sample_issue": {"type": "string"},
                        "sample_query": {"type": "string", "default": "Rīga"},
                        "save": {"type": "boolean", "default": True},
                    },
                },
            },
            t_probe,
        ),
        "periodika_detect_orthography": (
            {
                "description": "Novērtē, vai teksts ir vecajā ortogrāfijā (0..1).",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
            t_detect,
        ),
    }


def serve(config: Config | None = None, stdin=None, stdout=None) -> None:
    """Apkalpo MCP stdio sesiju līdz straumes beigām."""
    config = config or Config.load()
    tools = build_tools(config)
    fin = stdin or sys.stdin
    fout = stdout or sys.stdout

    def send(message: dict) -> None:
        fout.write(json.dumps(message, ensure_ascii=False) + "\n")
        fout.flush()

    for line in fin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = request.get("method", "")
        req_id = request.get("id")

        try:
            if method == "initialize":
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                }
            elif method in ("notifications/initialized", "initialized", "notifications/cancelled"):
                continue
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {
                    "tools": [
                        {"name": name, **schema} for name, (schema, _) in tools.items()
                    ]
                }
            elif method == "tools/call":
                params = request.get("params") or {}
                name = params.get("name", "")
                if name not in tools:
                    raise KeyError(f"nezināms rīks: {name}")
                handler = tools[name][1]
                result = _text_result(handler(params.get("arguments") or {}))
            else:
                if req_id is None:
                    continue
                send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"nezināma metode: {method}"},
                })
                continue
        except Exception as exc:  # noqa: BLE001
            if req_id is None:
                continue
            send({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32000,
                    "message": str(exc),
                    "data": traceback.format_exc(limit=3),
                },
            })
            continue

        if req_id is not None:
            send({"jsonrpc": "2.0", "id": req_id, "result": result})


if __name__ == "__main__":
    serve()
