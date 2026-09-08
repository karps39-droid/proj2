"""Ieiešana lapā ar īstu pārlūku (headless Chromium).

``periodika2-viewer`` ir vienas lapas lietotne: HTML avotā teksta nav, tas
ienāk ar JavaScript. Parasts HTTP pieprasījums tur neko neatrod. Šis modulis
atver lapu īstā pārlūkā, sagaida, kamēr saturs ir uzzīmēts, un nolasa to.

Divi ieguvumi, no kuriem otrais ir svarīgākais:

1. **Nolasīšana.** Uzzīmētais teksts, saites (arī hash-maršruti, ko izveido pati
   lietotne) un, ja vajag, ekrānuzņēmums, ko var padot atpazīšanai.
2. **Datu slāņa atklāšana.** Pārlūks pieraksta *katru* pieprasījumu, ko lietotne
   izdara. Tur redzams, no kurienes tā ņem METS/ALTO/JSON — un no tā tiek
   uzģenerētas URL veidnes profilam. Pēc tam rāpulis var strādāt tieši ar datu
   slāni: ātri, pieklājīgi un bez pārlūka.

Playwright ir neobligāta atkarība; bez tās viss pārējais strādā kā līdz šim.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "BrowserUnavailable",
    "BrowserReader",
    "PageRead",
    "browser_available",
    "find_chromium",
    "generalize_urls",
    "read_page",
    "discover_endpoints",
]


class BrowserUnavailable(RuntimeError):
    def __init__(self, detail: str = "") -> None:
        super().__init__(
            "Nav pieejams headless pārlūks. Uzstādi ar:\n"
            "    pip install 'periodika-agent[browser]'\n"
            "    python -m playwright install chromium\n"
            "Ja Chromium jau ir, norādi to ar PERIODIKA_CHROMIUM=/ceļš/uz/chrome."
            + (f"\nSīkāk: {detail}" if detail else "")
        )


# ------------------------------------------------------------ pārlūka meklēšana
_CHROMIUM_GLOBS = (
    "chromium-*/chrome-linux*/chrome",
    "chromium-*/chrome-linux*/headless_shell",
    "chromium_headless_shell-*/chrome-linux*/headless_shell",
    "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
    "chromium-*/chrome-win*/chrome.exe",
)
_SYSTEM_BROWSERS = (
    "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)


def find_chromium() -> str:
    """Atrod lietojamu Chromium: vides mainīgais, Playwright mape, sistēma.

    Playwright noklusējuma ceļš mēdz nesakrist ar to, kas tiešām ir uz diska
    (piemēram, konteinerā ar iepriekš uzstādītu pārlūku), tāpēc meklējam paši.
    """
    explicit = os.environ.get("PERIODIKA_CHROMIUM")
    if explicit and Path(explicit).exists():
        return explicit
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""), str(Path.home() / ".cache/ms-playwright")]
    for root in roots:
        if not root or not Path(root).is_dir():
            continue
        for pattern in _CHROMIUM_GLOBS:
            found = sorted(Path(root).glob(pattern))
            if found:
                return str(found[-1])
    for candidate in _SYSTEM_BROWSERS:
        if Path(candidate).exists():
            return candidate
    return ""


def browser_available() -> dict[str, Any]:
    try:
        import playwright  # noqa: PLC0415, F401
        has_playwright = True
    except ImportError:
        has_playwright = False
    executable = find_chromium() if has_playwright else ""
    return {
        "playwright": has_playwright,
        "chromium": executable,
        "pieejams": bool(has_playwright and executable),
        "norāde": (
            "" if has_playwright and executable
            else "pip install 'periodika-agent[browser]' && python -m playwright install chromium"
        ),
    }


# ------------------------------------------------------------------ rezultāts
@dataclass
class NetworkCall:
    url: str
    method: str = "GET"
    status: int = 0
    content_type: str = ""
    resource_type: str = ""
    size: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "url": self.url, "metode": self.method, "statuss": self.status,
            "tips": self.content_type or self.resource_type, "baiti": self.size,
        }


@dataclass
class PageRead:
    url: str
    title: str = ""
    text: str = ""
    links: list[str] = field(default_factory=list)
    calls: list[NetworkCall] = field(default_factory=list)
    saved: dict[str, str] = field(default_factory=dict)
    screenshot: str = ""
    html: str = ""

    def data_calls(self) -> list[NetworkCall]:
        """Pieprasījumi, kas izskatās pēc datu slāņa (XML/JSON), ne pēc dizaina."""
        out: list[NetworkCall] = []
        for call in self.calls:
            blob = f"{call.content_type} {call.url}".lower()
            if any(k in blob for k in ("xml", "json", "alto", "mets", "tei", "text/plain")):
                if not any(k in blob for k in (".js", ".css", ".woff", "font", "sourcemap")):
                    out.append(call)
        return out

    def to_json(self, *, include_text: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "url": self.url,
            "virsraksts": self.title,
            "saites": self.links[:80],
            "datu_pieprasījumi": [c.to_json() for c in self.data_calls()],
            "visi_pieprasījumi": len(self.calls),
        }
        if include_text:
            out["teksts"] = self.text
        if self.saved:
            out["saglabātie_faili"] = self.saved
        if self.screenshot:
            out["ekrānuzņēmums"] = self.screenshot
        return out


# ------------------------------------------------------------------- lasītājs
class BrowserReader:
    """Konteksta pārvaldnieks ap Playwright sinhrono API.

        with BrowserReader() as reader:
            result = reader.read(url, wait_for=".article-text")
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        user_agent: str = "",
        timeout: float = 45.0,
        executable_path: str = "",
        locale: str = "lv-LV",
        block_media: bool = True,
    ) -> None:
        self.headless = headless
        self.user_agent = user_agent
        self.timeout = timeout * 1000
        self.executable_path = executable_path or find_chromium()
        self.locale = locale
        self.block_media = block_media
        self._playwright = None
        self._browser = None
        self._context = None

    def __enter__(self) -> "BrowserReader":
        try:
            from playwright.sync_api import sync_playwright  # noqa: PLC0415
        except ImportError as exc:
            raise BrowserUnavailable(str(exc)) from exc
        self._playwright = sync_playwright().start()
        launch: dict[str, Any] = {"headless": self.headless}
        if self.executable_path:
            launch["executable_path"] = self.executable_path
        try:
            self._browser = self._playwright.chromium.launch(**launch)
        except Exception as exc:  # noqa: BLE001
            self._playwright.stop()
            self._playwright = None
            raise BrowserUnavailable(str(exc)) from exc
        self._context = self._browser.new_context(
            user_agent=self.user_agent or None, locale=self.locale
        )
        self._context.set_default_timeout(self.timeout)
        return self

    def __exit__(self, *exc_info) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # noqa: BLE001
                pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
        self._context = self._browser = self._playwright = None

    # ------------------------------------------------------------------
    def read(
        self,
        url: str,
        *,
        wait_for: str = "",
        wait_text: str = "",
        settle_ms: int = 700,
        max_settle: int = 12,
        save_data_to: str | Path | None = None,
        screenshot_to: str | Path | None = None,
        full_page: bool = True,
        scroll: bool = True,
    ) -> PageRead:
        """Atver lapu, sagaida uzzīmēšanu un nolasa saturu.

        SPA satura gaidīšana ir divpakāpju: vispirms tīkla rimšana, tad teksta
        garuma stabilizēšanās — lietotne mēdz zīmēt pakāpeniski, un ``networkidle``
        viens pats bieži nostrādā par agru.
        """
        if self._context is None:
            raise BrowserUnavailable("BrowserReader jālieto kā `with BrowserReader() as r:`")
        page = self._context.new_page()
        calls: list[NetworkCall] = []
        bodies: dict[str, bytes] = {}

        def on_response(response) -> None:
            try:
                headers = response.headers
                call = NetworkCall(
                    url=response.url,
                    method=response.request.method,
                    status=response.status,
                    content_type=(headers.get("content-type") or "").split(";")[0],
                    resource_type=response.request.resource_type,
                    size=int(headers.get("content-length") or 0),
                )
                calls.append(call)
                if save_data_to and _looks_like_data(call):
                    bodies[response.url] = response.body()
            except Exception:  # noqa: BLE001 - atbildes ķeršana nedrīkst apturēt lasīšanu
                pass

        page.on("response", on_response)
        if self.block_media:
            page.route(
                re.compile(r"\.(woff2?|ttf|eot|mp4|webm|avi)(\?|$)", re.I),
                lambda route: route.abort(),
            )

        result = PageRead(url=url)
        try:
            page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=self.timeout)
            except Exception:  # noqa: BLE001 - dažas lapas nekad nenorimst
                pass
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=self.timeout)
                except Exception:  # noqa: BLE001
                    pass
            if wait_text:
                try:
                    page.wait_for_function(
                        "t => document.body && document.body.innerText.includes(t)",
                        arg=wait_text, timeout=self.timeout,
                    )
                except Exception:  # noqa: BLE001
                    pass
            if scroll:
                _scroll_through(page)
            _wait_until_settled(page, settle_ms, max_settle)

            result.title = page.title()
            result.html = page.content()
            result.text = _clean_text(page.inner_text("body"))
            result.links = _collect_links(page)
            if screenshot_to:
                target = Path(screenshot_to)
                target.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(target), full_page=full_page)
                result.screenshot = str(target)
        finally:
            result.calls = calls
            page.close()

        if save_data_to and bodies:
            result.saved = _save_bodies(bodies, Path(save_data_to))
        return result

    def read_many(self, urls: Sequence[str], **kwargs: Any) -> list[PageRead]:
        return [self.read(url, **kwargs) for url in urls]


