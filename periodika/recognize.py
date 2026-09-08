"""Atpazīšana: druka (OCR) un rokraksts (HTR).

Neviens modelis nav iepakots šajā repozitorijā — te ir *adapteri*, un katrs no
tiem pasaka, kas tam vajadzīgs. Praksē latviešu materiālam der šādi ceļi:

===================  =========================================================
``agent``            Noklusējums, neko nevajag uzstādīt. Attēls tiek sagatavots
                     (izlīdzināts, binarizēts, sagriezts rindās) un aģentam tiek
                     atdota lasīšanas pakete: ceļi + uzvedne. Aģents pats ir
                     redzes modelis un nolasa attēlu ar saviem rīkiem. Šobrīd
                     labākais pieejamais ceļš latviešu **rokrakstam**.
``claude``           Tas pats, bet caur Anthropic API (vajag ``anthropic`` un
                     akreditāciju). Der pakešapstrādei bez aģenta klātbūtnes.
``tesseract``        Laba **druka**, arī Fraktur (``frk``/``deu_frak``) un
                     latviešu (``lav``). Rokrakstam neder — tas nav HTR dzinējs.
``command``          Jebkurš HTR dzinējs ar komandrindu (Kraken, Loghi,
                     Calamari, eScriptorium eksports). Komandu norāda konfigurācijā,
                     izvadi (PAGE XML / ALTO / hOCR / teksts) parsē šis modulis.
===================  =========================================================

Visos gadījumos rezultāts iet caur to pašu pēcapstrādi: ``correct`` (Fraktur/
Kurrent kļūdu labošana pēc vārdnīcas) un ``latvian.analyze_article``
(ortogrāfija, valoda, datums, vietvārdi).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import images as images_mod
from .alto import AltoPage
from .correct import correct_text
from .latvian import analyze_article
from .pagexml import is_hocr, is_page_xml, parse_hocr, parse_page_xml

__all__ = [
    "RecognitionResult",
    "RecognitionError",
    "recognize",
    "available_engines",
    "TRANSCRIPTION_PROMPT",
    "prepare_for_agent",
]


class RecognitionError(RuntimeError):
    pass


#: Uzvedne redzes modelim. Rakstīta latviešu vecās drukas materiālam un
#: apzināti pieprasa *diplomātisku* transkripciju: nekādas modernizācijas
#: nolasīšanas laikā — pārrakstīšana mūsdienu rakstībā ir atsevišķs solis,
#: ko dara ``orthography``/``latvian`` moduļi, un oriģinālam jāpaliek pieejamam.
TRANSCRIPTION_PROMPT = """Tu lasi latviešu periodikas vai rokraksta skenējumu no 18.–20. gadsimta.

Pārraksti tekstu tieši tā, kā tas ir attēlā — diplomātiski, bez modernizācijas:

1. Saglabā veco ortogrāfiju tādu, kāda tā ir: `w` (nevis `v`), `ee` (nevis `ie`),
   `sch` (nevis `š`), `ah/eh/ih/uh` garumzīmju vietā, garo `ſ` raksti kā `ſ`.
   NEPĀRRAKSTI mūsdienu rakstībā — to izdara cits solis.
2. Saglabā rindu dalījumu. Vārda pārnesumu rindas galā atstāj ar defisi.
3. Saglabā oriģinālo pieturzīmju un lielo burtu lietojumu (vecajos tekstos
   lietvārdi bieži rakstīti ar lielo burtu — atstāj tā).
4. Ja burts vai vārds nav skaidri salasāms, raksti to iekavās ar jautājumzīmi:
   `[vārds?]`. Ja pilnīgi nesalasāms — `[…]`. Nekad neizdomā tekstu.
5. Slejas lasi pa vienai, no kreisās uz labo; katras slejas sākumā raksti
   `--- sleja N ---`.
6. Ja teksts nav latviešu valodā (vācu, krievu), pārraksti to tāpat un pirmajā
   rindā norādi `[valoda: vācu]`.
7. Atbildē iekļauj TIKAI transkripciju — bez ievada, komentāriem vai tulkojuma.

