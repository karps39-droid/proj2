"""Latviešu valodas specializācija: morfoloģija, leksikons, datumi, vietvārdi.

Šis modulis padara pārējos rīkus tiešām *latviskus*, nevis vispārīgus:

* **Morfoloģija.** Latviešu valoda ir stipri locīta, tāpēc ``sabiedrība`` un
  ``sabeedribas`` bez celmošanas nesatiekas. ``stem`` dod celmu meklēšanai,
  ``inflect_variants`` — locījumus vietnes meklētājam.
* **Leksikons.** Vecajā ortogrāfijā ``s`` apzīmē gan mūsdienu ``s``, gan ``z``
  (``ſirgs`` → ``zirgs``). Nolemt var tikai vārdnīca — ``Lexicon.correct``.
* **Valodas noteikšana.** periodika satur ne tikai latviešu, bet arī vācu un
  krievu presi. Latviešu vecās drukas noteikumus nedrīkst laist pāri vācu
  rakstam, tāpēc ``detect_language`` ir priekšnosacījums normalizācijai.
* **Datumi.** ``1899. gada 1. (13.) maijā`` — ar veco un jauno stilu vienā
  rindā, kas 19. gs. Krievijas impērijas presē ir ikdiena.
* **Vietvārdi.** ``Mitau`` / ``Митава`` / ``Jelgawa`` = ``Jelgava``. Bez šīs
  tabulas meklēšana vācu un krievu presē neatrod neko.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Sequence

from .orthography import (
    NormalizeOptions,
    clean_ocr,
    fold,
    long_s_variant,
    modern_to_old_variants,
    old_to_modern,
)

__all__ = [
    "deaccent",
    "stem",
    "inflect_variants",
    "Lexicon",
    "default_lexicon",
    "detect_language",
    "LANGUAGE_NAMES",
    "parse_latvian_date",
    "julian_to_gregorian",
    "MONTHS",
    "FOLK_MONTHS",
    "PLACES",
    "place_variants",
    "find_places",
    "split_sentences",
    "expand_query_lv",
]

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def deaccent(text: str) -> str:
    """``Rīgā`` → ``riga``. Latviešu diakritika ir tikai garumzīmes un mīkstinājumi,
    tāpēc noņemot tās, ``š/s``, ``ž/z``, ``ķ/k`` sakrīt — meklēšanai tas ir vēlams."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


# --------------------------------------------------------------- morfoloģija
#: Galotnes un izskaņas, ko celmošana nogriež (garākās vispirms).
_SUFFIXES: tuple[str, ...] = (
    "sanas", "sanai", "sanam", "sanos", "sana", "sanu",
    "ajiem", "ajam", "ajos", "ajas", "ajai", "aja",
    "ibas", "ibai", "ibam", "ibam", "iba", "ibu",
    "usies", "ties", "usas", "usi", "usu", "uso",
    "iem", "ies", "ams", "ots", "oti", "ota", "isi", "isu",
    "am", "as", "em", "es", "im", "is", "os", "us", "ai", "ei", "ie", "ju", "um",
    "a", "e", "i", "s", "u", "t",
)
_MIN_STEM = 3


@lru_cache(maxsize=100_000)
def stem(word: str) -> str:
    """Latviešu vieglā celmošana: ``sabiedrībām`` → ``sabiedrib``.

    Vispirms noņem diakritiku (tā mīkstinājumi vairs netraucē salīdzināt
    ``upe``/``upju``), tad nogriež garāko atpazīto galotni.
    """
    base = deaccent(word)
    if len(base) <= _MIN_STEM:
        return base
    for suffix in sorted(_SUFFIXES, key=len, reverse=True):
        if base.endswith(suffix) and len(base) - len(suffix) >= _MIN_STEM:
            return base[: -len(suffix)]
    return base


def stem_text(text: str) -> str:
    """Celmo visu tekstu (lieto pilnteksta indeksam)."""
    return " ".join(stem(w) for w in _WORD_RE.findall(text))


