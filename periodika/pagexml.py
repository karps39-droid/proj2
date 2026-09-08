"""PAGE XML un hOCR parsēšana — rokraksta atpazīšanas (HTR) izvade.

Transkribus, eScriptorium, Loghi un Kraken izvada PAGE XML, nevis ALTO. Šis
modulis to pārvērš tajās pašās ``AltoPage`` / ``AssembledArticle`` struktūrās,
ko lieto pārējais cauruļvads, tāpēc rokraksta lappuse tālāk iet cauri tam pašam
ceļam kā drukāta: ortogrāfijas normalizācija, celmošana, indeksēšana.

PAGE XML uzbūve, kas mūs interesē::

    <PcGts><Page imageWidth imageHeight>
      <TextRegion id="r1" type="paragraph">
        <Coords points="..."/>
        <TextLine id="l1">
          <Baseline points="..."/>
          <TextEquiv conf="0.87"><Unicode>rindas teksts</Unicode></TextEquiv>
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from .alto import AltoBlock, AltoLine, AltoPage, AltoString, _iter_local, _local, _parse_xml

__all__ = ["parse_page_xml", "parse_hocr", "is_page_xml", "is_hocr"]

_POINTS_RE = re.compile(r"(-?\d+)\s*,\s*(-?\d+)")


def is_page_xml(data: bytes) -> bool:
    head = data[:2048].lower()
    return b"<pcgts" in head or b"pagecontent" in head


def is_hocr(data: bytes) -> bool:
    head = data[:4096].lower()
    return b"ocr_page" in head or b"ocr_line" in head


def _bbox(points: str) -> tuple[int, int, int, int]:
    """``Coords points="10,20 100,20 100,60 10,60"`` -> (hpos, vpos, width, height)."""
    coords = [(int(x), int(y)) for x, y in _POINTS_RE.findall(points or "")]
    if not coords:
        return (0, 0, 0, 0)
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def _text_equiv(elem: ET.Element) -> tuple[str, float | None]:
    """Tuvākais ``TextEquiv/Unicode`` ar ticamību (dziļākais uzvar)."""
    for child in elem:
        if _local(child.tag) != "TextEquiv":
            continue
        conf = child.get("conf")
        for sub in child:
            if _local(sub.tag) == "Unicode":
                try:
                    confidence = float(conf) if conf is not None else None
                except ValueError:
                    confidence = None
                return (sub.text or "").strip(), confidence
    return "", None


def parse_page_xml(data: bytes | str, source_url: str = "") -> AltoPage:
    """Parsē PAGE XML lappusi. Katrs ``TextRegion`` kļūst par teksta bloku."""
    root = _parse_xml(data)
    page_el = next(_iter_local(root, "Page"), None)
    page = AltoPage(source_url=source_url)
    if page_el is not None:
        page.id = page_el.get("id", "") or page_el.get("imageFilename", "")
        try:
            page.width = int(page_el.get("imageWidth") or 0)
            page.height = int(page_el.get("imageHeight") or 0)
        except ValueError:
            pass
        m = re.search(r"(\d+)", page_el.get("imageFilename", "") or "")
        if m:
            page.number = int(m.group(1))

    scope = page_el if page_el is not None else root
    for region in _iter_local(scope, "TextRegion"):
        coords = next((c for c in region if _local(c.tag) == "Coords"), None)
        hpos, vpos, width, height = _bbox(coords.get("points", "") if coords is not None else "")
        block = AltoBlock(
            id=region.get("id", ""),
            hpos=hpos, vpos=vpos, width=width, height=height,
            type=region.get("type", "") or "paragraph",
        )
        for line_el in _iter_local(region, "TextLine"):
            text, conf = _text_equiv(line_el)
            if not text:
                continue
            line_coords = next((c for c in line_el if _local(c.tag) == "Coords"), None)
            lx, ly, _, _ = _bbox(line_coords.get("points", "") if line_coords is not None else "")
            line = AltoLine(id=line_el.get("id", ""), hpos=lx, vpos=ly)
            # PAGE glabā rindu kā vienu virkni; sadalām vārdos, lai struktūra
            # sakristu ar ALTO un ticamība saglabātos katram vārdam.
            for token in text.split():
                line.strings.append(AltoString(content=token, confidence=conf))
            if line.strings:
                block.lines.append(line)
        if block.lines:
            page.blocks.append(block)

    if not page.blocks:
        # daži rīki liek TextLine tieši zem Page, bez TextRegion
        block = AltoBlock(id="page")
        for line_el in _iter_local(scope, "TextLine"):
            text, conf = _text_equiv(line_el)
            if not text:
                continue
            line = AltoLine(id=line_el.get("id", ""))
            for token in text.split():
                line.strings.append(AltoString(content=token, confidence=conf))
            block.lines.append(line)
        if block.lines:
            page.blocks.append(block)
    return page


_HOCR_BBOX = re.compile(r"bbox (\d+) (\d+) (\d+) (\d+)")
_HOCR_CONF = re.compile(r"x_wconf (\d+)")


def parse_hocr(data: bytes | str, source_url: str = "") -> AltoPage:
    """Parsē hOCR (Tesseract noklusējuma HTML izvade)."""
    if isinstance(data, bytes):
        markup = data.decode("utf-8", errors="replace")
    else:
        markup = data
    # hOCR ir XHTML; ja tas nav labi formēts, atkāpjamies uz regex ceļu
    try:
        root = _parse_xml(re.sub(r"<\?xml[^>]*\?>", "", markup).strip())
    except ET.ParseError:
        return _parse_hocr_regex(markup, source_url)

    page = AltoPage(source_url=source_url)
    for el in root.iter():
        cls = el.get("class", "")
        title = el.get("title", "")
        if cls == "ocr_page":
            box = _HOCR_BBOX.search(title)
            if box:
                page.width = int(box.group(3))
                page.height = int(box.group(4))
    blocks: dict[str, AltoBlock] = {}
    for el in root.iter():
        if el.get("class") not in ("ocr_line", "ocr_textline", "ocr_caption"):
            continue
        parent_id = el.get("id", "") or f"line{len(blocks)}"
        box = _HOCR_BBOX.search(el.get("title", ""))
        line = AltoLine(
            id=el.get("id", ""),
            hpos=int(box.group(1)) if box else 0,
            vpos=int(box.group(2)) if box else 0,
        )
        for word in el.iter():
            if word.get("class") not in ("ocrx_word", "ocr_word"):
                continue
            content = "".join(word.itertext()).strip()
            if not content:
                continue
            conf = _HOCR_CONF.search(word.get("title", ""))
            line.strings.append(
                AltoString(
                    content=content,
                    confidence=float(conf.group(1)) / 100 if conf else None,
                )
            )
        if not line.strings:
            text = "".join(el.itertext()).strip()
            line.strings = [AltoString(content=t) for t in text.split()]
        if not line.strings:
            continue
        block_id = _hocr_block_id(el) or parent_id
        block = blocks.setdefault(
            block_id, AltoBlock(id=block_id, hpos=line.hpos, vpos=line.vpos)
        )
        block.lines.append(line)
    page.blocks = list(blocks.values())
    return page


def _hocr_block_id(el: ET.Element) -> str:
    return el.get("data-block") or ""


#: Rindu izgriešana no bojāta hOCR: aizverošais tags var arī trūkt, tāpēc
#: rindu beidzam pie </span>, pie nākamās rindas sākuma vai faila beigām.
_HOCR_LINE_RE = re.compile(
    r"<span[^>]*class=['\"]ocr[x_]*line['\"][^>]*>"
    r"(.*?)(?=</span>|<span[^>]*class=['\"]ocr[x_]*line|\Z)",
    re.S | re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _parse_hocr_regex(markup: str, source_url: str) -> AltoPage:
    page = AltoPage(source_url=source_url)
    block = AltoBlock(id="hocr")
    for i, raw in enumerate(_HOCR_LINE_RE.findall(markup)):
        text = _TAG_RE.sub(" ", raw)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        line = AltoLine(id=f"l{i + 1}")
        line.strings = [AltoString(content=t) for t in text.split()]
        block.lines.append(line)
    if block.lines:
        page.blocks.append(block)
    return page
