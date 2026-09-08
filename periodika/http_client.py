"""Pieklājīgs HTTP klients bez ārējām atkarībām.

Iespējas:
  * robots.txt ievērošana (ar kešu katram saimniekdatoram) un ``Crawl-delay``,
  * marķieru groza (token bucket) ātruma ierobežotājs katram saimniekdatoram,
  * eksponenciāla atkāpšanās ar ``Retry-After`` ievērošanu (429/503),
  * pastāvīgs SQLite kešs ar ETag/Last-Modified nosacījuma pieprasījumiem,
  * gzip/deflate atspiešana un izmēra limits.

Viss balstīts uz ``urllib``, lai rīku varētu palaist tukšā vidē bez ``pip install``.
"""

from __future__ import annotations

import email.utils
import gzip
import hashlib
import io
import random
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .config import CrawlPolicy

__all__ = ["Response", "HttpClient", "RateLimiter", "FetchError"]


def _latin1_safe(value: str) -> str:
    try:
        value.encode("latin-1")
        return value
    except UnicodeEncodeError:
        import unicodedata

        return (
            unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
        )


class FetchError(RuntimeError):
    """Neatgūstama kļūda pēc visiem atkārtojumiem."""

    def __init__(self, url: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{url}: {message}")
        self.url = url
        self.status = status
        self.message = message


@dataclass
class Response:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    from_cache: bool = False
    elapsed: float = 0.0

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()

    @property
    def charset(self) -> str:
        ct = self.headers.get("content-type") or ""
        for part in ct.split(";")[1:]:
            k, _, v = part.strip().partition("=")
            if k.lower() == "charset":
                return v.strip().strip('"') or "utf-8"
        return ""

    def text(self) -> str:
        enc = self.charset
        if enc:
            try:
                return self.body.decode(enc, errors="replace")
            except LookupError:
                pass
        # XML deklarācija: <?xml version="1.0" encoding="cp1257"?>
        m = re.search(rb"""encoding=["']([A-Za-z0-9_.\-]+)["']""", self.body[:200])
        if m:
            try:
                return self.body.decode(m.group(1).decode("ascii", "ignore"), errors="replace")
            except (LookupError, UnicodeDecodeError):
                pass
        for enc in ("utf-8", "cp1257", "iso-8859-13", "latin-1"):
            try:
                return self.body.decode(enc)
            except UnicodeDecodeError:
                continue
        return self.body.decode("utf-8", errors="replace")

    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


class RateLimiter:
    """Marķieru grozs: ``rate`` pieprasījumi sekundē ar ``burst`` uzkrājumu."""

    def __init__(self, rate: float, burst: int = 1) -> None:
        self.rate = max(rate, 0.001)
        self.burst = max(burst, 1)
        self._tokens = float(self.burst)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, extra_delay: float = 0.0) -> None:
        """Bloķē, līdz ir pieejams marķieris; ``extra_delay`` = robots Crawl-delay."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    float(self.burst), self._tokens + (now - self._updated) * self.rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    wait = 0.0
                else:
                    wait = (1.0 - self._tokens) / self.rate
            if wait <= 0.0:
                if extra_delay > 0.0:
                    time.sleep(extra_delay)
                return
            time.sleep(wait)


class _Cache:
    """SQLite kešs ar validatoriem (ETag / Last-Modified)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._conn() as con:
            con.execute(
                """CREATE TABLE IF NOT EXISTS http_cache (
                    url TEXT PRIMARY KEY,
                    status INTEGER NOT NULL,
                    headers TEXT NOT NULL,
                    body BLOB NOT NULL,
                    etag TEXT,
                    last_modified TEXT,
                    fetched_at REAL NOT NULL
                )"""
            )

    def _conn(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = sqlite3.connect(self.path, timeout=30)
            con.execute("PRAGMA journal_mode=WAL")
            self._local.con = con
        return con

    def get(self, url: str) -> tuple[Response, str | None, str | None, float] | None:
        cur = self._conn().execute(
            "SELECT status, headers, body, etag, last_modified, fetched_at "
            "FROM http_cache WHERE url=?",
            (url,),
        )
        row = cur.fetchone()
        if not row:
            return None
        status, headers_blob, body, etag, last_mod, fetched_at = row
        headers = dict(
            line.split("\t", 1) for line in headers_blob.split("\n") if "\t" in line
        )
        return (
            Response(url=url, status=status, headers=headers, body=body, from_cache=True),
            etag,
            last_mod,
            fetched_at,
        )

    def put(self, resp: Response) -> None:
        headers_blob = "\n".join(f"{k}\t{v}" for k, v in resp.headers.items())
        with self._conn() as con:
            con.execute(
                "INSERT OR REPLACE INTO http_cache "
                "(url, status, headers, body, etag, last_modified, fetched_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    resp.url,
                    resp.status,
                    headers_blob,
                    resp.body,
                    resp.headers.get("etag"),
                    resp.headers.get("last-modified"),
                    time.time(),
                ),
            )

    def touch(self, url: str) -> None:
        with self._conn() as con:
            con.execute("UPDATE http_cache SET fetched_at=? WHERE url=?", (time.time(), url))