#: Vienkāršotas paradigmas: locījumu galotnes pēc deklinācijas klases.
#: Pirmā galotne ir nominatīvs, tāpēc no jebkuras formas var atgriezties pamatformā.
_PARADIGMS: dict[str, tuple[str, ...]] = {
    "a": ("a", "as", "ai", "u", "ā", "ām", "ās"),          # sieviešu dz.: skola, Jelgava
    "e": ("e", "es", "ei", "i", "ē", "ēm", "ēs"),          # upe, biedrene
    "s": ("s", "a", "am", "u", "ā", "i", "iem", "os"),     # vīriešu dz.: cilvēks
    "is": ("is", "ja", "im", "i", "ī", "ji", "jiem", "jos"),
    "us": ("us", "um", "u", "ū", "i", "iem", "os"),
    "t": ("t", "ja", "ju", "s"),                           # darbības vārds: rakstīt/-īja/-īju/-īs
}

#: Galotne -> (nogriežamo burtu skaits, paradigmas klase). Garākās vispirms,
#: lai ``Jelgavas`` netiktu uztverts kā vīriešu dzimtes ``-s`` pamatforma.
#: Atvasinājumu izskaņas (-ība, -šana, -nieks) NAV šeit: tās nogriež par daudz.
#: ``biedrība`` beidzas ar ``a`` -> celms ``biedrīb`` -> ``biedrības``, ``biedrībai``.
_ENDINGS: tuple[tuple[str, str], ...] = (
    ("ties", "t"),
    ("iem", "s"), ("ām", "a"), ("ās", "a"), ("ēm", "e"), ("ēs", "e"),
    ("is", "is"), ("us", "us"), ("as", "a"), ("es", "e"), ("os", "s"),
    ("am", "s"), ("ai", "a"), ("ei", "e"), ("im", "is"),
    ("ā", "a"), ("ē", "e"), ("š", "s"), ("s", "s"), ("a", "a"), ("e", "e"), ("t", "t"),
)
_MIN_BASE = 2


def inflect_variants(word: str, max_variants: int = 8) -> list[str]:
    """No jebkuras formas ģenerē biežākos locījumus (vietnes meklētājam).

    ``Jelgavas`` un ``Jelgavā`` abi dod to pašu rindu ``Jelgava, Jelgavas,
    Jelgavai, Jelgavu, Jelgavā, Jelgavām, Jelgavās``. Palatalizācija
    (``lācis`` → ``lāča``) netiek modelēta — meklēšanai pietiek ar celmu, un
    lieks variants nemaksā neko.
    """
    lowered = word.lower()
    for ending, klass in _ENDINGS:
        if not lowered.endswith(ending):
            continue
        base = lowered[: -len(ending)]
        if len(base) < _MIN_BASE:
            continue
        forms = [base + suffix for suffix in _PARADIGMS[klass]]
        return [f for f in dict.fromkeys(forms) if f != lowered][:max_variants]
    return []