Rokraksts 19. gadsimtā gandrīz vienmēr ir vācu Kurrent kursīvs: `e` bieži izskatās
kā divi sīki vilcieni, `n` un `u` atšķiras tikai ar lociņu virs `u`, `h` un `b`
mēdz sajaukties. Ja neesi drošs, liec `[?]`, nevis mini."""


@dataclass
class RecognitionResult:
    text: str = ""
    engine: str = ""
    lines: list[str] = field(default_factory=list)
    confidence: float | None = None
    page: AltoPage | None = None
    handwriting: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self, *, include_text: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "dzinējs": self.engine,
            "rokraksts": self.handwriting,
            "rindu_skaits": len(self.lines),
        }
        if self.confidence is not None:
            out["ticamība"] = round(self.confidence, 3)
        if include_text:
            out["teksts"] = self.text
        if self.meta:
            out["papildus"] = self.meta
        return out


# ---------------------------------------------------------------- palīgfunkcijas
def _prepared_image(path: Path, workdir: Path, preprocess: bool) -> tuple[Path, dict[str, Any]]:
    if not preprocess:
        return path, {}
    try:
        result = images_mod.preprocess(path, workdir / (path.stem + ".prep.png"))
        return result.path, {"priekšapstrāde": result.to_json()}
    except images_mod.ImagingUnavailable as exc:
        return path, {"priekšapstrāde": f"izlaista: {exc}"}
    except Exception as exc:  # noqa: BLE001 - bojāts attēls nedrīkst apturēt visu
        return path, {"priekšapstrāde": f"neizdevās: {exc}"}


def _parse_engine_output(data: bytes, source: str) -> tuple[str, AltoPage | None]:
    """Atpazīst dzinēja izvades formātu un izvelk tekstu."""
    head = data[:2048].lstrip()
    if head[:1] == b"<":
        if is_page_xml(data):
            page = parse_page_xml(data, source)
            return page.text(), page
        if b"<alto" in head.lower():
            from .alto import parse_alto  # lokāls imports, lai izvairītos no cikla

            page = parse_alto(data, source)
            return page.text(reading_order="columns"), page
        if is_hocr(data):
            page = parse_hocr(data, source)
            return page.text(), page
    return data.decode("utf-8", errors="replace"), None


# ------------------------------------------------------------------- dzinēji
def prepare_for_agent(
    image_path: str | Path,
    *,
    workdir: str | Path | None = None,
    preprocess: bool = True,
    segment: bool = True,
    handwriting: bool = False,
) -> dict[str, Any]:
    """Sagatavo attēlu aģenta paša redzei un atgriež lasīšanas paketi.

    Aģents (Claude) pats ir redzes modelis, tāpēc vienkāršākais ceļš ir: šeit
    sagatavot attēlu un rindu izgriezumus, bet nolasīt tos ar aģenta ``Read``
    rīku. Atgriež ceļus, uzvedni un norādes, ko darīt ar rezultātu.
    """
    src = Path(image_path)
    if not src.exists():
        raise RecognitionError(f"attēls nav atrasts: {src}")
    work = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="periodika-"))
    work.mkdir(parents=True, exist_ok=True)

    prepared, meta = _prepared_image(src, work, preprocess)
    line_files: list[str] = []
    if segment:
        try:
            line_files = [str(p) for p in images_mod.segment_lines(prepared, work / "lines")]
        except images_mod.ImagingUnavailable:
            meta["rindu_sagriešana"] = "izlaista (nav Pillow)"
        except Exception as exc:  # noqa: BLE001
            meta["rindu_sagriešana"] = f"neizdevās: {exc}"

    return {
        "uzdevums": "nolasi_attēlu",
        "sagatavotais_attēls": str(prepared),
        "oriģināls": str(src),
        "rindu_izgriezumi": line_files,
        "uzvedne": TRANSCRIPTION_PROMPT,
        "rokraksts": handwriting,
        "norādes": [
            "Atver sagatavoto attēlu ar Read rīku (rokrakstam — pa vienam rindu izgriezumam).",
            "Pārraksti tekstu pēc uzvednes: diplomātiski, bez modernizācijas.",
            "Rezultātu padod atpakaļ rīkam periodika_correct_text "
            f"ar handwriting={'true' if handwriting else 'false'}.",
            "Tad periodika_normalize_text dos mūsdienu rakstību, valodu, datumu un vietvārdus.",
        ],
        **meta,
    }


def _recognize_with_claude(
    image_path: Path,
    *,
    handwriting: bool,
    model: str,
    effort: str,
    max_tokens: int,
    extra_prompt: str,
) -> RecognitionResult:
    try:
        import anthropic  # noqa: PLC0415
    except ImportError as exc:
        raise RecognitionError(
            "Nav uzstādīts `anthropic` pakotne. Uzstādi ar "
            "`pip install 'periodika-agent[claude]'`, vai lieto dzinēju "
            "`agent`, kam nekas nav vajadzīgs."
        ) from exc

    data, media_type = images_mod.encode_base64(image_path)
    prompt = TRANSCRIPTION_PROMPT + (f"\n\n{extra_prompt}" if extra_prompt else "")
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": data},
                },
                {"type": "text", "text": prompt},
            ],
        }
    ]
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
        "messages": messages,
    }

    client = anthropic.Anthropic()
    try:
        # Servera puses atkāpšanās: ja modelis atsakās, to pašu pieprasījumu
        # izpilda rezerves modelis tajā pašā izsaukumā.
        response = client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params
        )
    except (anthropic.BadRequestError, TypeError, AttributeError):
        response = client.messages.create(**params)

    if getattr(response, "stop_reason", "") == "refusal":
        details = getattr(response, "stop_details", None)
        raise RecognitionError(
            "Modelis atteicās nolasīt šo attēlu"
            + (f" ({getattr(details, 'category', '')})" if details else "")
        )

    text = "\n".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    ).strip()
    usage = getattr(response, "usage", None)
    return RecognitionResult(
        text=text,
        engine=f"claude:{model}",
        lines=[l for l in text.splitlines() if l.strip()],
        handwriting=handwriting,
        meta={
            "modelis": getattr(response, "model", model),
            "ievades_marķieri": getattr(usage, "input_tokens", None),
            "izvades_marķieri": getattr(usage, "output_tokens", None),
        },
    )


def _recognize_with_tesseract(
    image_path: Path, *, langs: str, psm: int, workdir: Path
) -> RecognitionResult:
    binary = shutil.which("tesseract")
    if not binary:
        raise RecognitionError(
            "tesseract nav atrasts. Debian/Ubuntu: "
            "`apt install tesseract-ocr tesseract-ocr-lav` (Fraktur drukai vēl "
            "`tesseract-ocr-frk`); macOS: `brew install tesseract tesseract-lang`."
        )
    installed = _tesseract_languages(binary)
    requested = [l for l in langs.split("+") if l]
    missing = [l for l in requested if installed and l not in installed]
    if missing and installed:
        usable = [l for l in requested if l in installed] or ["eng"]
        langs = "+".join(usable)

    out_base = workdir / "tess"
    cmd = [binary, str(image_path), str(out_base), "-l", langs, "--psm", str(psm), "hocr"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    hocr = out_base.with_suffix(".hocr")
    if proc.returncode != 0 or not hocr.exists():
        raise RecognitionError(f"tesseract kļūda: {proc.stderr.strip()[:400]}")
    page = parse_hocr(hocr.read_bytes(), str(image_path))
    text = page.text()
    return RecognitionResult(
        text=text,
        engine=f"tesseract:{langs}",
        lines=[l for l in text.splitlines() if l.strip()],
        confidence=page.confidence,
        page=page,
        meta={"trūkstošās_valodas": missing} if missing else {},
    )


def _tesseract_languages(binary: str) -> set[str]:
    try:
        proc = subprocess.run([binary, "--list-langs"], capture_output=True, text=True)
        return {l.strip() for l in proc.stdout.splitlines()[1:] if l.strip()}
    except Exception:  # noqa: BLE001
        return set()


def _recognize_with_command(
    image_path: Path, *, command: str, workdir: Path, handwriting: bool
) -> RecognitionResult:
    """Jebkurš ārējs HTR/OCR dzinējs; ``{image}`` un ``{out}`` veidnē."""
    out_path = workdir / "out.xml"
    rendered = command.format(image=str(image_path), out=str(out_path), workdir=str(workdir))
    proc = subprocess.run(rendered, shell=True, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RecognitionError(f"komanda neizdevās ({proc.returncode}): {proc.stderr[:400]}")
    if out_path.exists():
        text, page = _parse_engine_output(out_path.read_bytes(), str(image_path))
    else:
        produced = sorted(
            [p for p in workdir.iterdir() if p.is_file() and p != image_path],
            key=lambda p: p.stat().st_mtime,
        )
        if produced:
            text, page = _parse_engine_output(produced[-1].read_bytes(), str(image_path))
        else:
            text, page = proc.stdout, None
    return RecognitionResult(
        text=text.strip(),
        engine="command",
        lines=[l for l in text.splitlines() if l.strip()],
        confidence=page.confidence if page else None,
        page=page,
        handwriting=handwriting,
        meta={"komanda": rendered},
    )


# ------------------------------------------------------------------ saskarne
def available_engines() -> dict[str, dict[str, Any]]:
    """Kuri dzinēji šajā vidē tiešām ir lietojami."""
    caps = images_mod.capabilities()
    try:
        import anthropic  # noqa: PLC0415, F401

        has_sdk = True
    except ImportError:
        has_sdk = False
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    return {
        "agent": {
            "pieejams": True,
            "rokrakstam": True,
            "apraksts": "Sagatavo attēlu un rindas; nolasa pats aģents ar savu redzi.",
            "priekšapstrāde": caps["pillow"],
        },
        "claude": {
            "pieejams": has_sdk,
            "rokrakstam": True,
            "apraksts": "Anthropic API (vajag `anthropic` pakotni un akreditāciju).",
            "sdk": has_sdk,
            "akreditācija_vidē": has_key,
            "piezīme": "" if has_sdk else "pip install 'periodika-agent[claude]'",
        },
        "tesseract": {
            "pieejams": caps["tesseract"],
            "rokrakstam": False,
            "apraksts": "Druka, arī Fraktur (frk/deu_frak) un latviešu (lav).",
            "valodas": sorted(_tesseract_languages(shutil.which("tesseract") or "")) or [],
        },
        "command": {
            "pieejams": True,
            "rokrakstam": True,
            "apraksts": "Ārējs HTR dzinējs (Kraken, Loghi, Calamari, eScriptorium).",
            "izmantošana": "--command 'kraken -i {image} {out} segment ocr -m model.mlmodel'",
        },
    }


def recognize(
    image_path: str | Path,
    *,
    engine: str = "agent",
    handwriting: bool = False,
    langs: str = "",
    psm: int = 4,
    command: str = "",
    model: str = "claude-opus-5",
    effort: str = "high",
    max_tokens: int = 16000,
    extra_prompt: str = "",
    preprocess: bool = True,
    segment: bool = False,
    correct: bool = True,
    workdir: str | Path | None = None,
) -> dict[str, Any]:
    """Nolasa attēlu un izlaiž rezultātu caur latviešu pēcapstrādi.

    Atgriež vārdnīcu ar atpazīto tekstu, labojumiem un valodas analīzi. Dzinējam
    ``agent`` teksta vēl nav — tiek atgriezta lasīšanas pakete aģentam.
    """
    src = Path(image_path)
    if not src.exists():
        raise RecognitionError(f"attēls nav atrasts: {src}")
    work = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="periodika-"))
    work.mkdir(parents=True, exist_ok=True)

    if engine == "agent":
        return prepare_for_agent(
            src, workdir=work, preprocess=preprocess, segment=segment or handwriting,
            handwriting=handwriting,
        )

    prepared, meta = _prepared_image(src, work, preprocess)

    if engine == "claude":
        result = _recognize_with_claude(
            prepared, handwriting=handwriting, model=model, effort=effort,
            max_tokens=max_tokens, extra_prompt=extra_prompt,
        )
    elif engine == "tesseract":
        result = _recognize_with_tesseract(
            prepared, langs=langs or "lav+frk", psm=psm, workdir=work
        )
    elif engine == "command":
        if not command:
            raise RecognitionError("dzinējam `command` jānorāda --command veidne")
        result = _recognize_with_command(
            prepared, command=command, workdir=work, handwriting=handwriting
        )
    else:
        raise RecognitionError(
            f"nezināms dzinējs: {engine}. Pieejamie: {', '.join(available_engines())}"
        )

    result.handwriting = handwriting
    result.meta.update(meta)
    return postprocess(result, correct=correct)


def postprocess(result: RecognitionResult, *, correct: bool = True) -> dict[str, Any]:
    """Atpazīto tekstu izlaiž caur labošanu un latviešu analīzi."""
    payload: dict[str, Any] = {"atpazīšana": result.to_json(include_text=False)}
    text = result.text
    if correct and text:
        report = correct_text(text, handwriting=result.handwriting)
        payload["labojumi"] = [c.to_json() for c in report.corrections]
        payload["neatpazītie_vārdi"] = report.unresolved[:30]
        text = report.text
    payload["teksts"] = text
    if text:
        analysis = analyze_article(text)
        analysis.pop("date_details", None)
        payload["analīze"] = analysis
    return payload
