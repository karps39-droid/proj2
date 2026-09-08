"""``periodika doctor`` — pārbauda, vai viss tiešām strādā uz šī datora.

Divas daļas:

1. **Vides pārbaude** — Python, SQLite ar FTS5, rakstīšanas tiesības, leksikons,
   neobligātās atkarības (Pillow, tesseract, poppler, anthropic) un savienojums
   ar periodika.lndb.lv.
2. **Pašpārbaude** — nolaiž pilnu cauruļvadu bez tīkla uz iebūvēta parauga:
   ALTO+METS -> raksts -> vecās ortogrāfijas normalizācija -> indeksēšana ->
   meklēšana citā ortogrāfijā -> atpazīšanas kļūdu labošana. Ja šis iet cauri,
   rīks uz šī datora strādā; atliek tikai `probe` tīklam.

Paraugs ir iegults šeit, nevis ņemts no ``tests/``, lai pašpārbaude strādātu arī
tad, kad pakotne uzstādīta no wheel bez testiem.
"""

from __future__ import annotations

import platform
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

__all__ = ["Check", "run_diagnostics", "self_test", "format_report"]

MIN_PYTHON = (3, 9)

_SAMPLE_ALTO = """<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#"><Layout>
<Page ID="P1" PHYSICAL_IMG_NR="1" WIDTH="2000" HEIGHT="3000"><PrintSpace>
<TextBlock ID="TB1" HPOS="100" VPOS="100" WIDTH="800" HEIGHT="300">
  <TextLine ID="TL1"><String CONTENT="Rihgas" WC="0.9"/><SP/>
    <String CONTENT="Latweeschu" WC="0.9"/></TextLine>
  <TextLine ID="TL2"><String CONTENT="Beed-" SUBS_TYPE="HypPart1" SUBS_CONTENT="Beedriba" WC="0.9"/>
    <String CONTENT="riba" SUBS_TYPE="HypPart2" SUBS_CONTENT="Beedriba" WC="0.9"/><SP/>
    <String CONTENT="&#383;chodeen" WC="0.7"/></TextLine>
</TextBlock>
<TextBlock ID="TB2" HPOS="1000" VPOS="100" WIDTH="800" HEIGHT="200">
  <TextLine ID="TL3"><String CONTENT="Sludinajumi" WC="0.9"/></TextLine>
</TextBlock>
</PrintSpace></Page></Layout></alto>"""

_SAMPLE_METS = """<?xml version="1.0" encoding="UTF-8"?>
<mets:mets xmlns:mets="http://www.loc.gov/METS/" xmlns:mods="http://www.loc.gov/mods/v3"
           xmlns:xlink="http://www.w3.org/1999/xlink">
 <mets:dmdSec ID="D1"><mets:mdWrap MDTYPE="MODS"><mets:xmlData><mods:mods>
   <mods:titleInfo><mods:title>Baltijas Wehstnesis</mods:title></mods:titleInfo>
   <mods:originInfo><mods:dateIssued>1899-05-01</mods:dateIssued></mods:originInfo>
 </mods:mods></mets:xmlData></mets:mdWrap></mets:dmdSec>
 <mets:fileSec><mets:fileGrp USE="ALTO">
   <mets:file ID="ALTO_0001"><mets:FLocat xlink:href="alto/0001.xml"/></mets:file>
 </mets:fileGrp></mets:fileSec>
 <mets:structMap TYPE="LOGICAL"><mets:div ID="DIVL1" TYPE="NEWSPAPER">
   <mets:div ID="DIVL2" TYPE="ARTICLE" LABEL="Rihgas Latweeschu Beedriba">
     <mets:fptr><mets:area FILEID="ALTO_0001" BEGIN="TB1" END="TB1" BETYPE="IDREF"/></mets:fptr>
   </mets:div>
 </mets:div></mets:structMap>
</mets:mets>"""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    critical: bool = False
    hint: str = ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"pārbaude": self.name, "kārtībā": self.ok}
        if self.detail:
            out["ziņa"] = self.detail
        if not self.ok and self.hint:
            out["ko_darīt"] = self.hint
        return out


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    @property
    def critical_failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.critical]

    def to_json(self) -> dict[str, Any]:
        return {
            "pārbaudes": [c.to_json() for c in self.checks],
            "kļūdas": len(self.failures),
            "kritiskas_kļūdas": len(self.critical_failures),
            "kopsavilkums": (
                "viss kārtībā" if not self.failures
                else ("strādā, bet ar ierobežojumiem" if not self.critical_failures
                      else "nestrādās, kamēr kritiskās kļūdas nav novērstas")
            ),
        }


def _check(report: Report, name: str, fn: Callable[[], tuple[bool, str]], *,
           critical: bool = False, hint: str = "") -> Check:
    try:
        ok, detail = fn()
    except Exception as exc:  # noqa: BLE001 - diagnostika nedrīkst avarēt
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    return report.add(Check(name=name, ok=ok, detail=detail, critical=critical, hint=hint))


