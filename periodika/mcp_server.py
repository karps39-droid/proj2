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

from . import force_utf8_io
from .config import Config
from .crawl import CrawlLimits, Crawler
from .discovery import probe_site
from .http_client import HttpClient
from .latvian import (
    analyze_article,
    detect_language,
    expand_query_lv,
    find_places,
    parse_latvian_date,
    place_variants,
    split_sentences,
)
from .browser import BrowserUnavailable, browser_available, discover_endpoints, read_page
from .correct import correct_text, suspicious_words
from .netcheck import diagnose
from .orthography import looks_old
from .recognize import RecognitionError, available_engines, recognize
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
        language = str(args.get("language", ""))
        if source == "site":
            hits = site_search(
                client(), config.profile, query,
                expand_old_orthography=expand, limit=limit,
            )
        else:
            hits = local_search(
                store(), query, limit=limit,
                expand_old_orthography=expand, language=language,
            )
        return {
            "vaicājums": query,
            "izmēģinātie_varianti": expand_query_lv(query) if expand else [query],
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
            "valoda": doc.language or doc.metadata.get("valoda", ""),
            "ortogrāfija": doc.orthography,
            "vecuma_novērtējums": doc.old_score,
            "ocr_ticamība": doc.ocr_confidence,
            "vietvārdi": doc.metadata.get("vietvārdi", []),
            "datuma_detaļas": doc.metadata.get("datuma_detaļas", {}),
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
        result = analyze_article(text)
        result["vecuma_novērtējums"] = result.pop("old_score")
        result.pop("date_details", None)
        return result

    def t_expand(args: dict) -> Any:
        query = str(args.get("query", ""))
        return {
            "vaicājums": query,
            "varianti": expand_query_lv(
                query,
                inflections=bool(args.get("inflections", True)),
                old_orthography=bool(args.get("old_orthography", True)),
                places=bool(args.get("historic_place_names", True)),
                max_queries=int(args.get("limit", 16)),
            ),
        }

    def t_language(args: dict) -> Any:
        return detect_language(str(args.get("text", "")))

    def t_date(args: dict) -> Any:
        return parse_latvian_date(str(args.get("text", "")))

    def t_places(args: dict) -> Any:
        text = str(args.get("text", ""))
        name = str(args.get("name", ""))
        out: dict[str, Any] = {}
        if name:
            out["vēsturiskie_nosaukumi"] = place_variants(name)
        if text:
            out["atrastie_vietvārdi"] = find_places(text)
        return out or {"kļūda": "jānorāda 'name' vai 'text'"}

    def t_sentences(args: dict) -> Any:
        return {"teikumi": split_sentences(str(args.get("text", "")))}

    def t_read_image(args: dict) -> Any:
        path = str(args.get("image_path", "")).strip()
        if not path:
            return {"kļūda": "jānorāda image_path"}
        try:
            return recognize(
                path,
                engine=str(args.get("engine", "agent")),
                handwriting=bool(args.get("handwriting", False)),
                langs=str(args.get("langs", "")),
                command=str(args.get("command", "")),
                segment=bool(args.get("segment_lines", False)),
                preprocess=bool(args.get("preprocess", True)),
                correct=bool(args.get("correct", True)),
                workdir=args.get("workdir") or None,
            )
        except RecognitionError as exc:
            return {"kļūda": str(exc), "pieejamie_dzinēji": available_engines()}

    def t_correct(args: dict) -> Any:
        text = str(args.get("text", ""))
        if not text:
            return {"kļūda": "tukšs teksts"}
        handwriting = bool(args.get("handwriting", False))
        if args.get("only_report"):
            return {"aizdomīgie_vārdi": suspicious_words(text, handwriting=handwriting)}
        report = correct_text(
            text, handwriting=handwriting, max_edits=int(args.get("max_edits", 1))
        )
        return report.to_json()

    def t_engines(args: dict) -> Any:
        return {**available_engines(), "pārlūks": browser_available()}

    def t_netcheck(args: dict) -> Any:
        return diagnose(
            str(args.get("url") or config.profile.base_url),
            user_agent=config.policy.effective_user_agent(),
            ca_bundle=config.policy.ca_bundle,
            proxy=config.policy.proxy,
            check_browser=bool(args.get("check_browser", True)),
            timeout=config.policy.timeout,
        ).to_json()

    def t_browse(args: dict) -> Any:
        url = str(args.get("url", "")).strip()
        if not url:
            return {"kļūda": "jānorāda url"}
        try:
            page = read_page(
                url,
                wait_for=str(args.get("wait_for", "")),
                save_data_to=args.get("save_data_to") or None,
                screenshot_to=args.get("screenshot_to") or None,
                timeout=config.policy.timeout,
                user_agent=config.policy.effective_user_agent(),
            )
        except BrowserUnavailable as exc:
            return {"kļūda": str(exc), "pārlūks": browser_available()}
        payload = page.to_json()
        limit = int(args.get("max_chars", 20000))
        if len(payload.get("teksts", "")) > limit:
            payload["teksts"] = payload["teksts"][:limit]
            payload["saīsināts"] = True
        return payload

    def t_sniff(args: dict) -> Any:
        urls = args.get("urls") or ([args["url"]] if args.get("url") else [])
        if not urls:
            return {"kļūda": "jānorāda urls"}
        try:
            return discover_endpoints(
                [str(u) for u in urls],
                issue_id=str(args.get("issue_id", "")),
                save_data_to=args.get("save_data_to") or None,
                timeout=config.policy.timeout,
                user_agent=config.policy.effective_user_agent(),
            )
        except BrowserUnavailable as exc:
            return {"kļūda": str(exc), "pārlūks": browser_available()}

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
                        "language": {"type": "string", "enum": ["", "lv", "de", "ru", "et"],
                                     "default": "", "description": "filtrs lokālajai meklēšanai"},
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
                    "rakstībā, notīra OCR artefaktus (garais ſ, vārdu pārnesumi, "
                    "izretinājums) un ar latviešu vārdnīcas palīdzību izšķir veco 's' "
                    "(ſirgs -> zirgs). Atgriež arī valodu, datumu un vietvārdus."
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
                "description": (
                    "Parāda visus vaicājuma variantus: locījumus (latviešu valoda ir "
                    "stipri locīta), vecās ortogrāfijas rakstības un vēsturiskos vietvārdus."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "inflections": {"type": "boolean", "default": True},
                        "old_orthography": {"type": "boolean", "default": True},
                        "historic_place_names": {"type": "boolean", "default": True},
                        "limit": {"type": "integer", "default": 16},
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
        "periodika_read_image": (
            {
                "description": (
                    "Nolasa skenējuma attēlu — drukātu vai rokrakstu. Ar noklusējuma "
                    "dzinēju 'agent' neko nevajag uzstādīt: attēls tiek izlīdzināts, "
                    "binarizēts un sagriezts rindās, un tu atgriezto attēlu ceļu atver "
                    "ar Read rīku un pārraksti pats pēc dotās uzvednes (diplomātiski, "
                    "bez modernizācijas). Dzinējs 'tesseract' der drukai (lav+frk), "
                    "'claude' — Anthropic API, 'command' — ārējam HTR dzinējam. "
                    "Rezultāts iet caur latviešu kļūdu labošanu un analīzi."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "image_path": {"type": "string"},
                        "engine": {"type": "string",
                                   "enum": ["agent", "claude", "tesseract", "command"],
                                   "default": "agent"},
                        "handwriting": {"type": "boolean", "default": False,
                                        "description": "rokraksts (19. gs. Kurrent kursīvs)"},
                        "segment_lines": {"type": "boolean", "default": False},
                        "preprocess": {"type": "boolean", "default": True},
                        "correct": {"type": "boolean", "default": True},
                        "langs": {"type": "string", "description": "tesseract valodas, piem. lav+frk"},
                        "command": {"type": "string", "description": "ārēja dzinēja komanda"},
                        "workdir": {"type": "string"},
                    },
                    "required": ["image_path"],
                },
            },
            t_read_image,
        ),
        "periodika_correct_text": (
            {
                "description": (
                    "Labo atpazīšanas kļūdas latviešu tekstā, izmantojot Fraktur drukas "
                    "vai Kurrent rokraksta tipiskās sajaukšanas (ſ/f, n/u, e/n, h/b) un "
                    "latviešu vārdnīcu. Labo tikai tad, ja rezultāts ir atpazīstams vārds, "
                    "un atskaitās par katru izmaiņu. Lieto uzreiz pēc attēla nolasīšanas."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "handwriting": {"type": "boolean", "default": False},
                        "max_edits": {"type": "integer", "default": 1},
                        "only_report": {"type": "boolean", "default": False,
                                        "description": "tikai aizdomīgie vārdi, bez labošanas"},
                    },
                    "required": ["text"],
                },
            },
            t_correct,
        ),
        "periodika_recognition_engines": (
            {
                "description": (
                    "Kuri atpazīšanas dzinēji šajā vidē tiešām ir pieejami un kurš der "
                    "rokrakstam. Izsauc pirms attēla nolasīšanas."
                ),
                "inputSchema": {"type": "object", "properties": {}},
            },
            t_engines,
        ),
        "periodika_netcheck": (
            {
                "description": (
                    "Kad kaut kas 'nestrādā' vai vietne šķiet bloķēta — izsauc šo. "
                    "Pārbauda pa slāņiem (DNS, TCP, starpniekserveris, TLS, HTTP, "
                    "robots.txt, pārlūks) un pasaka, kurš krīt un ko darīt. Izšķir "
                    "organizācijas izejas politiku no vietnes atteikuma klientam."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "noklusējums: profila bāze"},
                        "check_browser": {"type": "boolean", "default": True},
                    },
                },
            },
            t_netcheck,
        ),
        "periodika_browse_page": (
            {
                "description": (
                    "Atver lapu īstā pārlūkā un nolasa uzzīmēto saturu. Vajadzīgs "
                    "periodika2-viewer lapām: tās ir vienas lapas lietotnes, kur teksta "
                    "HTML avotā nav un parasts pieprasījums neko neatrod. Atgriež arī "
                    "sarakstu ar datu pieprasījumiem, ko lietotne izdarīja."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "wait_for": {"type": "string", "description": "CSS selektors, ko sagaidīt"},
                        "save_data_to": {"type": "string",
                                         "description": "mape pārtverto datu failiem"},
                        "screenshot_to": {"type": "string",
                                          "description": "ekrānuzņēmums; padod to periodika_read_image"},
                        "max_chars": {"type": "integer", "default": 20000},
                    },
                    "required": ["url"],
                },
            },
            t_browse,
        ),
        "periodika_discover_endpoints": (
            {
                "description": (
                    "Atver skatītāja lapas pārlūkā un noskaidro, no kurienes lietotne ņem "
                    "datus (METS/ALTO/JSON), tad uzģenerē URL veidnes profilam. Pēc tam "
                    "rāpulis strādā tieši ar datu slāni — bez pārlūka. Lieto, kad `probe` "
                    "neatrada ceļus."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "urls": {"type": "array", "items": {"type": "string"}},
                        "issue_id": {"type": "string",
                                     "description": "laidiena ID, ko aizstāt ar {issue}"},
                        "save_data_to": {"type": "string"},
                    },
                    "required": ["urls"],
                },
            },
            t_sniff,
        ),
        "periodika_detect_language": (
            {
                "description": (
                    "Nosaka raksta valodu (latviešu / vācu / krievu / igauņu). periodika "
                    "satur arī baltvācu un krievu presi, un latviešu vecās drukas "
                    "noteikumus nedrīkst laist pāri citas valodas tekstam."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
            t_language,
        ),
        "periodika_parse_date": (
            {
                "description": (
                    "Izvelk datumu no latviska teksta: '1899. gada 1. (13.) maijā' -> "
                    "1899-05-13 (jaunais stils) un 1899-05-01 (vecais stils). Prot arī "
                    "vecās drukas mēnešu nosaukumus un tautas mēnešus (sērsnu mēnesis)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
            t_date,
        ),
        "periodika_place_names": (
            {
                "description": (
                    "Vēsturiskie vietvārdi: 'Jelgava' -> Mitau / Jelgawa / Митава, un "
                    "otrādi — atrod tekstā vecos nosaukumus. Bez tā meklēšana baltvācu "
                    "un krievu presē neatrod neko."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "mūsdienu vietvārds"},
                        "text": {"type": "string", "description": "teksts, kurā meklēt vietvārdus"},
                    },
                },
            },
            t_places,
        ),
        "periodika_split_sentences": (
            {
                "description": (
                    "Sadala latviešu tekstu teikumos, neapraujot saīsinājumus "
                    "('1899. g. 1. maijā', 'u.c.', 'lpp.')."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
            t_sentences,
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
    if stdin is None and stdout is None:
        force_utf8_io()
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
