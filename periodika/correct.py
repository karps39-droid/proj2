"""Atpazīšanas kļūdu labošana: Fraktur druka un Kurrent rokraksts.

Gan Fraktur druka, gan 19. gs. latviešu rokraksts (kas gandrīz vienmēr ir vācu
Kurrent kursīvs) rada *sistemātiskas*, nevis nejaušas atpazīšanas kļūdas: garais
``ſ`` kļūst par ``f``, ``n`` par ``u``, ``e`` par ``n``, Frakturā ``I`` un ``J``
ir gandrīz neatšķirami. Tas ļauj labot mērķtiecīgi: nevis "jebkurš vārds ar
attālumu 1", bet tikai tās aizstāšanas, kuras attiecīgais raksta veids tiešām
mēdz radīt — un tikai tad, ja rezultāts ir latviešu vārdnīcā atpazīstams vārds.

Katra izmaiņa tiek pierakstīta, tāpēc labojumus var pārskatīt vai atmest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .latvian import Lexicon, default_lexicon
from .orthography import clean_ocr

__all__ = [
    "CONFUSIONS",
    "Correction",
    "CorrectionReport",
    "correct_text",
    "correct_word",
    "suspicious_words",
    "candidates_for",
]

#: Simbolu pāri, ko attiecīgais raksta veids mēdz sajaukt (abos virzienos).
CONFUSIONS: dict[str, tuple[tuple[str, str], ...]] = {
    # Kopīgi abiem: garais s un tā ligatūras.
    "common": (
        ("f", "s"), ("ss", "ß"), ("ii", "n"), ("rn", "m"), ("cl", "d"),
        ("l", "i"), ("1", "l"), ("0", "o"), ("5", "s"), ("8", "s"),
    ),
    # Fraktur druka: I/J ir gandrīz identiski, n/u un e/c bieži jaucas.
    "fraktur": (
        ("I", "J"), ("n", "u"), ("e", "c"), ("r", "x"), ("k", "t"),
        ("b", "h"), ("v", "o"), ("tz", "ß"), ("ch", "ck"), ("m", "in"),
    ),
    # Kurrent rokraksts: e/n, u/n, h/b, a/o, z/y — tipiskākās kļūdas.
    "kurrent": (
        ("e", "n"), ("n", "u"), ("m", "n"), ("h", "b"), ("h", "f"),
        ("a", "o"), ("z", "y"), ("c", "e"), ("i", "e"), ("v", "r"),
        ("t", "k"), ("ll", "st"), ("w", "m"),
    ),
}

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_MIN_LENGTH = 3


def _pairs_for(styles: Sequence[str]) -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    for style in styles:
        out.extend(CONFUSIONS.get(style, ()))
    return tuple(dict.fromkeys(out))


def candidates_for(
    word: str, styles: Sequence[str] = ("common", "fraktur"), max_edits: int = 1
) -> list[str]:
    """Visi varianti, kas rodas, atgriežot ``max_edits`` tipiskās sajaukšanas."""
    pairs = _pairs_for(styles)
    level = {word}
    seen = {word}
    out: list[str] = []
    for _ in range(max(1, max_edits)):
        nxt: set[str] = set()
        for current in level:
            for a, b in pairs:
                for src, dst in ((a, b), (b, a)):
                    start = 0
                    while True:
                        i = current.lower().find(src, start)
                        if i < 0:
                            break
                        candidate = current[:i] + dst + current[i + len(src) :]
                        if candidate not in seen:
                            seen.add(candidate)
                            nxt.add(candidate)
                            out.append(candidate)
                        start = i + 1
        level = nxt
        if not level:
            break
    return out


@dataclass
class Correction:
    original: str
    corrected: str
    position: int
    alternatives: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        return {
            "bija": self.original,
            "kļuva": self.corrected,
            "pozīcija": self.position,
            **({"citi_varianti": self.alternatives} if self.alternatives else {}),
        }


@dataclass
class CorrectionReport:
    text: str
    corrections: list[Correction] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        return {
            "teksts": self.text,
            "labojumi": [c.to_json() for c in self.corrections],
            "neatpazītie_vārdi": self.unresolved,
        }


def correct_word(
    word: str,
    lexicon: Lexicon | None = None,
    *,
    styles: Sequence[str] = ("common", "fraktur"),
    max_edits: int = 1,
) -> tuple[str, list[str]]:
    """Atgriež (labotais vārds, citi derīgie varianti).

    Ja vārds jau ir atpazīstams — vai neviens variants nav vārdnīcā — vārds
    paliek neskarts. Labojums nekad netiek "uzminēts" bez vārdnīcas apstiprinājuma.
    """
    lex = lexicon if lexicon is not None else default_lexicon()
    if len(word) < _MIN_LENGTH or not len(lex):
        return word, []
    if lex.matches(word):
        return word, []
    matches = [c for c in candidates_for(word, styles, max_edits) if lex.matches(c)]
    if not matches:
        return word, []
    # priekšroka variantam ar vismazāko izmaiņu skaitu un tuvāko garumu
    matches.sort(key=lambda c: (_distance(word, c), abs(len(c) - len(word))))
    best = matches[0]
    if word[:1].isupper():
        best = best[:1].upper() + best[1:]
    return best, [m for m in matches[1:4]]


def _distance(a: str, b: str) -> int:
    """Levenšteina attālums (mazām virknēm pietiekami ātrs)."""
    if a == b:
        return 0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            )
        previous = current
    return previous[-1]


def correct_text(
    text: str,
    lexicon: Lexicon | None = None,
    *,
    handwriting: bool = False,
    max_edits: int = 1,
    clean: bool = True,
) -> CorrectionReport:
    """Izlabo atpazīšanas kļūdas visā tekstā un atskaitās par katru izmaiņu.

    ``handwriting=True`` pieslēdz Kurrent kursīva sajaukšanas (rokrakstam),
    citādi tiek lietotas Fraktur drukas sajaukšanas.
    """
    styles: tuple[str, ...] = ("common", "kurrent") if handwriting else ("common", "fraktur")
    lex = lexicon if lexicon is not None else default_lexicon()
    source = clean_ocr(text) if clean else (text or "")
    report = CorrectionReport(text=source)
    if not source or not len(lex):
        return report

    corrections: list[Correction] = []
    unresolved: list[str] = []

    def replace(match: re.Match[str]) -> str:
        word = match.group(0)
        fixed, alternatives = correct_word(word, lex, styles=styles, max_edits=max_edits)
        if fixed != word:
            corrections.append(
                Correction(
                    original=word,
                    corrected=fixed,
                    position=match.start(),
                    alternatives=alternatives,
                )
            )
            return fixed
        if len(word) >= _MIN_LENGTH and not lex.matches(word):
            unresolved.append(word)
        return word

    report.text = _WORD_RE.sub(replace, source)
    report.corrections = corrections
    # neatpazītos rādām unikālus, saglabājot secību
    report.unresolved = list(dict.fromkeys(unresolved))
    return report


def suspicious_words(
    text: str, lexicon: Lexicon | None = None, *, handwriting: bool = False, limit: int = 50
) -> list[dict[str, object]]:
    """Vārdi, ko vārdnīca neatpazīst, ar iespējamiem labojumiem.

    Noder aģentam: saraksts parāda, kur atpazīšana, visticamāk, kļūdījusies, un
    ko tur varētu būt rakstīts — bez teksta automātiskas pārrakstīšanas.
    """
    lex = lexicon if lexicon is not None else default_lexicon()
    styles: tuple[str, ...] = ("common", "kurrent") if handwriting else ("common", "fraktur")
    out: list[dict[str, object]] = []
    seen: set[str] = set()
    for word in _WORD_RE.findall(clean_ocr(text)):
        low = word.lower()
        if len(word) < _MIN_LENGTH or low in seen or lex.matches(word):
            continue
        seen.add(low)
        options = [c for c in candidates_for(word, styles, 1) if lex.matches(c)]
        out.append({"vārds": word, "iespējamie_labojumi": options[:5]})
        if len(out) >= limit:
            break
    return out
