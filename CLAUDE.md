# Norādes aģentam, kas strādā ar šo repozitoriju

## Kas šis ir
Rīku komplekts periodika.lndb.lv pārmeklēšanai un vecās ortogrāfijas rakstu
nolasīšanai. Tikai Python 3.11+ stdlib — **nepievieno ārējas atkarības** bez
vajadzības (rīkam jāstrādā tukšā vidē bez `pip install`).

## Pirms koda maiņas
```bash
python3 -m unittest discover -s tests    # visiem jābūt zaļiem, aizņem <1 s
python3 -m periodika doctor --offline    # vai cauruļvads uz šī datora iet cauri
```

Ja lietotājs saka, ka "nestrādā", sāc ar `periodika doctor` — tas pārbauda vidi
un nolaiž visu cauruļvadu bez tīkla, un katra kļūda pasaka, ko palaist.

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

## Kad kaut kas ir "bloķēts"
Nekad nemini un neapej. Palaid `periodika_netcheck` — tas pasaka, kurš slānis
krīt. Trīs atšķirīgas lietas, ko nedrīkst jaukt:
- **Starpniekservera 403/407** = organizācijas izejas politika. To neapiet:
  vajag administratora atļauju vai citu tīklu. Ziņo lietotājam.
- **Vietnes 403** = atteikts klientam. Palīdz godīgs `--contact`, lēnāks ātrums
  vai `--via-browser` (pieprasījums caur īstu pārlūku ar sīkdatnēm).
- **robots.txt aizliegums** = vietnes noteikums. Meklē citu ceļu pie tiem pašiem
  datiem; `--ignore-robots` tikai ar lietotāja skaidru atļauju.

## Kad lapa neko neatdod
periodika2-viewer ir SPA — teksta HTML avotā nav. Secība:
1. `periodika_browse_page` — atver lapu īstā pārlūkā un nolasa uzzīmēto.
2. `periodika_discover_endpoints` — no lietotnes pieprasījumiem uzģenerē METS/ALTO
   veidnes un ieraksta profilā.
3. Tālāk rāpo **bez pārlūka**, pa datu slāni. Pārlūks ir veids, kā ceļu atrast,
   nevis ikdienas darbarīks: tas ir lēns un noslogo bibliotēkas serveri vairāk.

Ja teksts ir tikai attēlā, ņem `browse` ekrānuzņēmumu un padod to
`periodika_read_image`.

## Kad jālasa attēls vai rokraksts
1. `periodika_recognition_engines` — pārbaudi, kas vidē ir pieejams.
2. `periodika_read_image` ar `engine="agent"` (noklusējums) sagatavo attēlu un
   atdod ceļus + uzvedni. **Tu pats esi redzes modelis** — atver sagatavoto
   attēlu (rokrakstam: rindu izgriezumus pa vienam) ar `Read` un pārraksti.
3. Transkribē **diplomātiski**: `w` paliek `w`, `ee` paliek `ee`, garais `ſ`
   paliek. Nesalasāmu vārdu atzīmē `[vārds?]` — nekad nemini.
4. Rezultātu padod `periodika_correct_text` (ar `handwriting=true`, ja rokraksts),
   tad `periodika_normalize_text`.

Rokraksts 19. gs. ir vācu Kurrent kursīvs: `n`/`u` atšķiras tikai ar lociņu,
`e` ir divi sīki vilcieni, `h`/`b` mēdz sajaukties. Ja neesi drošs — atzīmē, nemini.
`tesseract` rokrakstam neder; tas ir drukas dzinējs.

## Rāpošanas ētika
Noklusējuma 1 pieprasījums/s un `robots.txt` ievērošana nav dekorācija.
Nepalielini `--rate` un neieslēdz `--ignore-robots` bez lietotāja skaidra
norādījuma.