# ---------------------------------------------------------------- leksikons
@dataclass
class Lexicon:
    """Latviešu vārdu kopa ar celmu indeksu vecās drukas neviennozīmību šķiršanai."""

    words: set[str] = field(default_factory=set)
    stems: set[str] = field(default_factory=set)
    #: "Salocītās" formas — sedz gan veco, gan mūsdienu rakstību (ſchodeen = šodien).
    folds: set[str] = field(default_factory=set)

    @classmethod
    def from_lines(cls, lines: Iterable[str]) -> "Lexicon":
        lex = cls()
        for line in lines:
            word = line.strip()
            if not word or word.startswith("#"):
                continue
            lex.add(word)
        return lex

    def add(self, word: str) -> None:
        lowered = word.lower()
        self.words.add(lowered)
        self.words.add(deaccent(lowered))
        self.stems.add(stem(lowered))
        self.folds.add(fold(lowered))

    def __len__(self) -> int:
        return len(self.words)

    def contains(self, word: str) -> bool:
        lowered = word.lower()
        return (
            lowered in self.words
            or deaccent(lowered) in self.words
            or stem(lowered) in self.stems
        )

    def matches(self, word: str, *, allow_old_orthography: bool = True) -> bool:
        """Vai vārds ir atpazīstams — mūsdienu vai vecajā rakstībā, jebkurā locījumā."""
        if self.contains(word):
            return True
        if not allow_old_orthography:
            return False
        folded = fold(word)
        if folded in self.folds:
            return True
        modern = old_to_modern(word)
        return modern.lower() != word.lower() and self.contains(modern)

    def correct(self, word: str) -> str:
        """Izšķir veco ``s`` (= mūsdienu ``s`` vai ``z``) ar vārdnīcas palīdzību.

        ``ſirgs`` → ``sirgs`` (pēc noteikumiem) → ``zirgs`` (pēc vārdnīcas).
        Ja neviens variants vārdnīcā nav, atgriež ievadu neskartu.
        """
        if not word or self.contains(word):
            return word
        positions = [i for i, ch in enumerate(word.lower()) if ch == "s"]
        if not positions or len(positions) > 3:
            return word
        for candidate in _s_to_z_candidates(word, positions):
            if self.contains(candidate):
                return candidate
        return word


def _s_to_z_candidates(word: str, positions: Sequence[int]) -> list[str]:
    """Visas ``s`` -> ``z`` aizstāšanas kombinācijas (mazākās izmaiņas vispirms)."""
    out: list[str] = []
    total = 1 << len(positions)
    for mask in range(1, total):
        chars = list(word)
        for bit, pos in enumerate(positions):
            if mask >> bit & 1:
                chars[pos] = "Z" if chars[pos].isupper() else "z"
        out.append("".join(chars))
    out.sort(key=lambda c: sum(1 for a, b in zip(c.lower(), word.lower()) if a != b))
    return out


def _wordlist_path() -> Path:
    env = os.environ.get("PERIODIKA_LV_WORDLIST")
    if env:
        return Path(env).expanduser()
    return Path(__file__).parent / "data" / "lv_wordlist.txt"


@lru_cache(maxsize=4)
def default_lexicon(path: str | None = None) -> Lexicon:
    """Ielādē iebūvēto (vai ``PERIODIKA_LV_WORDLIST`` norādīto) vārdu sarakstu."""
    target = Path(path) if path else _wordlist_path()
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    lexicon = Lexicon.from_lines(lines)
    # Vietvārdi arī pieder leksikonam: citādi pēclabošana mēģinātu "izlabot"
    # Rīgu vai Mitau par kaut ko citu.
    for modern, historic in PLACES.items():
        lexicon.add(modern)
        for name in historic:
            lexicon.add(name)
    return lexicon


def normalize_lv(text: str, *, use_lexicon: bool = True,
                 options: NormalizeOptions | None = None) -> str:
    """Vecā ortogrāfija -> mūsdienu latviešu rakstība ar vārdnīcas korekciju."""
    converted = old_to_modern(text, options)
    if not use_lexicon:
        return converted
    lexicon = default_lexicon()
    if not len(lexicon):
        return converted
    return _WORD_RE.sub(lambda m: lexicon.correct(m.group(0)), converted)


# ----------------------------------------------------------- valodas noteikšana
LANGUAGE_NAMES = {
    "lv": "latviešu",
    "de": "vācu",
    "ru": "krievu",
    "et": "igauņu",
    "unknown": "nenoteikta",
}

