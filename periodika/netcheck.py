"""Kas tieši bloķē — tīkls, starpniekserveris, sertifikāts vai pati vietne?

"Nestrādā" var nozīmēt sešas dažādas lietas, un katrai ir cits risinājums. Šis
modulis tās izšķir pa slāņiem un katram atradumam pieliek konkrētu darbību:

    DNS -> TCP -> starpniekserveris (CONNECT) -> TLS -> HTTP -> robots.txt

Beigās, ja pieejams pārlūks, tiek pārbaudīts arī tas: ja Chromium lapu atver, bet
vienkāršs pieprasījums nē, tad bloķē nevis tīkls, bet vietnes pārbaude klientam
(User-Agent, sīkdatnes, JavaScript) — un to atrisina ``--via-browser``.
"""

from __future__ import annotations

import os
import socket
import ssl
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Layer", "NetDiagnosis", "diagnose"]

_TIMEOUT = 12.0


@dataclass
class Layer:
    name: str
    ok: bool | None            # None = nepārbaudīts / neattiecas
    detail: str = ""
    remedy: str = ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"slānis": self.name, "kārtībā": self.ok}
        if self.detail:
            out["ziņa"] = self.detail
        if self.remedy and self.ok is not True:
            out["ko_darīt"] = self.remedy
        return out


@dataclass
class NetDiagnosis:
    url: str
    layers: list[Layer] = field(default_factory=list)
    verdict: str = ""
    remedies: list[str] = field(default_factory=list)

    def add(self, layer: Layer) -> Layer:
        self.layers.append(layer)
        return layer

    def to_json(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "slāņi": [l.to_json() for l in self.layers],
            "secinājums": self.verdict,
            "risinājumi": self.remedies,
        }

    def as_text(self) -> str:
        lines = [f"periodika netcheck: {self.url}", "=" * 60]
        for layer in self.layers:
            mark = {True: "✓", False: "✗", None: "–"}[layer.ok]
            lines.append(f"  [{mark}] {layer.name}: {layer.detail}")
            if layer.ok is not True and layer.remedy:
                lines.append(f"        -> {layer.remedy}")
        lines += ["", "-" * 60, self.verdict]
        lines += [f"  * {r}" for r in self.remedies]
        return "\n".join(lines)


def _proxy_for(scheme: str) -> str:
    keys = ("https_proxy", "HTTPS_PROXY") if scheme == "https" else ("http_proxy", "HTTP_PROXY")
    for key in keys:
        value = os.environ.get(key)
        if value:
            return value
    return ""


def _bypasses_proxy(host: str) -> bool:
    # Abi mainīgie var būt uzstādīti vienlaikus un ar dažādu saturu — skatām abus.
    entries = ",".join(
        v for v in (os.environ.get("no_proxy"), os.environ.get("NO_PROXY")) if v
    )
    for entry in entries.split(","):
        entry = entry.strip().lstrip(".")
        if entry and (host == entry or host.endswith("." + entry)):
            return True
    return False


def diagnose(
    url: str,
    *,
    user_agent: str = "",
    ca_bundle: str = "",
    proxy: str = "",
    check_browser: bool = True,
    timeout: float = _TIMEOUT,
) -> NetDiagnosis:
    """Pārbauda savienojumu pa slāņiem un pasaka, kurš no tiem krīt."""
    parts = urllib.parse.urlsplit(url if "://" in url else f"https://{url}")
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    result = NetDiagnosis(url=url)

    proxy_url = proxy or _proxy_for(parts.scheme)
    bypass = _bypasses_proxy(host)
    result.add(Layer(
        name="Vide",
        ok=True,
        detail=(
            f"starpniekserveris: {proxy_url or 'nav'}"
            + (" (šim saimniekdatoram apiets)" if proxy_url and bypass else "")
            + f"; CA fails: {ca_bundle or os.environ.get('SSL_CERT_FILE') or 'sistēmas'}"
        ),
    ))

    # --- DNS ----------------------------------------------------------
    dns = result.add(_check_dns(host))
    if dns.ok is False:
        return _conclude(result)

    # --- starpniekserveris vai tiešs TCP -------------------------------
    if proxy_url and not bypass:
        tunnel = result.add(_check_proxy_connect(proxy_url, host, port, timeout))
        if tunnel.ok is False:
            return _conclude(result, check_browser=check_browser, url=url,
                             user_agent=user_agent)
    else:
        tcp = result.add(_check_tcp(host, port, timeout))
        if tcp.ok is False:
            return _conclude(result)
        if parts.scheme == "https":
            result.add(_check_tls(host, port, ca_bundle, timeout))

    # --- HTTP ----------------------------------------------------------
    result.add(_check_http(url, user_agent=user_agent, ca_bundle=ca_bundle,
                           proxy=proxy, timeout=timeout))
    result.add(_check_robots(url, user_agent=user_agent, timeout=timeout))
    return _conclude(result, check_browser=check_browser, url=url, user_agent=user_agent)