@dataclass
class HttpClient:
    policy: CrawlPolicy = field(default_factory=CrawlPolicy)
    cache_path: Path | None = None
    _limiters: dict[str, RateLimiter] = field(default_factory=dict, init=False)
    _robots: dict[str, urllib.robotparser.RobotFileParser | None] = field(
        default_factory=dict, init=False
    )
    _crawl_delay: dict[str, float] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        self._cache = _Cache(self.cache_path) if self.cache_path else None
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPRedirectHandler(),
            urllib.request.HTTPCookieProcessor(),
        )
        self.stats = {"requests": 0, "cache_hits": 0, "not_modified": 0, "errors": 0}

    # -- robots ---------------------------------------------------------
    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parts = urllib.parse.urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            if host in self._robots:
                return self._robots[host]
        rp: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
        assert rp is not None
        rp.set_url(host + "/robots.txt")
        try:
            raw = self._raw_get(host + "/robots.txt", extra_headers={})
            rp.parse(raw.text().splitlines())
            delay = rp.crawl_delay(self.policy.effective_user_agent())
            if delay:
                with self._lock:
                    self._crawl_delay[host] = float(delay)
        except Exception:
            rp = None  # nav robots.txt vai nav sasniedzams -> neierobežojam
        with self._lock:
            self._robots[host] = rp
        return rp

    def allowed(self, url: str) -> bool:
        if not self.policy.obey_robots:
            return True
        rp = self._robots_for(url)
        if rp is None:
            return True
        return rp.can_fetch(self.policy.effective_user_agent(), url)

    def sitemaps_from_robots(self, base_url: str) -> list[str]:
        rp = self._robots_for(base_url)
        if rp is None:
            return []
        return list(getattr(rp, "site_maps", None) or [])

    # -- limiter --------------------------------------------------------
    def _limiter(self, url: str) -> tuple[RateLimiter, float]:
        parts = urllib.parse.urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            lim = self._limiters.get(host)
            if lim is None:
                lim = RateLimiter(self.policy.requests_per_second, self.policy.burst)
                self._limiters[host] = lim
            delay = self._crawl_delay.get(host, 0.0)
        return lim, delay

    # -- zemā līmeņa pieprasījums ---------------------------------------
    def _raw_get(
        self, url: str, extra_headers: dict[str, str] | None = None, method: str = "GET"
    ) -> Response:
        headers = {
            "User-Agent": self.policy.effective_user_agent(),
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "lv,en;q=0.7",
        }
        headers.update(extra_headers or {})
        # urllib kodē galvenes latin-1: viena garumzīme te nogalinātu pieprasījumu.
        headers = {k: _latin1_safe(v) for k, v in headers.items()}
        req = urllib.request.Request(url, headers=headers, method=method)
        started = time.monotonic()
        with self._opener.open(req, timeout=self.policy.timeout) as fh:
            raw = self._read_limited(fh)
            body = self._decompress(raw, fh.headers.get("Content-Encoding", ""))
            resp_headers = {k.lower(): v for k, v in fh.headers.items()}
            return Response(
                url=fh.geturl(),
                status=fh.status,
                headers=resp_headers,
                body=body,
                elapsed=time.monotonic() - started,
            )

    def _read_limited(self, fh) -> bytes:
        limit = self.policy.max_bytes
        if not limit:
            return fh.read()
        buf = io.BytesIO()
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            buf.write(chunk)
            if buf.tell() > limit:
                raise FetchError(fh.geturl(), f"atbilde pārsniedz {limit} baitus")
        return buf.getvalue()

    @staticmethod
    def _decompress(raw: bytes, encoding: str) -> bytes:
        enc = (encoding or "").lower()
        try:
            if "gzip" in enc:
                return gzip.decompress(raw)
            if "deflate" in enc:
                try:
                    return zlib.decompress(raw)
                except zlib.error:
                    return zlib.decompress(raw, -zlib.MAX_WBITS)
        except (OSError, zlib.error):
            return raw
        return raw

    # -- publiskā saskarne ----------------------------------------------
    def get(
        self,
        url: str,
        *,
        use_cache: bool = True,
        force_refresh: bool = False,
        headers: dict[str, str] | None = None,
        check_robots: bool = True,
    ) -> Response:
        """Ielādē URL, ievērojot robots.txt, kešu un ātruma ierobežojumu."""
        if check_robots and not self.allowed(url):
            raise FetchError(url, "robots.txt aizliedz šo ceļu", status=None)

        cached = self._cache.get(url) if (self._cache and use_cache) else None
        extra: dict[str, str] = dict(headers or {})
        if cached and not force_refresh:
            resp, etag, last_mod, fetched_at = cached
            if time.time() - fetched_at < self.policy.cache_ttl:
                self.stats["cache_hits"] += 1
                return resp
            if etag:
                extra["If-None-Match"] = etag
            if last_mod:
                extra["If-Modified-Since"] = last_mod

        limiter, crawl_delay = self._limiter(url)
        delay = 0.0
        last_exc: Exception | None = None
        for attempt in range(self.policy.max_retries + 1):
            limiter.acquire(extra_delay=crawl_delay)
            if delay:
                time.sleep(delay)
            try:
                self.stats["requests"] += 1
                resp = self._raw_get(url, extra)
                if self._cache and use_cache and resp.status == 200:
                    self._cache.put(resp)
                return resp
            except urllib.error.HTTPError as exc:  # noqa: PERF203
                status = exc.code
                if status == 304 and cached:
                    self.stats["not_modified"] += 1
                    self._cache.touch(url)  # type: ignore[union-attr]
                    return cached[0]
                if status in (408, 425, 429, 500, 502, 503, 504):
                    last_exc = exc
                    delay = self._retry_delay(exc.headers, attempt)
                    continue
                self.stats["errors"] += 1
                raise FetchError(url, f"HTTP {status}", status=status) from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last_exc = exc
                delay = self._retry_delay(None, attempt)
                continue
        self.stats["errors"] += 1
        raise FetchError(url, f"neizdevās pēc {self.policy.max_retries} atkārtojumiem: {last_exc}")

    def _retry_delay(self, headers, attempt: int) -> float:
        if headers is not None:
            ra = headers.get("Retry-After")
            if ra:
                try:
                    return min(float(ra), self.policy.backoff_cap)
                except ValueError:
                    parsed = email.utils.parsedate_to_datetime(ra)
                    if parsed:
                        return max(0.0, min(parsed.timestamp() - time.time(), self.policy.backoff_cap))
        base = self.policy.backoff_base ** attempt
        return min(base, self.policy.backoff_cap) * (0.5 + random.random() / 2)

    def head_ok(self, url: str) -> bool:
        """Ātra pārbaude, vai URL eksistē (izmanto ``probe``)."""
        try:
            resp = self.get(url, use_cache=True)
            return 200 <= resp.status < 300 and bool(resp.body)
        except FetchError:
            return False
        except Exception:
            return False

    def get_many(self, urls: Iterable[str], **kwargs) -> list[Response | FetchError]:
        out: list[Response | FetchError] = []
        for url in urls:
            try:
                out.append(self.get(url, **kwargs))
            except FetchError as exc:
                out.append(exc)
        return out