# ------------------------------------------------------------------- vide
def run_diagnostics(config=None, *, check_network: bool = True) -> Report:
    from .config import Config

    cfg = config or Config.load()
    report = Report()

    _check(
        report, "Python versija",
        lambda: (
            sys.version_info >= MIN_PYTHON,
            f"{platform.python_version()} ({sys.executable})",
        ),
        critical=True,
        hint=f"Vajadzīgs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} vai jaunāks.",
    )
    _check(
        report, "Operētājsistēma",
        lambda: (True, f"{platform.system()} {platform.release()} ({platform.machine()})"),
    )
    _check(
        report, "periodika pakotne",
        lambda: (True, str(Path(__file__).resolve().parent)),
        critical=True,
    )

    def sqlite_fts() -> tuple[bool, str]:
        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
            return True, f"SQLite {sqlite3.sqlite_version}, FTS5 pieejams"
        except sqlite3.OperationalError:
            return False, f"SQLite {sqlite3.sqlite_version} bez FTS5"
        finally:
            con.close()

    _check(
        report, "SQLite pilnteksta meklēšana (FTS5)", sqlite_fts,
        hint=("Bez FTS5 meklēšana strādās, bet lēnāk (LIKE). Parasti palīdz "
              "sistēmas Python vai jaunāka SQLite bibliotēka."),
    )

    def writable() -> tuple[bool, str]:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.data_dir / ".raksta_parbaude"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, str(cfg.data_dir)

    _check(
        report, "Datu mape ir rakstāma", writable, critical=True,
        hint="Norādi citu mapi ar --data-dir vai PERIODIKA_HOME.",
    )

    def lexicon() -> tuple[bool, str]:
        from .latvian import default_lexicon

        lex = default_lexicon()
        return len(lex) > 100, f"{len(lex)} vārdformas"

    _check(
        report, "Latviešu leksikons", lexicon,
        hint="Iztrūkst periodika/data/lv_wordlist.txt vai PERIODIKA_LV_WORDLIST ceļš ir nepareizs.",
    )

    # --- neobligātās iespējas -----------------------------------------
    from . import images as images_mod

    caps = images_mod.capabilities()
    report.add(Check(
        name="Pillow (attēlu priekšapstrāde)", ok=bool(caps["pillow"]),
        detail=caps.get("pillow_versija") or "nav uzstādīts",
        hint="pip install 'periodika-agent[images]' — bez tā rindu sagriešana nestrādā.",
    ))
    tess = shutil.which("tesseract")
    report.add(Check(
        name="tesseract (drukas OCR)", ok=bool(tess), detail=tess or "nav atrasts",
        hint=("Debian/Ubuntu: apt install tesseract-ocr tesseract-ocr-lav tesseract-ocr-frk; "
              "macOS: brew install tesseract tesseract-lang; "
              "Windows: winget install UB-Mannheim.TesseractOCR"),
    ))
    if tess:
        from .recognize import _tesseract_languages

        langs = _tesseract_languages(tess)
        needed = {"lav", "frk"} - langs
        report.add(Check(
            name="tesseract latviešu/Fraktur valodas", ok=not needed,
            detail=f"uzstādītas: {', '.join(sorted(langs)) or '-'}",
            hint=f"Trūkst valodu datnes: {', '.join(sorted(needed))}",
        ))
    poppler = shutil.which("pdftoppm")
    report.add(Check(
        name="poppler (PDF -> attēli)", ok=bool(poppler), detail=poppler or "nav atrasts",
        hint="Debian/Ubuntu: apt install poppler-utils; macOS: brew install poppler",
    ))

    def anthropic_sdk() -> tuple[bool, str]:
        import importlib

        module = importlib.import_module("anthropic")
        return True, f"anthropic {getattr(module, '__version__', '?')}"

    report.add(Check(
        name="anthropic SDK (dzinējs `claude`)",
        ok=_safe(anthropic_sdk)[0], detail=_safe(anthropic_sdk)[1] or "nav uzstādīts",
        hint=("pip install 'periodika-agent[claude]' — nav vajadzīgs dzinējam `agent`, "
              "kur attēlu nolasa pats aģents."),
    ))

    # --- profils un tīkls ---------------------------------------------
    profile = cfg.profile
    configured = [
        name for name, value in (
            ("sitemap", profile.sitemap_urls), ("OAI-PMH", profile.oai_endpoints),
            ("meklēšana", profile.search_url_template), ("METS", profile.mets_url_template),
        ) if value
    ]
    report.add(Check(
        name="Vietnes profils", ok=bool(configured),
        detail=(f"{profile.base_url}; noskaidroti: {', '.join(configured)}"
                if configured else f"{profile.base_url}; nekas vēl nav noskaidrots"),
        hint="Palaid `periodika probe --sample-issue <ID>`, lai noskaidrotu vietnes ceļus.",
    ))

    if check_network:
        def reach() -> tuple[bool, str]:
            from .http_client import HttpClient

            policy = cfg.policy
            policy.timeout = min(policy.timeout, 20.0)
            client = HttpClient(policy)
            resp = client.get(profile.base_url.rstrip("/") + "/robots.txt",
                              use_cache=False, check_robots=False)
            return 200 <= resp.status < 400, f"HTTP {resp.status} no {resp.url}"

        _check(
            report, "Savienojums ar vietni", reach,
            hint=("Pārbaudi internetu un starpniekserveri (HTTPS_PROXY). "
                  "Bez tīkla strādā tikai lokālais indekss un attēlu/teksta rīki."),
        )
    return report