def _looks_like_data(call: NetworkCall) -> bool:
    blob = f"{call.content_type} {call.url}".lower()
    if any(k in blob for k in (".js", ".css", ".png", ".jpg", ".svg", ".woff", "font")):
        return False
    return any(k in blob for k in ("xml", "json", "alto", "mets", "tei", "text/plain"))


def _scroll_through(page, steps: int = 6) -> None:
    """Dažas lietotnes ielādē saturu tikai tad, kad tas nonāk redzamajā daļā."""
    try:
        for _ in range(steps):
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(120)
        page.evaluate("window.scrollTo(0, 0)")
    except Exception:  # noqa: BLE001
        pass


def _wait_until_settled(page, settle_ms: int, max_rounds: int) -> None:
    previous = -1
    for _ in range(max_rounds):
        try:
            length = page.evaluate("document.body ? document.body.innerText.length : 0")
        except Exception:  # noqa: BLE001
            return
        if length == previous and length > 0:
            return
        previous = length
        page.wait_for_timeout(settle_ms)


def _collect_links(page) -> list[str]:
    try:
        hrefs = page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.href)"
        )
    except Exception:  # noqa: BLE001
        return []
    seen: set[str] = set()
    out: list[str] = []
    for href in hrefs:
        if href and href not in seen and href.startswith(("http://", "https://")):
            seen.add(href)
            out.append(href)
    return out


