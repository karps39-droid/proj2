"""Rokraksta un drukas atpazīšanas testi (bez ārējiem dzinējiem un tīkla)."""

import tempfile
import unittest
from pathlib import Path

from periodika import images as images_mod
from periodika.correct import (
    CONFUSIONS,
    candidates_for,
    correct_text,
    correct_word,
    suspicious_words,
)
from periodika.latvian import Lexicon, default_lexicon
from periodika.pagexml import is_hocr, is_page_xml, parse_hocr, parse_page_xml
from periodika.recognize import (
    TRANSCRIPTION_PROMPT,
    RecognitionError,
    RecognitionResult,
    available_engines,
    postprocess,
    prepare_for_agent,
    recognize,
)

FIXTURES = Path(__file__).parent / "fixtures"


class TestPageXml(unittest.TestCase):
    def setUp(self):
        self.page = parse_page_xml((FIXTURES / "page_handwriting.xml").read_bytes())

    def test_detects_format(self):
        self.assertTrue(is_page_xml((FIXTURES / "page_handwriting.xml").read_bytes()))
        self.assertFalse(is_page_xml(b"<alto xmlns='x'>"))

    def test_page_geometry(self):
        self.assertEqual((self.page.width, self.page.height), (2480, 3508))
        self.assertEqual(self.page.number, 3)

    def test_regions_become_blocks(self):
        self.assertEqual([b.id for b in self.page.blocks], ["r1", "r2"])
        self.assertEqual(self.page.blocks[0].hpos, 200)

    def test_lines_and_text(self):
        text = self.page.blocks[0].text
        self.assertIn("Mihļais brahli", text)
        self.assertEqual(len(self.page.blocks[0].lines), 2)

    def test_line_confidence_is_kept(self):
        conf = self.page.blocks[1].confidence
        self.assertIsNotNone(conf)
        self.assertAlmostEqual(conf, 0.61, places=2)

    def test_whole_page_text(self):
        self.assertIn("Tavs tehws", self.page.text())


class TestHocr(unittest.TestCase):
    def setUp(self):
        self.data = (FIXTURES / "page_tesseract.hocr").read_bytes()

    def test_detects_format(self):
        self.assertTrue(is_hocr(self.data))

    def test_words_and_confidence(self):
        page = parse_hocr(self.data)
        self.assertIn("Rihgas Latweeschu", page.text())
        self.assertIsNotNone(page.confidence)
        self.assertTrue(0.7 < page.confidence < 0.95)

    def test_malformed_hocr_falls_back(self):
        broken = b"<html><span class='ocr_line'>Rihgas Beedriba<span></html>"
        page = parse_hocr(broken)
        self.assertIn("Rihgas", page.text())


class TestConfusionCandidates(unittest.TestCase):
    def test_generates_expected_substitutions(self):
        variants = candidates_for("fkola", ("common",))
        self.assertIn("skola", variants)

    def test_handwriting_set_differs_from_print(self):
        self.assertNotEqual(CONFUSIONS["fraktur"], CONFUSIONS["kurrent"])
        self.assertIn("zeme", candidates_for("zemn", ("kurrent",)))

    def test_two_edits_are_bounded(self):
        one = candidates_for("cilvnks", ("common", "kurrent"), 1)
        two = candidates_for("cilvnks", ("common", "kurrent"), 2)
        self.assertGreater(len(two), len(one))
        self.assertLess(len(two), 20000)


class TestCorrection(unittest.TestCase):
    def test_fraktur_long_s_read_as_f(self):
        report = correct_text("Rigaf latviefu biedriba un fkola")
        self.assertEqual(report.text, "Rigas latviesu biedriba un skola")
        self.assertEqual(len(report.corrections), 3)

    def test_kurrent_handwriting_errors(self):
        report = correct_text("zemn un cilvnks raksta vnstuli", handwriting=True)
        self.assertEqual(report.text, "zeme un cilveks raksta vestuli")

    def test_known_words_are_left_alone(self):
        text = "Šodien Rīgā notika biedrības sapulce."
        self.assertEqual(correct_text(text).text, text)

    def test_proper_names_are_not_mangled(self):
        report = correct_text("Blaumanis un Poruks")
        self.assertIn("Blaumanis", report.text)
        self.assertIn("Blaumanis", report.unresolved)

    def test_place_names_are_known(self):
        report = correct_text("Mitau un Wenden")
        self.assertEqual(report.text, "Mitau un Wenden")
        self.assertEqual(report.corrections, [])

    def test_capitalisation_is_preserved(self):
        fixed, _ = correct_word("Fkola", default_lexicon())
        self.assertEqual(fixed, "Skola")

    def test_correction_requires_dictionary_support(self):
        lex = Lexicon.from_lines(["skola"])
        # "xyzzy" nav neviena varianta vārdnīcā -> paliek neskarts
        self.assertEqual(correct_word("xyzzy", lex)[0], "xyzzy")

    def test_short_words_are_skipped(self):
        self.assertEqual(correct_word("uf", default_lexicon())[0], "uf")

    def test_report_lists_alternatives(self):
        report = correct_text("zemn", handwriting=True)
        self.assertTrue(report.corrections[0].alternatives)

    def test_suspicious_words_do_not_rewrite(self):
        found = suspicious_words("Rigaf Blaumanis")
        words = {f["vārds"] for f in found}
        self.assertIn("Blaumanis", words)
        rigaf = next(f for f in found if f["vārds"] == "Rigaf")
        self.assertIn("Rigas", rigaf["iespējamie_labojumi"])


