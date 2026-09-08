# periodika-agent

Rīku komplekts, ar ko **Claude aģents var pārmeklēt visu periodika.lndb.lv un
kārtīgi nolasīt tur publicētos avīžu rakstus — arī tos, kas iespiesti vecajā
ortogrāfijā (frakturā).**

Trīs daļas:

| Daļa | Ko dara |
|---|---|
| **Rāpulis** | Atsākama, pieklājīga visas vietnes pārmeklēšana ar SQLite fronti — sitemap → OAI-PMH → saišu grafs. |
| **Nolasītājs** | METS + ALTO (un TEI) parsēšana: no OCR blokiem uz veseliem rakstiem ar virsrakstu, datumu, lappusi un OCR ticamību. |
| **Vecā druka** | Fraktur OCR tīrīšana, vecās ortogrāfijas pārrakstīšana mūsdienu rakstībā un vaicājumu paplašināšana pretējā virzienā. |

Nav nevienas ārējas atkarības — tikai Python 3.11+ standarta bibliotēka.

---

## Ātrais sākums

```bash
git clone <šis repo> && cd proj2

# 1. Noskaidro, kuri vietnes galapunkti tiešām strādā (sk. "Kāpēc probe" zemāk)
python3 -m periodika probe --contact "vards@piemers.lv" \
        --sample-issue p_001_xxxx1899n01

# 2. Pārmeklē (atsākami — Ctrl+C jebkurā brīdī, pēc tam --resume)
python3 -m periodika crawl --time 3600 --rate 1 --contact "vards@piemers.lv"
python3 -m periodika crawl --resume

# 3. Meklē un lasi
python3 -m periodika search "sabiedrība"           # lokālajā indeksā
python3 -m periodika search "sabiedrība" --site    # dzīvajā vietnē
python3 -m periodika get <dokumenta-id> --both     # oriģināls + mūsdienu rakstība
```

## Kā to pieslēgt Claude aģentam

Rīki tiek piedāvāti kā MCP serveris (stdio, bez atkarībām):

```bash
claude mcp add periodika -- python3 -m periodika.mcp_server
```

vai `~/.claude.json` / `.mcp.json`:

```json
{
  "mcpServers": {
    "periodika": {
      "command": "python3",
      "args": ["-m", "periodika.mcp_server"],
      "cwd": "/ceļš/uz/proj2",
      "env": { "PERIODIKA_HOME": "~/.config/periodika" }
    }
  }
}
```

Pieejamie rīki:

| Rīks | Nozīme |
|---|---|
| `periodika_search` | Meklē lokālajā indeksā vai vietnē; vaicājumu automātiski papildina ar vecās drukas variantiem. |
| `periodika_read_article` | Atdod raksta pilno tekstu **divās versijās** — oriģinālā un mūsdienu rakstībā — ar metadatiem. |
| `periodika_fetch_issue` | Ielādē vienu laidienu pa datu slāni (METS → ALTO) un sagriež to rakstos. |
| `periodika_crawl` | Ierobežots rāpošanas cikls (noklusējums 200 URL / 5 min), atsākams. |
| `periodika_normalize_text` | Vecā ortogrāfija / Fraktur OCR → mūsdienu latviešu rakstība. |
| `periodika_expand_query` | Parāda, kā vārds meklējams vecajā rakstībā. |
| `periodika_detect_orthography` | Novērtē 0..1, cik teksts ir "vecs". |
| `periodika_probe` | Noskaidro vietnes galapunktus un saglabā profilu. |
| `periodika_status` | Frontes, krātuves un HTTP statistika. |

---

## Kāpēc `probe` ir pirmais solis

Vietnes iekšējie ceļi (meklēšanas API, METS/ALTO faili, sitemap, OAI-PMH) **nav
iekodēti cieti**. `probe` izmēģina kandidātu sarakstu (`periodika/config.py`)
pret dzīvo vietni un saglabā profilu `~/.config/periodika/config.json`. Tas ir
apzināts lēmums: LNB gadu gaitā ir mainījusi platformas, un cieti iekodēti URL
noveco klusi. Profilu var arī labot ar roku.

> Šis repozitorijs tapa vidē, kurā izejošie savienojumi uz `periodika.lndb.lv`
> bija bloķēti, tāpēc noklusējuma veidnes (`periodika2-viewer`, `periodika2-data`)
> ir balstītas uz zināmajiem LNB URL paraugiem un **jāapstiprina ar `probe`**.
> Viss pārējais — ALTO/METS parsēšana, ortogrāfijas dzinējs, rāpuļa infrastruktūra —
> ir nosegts ar testiem un nav atkarīgs no minējumiem.

