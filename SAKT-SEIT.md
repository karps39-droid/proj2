# Uzdevums Claude Code: palaist periodika-agent uz šī datora

> **Kā lietot:** atver Claude Code mapē, kur gribi projektu, un raksti:
> *"Izlasi SAKT-SEIT.md un izpildi to."*
> (vai vienkārši ielīmē šī faila saturu sarunā)

Tu esi uz lietotāja datora. Projekts jau ir uzrakstīts un notestēts, bet **nekad
nav bijis savienots ar dzīvo vietni** — vidē, kur tas tapa, `periodika.lndb.lv`
bija slēgts ar organizācijas izejas politiku. Tavs uzdevums ir to palaist īstajā
tīklā, pārbaudīt pieņēmumus un salabot to, kas nesakrīt ar realitāti.

---

## 0. Kas tas ir

Rīku komplekts, ar ko Claude aģents var pārmeklēt visu **periodika.lndb.lv** un
nolasīt tur publicētos avīžu rakstus, arī vecajā ortogrāfijā (frakturā).

- Rāpulis ar SQLite fronti (atsākams), ALTO/METS/TEI/PAGE-XML parsēšana
- Latviešu valoda: celmošana, locīšana, vecās ortogrāfijas pārrakstīšana,
  vārdnīca `s`/`z` izšķiršanai, valodas noteikšana, datumi ar veco/jauno stilu,
  vēsturiskie vietvārdi
- Rokraksta un drukas atpazīšana (OCR/HTR adapteri)
- Headless Chromium SPA lapām un datu galapunktu atklāšanai
- MCP serveris ar 19 rīkiem
- Pamatam nav nevienas ārējas atkarības; Python 3.9+

**Repozitorijs:** `https://github.com/karps39-droid/proj2`
**Zars:** `claude/periodika-agent-search-clihwr`

---

## 1. Uzstādīšana

```bash
git clone -b claude/periodika-agent-search-clihwr https://github.com/karps39-droid/proj2 periodika-agent
cd periodika-agent
./install.sh --all          # Windows: .\install.ps1 -All
source .venv/bin/activate   # Windows: .\.venv\Scripts\Activate.ps1
```

`--all` pievieno pārlūku (SPA lapām), Pillow (attēliem) un Anthropic SDK.
Neobligāti sistēmas rīki: `tesseract-ocr` ar `lav` un `frk` valodām (drukas OCR),
`poppler-utils` (PDF).

## 2. Pārbaudi, ka viss strādā

```bash
periodika doctor
```

Tas pārbauda vidi **un** nolaiž visu cauruļvadu bez tīkla (ALTO → raksts →
ortogrāfija → indekss → meklēšana → kļūdu labošana → datumi → vietvārdi).
Ja pašpārbaude ir zaļa, kods strādā; ja kaut kā trūkst, katra rinda pasaka, ko
palaist. Palaid arī testus: `python -m unittest discover -s tests` (190 testi).

Ja tīkls kaut kur kavējas:

```bash
periodika netcheck
```

Tas izšķir, **kurš slānis** bloķē. Trīs lietas, ko nedrīkst jaukt:
- **starpniekservera 403/407** = organizācijas politika → vajag atļauju vai citu
  tīklu, to neapiet;
- **vietnes HTTP 403** = atteikts klientam → `--contact` ar īstu e-pastu,
  `--rate 0.5`, vai `--via-browser atkāpjoties`;
- **robots.txt aizliegums** = vietnes noteikums → meklē citu ceļu pie tiem pašiem
  datiem; `--ignore-robots` tikai ar lietotāja skaidru atļauju.

---

## 3. Galvenais darbs: pārbaudīt pieņēmumus par vietni

**Šī ir tā daļa, kas nekad nav pārbaudīta dzīvē.** Vietnes iekšējie ceļi nav
iekodēti cieti — `periodika/config.py` satur *kandidātu* sarakstu, kas balstīts
uz zināmajiem LNB URL paraugiem (`periodika2-viewer`, `periodika2-data`).
Tie var būt novecojuši vai vienkārši nepareizi.

### 3.1. Noskaidro galapunktus

```bash
periodika probe --contact "tavs@epasts.lv"
```

Tas pārbauda sitemap, OAI-PMH, meklēšanas API, IIIF un skatītāja ceļus pret dzīvo
vietni un saglabā profilu `~/.config/periodika/config.json`. **Izlasi izvadi** —
sadaļa `trūkst` pasaka, kā nav.

### 3.2. Dabū viena laidiena ID

Atver `https://periodika.lndb.lv` pārlūkā, ieej jebkurā avīzē un nokopē adresi.
LNB skatītāja saite izskatās apmēram šādi:

```
.../periodika2-viewer/?lang=lv#panel:pa|issue:/p_001_xxxx1899n01|article:DIVL75|page:1
```

Daļa aiz `issue:/` ir laidiena ID. Padod to atpakaļ:

```bash
periodika probe --sample-issue p_001_xxxx1899n01
```

### 3.3. Ja probe neatrada datu ceļus — palaid pārlūku

