"""Vecās ortogrāfijas apstrādes testi."""

import unittest

from periodika.orthography import (
    NormalizeOptions,
    clean_ocr,
    expand_query,
    fold,
    long_s_variant,
    looks_old,
    modern_to_old_variants,
    normalize_article_text,
    old_to_modern,
)


class TestCleanOcr(unittest.TestCase):
    def test_long_s_and_ligatures(self):
        self.assertEqual(clean_ocr("Wehſtneſis"), "Wehstnesis")
        self.assertEqual(clean_ocr("ﬁrma"), "firma")

    def test_joins_hyphenated_line_break(self):
        self.assertEqual(clean_ocr("Beed-\nriba"), "Beedriba")
        self.assertEqual(clean_ocr("Beed=\nriba"), "Beedriba")

    def test_collapses_fraktur_letterspacing(self):
        self.assertEqual(clean_ocr("L a t w i j a"), "Latwija")

    def test_keeps_short_sequences_intact(self):
        # divi atsevišķi vārdi nedrīkst salipt
        self.assertEqual(clean_ocr("es un tu"), "es un tu")

    def test_paragraphs_survive(self):
        self.assertEqual(clean_ocr("viens\n\n\n\ndivi"), "viens\n\ndivi")


class TestOldToModern(unittest.TestCase):
    def test_core_substitutions(self):
        cases = {
            "ſchodeen": "šodien",
            "tehws": "tēvs",
            "muhſu": "mūsu",
            "Latweeschu": "Latviešu",
            "wiſſi": "visi",
            "zilweks": "cilveks",
            "dſihwe": "dzīve",
            "Wehſtneſis": "Vēstnesis",
        }
        for old, modern in cases.items():
            with self.subTest(old=old):
                self.assertEqual(old_to_modern(old), modern)

    def test_case_is_preserved(self):
        self.assertEqual(old_to_modern("WEHSTNESIS"), "VĒSTNESIS")
        self.assertEqual(old_to_modern("Wehſtneſis"), "Vēstnesis")

    def test_prefix_doubles_are_not_collapsed(self):
        # "attehlot" = at + tēlot; dubultais t ir īsts, ne vecās drukas artefakts
        self.assertEqual(old_to_modern("attehlot"), "attēlot")
        self.assertEqual(old_to_modern("apprezeht"), "apprecēt")
        # bet vecās drukas dubultojums vārda vidū tiek vienkāršots
        self.assertEqual(old_to_modern("wiſſi"), "visi")

    def test_modern_text_is_left_alone(self):
        text = "Šodien Rīgā notika Latviešu biedrības sapulce."
        self.assertEqual(old_to_modern(text), text)

    def test_options_can_disable_rule_groups(self):
        conservative = NormalizeOptions(doubles=False, germanisms=False, palatals=False)
        self.assertEqual(old_to_modern("wiſſi", conservative), "vissi")

    def test_punctuation_and_numbers_survive(self):
        self.assertEqual(old_to_modern("1899. gadā, ſchodeen!"), "1899. gadā, šodien!")


class TestDetection(unittest.TestCase):
    def test_old_text_scores_high(self):
        self.assertGreater(looks_old("Rihgas Latweeschu Beedriba ſchodeen"), 0.5)

    def test_modern_text_scores_zero(self):
        self.assertLess(looks_old("Šodien Rīgā notika sapulce."), 0.1)

    def test_normalize_article_text_labels_orthography(self):
        old = normalize_article_text("Wehſtneſis ſchodeen raksta.")
        self.assertEqual(old["orthography"], "veca")
        self.assertIn("Vēstnesis", old["text_modern"])
        self.assertIn("Wehstnesis", old["text_raw"])

        modern = normalize_article_text("Vēstnesis šodien raksta.")
        self.assertEqual(modern["orthography"], "jauna")


class TestQueryExpansion(unittest.TestCase):
    def test_original_query_comes_first(self):
        self.assertEqual(expand_query("sabiedrība")[0], "sabiedrība")

    def test_generates_old_spelling(self):
        variants = expand_query("sabiedrība")
        self.assertIn("sabeedriba", variants)

    def test_long_s_variant_for_plain_words(self):
        self.assertIn("ſkola", expand_query("skola"))
        self.assertEqual(long_s_variant("visi"), "viſi")

    def test_modern_to_old_variants(self):
        self.assertIn("zilwehks", modern_to_old_variants("cilvēks"))

    def test_variants_are_bounded(self):
        self.assertLessEqual(len(modern_to_old_variants("sabiedrošanās", 5)), 5)


class TestFold(unittest.TestCase):
    def test_old_and_modern_fold_together(self):
        self.assertEqual(fold("ſchodeen"), fold("šodien"))
        self.assertEqual(fold("Latweeschu"), fold("latviešu"))

    def test_fold_is_diacritic_free(self):
        self.assertEqual(fold("Rīgā"), "riga")


if __name__ == "__main__":
    unittest.main()