def _clean_text(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text or "")
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _save_bodies(bodies: dict[str, bytes], out_dir: Path) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: dict[str, str] = {}
    for i, (url, body) in enumerate(bodies.items(), start=1):
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", url.split("?")[0].rsplit("/", 1)[-1]) or "dati"
        target = out_dir / f"{i:03d}_{name}"
        if not target.suffix:
            target = target.with_suffix(".xml" if body.lstrip()[:1] == b"<" else ".json")
        target.write_bytes(body)
        saved[url] = str(target)
    return saved


# --------------------------------------------------- URL veidņu uzģenerēšana
_KIND_HINTS = (
    ("mets", ("mets",)),
    ("alto", ("alto",)),
    ("tei", ("tei",)),
    ("iiif", ("manifest", "iiif")),
    ("plaintext", ("text", "txt", "fulltext")),
)
_NUM_SEGMENT = re.compile(r"(?<![A-Za-z0-9])(\d{1,8})(?![A-Za-z0-9])")


def _guess_kind(url: str, content_type: str = "") -> str:
    low = url.lower()
    for kind, hints in _KIND_HINTS:
        if any(h in low for h in hints):
            return kind
    if "json" in (content_type or "").lower() or low.endswith(".json"):
        return "json"
    if low.endswith(".xml") or "xml" in (content_type or "").lower():
        return "xml"
    return "cits"