```bash
periodika sniff "<skatītāja saite>" --issue p_001_xxxx1899n01 --save-profile
```

Šī komanda atver lapu īstā Chromium, pieraksta **katru** pieprasījumu, ko
lietotne izdara, un no tiem uzģenerē URL veidnes (`{issue}`, `{n}`) un ieraksta
profilā. Tas ir drošākais ceļš: lietotne pati parāda, kur dati atrodas.

Ja arī tas neko nedod, apskaties, ko lapa vispār atdod:

```bash
periodika browse "<skatītāja saite>" --save-data ./dati
```

### 3.4. Pārbaudi, ka datu slānis tiešām strādā

```bash
periodika issue p_001_xxxx1899n01 --modern | head -40
```

Ja te parādās salasāms raksta teksts mūsdienu rakstībā — ceļš ir atrasts un viss
pārējais darbosies.

---

## 4. Rāpošana

```bash
periodika crawl --contact "tavs@epasts.lv" --rate 1 --time 3600
periodika crawl --resume          # turpina; Ctrl+C jebkurā brīdī ir droši
periodika stats
```

Ja lapas ir SPA un datu slānis nav atrasts, pievieno `--render` (lēnāk).
Noklusējums ir apzināti pieklājīgs: 1 pieprasījums sekundē, viens darbinieks,
`robots.txt` tiek ievērots. **Nepalielini ātrumu bez vajadzības** — tas ir
bibliotēkas serveris.

Meklēšana un lasīšana:

```bash
periodika search "sabiedrība"              # lokālajā indeksā
periodika search "biedrība" --lang lv
periodika get <dokumenta-id> --both        # oriģināls + mūsdienu rakstība
periodika export raksti.jsonl
```

---

## 5. Pieslēdz sev kā MCP rīkus

```bash
periodika mcp-install --target claude-code-lietotāja --write
```

Uzraksta konfigurāciju ar šī datora ceļiem (dublējums tiek saglabāts, pārējie MCP
serveri paliek neskarti). Pēc tam pārstartē Claude Code. Būs pieejami rīki
`periodika_search`, `periodika_read_article`, `periodika_fetch_issue`,
`periodika_normalize_text`, `periodika_browse_page`, `periodika_read_image`,
`periodika_netcheck` u.c.

---

## 6. Ko ņemt vērā, strādājot ar tekstu

- **Citātam vienmēr lieto oriģinālo tekstu** (`text_raw`) un saiti uz skenējumu
  (`viewer_url`). `text_modern` ir heiristisks minējums lasīšanai un meklēšanai,
  nevis autoritatīva transkripcija.
- Vecajā ortogrāfijā `s` apzīmē gan `s`, gan `z` — vārdnīca to atrisina daļēji.
- periodika satur arī baltvācu un krievu presi. Latviešu noteikumus tām nelaiž
  pāri; `analyze_article` valodu nosaka pati.
- Meklējot vienmēr lieto vaicājumu paplašināšanu (`periodika expand` / rīks to
  dara pats): 19. gs. tekstos `sabiedrība` ir uzrakstīta `sabeedriba`.
- Vecākie laidieni parasti ir publiskajā īpašumā, jaunākie var būt aizsargāti ar
  autortiesībām — savāktais teksts ir pētnieciskai lasīšanai un analīzei.

---

## 7. Ja kaut kas nesakrīt — labo kodu

Projekts ir uzbūvēts tā, lai to varētu pielāgot:

| Kur | Kas tur ir |
|---|---|
| `periodika/config.py` | vietnes profils, kandidātu galapunkti, URL atpazīšanas regex |
| `periodika/alto.py` | ALTO/METS/TEI → raksti |
| `periodika/orthography.py` | vecā ↔ mūsdienu rakstība, noteikumu grupas |
| `periodika/latvian.py` | celmošana, locīšana, leksikons, datumi, vietvārdi |
| `periodika/browser.py` | SPA lasīšana, datu galapunktu atklāšana, transports |
| `periodika/data/lv_wordlist.txt` | vārdnīca (lielāku padod ar `PERIODIKA_LV_WORDLIST`) |

Pirms jebkuras izmaiņas un pēc tās:

```bash
python -m unittest discover -s tests
periodika doctor --offline
```

Testi neiet pret dzīvo vietni — tie lieto vietējos serverus un paraugfailus.
Ja labo kaut ko, kas saistīts ar dzīvo vietni, **pieliec testu ar īsto atbildi
kā paraugu** (`tests/fixtures/`), nevis testu, kas prasa tīklu.

---

## 8. Ko atskaitīt lietotājam

Kad esi izgājis 1.–4. soli, pasaki īsi:

1. Vai `doctor` un testi ir zaļi.
2. Ko `probe`/`sniff` atrada — kuras veidnes tiešām strādā (tas ir jaunums, ko
   neviens vēl nezināja).
3. Vai `periodika issue <ID>` atdeva salasāmu rakstu.
4. Kas nesakrita ar kodā ieliktajiem pieņēmumiem un ko izlaboji.
5. Cik dokumentu savākts pirmajā rāpošanas reizē.
