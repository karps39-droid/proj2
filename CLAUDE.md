# Norādes aģentam, kas strādā ar šo repozitoriju

## Kas šis ir
Rīku komplekts periodika.lndb.lv pārmeklēšanai un vecās ortogrāfijas rakstu
nolasīšanai. Tikai Python 3.11+ stdlib — **nepievieno ārējas atkarības** bez
vajadzības (rīkam jāstrādā tukšā vidē bez `pip install`).

## Pirms koda maiņas
```bash
python3 -m unittest discover -s tests    # visiem jābūt zaļiem, aizņem <1 s
```

## Darba secība ar dzīvo vietni
1. `python3 -m periodika probe --sample-issue <ID>` — bez tā profilā nav
   METS/ALTO ceļu, un `crawl` atradīs tikai HTML lapas.
2. `python3 -m periodika crawl --time 3600` — atsākami; atkārto ar `--resume`.
3. Meklē lokāli (`search`), nevis vietnē, kad indekss jau ir — tas ir ātrāk un
   nenoslogo bibliotēkas serveri.

## Kad lasi rakstu
- Citātam lieto **`text_raw`** (oriģinālā rakstība) + `viewer_url`.
- Saprašanai un meklēšanai lieto `text_modern`.
- `orthography` un `old_score` pasaka, cik tālu teksts ir no mūsdienu rakstības;
  `ocr_confidence` zem ~0.7 nozīmē, ka OCR var būt kļūdains — nepārcenties
  interpretēt atsevišķus vārdus.
- `text_modern` ir heiristisks minējums (sk. README "Ko šis rīks nezina").
  Nekad nepasniedz to kā oriģinālu citātu.

## Kad meklē
Vaicājumu vienmēr laid caur `expand_query_lv` (`periodika_search` to dara pats) —
tas sedz trīs lietas vienlaikus:
- **locījumus** (latviešu valoda ir stipri locīta: `sabiedrība` / `sabiedrībām`),
- **veco ortogrāfiju** (`sabeedriba`, `ſabeedriba`),
- **vēsturiskos vietvārdus** (`Jelgava` → `Mitau`, `Митава`).

Meklējot vietu vai iestādi vācu vai krievu presē, sāc ar `periodika_place_names`.

## Valoda pirms normalizācijas
periodika satur latviešu, vācu un krievu presi. `analyze_article` vispirms
nosaka valodu un latviešu vecās drukas noteikumus laiž pāri **tikai** latviešu
tekstam — nemēģini normalizēt vācu rakstu ar `old_to_modern`, tas to sabojās
(`Wenden` → `Venden`). Ne-latviešu dokumentiem `orthography` ir `cita valoda`.

## Datumi
`periodika_parse_date` atgriež gan jauno, gan veco stilu. Līdz 1918. gadam
presē datums parasti ir Jūlija kalendārā — ja raksti par datējumu, saki, kurš
stils ir domāts (`calendar` lauks to pasaka).

## Rāpošanas ētika
Noklusējuma 1 pieprasījums/s un `robots.txt` ievērošana nav dekorācija.
Nepalielini `--rate` un neieslēdz `--ignore-robots` bez lietotāja skaidra
norādījuma.
