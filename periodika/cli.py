"""Komandrindas saskarne: ``python -m periodika <komanda>``.

    periodika probe --sample-issue p_001_xxxx1899n01   # noskaidro vietnes ceļus
    periodika crawl --time 3600                        # rāpo (atsākami)
    periodika issue p_001_xxxx1899n01                  # viens laidiens -> raksti
    periodika search "sabiedrība" --site               # meklē vietnē (ar veco drukas variantiem)
    periodika search "sabiedrība"                      # meklē lokālajā indeksā
    periodika get <dokumenta-id> --modern              # nolasa rakstu
    periodika normalize teksts.txt                     # veco rakstību -> mūsdienu
    periodika lang teksts.txt                          # latviešu / vācu / krievu?
    periodika date "1899. gada 1. (13.) maijā"         # vecais un jaunais stils
    periodika places --name Jelgava                    # Mitau, Jelgawa, Митава
    periodika read lapa.png --handwriting              # rokraksta nolasīšana
    periodika engines                                  # kas šajā vidē pieejams
    periodika correct ocr.txt --handwriting            # atpazīšanas kļūdu labošana
    periodika export raksti.jsonl
    periodika mcp                                      # MCP serveris Claude aģentam
"""

from __future__ import annotations

import argparse
import json
import sys
from argparse import SUPPRESS
from pathlib import Path

from .config import Config
from .crawl import CrawlLimits, Crawler
from .discovery import probe_site
from .http_client import HttpClient
from .latvian import (
    analyze_article,
    detect_language,
    expand_query_lv,
    find_places,
    normalize_lv,
    parse_latvian_date,
    place_variants,
)
from .orthography import (
    NormalizeOptions,
    clean_ocr,
    looks_old,
    modern_to_old_variants,
)
from .correct import correct_text, suspicious_words
from .recognize import RecognitionError, available_engines, recognize
from .search import local_search, site_search
from .store import Store


def _build_config(args: argparse.Namespace) -> Config:
    cfg = Config.load(args.config)
    if args.base:
        cfg.profile.base_url = args.base.rstrip("/")
        host = args.base.split("//")[-1].split("/")[0].split(":")[0]
        if host not in cfg.profile.allowed_hosts:
            cfg.profile.allowed_hosts.append(host)
    if args.data_dir:
        cfg.data_dir = Path(args.data_dir).expanduser()
    if args.rate:
        cfg.policy.requests_per_second = args.rate
    if args.workers:
        cfg.policy.workers = args.workers
    if args.contact:
        cfg.policy.contact = args.contact
    if args.ignore_robots:
        cfg.policy.obey_robots = False
    return cfg