# ------------------------------------------------------------------ slāņi
def _check_dns(host: str) -> Layer:
    try:
        infos = socket.getaddrinfo(host, None)
        addresses = sorted({info[4][0] for info in infos})
        return Layer("DNS", True, f"{host} -> {', '.join(addresses[:4])}")
    except socket.gaierror as exc:
        return Layer(
            "DNS", False, f"{host}: {exc}",
            remedy=("Vārds neatrisinās. Pārbaudi interneta savienojumu un DNS; "
                    "korporatīvā tīklā vietne var būt slēgta jau DNS līmenī."),
        )


def _check_tcp(host: str, port: int, timeout: float) -> Layer:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return Layer("TCP savienojums", True, f"{host}:{port} atvērts")
    except OSError as exc:
        return Layer(
            "TCP savienojums", False, f"{host}:{port}: {exc}",
            remedy=("Savienojums netiek izveidots. Ja esi aiz korporatīvā "
                    "starpniekservera, norādi to: --proxy http://serveris:ports "
                    "(vai HTTPS_PROXY vides mainīgais)."),
        )


def _check_proxy_connect(proxy_url: str, host: str, port: int, timeout: float) -> Layer:
    parts = urllib.parse.urlsplit(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    try:
        with socket.create_connection((parts.hostname or "", parts.port or 8080),
                                      timeout=timeout) as sock:
            sock.sendall(
                f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode()
            )
            status_line = sock.recv(256).decode("latin-1", "replace").split("\r\n")[0]
    except OSError as exc:
        return Layer(
            "Starpniekserveris", False, f"{proxy_url}: {exc}",
            remedy="Starpniekserveris nav sasniedzams. Pārbaudi adresi un portu.",
        )
    code = status_line.split()[1] if len(status_line.split()) > 1 else "?"
    if code == "200":
        return Layer("Starpniekserveris", True, f"CONNECT {host}:{port} atļauts")
    if code in ("403", "407"):
        return Layer(
            "Starpniekserveris", False, f"CONNECT {host}:{port} -> {status_line.strip()}",
            remedy=(
                "Starpniekserveris neatļauj šo saimniekdatoru — tas ir organizācijas "
                "politikas lēmums, ne kļūda. To neapiet: palūdz administratoram "
                f"pievienot {host} atļauto sarakstam, vai palaid rīku tīklā bez šī "
                "ierobežojuma (piemēram, uz sava datora)."
                + (" 407 nozīmē, ka trūkst starpniekservera akreditācijas."
                   if code == "407" else "")
            ),
        )
    return Layer(
        "Starpniekserveris", False, f"CONNECT -> {status_line.strip()}",
        remedy="Neparasta atbilde uz CONNECT; skaties starpniekservera žurnālā.",
    )


def _check_tls(host: str, port: int, ca_bundle: str, timeout: float) -> Layer:
    context = ssl.create_default_context(cafile=ca_bundle or None)
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert() or {}
                issuer = dict(x[0] for x in cert.get("issuer", ())).get("organizationName", "?")
                return Layer("TLS", True, f"{tls.version()}, izsniedzējs: {issuer}")
    except ssl.SSLCertVerificationError as exc:
        return Layer(
            "TLS", False, str(exc),
            remedy=("Sertifikāts netiek atzīts — parasti tas nozīmē korporatīvu "
                    "starpniekserveri, kas pārtver TLS. Norādi tā sertifikātu ar "
                    "--ca-bundle /ceļš/uz/ca.crt (vai SSL_CERT_FILE). "
                    "Nekad neizslēdz pārbaudi."),
        )
    except OSError as exc:
        return Layer("TLS", False, str(exc), remedy="Rokasspiediens neizdevās.")


def _check_http(url: str, *, user_agent: str, ca_bundle: str, proxy: str,
                timeout: float) -> Layer:
    from .config import CrawlPolicy
    from .http_client import FetchError, HttpClient

    policy = CrawlPolicy(timeout=timeout, max_retries=0, obey_robots=False)
    if user_agent:
        policy.user_agent = user_agent
    policy.ca_bundle = ca_bundle
    policy.proxy = proxy
    client = HttpClient(policy)
    # Ja dots tikai saimniekdators, pārbaudām robots.txt; ja pilns ceļš — to pašu.
    parts = urllib.parse.urlsplit(url)
    target = (
        urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
        if parts.path in ("", "/")
        else url
    )
    try:
        resp = client.get(target, use_cache=False, check_robots=False)
    except FetchError as exc:
        status = exc.status
        if status == 403:
            return Layer(
                "HTTP", False, f"{target}: HTTP 403",
                remedy=("Serveris atteica *šim klientam*, nevis tīklam. Parasti "
                        "palīdz: --contact ar īstu e-pastu (godīgs User-Agent), "
                        "lēnāks ātrums (--rate 0.5), vai --via-browser, kas iet "
                        "caur īstu pārlūku ar sīkdatnēm."),
            )
        if status == 429:
            return Layer(
                "HTTP", False, f"{target}: HTTP 429 (par daudz pieprasījumu)",
                remedy="Samazini ātrumu: --rate 0.5 --workers 1. Rīks ievēro Retry-After.",
            )
        if status in (502, 503, 504):
            return Layer("HTTP", False, f"{target}: HTTP {status}",
                         remedy="Vietne pati nav pieejama. Pamēģini vēlāk.")
        return Layer("HTTP", False, f"{target}: {exc.message}",
                     remedy="Skaties iepriekšējos slāņus — kļūda nāk no zemāka līmeņa.")
    return Layer("HTTP", True, f"{resp.url}: HTTP {resp.status}, {len(resp.body)} baiti")


def _check_robots(url: str, *, user_agent: str, timeout: float) -> Layer:
    from .config import CrawlPolicy
    from .http_client import HttpClient

    policy = CrawlPolicy(timeout=timeout, max_retries=0)
    if user_agent:
        policy.user_agent = user_agent
    client = HttpClient(policy)
    try:
        allowed = client.allowed(url)
    except Exception as exc:  # noqa: BLE001
        return Layer("robots.txt", None, f"nevarēja nolasīt: {exc}")
    if allowed:
        return Layer("robots.txt", True, "šis ceļš ir atļauts")
    return Layer(
        "robots.txt", False, "vietne aizliedz šo ceļu šim User-Agent",
        remedy=("Tas nav tīkla bloks, bet vietnes noteikums. Ievēro to: meklē citu "
                "ceļu pie tiem pašiem datiem (OAI-PMH, datu slānis) vai sazinies ar "
                "bibliotēku. --ignore-robots lieto tikai ar atļauju."),
    )


def _check_browser(url: str, user_agent: str) -> Layer:
    from .browser import BrowserReader, BrowserUnavailable, browser_available

    if not browser_available()["pieejams"]:
        return Layer("Pārlūks", None, "nav uzstādīts (neobligāts)")
    try:
        with BrowserReader(timeout=30, user_agent=user_agent) as reader:
            page = reader.read(url)
    except BrowserUnavailable as exc:
        return Layer("Pārlūks", None, str(exc))
    except Exception as exc:  # noqa: BLE001
        return Layer("Pārlūks", False, f"{type(exc).__name__}: {exc}",
                     remedy="Arī pārlūks lapu neatvēra — bloks ir zemāk par HTTP slāni.")
    return Layer("Pārlūks", True, f"lapa atvērta, {len(page.text)} rakstzīmes teksta")


# ---------------------------------------------------------------- secinājums
def _conclude(result: NetDiagnosis, *, check_browser: bool = False, url: str = "",
              user_agent: str = "") -> NetDiagnosis:
    by_name = {l.name: l for l in result.layers}
    failed = [l for l in result.layers if l.ok is False]

    if check_browser and url and failed:
        result.add(_check_browser(url, user_agent))
        by_name = {l.name: l for l in result.layers}

    if not failed:
        result.verdict = "Savienojums ir kārtībā — bloķēšanas nav."
        return result

    first = failed[0]
    result.remedies = [l.remedy for l in failed if l.remedy]
    browser = by_name.get("Pārlūks")
    if browser is not None and browser.ok and first.name == "HTTP":
        result.verdict = (
            "Tīkls ir kārtībā: pārlūks lapu atver, bet vienkāršs pieprasījums tiek "
            "atteikts. Bloķē pati vietne pēc klienta pazīmēm — lieto --via-browser."
        )
        result.remedies.insert(0, "periodika crawl --via-browser (vai browse/read)")
    elif first.name == "Starpniekserveris":
        result.verdict = (
            "Bloķē starpniekserveris jeb organizācijas izejas politika, nevis vietne. "
            "To neapiet — vajag atļauju vai citu tīklu."
        )
    elif first.name == "TLS":
        result.verdict = "Savienojums ir, bet sertifikāts netiek atzīts (TLS pārtveršana)."
    elif first.name == "robots.txt":
        result.verdict = "Tīkls strādā; ceļu aizliedz vietnes robots.txt."
    else:
        result.verdict = f"Krīt slānis: {first.name}."
    return result