_STOPWORDS: dict[str, frozenset[str]] = {
    # latviešu — gan mūsdienu, gan vecās drukas formas
    "lv": frozenset(
        """un ir ar par no uz pie pēc bez kas ka tas tā tie tās šis šī bet vai jau tikai
        arī viņš viņa mēs jūs es tu man tev nav būt bija būs kad kur kā ļoti daudz
        irr pee us arri tikkai wai winsch mehs wiſſi wissi tahs schi tee kaß nhe""".split()
    ),
    "de": frozenset(
        """der die das und den dem des ein eine einen ist sind war nicht für von mit auf
        aus im zu sich auch wird werden dass daß hat haben wie nur noch bei nach über
        er sie es wir ihr man schon aber""".split()
    ),
    "ru": frozenset(
        """и в не на что с по как из за от для это но они мы вы он она его ее к до же
        или уже был была было были""".split()
    ),
    "et": frozenset(
        """ja on ei ning see kes oma aga ka siis kui tema meie teie olen oli ole kõik
        veel nüüd""".split()
    ),
}

_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")


def detect_language(text: str) -> dict[str, object]:
    """Nosaka raksta valodu pēc palīgvārdiem un rakstības.

    Atgriež ``{"language", "name", "confidence", "scores"}``. Latviešu vecā
    druka tiek atpazīta kā ``lv`` (nevis ``de``), jo palīgvārdi atšķiras pat
    tad, kad pārējā rakstība ir vāciska.
    """
    cleaned = clean_ocr(text or "")
    words = [w.lower() for w in _WORD_RE.findall(cleaned)]
    if not words:
        return {"language": "unknown", "name": LANGUAGE_NAMES["unknown"],
                "confidence": 0.0, "scores": {}}

    cyrillic = len(_CYRILLIC_RE.findall(cleaned))
    letters = sum(1 for ch in cleaned if ch.isalpha())
    if letters and cyrillic / letters > 0.2:
        return {"language": "ru", "name": LANGUAGE_NAMES["ru"],
                "confidence": round(min(1.0, cyrillic / letters), 3),
                "scores": {"ru": round(cyrillic / letters, 3)}}

    scores: dict[str, float] = {}
    for lang, stops in _STOPWORDS.items():
        if lang == "ru":
            continue
        scores[lang] = sum(1 for w in words if w in stops) / len(words)
    # latviešu diakritika un raksturīgās galotnes ir papildu liecība
    lv_marks = sum(1 for w in words if re.search(r"[āčēģīķļņšūž]", w))
    scores["lv"] += min(0.25, lv_marks / len(words))

    best = max(scores, key=lambda k: scores[k])
    if scores[best] < 0.03:
        return {"language": "unknown", "name": LANGUAGE_NAMES["unknown"],
                "confidence": round(scores[best], 3),
                "scores": {k: round(v, 3) for k, v in scores.items()}}
    return {
        "language": best,
        "name": LANGUAGE_NAMES[best],
        "confidence": round(min(1.0, scores[best] * 4), 3),
        "scores": {k: round(v, 3) for k, v in scores.items()},
    }


# ------------------------------------------------------------------- datumi
MONTHS: dict[str, int] = {
    "janvar": 1, "februar": 2, "mart": 3, "april": 4, "maij": 5, "junij": 6,
    "julij": 7, "august": 8, "septembr": 9, "oktobr": 10, "oktober": 10,
    "novembr": 11, "decembr": 12,
    # vecās drukas formas, ja teksts nav izlaists caur old_to_modern
    "janwahr": 1, "februahr": 2, "mahrt": 3, "junij_": 6, "nowembr": 11,
    "dezembr": 12, "septembers": 9,
}

#: Latviešu tautas mēnešu nosaukumi. Avoti tos vieno atšķirīgi, tāpēc katram
#: nosaukumam glabājam visus zināmos mēnešus — pirmais ir biežāk lietotais.
FOLK_MONTHS: dict[str, tuple[int, ...]] = {
    "ziemas menesis": (1, 12),
    "svecu menesis": (2, 1),
    "sersnu menesis": (3,),
    "sulu menesis": (4,),
    "lapu menesis": (5,),
    "ziedu menesis": (6, 5),
    "siena menesis": (7,),
    "rudzu menesis": (8,),
    "silu menesis": (9,),
    "velu menesis": (10, 11),
    "salnu menesis": (11,),
    "vilku menesis": (12,),
}

