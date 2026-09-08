"""Minimāla HTML apstrāde uz ``html.parser`` bāzes (bez bs4/lxml).

Pietiekami periodika.lv navigācijai: saites, metadati, JSON-LD, iegultie
``window.__INITIAL_STATE__`` bloki un lasāms teksts ar saglabātām rindkopām.
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser

__all__ = ["HtmlDocument", "parse_html", "absolutize", "strip_tags"]

_SKIP_CONTENT = {"script", "style", "noscript", "template", "svg", "canvas"}
_BLOCK = {
    "p", "div", "br", "li", "tr", "section", "article", "header", "footer",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "table", "ul", "ol",
}
#: Konteineri, kas gandrīz nekad nesatur raksta tekstu.
_CHROME = {"nav", "header", "footer", "aside", "form"}


@dataclass
class HtmlDocument:
    url: str = ""
    title: str = ""
    lang: str = ""
    meta: dict[str, str] = field(default_factory=dict)
    links: list[str] = field(default_factory=list)
    link_texts: dict[str, str] = field(default_factory=dict)
    text: str = ""
    main_text: str = ""
    json_ld: list[dict] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)

    def meta_get(self, *names: str) -> str:
        for n in names:
            v = self.meta.get(n.lower())
            if v:
                return v
        return ""


class _Parser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.doc = HtmlDocument(url=base_url)
        self._skip_depth = 0
        self._chrome_depth = 0
        self._in_title = False
        self._script_type = ""
        self._script_buf: list[str] = []
        self._chunks: list[str] = []
        self._main_chunks: list[str] = []
        self._href_stack: list[str] = []

    # -- palīgi ---------------------------------------------------------
    def _emit(self, text: str) -> None:
        self._chunks.append(text)
        if self._chrome_depth == 0:
            self._main_chunks.append(text)

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in _SKIP_CONTENT:
            self._skip_depth += 1
            if tag == "script":
                self._script_type = a.get("type", "").lower()
                self._script_buf = []
            return
        if tag in _CHROME:
            self._chrome_depth += 1
        if tag == "html" and a.get("lang"):
            self.doc.lang = a["lang"]
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = a.get("name") or a.get("property") or a.get("http-equiv") or ""
            content = a.get("content", "")
            if name and content:
                self.doc.meta[name.lower()] = content
        elif tag == "a":
            href = a.get("href", "").strip()
            self._href_stack.append(href)
            if href:
                absolute = absolutize(self.base_url, href)
                if absolute:
                    self.doc.links.append(absolute)
        elif tag in ("link",):
            rel = a.get("rel", "").lower()
            href = a.get("href", "")
            if href and rel in ("canonical", "alternate", "next", "prev"):
                absolute = absolutize(self.base_url, href)
                if absolute:
                    self.doc.meta.setdefault(f"link:{rel}", absolute)
        elif tag in ("iframe", "frame", "embed"):
            src = a.get("src", "")
            if src:
                absolute = absolutize(self.base_url, src)
                if absolute:
                    self.doc.links.append(absolute)
        if tag in _BLOCK:
            self._emit("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in _SKIP_CONTENT:
            if tag == "script" and self._script_buf:
                blob = "".join(self._script_buf)
                if "ld+json" in self._script_type:
                    try:
                        data = json.loads(blob)
                        self.doc.json_ld.extend(data if isinstance(data, list) else [data])
                    except Exception:
                        pass
                else:
                    self.doc.scripts.append(blob)
                self._script_buf = []
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in _CHROME:
            self._chrome_depth = max(0, self._chrome_depth - 1)
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._href_stack:
            self._href_stack.pop()
        if tag in _BLOCK:
            self._emit("\n")

    def handle_data(self, data):
        if self._skip_depth:
            if self._script_buf is not None:
                self._script_buf.append(data)
            return
        if self._in_title:
            self.doc.title += data.strip()
            return
        if data.strip():
            self._emit(data)
            if self._href_stack and self._href_stack[-1]:
                absolute = absolutize(self.base_url, self._href_stack[-1])
                if absolute:
                    prev = self.doc.link_texts.get(absolute, "")
                    self.doc.link_texts[absolute] = (prev + " " + data.strip()).strip()[:300]

    def finish(self) -> HtmlDocument:
        self.doc.text = _tidy("".join(self._chunks))
        self.doc.main_text = _tidy("".join(self._main_chunks)) or self.doc.text
        # dublikātus prom, saglabājot secību
        seen: set[str] = set()
        self.doc.links = [u for u in self.doc.links if not (u in seen or seen.add(u))]
        return self.doc


def _tidy(text: str) -> str:
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def absolutize(base_url: str, href: str) -> str:
    href = href.strip()
    if not href or href.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
        return ""
    try:
        return urllib.parse.urljoin(base_url, href)
    except ValueError:
        return ""


def parse_html(markup: str, base_url: str = "") -> HtmlDocument:
    parser = _Parser(base_url)
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        pass
    return parser.finish()


def strip_tags(markup: str) -> str:
    return parse_html(markup).text
