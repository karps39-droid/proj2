"""ALTO / METS / TEI parsēšana — no OCR blokiem uz rakstiem.

LNB digitalizētā periodika (tāpat kā lielākā daļa Eiropas avīžu arhīvu) glabā
OCR rezultātu ALTO XML formātā, bet loģisko struktūru — kurš teksta bloks
pieder kuram rakstam — METS ``structMap TYPE="LOGICAL"`` sadaļā. Lai raksts
būtu tiešām *nolasīts* (nevis izgriezts pa lappusēm), abi jāsaliek kopā:

    mets = parse_mets(mets_bytes)
    pages = {fid: parse_alto(data) for fid, data in downloaded.items()}
    articles = assemble_articles(mets, pages)

Parsēšana ir izturīga pret nosaukumtelpu versijām (ALTO v2/v3/v4, METS ar
jebkuru prefiksu) — visur salīdzinām tikai elementa lokālo vārdu.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterable, Iterator

__all__ = [
    "AltoString",
    "AltoLine",
    "AltoBlock",
    "AltoPage",
    "MetsFile",
    "MetsDiv",
    "Mets",
    "AssembledArticle",
    "parse_alto",
    "parse_mets",
    "parse_tei",
    "assemble_articles",
]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _iter_local(elem: ET.Element, name: str) -> Iterator[ET.Element]:
    for child in elem.iter():
        if _local(child.tag) == name:
            yield child


def _int(value: str | None, default: int = 0) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _float(value: str | None) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_xml(data: bytes | str) -> ET.Element:
    if isinstance(data, str):
        data = data.encode("utf-8")
    # dažos LNB failos ir BOM vai neatļauti kontrolsimboli
    data = data.lstrip(b"\xef\xbb\xbf")
    data = re.sub(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", b" ", data)
    return ET.fromstring(data)


# --------------------------------------------------------------------- ALTO
@dataclass
class AltoString:
    content: str
    hpos: int = 0
    vpos: int = 0
    width: int = 0
    height: int = 0
    confidence: float | None = None
    style: str = ""


@dataclass
class AltoLine:
    id: str = ""
    strings: list[AltoString] = field(default_factory=list)
    hpos: int = 0
    vpos: int = 0

    @property
    def text(self) -> str:
        return " ".join(s.content for s in self.strings if s.content).strip()

    @property
    def confidence(self) -> float | None:
        vals = [s.confidence for s in self.strings if s.confidence is not None]
        return sum(vals) / len(vals) if vals else None


@dataclass
class AltoBlock:
    id: str = ""
    lines: list[AltoLine] = field(default_factory=list)
    hpos: int = 0
    vpos: int = 0
    width: int = 0
    height: int = 0
    type: str = ""
    parent_id: str = ""

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines if line.text)

    @property
    def confidence(self) -> float | None:
        vals = [l.confidence for l in self.lines if l.confidence is not None]
        return sum(vals) / len(vals) if vals else None


@dataclass
class AltoPage:
    id: str = ""
    number: int = 0
    width: int = 0
    height: int = 0
    blocks: list[AltoBlock] = field(default_factory=list)
    language: str = ""
    source_url: str = ""

    def block(self, block_id: str) -> AltoBlock | None:
        for b in self.blocks:
            if b.id == block_id:
                return b
        return None

    def blocks_between(self, begin: str, end: str) -> list[AltoBlock]:
        """Bloki no ``begin`` līdz ``end`` (ieskaitot), dokumenta secībā."""
        ids = [b.id for b in self.blocks]
        try:
            i = ids.index(begin)
        except ValueError:
            return []
        if not end or end == begin:
            return [self.blocks[i]]
        try:
            j = ids.index(end)
        except ValueError:
            return [self.blocks[i]]
        if j < i:
            i, j = j, i
        return self.blocks[i : j + 1]

    def text(self, reading_order: str = "document", columns: int = 0) -> str:
        blocks = self.blocks
        if reading_order == "columns":
            blocks = order_blocks_by_columns(blocks, self.width, columns)
        return "\n\n".join(b.text for b in blocks if b.text.strip())

    @property
    def confidence(self) -> float | None:
        vals = [b.confidence for b in self.blocks if b.confidence is not None]
        return sum(vals) / len(vals) if vals else None


def parse_alto(data: bytes | str, source_url: str = "") -> AltoPage:
    """Parsē vienu ALTO lappusi, saliekot pārnestos vārdus (SUBS_TYPE) atpakaļ."""
    root = _parse_xml(data)
    page_el = next(_iter_local(root, "Page"), None)
    page = AltoPage(source_url=source_url)
    if page_el is not None:
        page.id = page_el.get("ID", "")
        page.number = _int(page_el.get("PHYSICAL_IMG_NR"), 0)
        page.width = _int(page_el.get("WIDTH"))
        page.height = _int(page_el.get("HEIGHT"))
    for desc in _iter_local(root, "Language"):
        if desc.text:
            page.language = desc.text.strip()
            break

    scope = page_el if page_el is not None else root
    for block_el in _iter_local(scope, "TextBlock"):
        block = AltoBlock(
            id=block_el.get("ID", ""),
            hpos=_int(block_el.get("HPOS")),
            vpos=_int(block_el.get("VPOS")),
            width=_int(block_el.get("WIDTH")),
            height=_int(block_el.get("HEIGHT")),
            type=block_el.get("TYPE", "") or block_el.get("STYLEREFS", ""),
        )
        for line_el in _iter_local(block_el, "TextLine"):
            line = AltoLine(
                id=line_el.get("ID", ""),
                hpos=_int(line_el.get("HPOS")),
                vpos=_int(line_el.get("VPOS")),
            )
            skip_next_hyphen_part = False
            for child in line_el:
                name = _local(child.tag)
                if name != "String":
                    continue
                subs_type = (child.get("SUBS_TYPE") or "").lower()
                if subs_type == "hyppart2":
                    # otrā pārnesuma daļa jau iekļauta SUBS_CONTENT
                    if skip_next_hyphen_part:
                        skip_next_hyphen_part = False
                        continue
                content = child.get("CONTENT", "")
                if subs_type == "hyppart1":
                    content = child.get("SUBS_CONTENT", content)
                    skip_next_hyphen_part = True
                if not content:
                    continue
                line.strings.append(
                    AltoString(
                        content=content,
                        hpos=_int(child.get("HPOS")),
                        vpos=_int(child.get("VPOS")),
                        width=_int(child.get("WIDTH")),
                        height=_int(child.get("HEIGHT")),
                        confidence=_float(child.get("WC")),
                        style=child.get("STYLEREFS", ""),
                    )
                )
            if line.strings:
                block.lines.append(line)
        if block.lines:
            page.blocks.append(block)

    # ComposedBlock piederība (raksta grupēšanai noder vecāka ID)
    for composed in _iter_local(scope, "ComposedBlock"):
        cid = composed.get("ID", "")
        for child in _iter_local(composed, "TextBlock"):
            blk = page.block(child.get("ID", ""))
            if blk is not None and not blk.parent_id:
                blk.parent_id = cid
    return page


def order_blocks_by_columns(
    blocks: list[AltoBlock], page_width: int, columns: int = 0
) -> list[AltoBlock]:
    """Sakārto blokus kolonnu lasīšanas secībā (avīzēm ar 4–8 slejām)."""
    if not blocks:
        return []
    width = page_width or max((b.hpos + b.width) for b in blocks) or 1
    if columns <= 0:
        # slejas novērtējam pēc tipiskā bloka platuma
        widths = sorted(b.width for b in blocks if b.width > 0)
        typical = widths[len(widths) // 2] if widths else width
        columns = max(1, min(12, round(width / typical) if typical else 1))
    col_width = width / columns
    return sorted(
        blocks,
        key=lambda b: (int(b.hpos // col_width) if col_width else 0, b.vpos, b.hpos),
    )


# --------------------------------------------------------------------- METS
@dataclass
class MetsFile:
    id: str
    href: str
    use: str = ""
    mimetype: str = ""


@dataclass
class MetsArea:
    file_id: str
    begin: str = ""
    end: str = ""
    betype: str = ""


@dataclass
class MetsDiv:
    id: str = ""
    type: str = ""
    label: str = ""
    order: int = 0
    dmdid: str = ""
    areas: list[MetsArea] = field(default_factory=list)
    children: list["MetsDiv"] = field(default_factory=list)

    def walk(self) -> Iterator["MetsDiv"]:
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass
class Mets:
    files: dict[str, MetsFile] = field(default_factory=dict)
    logical: MetsDiv | None = None
    physical: MetsDiv | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    source_url: str = ""

    def files_by_use(self, use: str) -> list[MetsFile]:
        u = use.upper()
        return [f for f in self.files.values() if f.use.upper() == u]

    def alto_files(self) -> list[MetsFile]:
        alto = self.files_by_use("ALTO")
        if alto:
            return alto
        return [
            f
            for f in self.files.values()
            if f.href.lower().endswith(".xml") and "alto" in f.href.lower()
        ]

    def page_order(self) -> dict[str, int]:
        """ALTO faila ID -> lappuses kārtas numurs (no fiziskās struktūras)."""
        out: dict[str, int] = {}
        if not self.physical:
            return out
        for i, div in enumerate(self.physical.walk(), start=1):
            if div.type.lower() not in ("page", "physical_page", ""):
                continue
            for area in div.areas:
                out.setdefault(area.file_id, div.order or i)
        return out


_ARTICLE_TYPES = {
    "article", "raksts", "text", "advertisement", "sludinajums", "issue_article",
    "heading", "title_section", "chapter", "section", "obituary", "illustration",
}


def parse_mets(data: bytes | str, source_url: str = "") -> Mets:
    root = _parse_xml(data)
    mets = Mets(source_url=source_url)

    for grp in _iter_local(root, "fileGrp"):
        use = grp.get("USE", "")
        for file_el in grp:
            if _local(file_el.tag) != "file":
                continue
            href = ""
            for loc in file_el:
                if _local(loc.tag) == "FLocat":
                    href = next(
                        (v for k, v in loc.attrib.items() if _local(k) == "href"), ""
                    )
                    break
            fid = file_el.get("ID", "")
            if fid:
                mets.files[fid] = MetsFile(
                    id=fid, href=href, use=use, mimetype=file_el.get("MIMETYPE", "")
                )

    def build_div(el: ET.Element) -> MetsDiv:
        div = MetsDiv(
            id=el.get("ID", ""),
            type=el.get("TYPE", "") or "",
            label=el.get("LABEL", "") or "",
            order=_int(el.get("ORDER") or el.get("ORDERLABEL")),
            dmdid=el.get("DMDID", "") or "",
        )
        for child in el:
            name = _local(child.tag)
            if name == "div":
                div.children.append(build_div(child))
            elif name == "fptr":
                fid = child.get("FILEID", "")
                if fid:
                    div.areas.append(MetsArea(file_id=fid))
                for sub in child.iter():
                    if _local(sub.tag) == "area":
                        div.areas.append(
                            MetsArea(
                                file_id=sub.get("FILEID", ""),
                                begin=sub.get("BEGIN", ""),
                                end=sub.get("END", ""),
                                betype=sub.get("BETYPE", ""),
                            )
                        )
        return div

    for smap in _iter_local(root, "structMap"):
        kind = (smap.get("TYPE") or "").upper()
        top = next((c for c in smap if _local(c.tag) == "div"), None)
        if top is None:
            continue
        built = build_div(top)
        if kind == "LOGICAL":
            mets.logical = built
        elif kind == "PHYSICAL":
            mets.physical = built
        elif mets.logical is None:
            mets.logical = built

    mets.metadata = _extract_mods(root)
    return mets


_MODS_FIELDS = {
    "title": ("title",),
    "publication": ("relatedItem", "seriesTitle"),
    "date": ("dateIssued", "dateCreated", "date"),
    "language": ("languageTerm",),
    "publisher": ("publisher",),
    "identifier": ("identifier", "recordIdentifier"),
    "place": ("placeTerm",),
}


def _extract_mods(root: ET.Element) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, tags in _MODS_FIELDS.items():
        for tag in tags:
            for el in _iter_local(root, tag):
                text = (el.text or "").strip()
                if text:
                    values.setdefault(key, text)
                    break
            if key in values:
                break
    # Dublin Core rezerve
    for el in root.iter():
        name = _local(el.tag)
        if name in ("title", "date", "creator", "publisher", "language", "source"):
            text = (el.text or "").strip()
            if text:
                values.setdefault(name, text)
    return values


@dataclass
class AssembledArticle:
    id: str
    title: str
    type: str
    text: str
    page_numbers: list[int] = field(default_factory=list)
    block_ids: list[str] = field(default_factory=list)
    confidence: float | None = None
    metadata: dict[str, str] = field(default_factory=dict)


def assemble_articles(
    mets: Mets,
    pages: dict[str, AltoPage],
    *,
    include_types: Iterable[str] | None = None,
    fallback_to_pages: bool = True,
) -> list[AssembledArticle]:
    """Saliek rakstus no METS loģiskās struktūras un ALTO blokiem.

    ``pages`` atslēga ir METS faila ID (piem. ``ALTO_0001``); ja METS nav vai
    tajā nav rakstu dalījuma, katra lappuse kļūst par vienu "rakstu".
    """
    wanted = {t.lower() for t in include_types} if include_types else None
    order = mets.page_order()
    articles: list[AssembledArticle] = []

    if mets.logical is not None:
        for div in mets.logical.walk():
            div_type = (div.type or "").lower()
            if not div.areas:
                continue
            if wanted is not None and div_type not in wanted:
                continue
            if wanted is None and div_type and div_type in ("newspaper", "issue", "volume"):
                continue
            chunks: list[str] = []
            block_ids: list[str] = []
            page_numbers: list[int] = []
            confidences: list[float] = []
            for area in div.areas:
                page = pages.get(area.file_id)
                if page is None:
                    continue
                if area.begin:
                    blocks = page.blocks_between(area.begin, area.end or area.begin)
                else:
                    blocks = page.blocks
                for block in blocks:
                    if block.text.strip():
                        chunks.append(block.text)
                        block_ids.append(block.id)
                        if block.confidence is not None:
                            confidences.append(block.confidence)
                num = page.number or order.get(area.file_id, 0)
                if num and num not in page_numbers:
                    page_numbers.append(num)
            if not chunks:
                continue
            articles.append(
                AssembledArticle(
                    id=div.id or f"div{len(articles) + 1}",
                    title=div.label.strip(),
                    type=div.type or "ARTICLE",
                    text="\n\n".join(chunks),
                    page_numbers=sorted(page_numbers),
                    block_ids=block_ids,
                    confidence=(sum(confidences) / len(confidences)) if confidences else None,
                    metadata=dict(mets.metadata),
                )
            )

    if not articles and fallback_to_pages:
        for fid, page in pages.items():
            text = page.text(reading_order="columns")
            if not text.strip():
                continue
            articles.append(
                AssembledArticle(
                    id=f"{fid}",
                    title="",
                    type="PAGE",
                    text=text,
                    page_numbers=[page.number or order.get(fid, 0)],
                    block_ids=[b.id for b in page.blocks],
                    confidence=page.confidence,
                    metadata=dict(mets.metadata),
                )
            )
    return articles


# ---------------------------------------------------------------------- TEI
def parse_tei(data: bytes | str, source_url: str = "") -> list[AssembledArticle]:
    """Izvelk rakstus no TEI faila (LNB lieto ``<div xml:id="DIVL...">``)."""
    root = _parse_xml(data)
    out: list[AssembledArticle] = []
    meta: dict[str, str] = {}
    for el in _iter_local(root, "titleStmt"):
        for t in _iter_local(el, "title"):
            if t.text:
                meta["title"] = t.text.strip()
                break
    for el in _iter_local(root, "date"):
        if el.text:
            meta.setdefault("date", el.text.strip())
            break

    def div_text(div: ET.Element) -> tuple[str, str]:
        head = ""
        parts: list[str] = []
        for el in div.iter():
            name = _local(el.tag)
            if name == "head" and not head:
                head = "".join(el.itertext()).strip()
            elif name in ("p", "l", "ab", "lg", "item"):
                text = " ".join("".join(el.itertext()).split())
                if text:
                    parts.append(text)
        return head, "\n\n".join(parts)

    for div in _iter_local(root, "div"):
        div_id = next((v for k, v in div.attrib.items() if _local(k) == "id"), "")
        if any(_local(c.tag) == "div" for c in div):
            continue  # tikai lapas (bez apakšnodalījumiem)
        head, text = div_text(div)
        if not text.strip():
            continue
        out.append(
            AssembledArticle(
                id=div_id or f"tei{len(out) + 1}",
                title=head,
                type=div.get("type", "ARTICLE"),
                text=text,
                metadata=dict(meta),
            )
        )
    if not out:
        body = next(_iter_local(root, "body"), None)
        if body is not None:
            head, text = div_text(body)
            if text.strip():
                out.append(
                    AssembledArticle(id="tei1", title=head, type="TEXT", text=text, metadata=meta)
                )
    return out