def generalize_urls(
    urls: Iterable[str], issue_id: str = "", content_types: dict[str, str] | None = None
) -> list[dict[str, str]]:
    """No novērotajiem URL izveido veidnes ar ``{issue}`` un ``{n}``.

    Tieši tā tiek atklāts nedokumentēts datu slānis: lietotne pati parāda, no
    kurienes ņem datus, un mēs to vispārinām līdz veidnei, ko var lietot rāpulis::

        .../periodika2-data/p_001_bw1899n01/alto/00000003.xml
        ->  .../periodika2-data/{issue}/alto/{n:08d}.xml
    """
    types = content_types or {}
    seen: dict[str, dict[str, str]] = {}
    for url in urls:
        template = url
        if issue_id and issue_id in template:
            template = template.replace(issue_id, "{issue}")

        # Lappuses numurs: pēdējais skaitlis *ceļā*. Saimniekdatoru un portu
        # neaiztiekam — citādi "127.0.0.1:8000" pārvērstos par veidni.
        parts = urllib.parse.urlsplit(template)
        path = parts.path
        matches = list(_NUM_SEGMENT.finditer(path))
        if matches:
            last = matches[-1]
            digits = last.group(1)
            placeholder = "{n:0%dd}" % len(digits) if digits.startswith("0") else "{n}"
            path = path[: last.start(1)] + placeholder + path[last.end(1) :]
            template = urllib.parse.urlunsplit(
                (parts.scheme, parts.netloc, path, parts.query, parts.fragment)
            )

        kind = _guess_kind(url, types.get(url, ""))
        entry = {"kind": kind, "template": template, "paraugs": url}
        seen.setdefault(f"{kind}|{template}", entry)
    order = {"mets": 0, "alto": 1, "tei": 2, "plaintext": 3, "iiif": 4, "json": 5, "xml": 6}
    return sorted(seen.values(), key=lambda e: (order.get(e["kind"], 9), e["template"]))


# ------------------------------------------------------------ augsta līmeņa
def read_page(
    url: str,
    *,
    wait_for: str = "",
    save_data_to: str | Path | None = None,
    screenshot_to: str | Path | None = None,
    headless: bool = True,
    timeout: float = 45.0,
    user_agent: str = "",
) -> PageRead:
    """Atver vienu lapu pārlūkā un atgriež nolasīto."""
    with BrowserReader(headless=headless, timeout=timeout, user_agent=user_agent) as reader:
        return reader.read(
            url, wait_for=wait_for, save_data_to=save_data_to, screenshot_to=screenshot_to
        )


def discover_endpoints(
    urls: Sequence[str],
    *,
    issue_id: str = "",
    save_data_to: str | Path | None = None,
    timeout: float = 45.0,
    user_agent: str = "",
) -> dict[str, Any]:
    """Atver lapas pārlūkā un noskaidro, no kurienes lietotne ņem datus.

    Atgriež ieteiktās profila veidnes, ko var ierakstīt konfigurācijā, lai
    turpmāk rāpotu bez pārlūka.
    """
    observed: list[str] = []
    types: dict[str, str] = {}
    reads: list[dict[str, Any]] = []
    with BrowserReader(timeout=timeout, user_agent=user_agent) as reader:
        for url in urls:
            result = reader.read(url, save_data_to=save_data_to)
            reads.append(result.to_json(include_text=False))
            for call in result.data_calls():
                observed.append(call.url)
                types[call.url] = call.content_type

    templates = generalize_urls(observed, issue_id=issue_id, content_types=types)
    profile_updates = {}
    for entry in templates:
        field_name = {
            "mets": "mets_url_template", "alto": "alto_url_template",
            "tei": "tei_url_template", "plaintext": "plaintext_url_template",
            "iiif": "iiif_manifest_template",
        }.get(entry["kind"])
        if field_name and field_name not in profile_updates:
            profile_updates[field_name] = entry["template"]
    return {
        "apmeklētās_lapas": reads,
        "atrastie_datu_pieprasījumi": len(observed),
        "veidnes": templates,
        "ieteiktais_profils": profile_updates,
        "norāde": (
            "Ieraksti 'ieteiktais_profils' konfigurācijā (periodika browse --save-profile), "
            "un turpmāk rāpulis strādās tieši ar datu slāni — bez pārlūka."
        ),
    }