Ja `probe` neatrod ne sitemap, ne OAI-PMH, tas to pasaka: tad pilna pārklāšana
notiek pa saišu grafu, kas ir lēnāk un var nepārklāt visu. Šādā gadījumā
visdrošākais ceļš uz *pilnīgi visu* ir laidienu ID saraksts (no OAI-PMH vai no
bibliotēkas) un `periodika issue <ID>` katram no tiem.

## Kā raksts tiek "labi nolasīts"

1. **METS `structMap TYPE="LOGICAL"`** pasaka, kuri ALTO teksta bloki pieder
   kuram rakstam — tāpēc rezultāts ir raksts, nevis visa lappuse ar kaimiņu
   sludinājumiem iekšā.
2. **ALTO** līmenī tiek salikti pārnestie vārdi (`SUBS_TYPE="HypPart1/2"`),
   saglabāta OCR ticamība (`WC`) un, ja vajag, sakārtota slejas lasīšanas secība.
3. **Fraktur tīrīšana**: garais `ſ`, ligatūras, mīkstās defises, rindu galu
   pārnesumi ar `-` / `=` / `¬`, izretinājums (`L a t w i j a` → `Latwija`).
4. **Ortogrāfijas pārrakstīšana** vārdu līmenī, saglabājot lielos burtus:

   | vecā druka | mūsdienu | vecā druka | mūsdienu |
   |---|---|---|---|
   | `ſchodeen` | `šodien` | `tehws` | `tēvs` |
   | `muhſu` | `mūsu` | `dſihwe` | `dzīve` |
   | `Latweeschu` | `Latviešu` | `wiſſi` | `visi` |

   Noteikumu grupas (`core`, `palatals`, `doubles`, `germanisms`) var izslēgt
   atsevišķi — sk. `NormalizeOptions`.
5. **Oriģināls netiek zaudēts.** Katram dokumentam glabājas gan `text_raw`, gan
   `text_modern`; citātam vienmēr jālieto oriģinālais teksts.

### Meklēšana abos virzienos

19. gs. OCR indeksā vārdi ir vecajā rakstībā, tāpēc vaicājums tiek paplašināts:

```
$ python3 -m periodika expand "sabiedrība"
sabiedrība · sabeedrihba · sabeedriba · sabeedryba · ſabiedrība · ſabeedriba …
```

Lokālais indekss papildus glabā "salocīto" formu (`fold`), kurā `ſchodeen` un
`šodien` sakrīt, tāpēc mūsdienu vārds atrod vecos rakstus un otrādi — arī tad,
ja pārrakstīšana konkrētajā vārdā nav ideāla.

### Ko šis rīks *nezina*

Vecajā ortogrāfijā `s` apzīmē gan mūsdienu `s`, gan `z` (`awiſes` → `avises`,
pareizi būtu `avīzes`), un dubultie līdzskaņi ne vienmēr ir artefakts. Tāpēc
`text_modern` ir **minējums lasīšanai un meklēšanai**, nevis autoritatīva
transkripcija. Zinātniskam citātam lieto `text_raw` un saiti uz skenējumu
(`viewer_url`).

---

## Pieklājīga rāpošana

Noklusējumi ir apzināti lēni: **1 pieprasījums sekundē, 1 darbinieks**,
`robots.txt` ievērošana (arī `Crawl-delay`), eksponenciāla atkāpšanās ar
`Retry-After`, ETag/Last-Modified nosacījuma pieprasījumi un pastāvīgs kešs —
atkārtota rāpošana neģenerē slodzi no jauna. Norādi `--contact`, lai bibliotēka
zina, kas rāpo. Ātrumu palielini tikai apzināti un saskaņoti.

Ņem vērā vietnes lietošanas noteikumus: vecākie laidieni parasti ir publiskajā
īpašumā, bet jaunākie var būt aizsargāti ar autortiesībām — savāktais teksts
paredzēts pētnieciskai lasīšanai un analīzei, ne pārpublicēšanai.

## Uzbūve

```
periodika/
  config.py       vietnes profils + kandidātu galapunkti (probe tos pārbauda)
  http_client.py  robots.txt, ātruma ierobežotājs, atkārtojumi, SQLite kešs
  htmlutil.py     HTML → saites / meta / teksts (bez bs4)
  alto.py         ALTO + METS + TEI → raksti
  orthography.py  Fraktur tīrīšana, vecā ↔ mūsdienu rakstība, vaicājumu varianti
  extract.py      resurss → Document (vienmēr abas ortogrāfijas)
  store.py        SQLite: fronte, dokumenti, FTS5 pilnteksts
  discovery.py    sitemap, OAI-PMH, probe
  crawl.py        atsākams rāpulis
  search.py       vietnes un lokālā meklēšana
  cli.py          komandrinda
  mcp_server.py   MCP rīki Claude aģentam
tests/            64 testi, bez tīkla (paraugi tests/fixtures/)
```

## Testi

```bash
python3 -m unittest discover -s tests -v
```
