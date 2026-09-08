"""Latviešu valodas specializācijas testi."""

import unittest

from periodika.latvian import (
    FOLK_MONTHS,
    Lexicon,
    analyze_article,
    deaccent,
    default_lexicon,
    detect_language,
    expand_query_lv,
    find_places,
    inflect_variants,
    julian_to_gregorian,
    normalize_lv,
    parse_latvian_date,
    place_variants,
    split_sentences,
    stem,
)


class TestStemmer(unittest.TestCase):
    def test_inflected_forms_share_a_stem(self):
        forms = ["sabiedrība", "sabiedrības", "sabiedrībai", "sabiedrību", "sabiedrībām"]
        stems = {stem(f) for f in forms}
        self.assertEqual(len(stems), 1, stems)

    def test_diacritics_do_not_split_stems(self):
        # OCR bieži pazaudē garumzīmes — celmam tas nedrīkst traucēt
        self.assertEqual(stem("Rīgā"), stem("riga"))

    def test_short_words_survive(self):
        self.assertEqual(stem("un"), "un")
        self.assertEqual(stem("ir"), "ir")

    def test_deaccent_maps_latvian_letters(self):
        self.assertEqual(deaccent("Šķūņī žļāgt"), "skuni zlagt")


class TestInflection(unittest.TestCase):
    def test_generates_paradigm_from_base_form(self):
        forms = inflect_variants("biedrība")
        self.assertIn("biedrības", forms)
        self.assertIn("biedrībai", forms)
        self.assertIn("biedrībās", forms)

    def test_works_from_any_form_not_only_nominative(self):
        # "Jelgavas" ir ģenitīvs, ne vīriešu dzimtes pamatforma
        forms = inflect_variants("Jelgavas")
        self.assertIn("jelgava", forms)
        self.assertNotIn("jelgavaa", forms)

    def test_masculine_noun(self):
        forms = inflect_variants("cilvēks")
        self.assertIn("cilvēka", forms)
        self.assertIn("cilvēkiem", forms)

    def test_verb_infinitive(self):
        self.assertIn("rakstīja", inflect_variants("rakstīt"))

    def test_derivational_suffix_is_not_cut(self):
        self.assertNotIn("biedra", inflect_variants("biedrība"))


class TestLexicon(unittest.TestCase):
    def test_bundled_lexicon_loads(self):
        self.assertGreater(len(default_lexicon()), 200)

    def test_old_s_becomes_z_when_dictionary_says_so(self):
        lex = Lexicon.from_lines(["zirgs", "zeme", "sirds"])
        self.assertEqual(lex.correct("sirgs"), "zirgs")
        self.assertEqual(lex.correct("seme"), "zeme")

    def test_known_word_is_left_alone(self):
        lex = Lexicon.from_lines(["zirgs", "sirds"])
        self.assertEqual(lex.correct("sirds"), "sirds")

    def test_unknown_word_is_returned_unchanged(self):
        lex = Lexicon.from_lines(["zirgs"])
        self.assertEqual(lex.correct("Blaumanis"), "Blaumanis")

    def test_normalize_lv_uses_lexicon(self):
        self.assertEqual(normalize_lv("ſirgi un ſeme"), "zirgi un zeme")
        self.assertEqual(normalize_lv("ſirgi un ſeme", use_lexicon=False), "sirgi un seme")


class TestLanguageDetection(unittest.TestCase):
    def test_modern_latvian(self):
        text = "Šodien Rīgā notika biedrības sapulce, kurā runāja par skolu un valodu."
        self.assertEqual(detect_language(text)["language"], "lv")

    def test_old_orthography_latvian_is_not_mistaken_for_german(self):
        text = ("Rihgas Latweeschu Beedriba ſchodeen ſapulzejahs un runaja par "
                "tautas leetahm, ka tas irr wajadsigs.")
        self.assertEqual(detect_language(text)["language"], "lv")

    def test_german(self):
        text = ("Die Rigasche Zeitung hat für die Stadt und das Land nicht nur "
                "von der Versammlung berichtet, sondern auch aus dem Bericht.")
        self.assertEqual(detect_language(text)["language"], "de")

    def test_russian_by_script(self):
        self.assertEqual(detect_language("Рижскій вѣстникъ и общество")["language"], "ru")

    def test_empty_text(self):
        self.assertEqual(detect_language("")["language"], "unknown")


