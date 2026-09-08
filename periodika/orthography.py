"""Vecās drukas (vecās ortogrāfijas) un Fraktur OCR teksta apstrāde.

Trīs uzdevumi, ko šis modulis risina, lai aģents varētu *labi nolasīt* vecos
rakstus:

1. ``clean_ocr`` — OCR artefaktu tīrīšana: garais ``ſ``, ligatūras, vārdu
   pārnesumi rindu galos (``-``, ``=``, ``¬``, mīkstā defise), Fraktur
   izretinājums (``L a t w i j a``), pēdiņas un domuzīmes.
2. ``old_to_modern`` — vecās ortogrāfijas pārrakstīšana mūsdienu rakstībā
   (``ſchodeen`` → ``šodien``, ``muhſu tehws`` → ``mūsu tēvs``).
3. ``expand_query`` / ``modern_to_old_variants`` — pretējais virziens: no
   mūsdienu vārda ģenerē vecās rakstības variantus, lai vietnes OCR indeksā
   vispār kaut ko atrastu (``sabiedrība`` → ``sabeedriba``, ``ſabeedriba`` …).

Noteikumi ir heiristiski: 19. gs. drukā nav vienotas normas, un dažas pretimstāvas
(vecais ``s`` = mūsdienu ``s`` vai ``z``) nav viennozīmīgi atrisināmas. Tāpēc
oriģinālais teksts vienmēr tiek saglabāts blakus normalizētajam, un
``old_to_modern`` atgriež *minējumu*, nevis autoritatīvu transkripciju.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Iterator

__all__ = [
    "NormalizeOptions",
    "clean_ocr",
    "old_to_modern",
    "modern_to_old_variants",
    "expand_query",
    "long_s_variant",
    "looks_old",
    "fold",
    "normalize_article_text",
]

LONG_S = "ſ"          # ſ
SOFT_HYPHEN = "­"

#: Simboli, ko Fraktur OCR mēdz izvadīt un kas jāaizstāj pirms jebkā cita.
CHAR_FIXUPS: dict[str, str] = {
    LONG_S: "s",
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
    "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
    "ß": "ss",           # ß
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "“": '"', "”": '"', "„": '"', "‟": '"', "«": '"',
    "»": '"', "‘": "'", "’": "'", "‚": "'",
    "…": "...",
    "ſch": "sch",
    SOFT_HYPHEN: "",
    "​": "", "﻿": "",
}

#: Latviešu priedēkļi — aiz tiem dubultlīdzskani ir īsti, ne vecās drukas artefakts.
PREFIXES: frozenset[str] = frozenset(
    {"at", "ap", "aiz", "ie", "iz", "ne", "pa", "pār", "par", "pie", "sa", "uz", "no", "jaun"}
)

_LETTER_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_SPACED_RE = re.compile(r"(?<![^\W\d_])((?:[^\W\d_]\s){3,}[^\W\d_])(?![^\W\d_])", re.UNICODE)
_HYPHEN_BREAK_RE = re.compile(r"([^\W\d_])[-=¬]\s*\n\s*([^\W\d_])", re.UNICODE)


@dataclass(frozen=True)
class NormalizeOptions:
    """Kuras noteikumu grupas piemērot.

    ``core``       — droši, plaši dokumentēti noteikumi (w→v, ee→ie, sch→š, ah→ā …).
    ``palatals``   — j-digrafi (nj→ņ, lj→ļ, kj→ķ, gj→ģ).
    ``doubles``    — dubulto līdzskaņu vienkāršošana (vissi→visi).
    ``germanisms`` — ck→k, th→t, ph→f, ch→h, ds→dz.
    ``decapitalize`` — vācu manierē lielo burtu lietvārdu atgriešana mazajos
                       (tikai meklēšanas atslēgai; teikuma sākumu netaisa mazu).
    """

    core: bool = True
    palatals: bool = True
    doubles: bool = True
    germanisms: bool = True
    decapitalize: bool = False


# --- Noteikumi vārda līmenī (lietojami uz mazajiem burtiem) ----------------
# Secība ir svarīga: garākie digrafi pirmie.
_CORE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"tsch"), "č"),
    (re.compile(r"dsch"), "dž"),
    (re.compile(r"sch"), "š"),
    (re.compile(r"zh"), "ž"),
    (re.compile(r"eeh"), "ie"),
    (re.compile(r"ee"), "ie"),
    (re.compile(r"ah"), "ā"),
    (re.compile(r"eh"), "ē"),
    (re.compile(r"ih"), "ī"),
    (re.compile(r"uh"), "ū"),
    (re.compile(r"oh"), "o"),
    (re.compile(r"w"), "v"),
    (re.compile(r"z"), "c"),
]

_GERMANISM_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"ck"), "k"),
    (re.compile(r"ds"), "dz"),
    (re.compile(r"th"), "t"),
    (re.compile(r"ph"), "f"),
    (re.compile(r"ch"), "h"),
    (re.compile(r"y"), "ī"),
]

_PALATAL_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"nj"), "ņ"),
    (re.compile(r"lj"), "ļ"),
    (re.compile(r"kj"), "ķ"),
    (re.compile(r"gj"), "ģ"),
]

_DOUBLE_RE = re.compile(r"([bcčdfgģklļmnņprsštvzž])\1")


def _collapse_doubles(word: str) -> str:
    """``vissi`` → ``visi``, bet ``atteikt`` paliek neskarts."""
    out = word
    while True:
        m = _DOUBLE_RE.search(out)
        if not m:
            return out
        i = m.start()
        # "atteikt" = at+teikt: dubultais burts sākas tieši aiz priedēkļa robežas
        if i > 0 and out[: i + 1] in PREFIXES:
            nxt = _DOUBLE_RE.search(out, m.end())
            if not nxt:
                return out
            j = nxt.start()
            if out[: j + 1] in PREFIXES:
                return out
            out = out[:j] + out[j + 1 :]
            continue
        out = out[:i] + out[i + 1 :]


def _apply_rules(word: str, opts: NormalizeOptions) -> str:
    out = word
    if opts.core:
        for pat, repl in _CORE_RULES:
            out = pat.sub(repl, out)
    if opts.germanisms:
        for pat, repl in _GERMANISM_RULES:
            out = pat.sub(repl, out)
    if opts.palatals:
        for pat, repl in _PALATAL_RULES:
            out = pat.sub(repl, out)
    if opts.doubles:
        out = _collapse_doubles(out)
    return out


def _restore_case(original: str, transformed: str) -> str:
    if original.isupper() and len(original) > 1:
        return transformed.upper()
    if original[:1].isupper():
        return transformed[:1].upper() + transformed[1:]
    return transformed


def _map_words(text: str, fn) -> str:
    return _LETTER_RE.sub(lambda m: fn(m.group(0)), text)


# --------------------------------------------------------------------------
def clean_ocr(
    text: str,
    *,
    join_hyphens: bool = True,
    collapse_letterspacing: bool = True,
) -> str:
    """Notīra Fraktur/OCR artefaktus, saglabājot rindkopu struktūru."""
    if not text:
        return ""
    out = unicodedata.normalize("NFC", text)
    for src, dst in CHAR_FIXUPS.items():
        if src in out:
            out = out.replace(src, dst)
    if join_hyphens:
        out = _HYPHEN_BREAK_RE.sub(r"\1\2", out)
    if collapse_letterspacing:
        out = _SPACED_RE.sub(lambda m: re.sub(r"\s+", "", m.group(1)), out)
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r" *\n *", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def old_to_modern(text: str, options: NormalizeOptions | None = None) -> str:
    """Pārraksta veco ortogrāfiju mūsdienu rakstībā (heiristisks minējums)."""
    opts = options or NormalizeOptions()
    if not text:
        return ""
    cleaned = clean_ocr(text)

    def convert(word: str) -> str:
        lowered = word.lower()
        transformed = _apply_rules(lowered, opts)
        return _restore_case(word, transformed)

    out = _map_words(cleaned, convert)
    if opts.decapitalize:
        out = _decapitalize_nouns(out)
    return out


def _decapitalize_nouns(text: str) -> str:
    """Vācu manierē rakstītos lielos lietvārdus padara mazus (izņemot teikuma sākumu)."""
    result: list[str] = []
    sentence_start = True
    for token in re.findall(r"[^\W\d_]+|\W+|\d+", text, re.UNICODE):
        if _LETTER_RE.fullmatch(token):
            if not sentence_start and token[:1].isupper() and not token.isupper():
                token = token[:1].lower() + token[1:]
            sentence_start = False
        elif any(ch in token for ch in ".!?\n"):
            sentence_start = True
        result.append(token)
    return "".join(result)


# --- pretējais virziens: mūsdienu -> vecie varianti ------------------------
#: Katram mūsdienu grafēmam iespējamie vecās drukas ekvivalenti, biežākais pirmais.
_MODERN_TO_OLD: list[tuple[str, tuple[str, ...]]] = [
    ("dž", ("dsch",)),
    ("ie", ("ee", "ie")),
    ("š", ("sch", "sh", "ſch")),
    ("ž", ("zh", "sh")),
    ("č", ("tsch",)),
    ("dz", ("ds", "dz")),
    ("ā", ("ah", "a")),
    ("ē", ("eh", "e")),
    ("ī", ("ih", "i", "y")),
    ("ū", ("uh", "u")),
    ("ō", ("oh", "o")),
    ("ņ", ("nj", "n")),
    ("ļ", ("lj", "l")),
    ("ķ", ("kj", "k")),
    ("ģ", ("gj", "g")),
    ("c", ("z",)),
    ("v", ("w",)),
    ("z", ("s", "z")),
    ("h", ("h", "ch")),
]


def modern_to_old_variants(word: str, max_variants: int = 16) -> list[str]:
    """No mūsdienu vārda ģenerē ticamākos vecās ortogrāfijas variantus."""
    lowered = word.lower()
    variants: list[str] = [""]
    i = 0
    while i < len(lowered):
        matched = False
        for graph, replacements in _MODERN_TO_OLD:
            if lowered.startswith(graph, i):
                variants = [
                    v + r for v in variants for r in replacements
                ][: max_variants * 4]
                i += len(graph)
                matched = True
                break
        if not matched:
            variants = [v + lowered[i] for v in variants]
            i += 1
    # unikāli, saglabājot secību; oriģinālu izmetam (to jau meklē bez paplašinājuma)
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v != lowered and v not in seen:
            seen.add(v)
            out.append(v)
        if len(out) >= max_variants:
            break
    return out


def long_s_variant(text: str) -> str:
    """Frakturā ``s`` vārda vidū raksta kā garo ``ſ`` (vārda beigās — parasto)."""

    def convert(word: str) -> str:
        chars = list(word)
        for i, ch in enumerate(chars[:-1]):
            if ch == "s":
                chars[i] = LONG_S
            elif ch == "S":
                chars[i] = LONG_S.upper() if LONG_S.upper() != LONG_S else "S"
        return "".join(chars)

    return _map_words(text, convert)


def expand_query(query: str, max_variants_per_word: int = 6, max_queries: int = 12) -> list[str]:
    """No mūsdienu vaicājuma izveido vaicājumu kopu, kas trāpa arī vecajā drukā.

    Pirmais elements vienmēr ir oriģinālais vaicājums.
    """
    words = _LETTER_RE.findall(query)
    queries: list[str] = [query.strip()]
    if not words:
        return queries
    per_word = {w: modern_to_old_variants(w, max_variants_per_word) for w in words}
    # katram vārdam pa kārtai pamainām uz variantu (nevis kombinatoriski visiem)
    for depth in range(max(len(v) for v in per_word.values()) if per_word else 0):
        candidate = query
        changed = False
        for w in words:
            variants = per_word[w]
            if depth < len(variants):
                candidate = re.sub(
                    rf"(?<![^\W\d_]){re.escape(w)}(?![^\W\d_])",
                    variants[depth],
                    candidate,
                )
                changed = True
        if changed and candidate not in queries:
            queries.append(candidate)
        if len(queries) >= max_queries:
            break
    # Fraktur garā s variants (piem. "skola" -> "ſkola") — vārdiem bez citām pazīmēm
    # tas bieži ir vienīgā atšķirība no mūsdienu rakstības.
    for base in list(queries[: max(2, max_queries // 3)]):
        variant = long_s_variant(base)
        if variant != base and variant not in queries:
            queries.append(variant)
    return queries[:max_queries]


# --- diagnostika un meklēšanas atslēga ------------------------------------
_OLD_MARKERS: tuple[tuple[re.Pattern[str], float], ...] = (
    (re.compile(r"sch|ſch"), 1.0),
    (re.compile(r"tsch|dsch"), 1.0),
    (re.compile(r"w"), 0.9),   # mūsdienu latviešu rakstībā w neeksistē
    (re.compile(r"[qxy]"), 0.4),
    (re.compile(r"ee"), 0.7),
    (re.compile(r"[aeiou]h(?![aeiouāēīū])"), 0.7),
    (re.compile(LONG_S), 1.0),
    (re.compile(r"zh"), 0.6),
)
_MODERN_MARKERS = re.compile(r"[āčēģīķļņšūž]")


def looks_old(text: str) -> float:
    """Atgriež 0..1 novērtējumu, cik ticami teksts ir vecajā ortogrāfijā."""
    words = _LETTER_RE.findall(text.lower())
    if not words:
        return 0.0
    score = 0.0
    for word in words:
        hit = 0.0
        for pattern, weight in _OLD_MARKERS:
            if pattern.search(word):
                hit = max(hit, weight)
        if _MODERN_MARKERS.search(word):
            hit -= 0.8
        score += max(hit, 0.0) if hit > 0 else hit
    return max(0.0, min(1.0, score / len(words) * 2.5))


def fold(text: str) -> str:
    """Meklēšanas atslēga: veco un mūsdienu rakstību saved vienā formā.

    ``ſchodeen`` un ``šodien`` abi dod ``sodien``.
    """
    modern = old_to_modern(text, NormalizeOptions(decapitalize=False))
    lowered = modern.lower()
    decomposed = unicodedata.normalize("NFD", lowered)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _map_words(stripped, _collapse_doubles)


def normalize_article_text(raw: str) -> dict[str, object]:
    """Vienā solī sagatavo raksta tekstu aģentam.

    Atgriež ``text_raw`` (tikai OCR tīrīšana), ``text_modern`` (mūsdienu rakstība),
    ``orthography`` ("veca"/"jauna"/"jaukta") un ``old_score``.
    """
    cleaned = clean_ocr(raw)
    score = looks_old(cleaned)
    modern = old_to_modern(cleaned) if score >= 0.12 else cleaned
    if score >= 0.35:
        kind = "veca"
    elif score >= 0.12:
        kind = "jaukta"
    else:
        kind = "jauna"
    return {
        "text_raw": cleaned,
        "text_modern": modern,
        "orthography": kind,
        "old_score": round(score, 3),
    }


def iter_words(text: str) -> Iterator[str]:
    yield from _LETTER_RE.findall(text)