_YEAR = r"(?P<y>1[5-9]\d{2}|20\d{2})"
_DAY = r"(?P<d>\d{1,2})"
_ALT_DAY = r"(?:\s*\(\s*(?P<d2>\d{1,2})\.?\s*\))?"
_MON = r"(?P<mon>[a-z]+)"

_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 1899. gada 1. (13.) maija
    re.compile(rf"{_YEAR}\.?\s*g(?:ada|adaa|\.)\s*{_DAY}\.?{_ALT_DAY}\s*{_MON}"),
    # 1. (13.) maija 1899. g.
    re.compile(rf"{_DAY}\.?{_ALT_DAY}\s*{_MON}\s*{_YEAR}"),
    # 1899. gada janvari
    re.compile(rf"{_YEAR}\.?\s*g(?:ada|\.)\s*{_MON}"),
    # 01.05.1899 / 1/5 1899
    re.compile(rf"{_DAY}[./]\s*(?P<m>\d{{1,2}})[./\s]\s*{_YEAR}"),
)


def _month_from_word(word: str) -> int | None:
    for root, number in MONTHS.items():
        if word.startswith(root[:5]) and len(word) >= 4:
            return number
    return None


def julian_to_gregorian(year: int, month: int, day: int) -> str:
    """Vecais stils -> jaunais stils (ISO). 19. gs. starpība ir 12 dienas."""
    if (year, month, day) < (1700, 3, 1):
        offset = 10
    elif (year, month, day) < (1800, 3, 1):
        offset = 11
    elif (year, month, day) < (1900, 3, 1):
        offset = 12
    else:
        offset = 13
    return (_dt.date(year, month, day) + _dt.timedelta(days=offset)).isoformat()


def parse_latvian_date(text: str) -> dict[str, object]:
    """Izvelk datumu no latviska teksta, ieskaitot veco/jauno stilu.

    ``"Rīgā, 1899. gada 1. (13.) maijā"`` ->
    ``{"date": "1899-05-13", "date_old_style": "1899-05-01", "calendar": "abi stili"}``
    """
    cleaned = clean_ocr(text or "")
    # Meklējam gan oriģinālajā, gan mūsdienu rakstībā pārrakstītajā formā:
    # pārrakstīšana atrod "Junijā"/"Nowembris", bet sabojātu "ziedu" -> "ciedu",
    # tāpēc ar vienu no abām formām nepietiek.
    variants = []
    for candidate in (cleaned, old_to_modern(cleaned)):
        normalized = re.sub(r"\s+", " ", deaccent(candidate))
        if normalized not in variants:
            variants.append(normalized)

    for pattern, normalized in ((p, v) for v in variants for p in _DATE_PATTERNS):
        m = pattern.search(normalized)
        if not m:
            continue
        groups = m.groupdict()
        year = int(groups["y"])
        month: int | None
        if groups.get("m"):
            month = int(groups["m"])
        else:
            month = _month_from_word(groups.get("mon") or "")
        if not month or not 1 <= month <= 12:
            continue
        day = int(groups["d"]) if groups.get("d") else None
        day2 = int(groups["d2"]) if groups.get("d2") else None
        if day and not 1 <= day <= 31:
            continue

        if day and day2:
            try:
                iso_new = _dt.date(year, month, day2).isoformat()
                iso_old = _dt.date(year, month, day).isoformat()
            except ValueError:
                continue
            return {
                "date": iso_new,
                "date_old_style": iso_old,
                "calendar": "abi stili",
                "year": year, "month": month, "day": day2,
                "matched": m.group(0),
            }
        if day:
            try:
                iso = _dt.date(year, month, day).isoformat()
            except ValueError:
                continue
            return {
                "date": iso,
                "date_old_style": "",
                "calendar": "nezināms stils",
                "gregorian_if_julian": julian_to_gregorian(year, month, day),
                "year": year, "month": month, "day": day,
                "matched": m.group(0),
            }
        return {
            "date": f"{year}-{month:02d}",
            "calendar": "nezināms stils",
            "year": year, "month": month, "day": None,
            "matched": m.group(0),
        }

    for name, months in FOLK_MONTHS.items():
        # "ziedu mēnesī" / "ziedu mēneša" — locījums nedrīkst traucēt
        root = name.split()[0]
        hit = next((v for v in variants if re.search(rf"\b{root}\s+menes", v)), "")
        if hit:
            year_m = re.search(_YEAR, hit)
            year = int(year_m.group("y")) if year_m else None
            return {
                "date": f"{year}-{months[0]:02d}" if year else "",
                "calendar": "tautas mēneša nosaukums",
                "year": year, "month": months[0], "day": None,
                "iespējamie_mēneši": list(months),
                "matched": name,
            }

    year_m = re.search(_YEAR, variants[0])
    if year_m:
        return {"date": year_m.group("y"), "calendar": "tikai gads",
                "year": int(year_m.group("y")), "month": None, "day": None,
                "matched": year_m.group(0)}
    return {"date": "", "calendar": "", "year": None, "month": None, "day": None,
            "matched": ""}