class TestDates(unittest.TestCase):
    def test_dual_style_date(self):
        result = parse_latvian_date("Rīgā, 1899. gada 1. (13.) maijā")
        self.assertEqual(result["date"], "1899-05-13")
        self.assertEqual(result["date_old_style"], "1899-05-01")
        self.assertEqual(result["calendar"], "abi stili")

    def test_old_orthography_month_name(self):
        result = parse_latvian_date("Rihgā, 1885. g. 3. Junijā")
        self.assertEqual(result["date"], "1885-06-03")

    def test_day_month_year_order(self):
        self.assertEqual(parse_latvian_date("15. aprīlī 1905. g.")["date"], "1905-04-15")

    def test_numeric_date(self):
        self.assertEqual(parse_latvian_date("01.05.1899")["date"], "1899-05-01")

    def test_year_only(self):
        result = parse_latvian_date("Izdots 1877. gadā Jelgavā")
        self.assertTrue(result["date"].startswith("1877"))

    def test_folk_month_is_reported_as_ambiguous(self):
        result = parse_latvian_date("1899. gada ziedu mēnesī")
        self.assertEqual(result["calendar"], "tautas mēneša nosaukums")
        self.assertEqual(result["iespējamie_mēneši"], list(FOLK_MONTHS["ziedu menesis"]))

    def test_no_date(self):
        self.assertEqual(parse_latvian_date("bez datuma")["date"], "")

    def test_julian_to_gregorian_offsets(self):
        self.assertEqual(julian_to_gregorian(1899, 5, 1), "1899-05-13")   # +12
        self.assertEqual(julian_to_gregorian(1917, 10, 25), "1917-11-07")  # +13
        self.assertEqual(julian_to_gregorian(1750, 1, 1), "1750-01-12")   # +11


class TestPlaces(unittest.TestCase):
    def test_modern_name_to_historic_variants(self):
        variants = place_variants("Jelgava")
        self.assertIn("Mitau", variants)
        self.assertIn("Митава", variants)

    def test_historic_name_resolves_too(self):
        self.assertIn("Jelgava", place_variants("Mitau"))

    def test_finds_places_in_text(self):
        found = find_places("Vēstis no Mitau un Wenden, arī no Двинск.")
        modern = {f["mūsdienās"] for f in found}
        self.assertEqual(modern, {"Jelgava", "Cēsis", "Daugavpils"})

    def test_unknown_place(self):
        self.assertEqual(place_variants("Ņujorka"), [])


class TestSentences(unittest.TestCase):
    def test_abbreviations_do_not_split_sentences(self):
        text = "Rīgā, 1899. g. 1. maijā notika sapulce. Tur bija daudz ļaužu."
        self.assertEqual(len(split_sentences(text)), 2)

    def test_common_abbreviation(self):
        text = "Tur bija skolotāji, ārsti u.c. ļaudis. Sapulce beidzās vēlu."
        self.assertEqual(len(split_sentences(text)), 2)


class TestQueryExpansionLv(unittest.TestCase):
    def test_combines_three_directions(self):
        variants = expand_query_lv("Jelgava", max_queries=16)
        self.assertEqual(variants[0], "Jelgava")
        self.assertIn("Mitau", variants)                       # vietvārds
        self.assertTrue(any(v.startswith("jelgav") and v != "jelgava" for v in variants))
        self.assertTrue(any("w" in v.lower() for v in variants))  # vecā ortogrāfija

    def test_long_s_variant_is_kept(self):
        self.assertIn("ſkola", expand_query_lv("skola"))

    def test_respects_limit(self):
        self.assertLessEqual(len(expand_query_lv("sabiedrība", max_queries=5)), 5)

    def test_switches_can_be_turned_off(self):
        self.assertEqual(
            expand_query_lv("Jelgava", inflections=False, old_orthography=False,
                            places=False),
            ["Jelgava"],
        )


class TestAnalyzeArticle(unittest.TestCase):
    def test_old_latvian_article(self):
        text = ("Rihgā, 1899. gada 1. (13.) maijā. Latweeschu Beedriba ſchodeen "
                "ſapulzejahs un runaja par ſeme un ſkolu.")
        result = analyze_article(text)
        self.assertEqual(result["language"], "lv")
        self.assertEqual(result["orthography"], "veca")
        self.assertEqual(result["date"], "1899-05-13")
        self.assertIn("zemi" if False else "zeme", result["text_modern"])
        self.assertEqual([p["mūsdienās"] for p in result["places"]], ["Rīga"])

    def test_german_article_is_not_run_through_latvian_rules(self):
        text = ("Die Rigasche Zeitung hat für die Stadt und das Land nicht nur von "
                "der Versammlung berichtet, sondern auch aus dem Bericht gelesen.")
        result = analyze_article(text)
        self.assertEqual(result["language"], "de")
        self.assertEqual(result["orthography"], "cita valoda")
        # vācu teksts paliek neskarts — nekādu w -> v vai ee -> ie
        self.assertEqual(result["text_modern"], result["text_raw"])

    def test_modern_latvian_article(self):
        result = analyze_article("Šodien Rīgā notika biedrības sapulce par skolu.")
        self.assertEqual(result["orthography"], "jauna")
        self.assertEqual(result["text_modern"], result["text_raw"])


if __name__ == "__main__":
    unittest.main()