def _safe(fn: Callable[[], tuple[bool, str]]) -> tuple[bool, str]:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return False, "" if isinstance(exc, ImportError) else f"{type(exc).__name__}: {exc}"


# -------------------------------------------------------------- pašpārbaude
def self_test() -> Report:
    """Nolaiž pilnu cauruļvadu bez tīkla. Katrs solis — atsevišķa pārbaude."""
    from .alto import assemble_articles, parse_alto, parse_mets
    from .correct import correct_text
    from .latvian import analyze_article, parse_latvian_date, place_variants
    from .search import local_search
    from .store import Document, Store

    report = Report()
    state: dict[str, Any] = {}

    def step_alto() -> tuple[bool, str]:
        page = parse_alto(_SAMPLE_ALTO)
        state["page"] = page
        text = page.blocks[0].text
        return ("Beedriba" in text and "Beed-" not in text,
                f"{len(page.blocks)} bloki, pārnesums salikts kopā")

    def step_mets() -> tuple[bool, str]:
        mets = parse_mets(_SAMPLE_METS)
        articles = assemble_articles(mets, {"ALTO_0001": state["page"]})
        state["article"] = articles[0] if articles else None
        ok = bool(articles) and "Sludinajumi" not in articles[0].text
        return ok, (f"{len(articles)} raksts; sludinājums nav ielīdis"
                    if ok else "raksta salikšana neizdevās")

    def step_orthography() -> tuple[bool, str]:
        article = state["article"]
        analysis = analyze_article(article.text if article else "")
        state["analysis"] = analysis
        ok = analysis["orthography"] == "veca" and "Biedriba" in str(analysis["text_modern"])
        return ok, f"{analysis['orthography']} -> {str(analysis['text_modern'])[:48]}"

    def step_index() -> tuple[bool, str]:
        tmp = Path(tempfile.mkdtemp(prefix="periodika-doctor-"))
        store = Store(tmp / "test.sqlite3")
        analysis = state["analysis"]
        store.upsert_document(Document(
            id="doctor1", kind="article", title="Baltijas Wehstnesis",
            text_raw=str(analysis["text_raw"]), text_modern=str(analysis["text_modern"]),
            date="1899-05-01", issue_id="doctor", orthography=str(analysis["orthography"]),
        ))
        state["store"] = store
        return store.stats()["dokumenti"] == 1, f"indekss: {tmp}"

    def step_search() -> tuple[bool, str]:
        store = state["store"]
        modern = local_search(store, "biedrība")
        old = local_search(store, "Beedriba")
        return bool(modern) and bool(old), (
            f"mūsdienu vaicājums: {len(modern)}, vecās drukas vaicājums: {len(old)}"
        )

    def step_correct() -> tuple[bool, str]:
        fixed = correct_text("Rigaf latviefu biedriba")
        return fixed.text == "Rigas latviesu biedriba", fixed.text

    def step_dates() -> tuple[bool, str]:
        parsed = parse_latvian_date("Rīgā, 1899. gada 1. (13.) maijā")
        return parsed["date"] == "1899-05-13", f"{parsed['date']} / {parsed.get('date_old_style')}"

    def step_places() -> tuple[bool, str]:
        variants = place_variants("Jelgava")
        return "Mitau" in variants, ", ".join(variants[:4])

    for name, fn in (
        ("ALTO parsēšana", step_alto),
        ("METS raksta salikšana", step_mets),
        ("Vecās ortogrāfijas normalizācija", step_orthography),
        ("Indeksēšana", step_index),
        ("Meklēšana abās ortogrāfijās", step_search),
        ("Atpazīšanas kļūdu labošana", step_correct),
        ("Datumi (vecais/jaunais stils)", step_dates),
        ("Vēsturiskie vietvārdi", step_places),
    ):
        _check(report, name, fn, critical=True)
    return report


def format_report(env: Report, tests: Report | None = None) -> str:
    lines = ["periodika doctor", "=" * 60, "", "Vide:"]
    for check in env.checks:
        mark = "✓" if check.ok else ("✗" if check.critical else "!")
        lines.append(f"  [{mark}] {check.name}: {check.detail}")
        if not check.ok and check.hint:
            lines.append(f"        -> {check.hint}")
    if tests is not None:
        lines += ["", "Pašpārbaude (bez tīkla):"]
        for check in tests.checks:
            lines.append(f"  [{'✓' if check.ok else '✗'}] {check.name}: {check.detail}")
    lines += ["", "-" * 60]
    critical = env.critical_failures + (tests.critical_failures if tests else [])
    if critical:
        lines.append("Nestrādās, kamēr nav novērsts: " + ", ".join(c.name for c in critical))
    elif env.failures:
        lines.append("Strādā. Neobligātās iespējas, kuru trūkst: "
                     + ", ".join(c.name for c in env.failures))
    else:
        lines.append("Viss kārtībā.")
    return "\n".join(lines)