# ---------------------------------------------------------------- vietvārdi
#: Mūsdienu nosaukums -> vēsturiskie (vācu, krievu, vecās drukas) varianti.
PLACES: dict[str, tuple[str, ...]] = {
    "Rīga": ("Riga", "Rihga", "Рига"),
    "Jelgava": ("Mitau", "Jelgawa", "Митава"),
    "Liepāja": ("Libau", "Leepaja", "Либава"),
    "Ventspils": ("Windau", "Wentspils", "Виндава"),
    "Kuldīga": ("Goldingen", "Kuldiga", "Гольдинген"),
    "Aizpute": ("Hasenpoth", "Aispute", "Газенпот"),
    "Grobiņa": ("Grobin", "Grobina"),
    "Tukums": ("Tuckum", "Tukkums", "Туккум"),
    "Talsi": ("Talsen", "Talsi"),
    "Kandava": ("Kandau", "Kandawa"),
    "Sabile": ("Zabeln", "Sabile"),
    "Piltene": ("Pilten", "Piltene"),
    "Durbe": ("Durben", "Durbe"),
    "Bauska": ("Bauske", "Bauska", "Бауск"),
    "Jaunjelgava": ("Friedrichstadt", "Jaunjelgawa", "Фридрихштадт"),
    "Jēkabpils": ("Jakobstadt", "Jehkabpils", "Якобштадт"),
    "Daugavpils": ("Dünaburg", "Duenaburg", "Dinaburga", "Daugawpils", "Двинск", "Динабург"),
    "Rēzekne": ("Rositten", "Rehsekne", "Режица"),
    "Ludza": ("Ludsen", "Ludsa", "Люцин"),
    "Krāslava": ("Kreslawka", "Krahslawa", "Краслава"),
    "Viļaka": ("Marienhausen", "Wilaka"),
    "Ilūkste": ("Illuxt", "Iluhkste", "Иллукст"),
    "Subate": ("Subbath", "Subate"),
    "Cēsis": ("Wenden", "Zehsis", "Венден"),
    "Valmiera": ("Wolmar", "Walmeera", "Вольмар"),
    "Valka": ("Walk", "Walka", "Валк"),
    "Limbaži": ("Lemsal", "Limbaschi"),
    "Ogre": ("Oger", "Ogre"),
    "Koknese": ("Kokenhusen", "Koknese"),
    "Pļaviņas": ("Stockmannshof", "Plawinas"),
    "Sloka": ("Schlock", "Sloka"),
    "Jūrmala": ("Majorenhof", "Dubbeln", "Bilderlingshof", "Juhrmala"),
    "Ķemeri": ("Kemmern", "Kjemeri"),
    "Alūksne": ("Marienburg", "Aluhksne", "Мариенбург"),
    "Gulbene": ("Schwanenburg", "Gulbene"),
    "Madona": ("Modohn", "Madona"),
    "Cesvaine": ("Sesswegen", "Zeswaine"),
    "Smiltene": ("Smilten", "Smiltene"),
    "Salaspils": ("Kirchholm", "Salaspils"),
    # vēsturiskie novadi un kaimiņi (bieži citēti tā laika presē)
    "Vidzeme": ("Livland", "Lifland", "Widseme", "Лифляндия"),
    "Kurzeme": ("Kurland", "Kurseme", "Курляндия"),
    "Zemgale": ("Semgallen", "Semgale"),
    "Latgale": ("Latgola", "Inflantija", "Инфлянты"),
    "Sēlija": ("Selonien", "Sehlija"),
    "Igaunija": ("Estland", "Igaunija", "Эстляндия"),
    "Tartu": ("Dorpat", "Terbata", "Юрьев", "Дерпт"),
    "Tallina": ("Reval", "Rewele", "Ревель"),
    "Pērnava": ("Pernau", "Pernawa", "Пернов"),
    "Viļņa": ("Wilna", "Wilna", "Вильно"),
    "Sanktpēterburga": ("St. Petersburg", "Petersburga", "Pehterburga", "Петербургъ"),
}