class TestEngines(unittest.TestCase):
    def test_engine_report_shape(self):
        engines = available_engines()
        self.assertIn("agent", engines)
        self.assertTrue(engines["agent"]["pieejams"])
        self.assertFalse(engines["tesseract"]["rokrakstam"])
        for name, info in engines.items():
            self.assertIn("apraksts", info, name)

    def test_agent_engine_needs_no_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "lapa.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
            packet = prepare_for_agent(image, workdir=tmp, handwriting=True)
        self.assertEqual(packet["uzdevums"], "nolasi_attēlu")
        self.assertTrue(packet["rokraksts"])
        self.assertEqual(packet["uzvedne"], TRANSCRIPTION_PROMPT)
        self.assertTrue(packet["norādes"])
        self.assertEqual(packet["oriģināls"], str(image))

    def test_prompt_demands_no_modernisation(self):
        self.assertIn("NEPĀRRAKSTI mūsdienu rakstībā", TRANSCRIPTION_PROMPT)
        self.assertIn("Kurrent", TRANSCRIPTION_PROMPT)

    def test_missing_image(self):
        with self.assertRaises(RecognitionError):
            recognize("/nav/tada/faila.png")

    def test_unknown_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "a.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n")
            with self.assertRaises(RecognitionError):
                recognize(image, engine="nav-tada", preprocess=False)


class TestPostprocess(unittest.TestCase):
    def test_recognised_text_goes_through_latvian_pipeline(self):
        result = RecognitionResult(
            text="Rigaf latviefu Beedriba ſchodeen ſapulzejahs.", engine="test"
        )
        payload = postprocess(result)
        self.assertIn("labojumi", payload)
        self.assertIn("Rigas", payload["teksts"])
        self.assertEqual(payload["analīze"]["orthography"], "veca")
        self.assertIn("Biedriba", payload["analīze"]["text_modern"])

    def test_correction_can_be_disabled(self):
        result = RecognitionResult(text="Rigaf latviefu", engine="test")
        payload = postprocess(result, correct=False)
        self.assertNotIn("labojumi", payload)
        self.assertEqual(payload["teksts"], "Rigaf latviefu")


class TestImages(unittest.TestCase):
    def test_capabilities_report(self):
        caps = images_mod.capabilities()
        self.assertIn("pillow", caps)
        self.assertIn("tesseract", caps)

    def test_media_type_detection(self):
        self.assertEqual(images_mod.media_type_for("a.jpg"), "image/jpeg")
        self.assertEqual(images_mod.media_type_for("a.tif"), "image/tiff")

    def test_base64_encoding_works_without_pillow(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n")
            data, media = images_mod.encode_base64(path)
        self.assertEqual(media, "image/png")
        self.assertNotIn("\n", data)

    @unittest.skipUnless(images_mod.capabilities()["pillow"], "Pillow nav uzstādīts")
    def test_preprocess_roundtrip(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "page.png"
            Image.new("L", (400, 200), color=255).save(src)
            result = images_mod.preprocess(src, Path(tmp) / "out.png")
        self.assertTrue(result.path.exists())
        self.assertTrue(result.steps)

    def test_preprocess_without_pillow_raises_actionable_error(self):
        if images_mod.capabilities()["pillow"]:
            self.skipTest("Pillow ir uzstādīts")
        with self.assertRaises(images_mod.ImagingUnavailable) as ctx:
            images_mod.preprocess("a.png")
        self.assertIn("pip install", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
