"""Attēlu sagatavošana atpazīšanai (neobligāta atkarība: Pillow).

Vecu avīžu un rokrakstu skenējumi reti ir gatavi atpazīšanai: tie ir šķībi,
nevienmērīgi apgaismoti un ar dzeltenu papīru. Šis modulis dara to minimumu,
kas patiešām uzlabo rezultātu — pelēktoņi, sašķiebuma izlīdzināšana, Otsu
binarizācija, mērogošana — un sagriež lappusi rindās, jo gan HTR modeļi, gan
redzes modeļi rokrakstu lasa daudz precīzāk pa vienai rindai.

Bāzes64 kodēšana un PDF sagriešana strādā bez Pillow, tāpēc Claude redzes ceļš
(``recognize.py``) ir izmantojams arī tukšā vidē.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

__all__ = [
    "ImagingUnavailable",
    "capabilities",
    "encode_base64",
    "media_type_for",
    "preprocess",
    "segment_lines",
    "pdf_to_images",
]


class ImagingUnavailable(RuntimeError):
    """Pillow nav pieejams; ziņojums pasaka, ko darīt."""

    def __init__(self, what: str = "attēlu apstrādei") -> None:
        super().__init__(
            f"Pillow nav instalēts, bet ir vajadzīgs {what}. "
            "Uzstādi ar `pip install 'periodika-agent[images]'` vai `pip install Pillow`. "
            "Bez Pillow strādā tikai neapstrādāta attēla nodošana atpazinējam."
        )


_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
    ".jp2": "image/jp2",
}


def media_type_for(path: str | Path) -> str:
    return _MEDIA_TYPES.get(Path(path).suffix.lower(), "image/png")


def _pillow():
    try:
        from PIL import Image  # noqa: PLC0415

        return Image
    except ImportError:
        return None


def capabilities() -> dict[str, Any]:
    """Ko šī vide tiešām spēj — aģentam derīgi zināt pirms plānošanas."""
    Image = _pillow()
    return {
        "pillow": bool(Image),
        "pillow_versija": getattr(Image, "__version__", "") if Image else "",
        "pdftoppm": bool(shutil.which("pdftoppm")),
        "tesseract": bool(shutil.which("tesseract")),
        "piezīme": (
            "Bez Pillow attēlu var nodot atpazinējam tāds, kāds tas ir — "
            "priekšapstrāde un rindu sagriešana nav pieejama."
        )
        if not Image
        else "",
    }


def encode_base64(path: str | Path) -> tuple[str, str]:
    """Atgriež (base64 virkne bez jaunrindām, media type) — Claude redzes ievadei."""
    data = Path(path).read_bytes()
    return base64.standard_b64encode(data).decode("ascii"), media_type_for(path)


# ------------------------------------------------------------ priekšapstrāde
def _row_profile(img) -> list[int]:
    """Katras rindas vidējais tumšums (0 = balts, 255 = melns) bez numpy."""
    width, height = img.size
    if width == 0 or height == 0:
        return []
    column = img.resize((1, height))
    return [255 - column.getpixel((0, y)) for y in range(height)]


def _otsu_threshold(histogram: Sequence[int]) -> int:
    total = sum(histogram)
    if not total:
        return 128
    sum_all = sum(i * h for i, h in enumerate(histogram))
    sum_b = 0.0
    weight_b = 0
    best_variance = -1.0
    threshold = 128
    for i, count in enumerate(histogram):
        weight_b += count
        if weight_b == 0:
            continue
        weight_f = total - weight_b
        if weight_f == 0:
            break
        sum_b += i * count
        mean_b = sum_b / weight_b
        mean_f = (sum_all - sum_b) / weight_f
        variance = weight_b * weight_f * (mean_b - mean_f) ** 2
        if variance > best_variance:
            best_variance = variance
            threshold = i
    return threshold


def _estimate_skew(img, limit: float = 3.0, step: float = 0.5) -> float:
    """Sašķiebums grādos: leņķis, kurā rindu profila dispersija ir vislielākā."""
    best_angle = 0.0
    best_score = -1.0
    angle = -limit
    while angle <= limit + 1e-9:
        rotated = img.rotate(angle, expand=False, fillcolor=255) if angle else img
        profile = _row_profile(rotated)
        if profile:
            mean = sum(profile) / len(profile)
            score = sum((v - mean) ** 2 for v in profile)
            if score > best_score:
                best_score = score
                best_angle = angle
        angle += step
    return best_angle


@dataclass
class PreprocessResult:
    path: Path
    width: int
    height: int
    skew_degrees: float
    threshold: int
    steps: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "fails": str(self.path),
            "izmērs": [self.width, self.height],
            "sašķiebums_grādos": round(self.skew_degrees, 2),
            "binarizācijas_slieksnis": self.threshold,
            "soļi": self.steps,
        }


def preprocess(
    path: str | Path,
    out_path: str | Path | None = None,
    *,
    grayscale: bool = True,
    deskew: bool = True,
    binarize: bool = True,
    min_height: int = 1600,
    invert: bool = False,
) -> PreprocessResult:
    """Sagatavo skenējumu atpazīšanai un saglabā rezultātu.

    ``min_height`` mērogo mazus skenējumus uz augšu — HTR modeļi un redzes
    modeļi uz sīka teksta kļūdās daudz biežāk nekā uz palielināta.
    """
    Image = _pillow()
    if Image is None:
        raise ImagingUnavailable("priekšapstrādei")
    src = Path(path)
    img = Image.open(src)
    steps: list[str] = []

    if img.mode not in ("L", "1") and grayscale:
        img = img.convert("L")
        steps.append("pelēktoņi")
    elif img.mode != "L":
        img = img.convert("L")

    if img.height < min_height:
        scale = min_height / max(1, img.height)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
        steps.append(f"mērogots x{scale:.1f}")

    skew = 0.0
    if deskew:
        skew = _estimate_skew(img)
        if abs(skew) >= 0.25:
            img = img.rotate(skew, expand=True, fillcolor=255, resample=Image.BICUBIC)
            steps.append(f"izlīdzināts {skew:+.1f}°")

    threshold = _otsu_threshold(img.histogram())
    if binarize:
        img = img.point(lambda v, t=threshold: 255 if v > t else 0, mode="L")
        steps.append(f"binarizēts (Otsu {threshold})")
    if invert:
        img = img.point(lambda v: 255 - v)
        steps.append("invertēts")

    target = Path(out_path) if out_path else src.with_name(src.stem + ".prep.png")
    target.parent.mkdir(parents=True, exist_ok=True)
    img.save(target)
    return PreprocessResult(
        path=target, width=img.width, height=img.height,
        skew_degrees=skew, threshold=threshold, steps=steps,
    )


def segment_lines(
    path: str | Path,
    out_dir: str | Path,
    *,
    min_line_height: int = 12,
    padding: int = 6,
    ink_threshold: float = 0.06,
    max_lines: int = 200,
) -> list[Path]:
    """Sagriež lappusi teksta rindās (horizontālais projekcijas profils).

    Rindu izgriezumi ir vienīgais praktiskais ceļš, kā rokrakstu nolasīt
    precīzi: vesela lappuse redzes modelim ir par blīvu, un HTR modeļi tāpat
    strādā ar rindām.
    """
    Image = _pillow()
    if Image is None:
        raise ImagingUnavailable("rindu sagriešanai")
    img = Image.open(path).convert("L")
    profile = _row_profile(img)
    if not profile:
        return []
    peak = max(profile) or 1
    threshold = peak * ink_threshold

    bands: list[tuple[int, int]] = []
    start: int | None = None
    for y, value in enumerate(profile):
        if value > threshold and start is None:
            start = y
        elif value <= threshold and start is not None:
            if y - start >= min_line_height:
                bands.append((start, y))
            start = None
    if start is not None and len(profile) - start >= min_line_height:
        bands.append((start, len(profile)))

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for i, (top, bottom) in enumerate(bands[:max_lines], start=1):
        box = (0, max(0, top - padding), img.width, min(img.height, bottom + padding))
        crop = img.crop(box)
        target = out / f"line_{i:03d}.png"
        crop.save(target)
        written.append(target)
    return written


def pdf_to_images(
    pdf_path: str | Path, out_dir: str | Path, *, dpi: int = 300, first: int = 1, last: int = 0
) -> list[Path]:
    """Sagriež PDF lappusēs ar ``pdftoppm`` (poppler-utils)."""
    if not shutil.which("pdftoppm"):
        raise RuntimeError(
            "pdftoppm nav atrasts. Uzstādi poppler-utils "
            "(Debian/Ubuntu: apt install poppler-utils; macOS: brew install poppler)."
        )
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    prefix = out / Path(pdf_path).stem
    cmd = ["pdftoppm", "-png", "-r", str(dpi), "-f", str(first)]
    if last:
        cmd += ["-l", str(last)]
    cmd += [str(pdf_path), str(prefix)]
    subprocess.run(cmd, check=True, capture_output=True)
    return sorted(out.glob(f"{Path(pdf_path).stem}*.png"))