#: Vēsturiskais nosaukums (mazie burti, bez diakritikas) -> mūsdienu nosaukums.
_HISTORIC_INDEX: dict[str, str] = {}
for _modern, _variants in PLACES.items():
    _HISTORIC_INDEX[deaccent(_modern)] = _modern
    for _v in _variants:
        _HISTORIC_INDEX.setdefault(deaccent(_v), _modern)


def place_variants(name: str) -> list[str]:
    """Mūsdienu vietvārds -> visi vēsturiskie varianti (meklēšanai vācu/krievu presē)."""
    modern = _HISTORIC_INDEX.get(deaccent(name))
    if not modern:
        return []
    return [modern, *PLACES[modern]]


def find_places(text: str) -> list[dict[str, str]]:
    """Atrod tekstā vēsturiskos vietvārdus un pieliek mūsdienu nosaukumu."""
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in _WORD_RE.finditer(text):
        word = match.group(0)
        modern = _HISTORIC_INDEX.get(deaccent(word))
        if modern and word.lower() not in seen:
            seen.add(word.lower())
            found.append({"tekstā": word, "mūsdienās": modern})
    return found


# ------------------------------------------------------------- teikumi u.c.
#: Latviešu saīsinājumi, aiz kuriem punkts nebeidz teikumu.
ABBREVIATIONS = frozenset(
    """g. gs. gadsk. lpp. nr. sk. piem. t.i. t.s. u.c. u.tml. utt. u.t.t. dz. d.
    mēn. rbļ. kap. kg. km. m. cm. st. k-gs kgs prof. dr. inž. red. izd.""".split()
)

_SENT_END = re.compile(r"(?<=[.!?])\s+")


def split_sentences(text: str) -> list[str]:
    """Sadala tekstu teikumos, neapraujot latviešu saīsinājumus (``1899. g. 1. maijā``)."""
    parts = _SENT_END.split(clean_ocr(text or ""))
    out: list[str] = []
    for part in parts:
        if not part.strip():
            continue
        last_token = part.strip().split()[-1].lower() if part.strip().split() else ""
        if out and (last_token in ABBREVIATIONS or re.fullmatch(r"\d{1,4}\.", last_token)):
            out[-1] = out[-1] + " " + part
            continue
        if out and re.fullmatch(r"\d{1,4}\.", out[-1].strip().split()[-1].lower()):
            out[-1] = out[-1] + " " + part
            continue
        out.append(part)
    # apvienojam fragmentus, kas beidzas ar saīsinājumu
    merged: list[str] = []
    for sentence in out:
        tokens = sentence.strip().split()
        if merged and tokens and merged[-1].strip().split()[-1].lower() in ABBREVIATIONS:
            merged[-1] = merged[-1] + " " + sentence
        else:
            merged.append(sentence.strip())
    return merged