def _print(data, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif isinstance(data, (dict, list)):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(data)


# --------------------------------------------------------------- komandas
def cmd_probe(args: argparse.Namespace) -> int:
    cfg = _build_config(args)
    client = HttpClient(cfg.policy, cache_path=cfg.cache_path())
    profile, report = probe_site(
        client, cfg.profile, sample_issue=args.sample_issue or "", sample_query=args.query
    )
    cfg.profile = profile
    if args.json:
        _print({"profils": profile.to_json(), "atskaite": report.found,
                "trūkst": report.missing, "piezīmes": report.notes}, True)
    else:
        print(report.as_text())
    if not args.no_save:
        path = cfg.save(args.config)
        print(f"\nProfils saglabāts: {path}", file=sys.stderr)
    return 0


def cmd_crawl(args: argparse.Namespace) -> int:
    cfg = _build_config(args)
    store = Store(cfg.db_path())
    crawler = Crawler(cfg, store=store, on_event=_progress(args.quiet))
    if not args.resume:
        added = crawler.seed(args.seed or ())
        print(f"Sēklas frontē: +{added}", file=sys.stderr)
    limits = CrawlLimits(
        max_urls=args.max_urls,
        max_documents=args.max_docs,
        max_depth=args.depth,
        time_budget=args.time,
        max_pages_per_issue=args.max_pages_per_issue,
    )
    result = crawler.run(limits)
    _print({"rezultāts": result.as_dict(), "statuss": crawler.status()}, args.json)
    return 0


def _progress(quiet: bool):
    state = {"n": 0}

    def handler(kind: str, data: dict) -> None:
        if quiet:
            return
        state["n"] += 1
        if kind == "error":
            print(f"  ! {data.get('url','')}: {data.get('kļūda','')}", file=sys.stderr)
        elif state["n"] % 20 == 0 or kind in ("sitemap", "oai"):
            print(f"  … {kind}: {json.dumps(data, ensure_ascii=False)[:160]}", file=sys.stderr)

    return handler


def cmd_issue(args: argparse.Namespace) -> int:
    cfg = _build_config(args)
    crawler = Crawler(cfg)
    docs = crawler.crawl_issue(args.issue_id, max_pages=args.max_pages_per_issue)
    if args.json:
        _print([d.to_json() for d in docs], True)
        return 0 if docs else 1
    if not docs:
        print("Nekas netika atrasts. Vai profilā ir METS/TEI veidnes? "
              "Palaid `periodika probe --sample-issue …`.", file=sys.stderr)
        return 1
    for doc in docs:
        print(f"\n=== [{doc.article_id or doc.page_number}] {doc.title or '(bez virsraksta)'} "
              f"({doc.orthography}, OCR {doc.ocr_confidence or '-'})")
        print((doc.text_modern if args.modern else doc.text_raw)[: args.chars])
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    cfg = _build_config(args)
    if args.site:
        client = HttpClient(cfg.policy, cache_path=cfg.cache_path())
        hits = site_search(
            client,
            cfg.profile,
            args.query,
            expand_old_orthography=not args.no_expand,
            limit=args.limit,
        )
    else:
        hits = local_search(
            Store(cfg.db_path()),
            args.query,
            limit=args.limit,
            expand_old_orthography=not args.no_expand,
            language=args.lang or "",
        )
    if args.json:
        _print([h.to_json() for h in hits], True)
        return 0
    if not hits:
        print("Nekas neatradās.")
        if not args.site:
            print("(lokālais indekss var būt tukšs — vispirms `periodika crawl`)")
        return 1
    for i, hit in enumerate(hits, 1):
        print(f"{i:3}. {hit.title or '(bez virsraksta)'}  [{hit.matched_query}]")
        if hit.snippet:
            print(f"     {hit.snippet[:200]}")
        print(f"     {hit.url}")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    cfg = _build_config(args)
    store = Store(cfg.db_path())
    doc = store.get_document(args.doc_id)
    if doc is None:
        matches = store.find_documents(issue_id=args.doc_id, limit=args.limit)
        if not matches:
            print("Nav tāda dokumenta lokālajā krātuvē.", file=sys.stderr)
            return 1
        doc = matches[0]
    if args.json:
        _print(doc.to_json(), True)
        return 0
    print(f"# {doc.title or '(bez virsraksta)'}")
    print(f"# {doc.publication} {doc.date} lpp. {doc.page_number or '-'} "
          f"| ortogrāfija: {doc.orthography} ({doc.old_score})")
    print(f"# {doc.viewer_url or doc.url}\n")
    if args.both:
        print("--- oriģināls ---")
        print(doc.text_raw[: args.chars])
        print("\n--- mūsdienu rakstībā ---")
        print(doc.text_modern[: args.chars])
    else:
        print((doc.text_modern if args.modern else doc.text_raw)[: args.chars])
    return 0


def cmd_normalize(args: argparse.Namespace) -> int:
    def read_input() -> str:
        if args.path in ("-", None):
            return "" if sys.stdin.isatty() else sys.stdin.read()
        return Path(args.path).read_text(encoding="utf-8", errors="replace")

    if args.to_old:
        words = args.text_or_words or read_input().split()
        for w in words:
            print(f"{w}: {', '.join(modern_to_old_variants(w, args.variants))}")
        return 0
    raw = read_input()
    opts = NormalizeOptions(
        core=True,
        palatals=not args.no_palatals,
        doubles=not args.no_doubles,
        germanisms=not args.no_germanisms,
        decapitalize=args.decapitalize,
    )
    cleaned = clean_ocr(raw)
    if args.json:
        analysis = analyze_article(cleaned)
        analysis["vecuma_novērtējums"] = analysis.pop("old_score")
        _print(analysis, True)
    else:
        print(normalize_lv(cleaned, use_lexicon=not args.no_lexicon, options=opts))
    return 0


def cmd_expand(args: argparse.Namespace) -> int:
    _print(
        expand_query_lv(
            args.query,
            inflections=not args.no_inflections,
            old_orthography=not args.no_old,
            places=not args.no_places,
            max_queries=args.limit,
        ),
        args.json,
    )
    return 0


def cmd_lang(args: argparse.Namespace) -> int:
    text = _read_text(args.path)
    _print(detect_language(text), True)
    return 0


def cmd_date(args: argparse.Namespace) -> int:
    text = args.text or _read_text(args.path)
    _print(parse_latvian_date(text), True)
    return 0


def cmd_places(args: argparse.Namespace) -> int:
    if args.name:
        _print({"vēsturiskie_nosaukumi": place_variants(args.name)}, True)
    else:
        _print({"atrastie_vietvārdi": find_places(_read_text(args.path))}, True)
    return 0


def _read_text(path: str | None) -> str:
    if path in ("-", None):
        return "" if sys.stdin.isatty() else sys.stdin.read()
    return Path(path).read_text(encoding="utf-8", errors="replace")


def cmd_read(args: argparse.Namespace) -> int:
    """Nolasa attēlu (druku vai rokrakstu) un izlaiž caur latviešu pēcapstrādi."""
    try:
        result = recognize(
            args.image,
            engine=args.engine,
            handwriting=args.handwriting,
            langs=args.langs or "",
            psm=args.psm,
            command=args.command or "",
            model=args.model,
            preprocess=not args.no_preprocess,
            segment=args.segment,
            correct=not args.no_correct,
            workdir=args.workdir,
        )
    except RecognitionError as exc:
        print(f"Atpazīšana neizdevās: {exc}", file=sys.stderr)
        return 1
    if args.json or args.engine == "agent":
        _print(result, True)
        return 0
    print(result.get("teksts", ""))
    if result.get("labojumi"):
        print(f"\n[{len(result['labojumi'])} labojumi; "
              f"neatpazīti: {len(result.get('neatpazītie_vārdi', []))}]", file=sys.stderr)
    return 0


def cmd_engines(args: argparse.Namespace) -> int:
    _print(available_engines(), True)
    return 0


def cmd_correct(args: argparse.Namespace) -> int:
    text = _read_text(args.path)
    if args.suspicious:
        _print(suspicious_words(text, handwriting=args.handwriting), True)
        return 0
    report = correct_text(text, handwriting=args.handwriting, max_edits=args.max_edits)
    if args.json:
        _print(report.to_json(), True)
    else:
        print(report.text)
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    cfg = _build_config(args)
    n = Store(cfg.db_path()).export_jsonl(args.path)
    print(f"Eksportēti {n} dokumenti -> {args.path}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = _build_config(args)
    _print(Store(cfg.db_path()).stats(), True)
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import serve

    serve(_build_config(args))
    return 0


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    # Globālās opcijas der gan pirms, gan pēc apakškomandas.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=SUPPRESS, help="ceļš uz config.json")
    common.add_argument("--data-dir", default=SUPPRESS,
                        help="kur glabāt SQLite krātuvi un kešu")
    common.add_argument("--base", default=SUPPRESS,
                        help="vietnes bāzes URL (noklusējums no profila)")
    common.add_argument("--rate", type=float, default=SUPPRESS,
                        help="pieprasījumi sekundē (noklusējums 1.0)")
    common.add_argument("--workers", type=int, default=SUPPRESS,
                        help="paralēlo darbinieku skaits")
    common.add_argument("--contact", default=SUPPRESS,
                        help="kontaktinformācija User-Agent rindā (pieklājīgi)")
    common.add_argument("--ignore-robots", action="store_true", default=SUPPRESS,
                        help="neievērot robots.txt (izmanto tikai ar atļauju)")
    common.add_argument("--json", action="store_true", default=SUPPRESS,
                        help="izvade JSON formātā")

    p = argparse.ArgumentParser(
        prog="periodika",
        parents=[common],
        description="periodika.lndb.lv pārmeklēšana un vecās drukas nolasīšana",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("probe", parents=[common], help="noskaidro vietnes galapunktus un saglabā profilu")
    sp.add_argument("--sample-issue", help="viena laidiena ID METS/ALTO ceļu pārbaudei")
    sp.add_argument("--query", default="Rīga", help="parauga meklējamais vārds")
    sp.add_argument("--no-save", action="store_true")
    sp.set_defaults(func=cmd_probe)

    sc = sub.add_parser("crawl", parents=[common], help="pārmeklē vietni (atsākami)")
    sc.add_argument("--seed", action="append", help="papildu sākuma URL")
    sc.add_argument("--resume", action="store_true", help="turpināt bez sēklu pievienošanas")
    sc.add_argument("--max-urls", type=int, default=0)
    sc.add_argument("--max-docs", type=int, default=0)
    sc.add_argument("--depth", type=int, default=12)
    sc.add_argument("--time", type=float, default=0.0, help="laika budžets sekundēs")
    sc.add_argument("--max-pages-per-issue", type=int, default=0)
    sc.add_argument("--quiet", action="store_true")
    sc.set_defaults(func=cmd_crawl)

    si = sub.add_parser("issue", parents=[common], help="ielādē vienu laidienu un izvelk rakstus")
    si.add_argument("issue_id")
    si.add_argument("--modern", action="store_true", help="rādīt mūsdienu rakstībā")
    si.add_argument("--chars", type=int, default=4000)
    si.add_argument("--max-pages-per-issue", type=int, default=0)
    si.set_defaults(func=cmd_issue)

    ss = sub.add_parser("search", parents=[common], help="meklē vietnē vai lokālajā indeksā")
    ss.add_argument("query")
    ss.add_argument("--site", action="store_true", help="meklēt dzīvajā vietnē")
    ss.add_argument("--no-expand", action="store_true", help="bez vecās drukas variantiem")
    ss.add_argument("--limit", type=int, default=25)
    ss.add_argument("--lang", choices=["lv", "de", "ru", "et"],
                    help="filtrē lokālos rezultātus pēc valodas")
    ss.set_defaults(func=cmd_search)

    sg = sub.add_parser("get", parents=[common], help="nolasa saglabātu rakstu")
    sg.add_argument("doc_id")
    sg.add_argument("--modern", action="store_true")
    sg.add_argument("--both", action="store_true", help="oriģināls un mūsdienu versija blakus")
    sg.add_argument("--chars", type=int, default=20000)
    sg.add_argument("--limit", type=int, default=5)
    sg.set_defaults(func=cmd_get)

    sn = sub.add_parser("normalize", parents=[common], help="vecā ortogrāfija -> mūsdienu rakstība")
    sn.add_argument("path", nargs="?", default="-", help="fails vai '-' (stdin)")
    sn.add_argument("--to-old", action="store_true", help="pretējais virziens: varianti meklēšanai")
    sn.add_argument("--text-or-words", nargs="*", help="vārdi --to-old režīmam")
    sn.add_argument("--variants", type=int, default=8)
    sn.add_argument("--no-palatals", action="store_true")
    sn.add_argument("--no-doubles", action="store_true")
    sn.add_argument("--no-germanisms", action="store_true")
    sn.add_argument("--decapitalize", action="store_true")
    sn.add_argument("--no-lexicon", action="store_true",
                    help="neizmantot latviešu vārdnīcu s/z izšķiršanai")
    sn.set_defaults(func=cmd_normalize)

    se = sub.add_parser("expand", parents=[common], help="parāda vaicājuma vecās drukas variantus")
    se.add_argument("query")
    se.add_argument("--limit", type=int, default=16)
    se.add_argument("--no-inflections", action="store_true", help="bez locījumiem")
    se.add_argument("--no-old", action="store_true", help="bez vecās ortogrāfijas")
    se.add_argument("--no-places", action="store_true", help="bez vēsturiskajiem vietvārdiem")
    se.set_defaults(func=cmd_expand)

    sl = sub.add_parser("lang", parents=[common], help="nosaka teksta valodu")
    sl.add_argument("path", nargs="?", default="-")
    sl.set_defaults(func=cmd_lang)

    sd = sub.add_parser("date", parents=[common], help="izvelk datumu (arī veco/jauno stilu)")
    sd.add_argument("text", nargs="?")
    sd.add_argument("--path", default="-")
    sd.set_defaults(func=cmd_date)

    spl = sub.add_parser("places", parents=[common], help="vēsturiskie vietvārdi")
    spl.add_argument("--name", help="mūsdienu vietvārds -> vēsturiskie varianti")
    spl.add_argument("path", nargs="?", default="-", help="teksts, kurā meklēt vietvārdus")
    spl.set_defaults(func=cmd_places)

    sr = sub.add_parser("read", parents=[common],
                        help="nolasa attēlu: druku vai rokrakstu (OCR/HTR)")
    sr.add_argument("image", help="attēla fails (PNG/JPG/TIFF)")
    sr.add_argument("--engine", default="agent",
                    choices=["agent", "claude", "tesseract", "command"])
    sr.add_argument("--handwriting", action="store_true", help="rokraksts (Kurrent kursīvs)")
    sr.add_argument("--langs", help="tesseract valodas, piem. lav+frk")
    sr.add_argument("--psm", type=int, default=4)
    sr.add_argument("--command", help="ārēja dzinēja komanda ar {image} un {out}")
    sr.add_argument("--model", default="claude-opus-5")
    sr.add_argument("--segment", action="store_true", help="sagriezt lappusi rindās")
    sr.add_argument("--no-preprocess", action="store_true")
    sr.add_argument("--no-correct", action="store_true")
    sr.add_argument("--workdir", help="kur likt sagatavotos attēlus")
    sr.set_defaults(func=cmd_read)

    sen = sub.add_parser("engines", parents=[common],
                         help="kuri atpazīšanas dzinēji ir pieejami")
    sen.set_defaults(func=cmd_engines)

    sco = sub.add_parser("correct", parents=[common],
                         help="labo OCR/HTR kļūdas pēc latviešu vārdnīcas")
    sco.add_argument("path", nargs="?", default="-")
    sco.add_argument("--handwriting", action="store_true", help="Kurrent rokraksta sajaukumi")
    sco.add_argument("--suspicious", action="store_true",
                     help="tikai saraksts ar neatpazītajiem vārdiem")
    sco.add_argument("--max-edits", type=int, default=1)
    sco.set_defaults(func=cmd_correct)

    sx = sub.add_parser("export", parents=[common], help="eksportē krātuvi JSONL formātā")
    sx.add_argument("path")
    sx.set_defaults(func=cmd_export)

    st = sub.add_parser("stats", parents=[common], help="krātuves un frontes statistika")
    st.set_defaults(func=cmd_stats)

    sm = sub.add_parser("mcp", parents=[common], help="palaiž MCP serveri (stdio) Claude aģentam")
    sm.set_defaults(func=cmd_mcp)
    return p


GLOBAL_DEFAULTS = {
    "config": None,
    "data_dir": None,
    "base": None,
    "rate": None,
    "workers": None,
    "contact": None,
    "ignore_robots": False,
    "json": False,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    for key, value in GLOBAL_DEFAULTS.items():
        if not hasattr(args, key):
            setattr(args, key, value)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\nPārtraukts. Stāvoklis saglabāts — turpini ar `periodika crawl --resume`.",
              file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