# ------------------------------------------------------- vaicājumu paplašināšana
def expand_query_lv(
    query: str,
    *,
    inflections: bool = True,
    old_orthography: bool = True,
    places: bool = True,
    max_queries: int = 16,
) -> list[str]:
    """Latviešu vaicājuma paplašināšana trijos virzienos.

    1. locījumi (``sabiedrība`` → ``sabiedrības``, ``sabiedrībai`` …),
    2. vecā ortogrāfija (``sabeedriba``, ``ſabeedriba`` …),
    3. vēsturiskie vietvārdi (``Jelgava`` → ``Mitau``, ``Митава`` …).

    Pirmais elements vienmēr ir sākotnējais vaicājums.
    """
    base = query.strip()
    out: list[str] = [base] if base else []
    words = _WORD_RE.findall(base)

    if places:
        for word in words:
            for variant in place_variants(word)[1:]:
                candidate = re.sub(
                    rf"(?<![^\W\d_]){re.escape(word)}(?![^\W\d_])", variant, base
                )
                if candidate not in out:
                    out.append(candidate)

    if inflections:
        for word in words:
            for form in inflect_variants(word, max_variants=4):
                candidate = re.sub(
                    rf"(?<![^\W\d_]){re.escape(word)}(?![^\W\d_])", form, base
                )
                if candidate not in out:
                    out.append(candidate)

    if old_orthography:
        budget = max(0, max_queries - 2)  # divas vietas atstājam garā ſ variantiem
        for candidate in list(out):
            for word in _WORD_RE.findall(candidate):
                for variant in modern_to_old_variants(word, 3):
                    replaced = re.sub(
                        rf"(?<![^\W\d_]){re.escape(word)}(?![^\W\d_])", variant, candidate
                    )
                    if replaced not in out:
                        out.append(replaced)
                    if len(out) >= budget:
                        break
                if len(out) >= budget:
                    break
            if len(out) >= budget:
                break

        # Frakturā vārda vidū raksta garo ſ — vārdiem bez citām pazīmēm
        # (piem. "skola") tā ir vienīgā atšķirība no mūsdienu rakstības.
        for candidate in list(out)[:3]:
            variant = long_s_variant(candidate)
            if variant != candidate and variant not in out:
                out.append(variant)
    return out[:max_queries]


# ------------------------------------------------------------ raksta analīze
def analyze_article(raw: str, *, hint_language: str = "") -> dict[str, object]:
    """Viss latviskais viena raksta apstrādē vienā solī.

    Atšķirībā no vispārīgās ``orthography.normalize_article_text`` šī funkcija
    vispirms noskaidro valodu: latviešu vecās drukas noteikumi netiek laisti
    pāri vācu vai krievu rakstam (periodika satur abus).
    """
    from .orthography import looks_old  # lokāls imports, lai izvairītos no cikla

    cleaned = clean_ocr(raw or "")
    detected = detect_language(cleaned)
    language = hint_language or str(detected["language"])
    score = looks_old(cleaned) if language in ("lv", "unknown") else 0.0

    if language in ("lv", "unknown") and score >= 0.12:
        modern = normalize_lv(cleaned)
        orthography = "veca" if score >= 0.35 else "jaukta"
    else:
        modern = cleaned
        orthography = "jauna" if language in ("lv", "unknown") else "cita valoda"

    date_info = parse_latvian_date(cleaned[:400]) if language in ("lv", "unknown") else {}
    return {
        "text_raw": cleaned,
        "text_modern": modern,
        "orthography": orthography,
        "old_score": round(score, 3),
        "language": language,
        "language_name": LANGUAGE_NAMES.get(language, language),
        "language_confidence": detected["confidence"],
        "date": date_info.get("date", "") if date_info else "",
        "date_details": date_info,
        "places": find_places(cleaned[:5000]),
    }
