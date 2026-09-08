"""ALTO / METS / TEI parsēšanas testi uz reālistiskiem paraugiem."""

import unittest
from pathlib import Path

from periodika.alto import (
    assemble_articles,
    order_blocks_by_columns,
    parse_alto,
    parse_mets,
    parse_tei,
)

FIXTURES = Path(__file__).parent / "fixtures"


class TestAlto(unittest.TestCase):
    def setUp(self):
        self.page = parse_alto((FIXTURES / "alto_page1.xml").read_bytes())

    def test_page_attributes(self):
        self.assertEqual(self.page.id, "PAGE1")
        self.assertEqual(self.page.number, 1)
        self.assertEqual((self.page.width, self.page.height), (4000, 6000))

    def test_blocks_and_lines(self):
        self.assertEqual([b.id for b in self.page.blocks],
                         ["P1_TB00001", "P1_TB00002", "P1_TB00003"])
        self.assertEqual(len(self.page.blocks[0].lines), 2)

    def test_hyphenated_word_is_rejoined_once(self):
        text = self.page.blocks[0].text
        self.assertIn("Beedriba", text)
        self.assertNotIn("Beed-", text)
        self.assertEqual(text.count("Beedriba"), 1)

    def test_confidence_is_averaged(self):
        conf = self.page.blocks[0].confidence
        self.assertIsNotNone(conf)
        self.assertTrue(0.6 < conf < 1.0)

    def test_composed_block_parent_is_recorded(self):
        self.assertEqual(self.page.blocks[0].parent_id, "CB1")
        self.assertEqual(self.page.blocks[2].parent_id, "")

    def test_blocks_between_returns_range(self):
        blocks = self.page.blocks_between("P1_TB00001", "P1_TB00002")
        self.assertEqual([b.id for b in blocks], ["P1_TB00001", "P1_TB00002"])

    def test_missing_block_id_is_not_an_error(self):
        self.assertEqual(self.page.blocks_between("NAV", "NAV"), [])

    def test_column_reading_order(self):
        ordered = order_blocks_by_columns(self.page.blocks, self.page.width, columns=4)
        # pirmās slejas bloki nāk pirms otrās slejas
        self.assertEqual([b.id for b in ordered][:2], ["P1_TB00001", "P1_TB00002"])
        self.assertEqual(ordered[-1].id, "P1_TB00003")


class TestMets(unittest.TestCase):
    def setUp(self):
        self.mets = parse_mets((FIXTURES / "mets_issue.xml").read_bytes())
        self.pages = {"ALTO_0001": parse_alto((FIXTURES / "alto_page1.xml").read_bytes())}

    def test_file_groups(self):
        self.assertEqual([f.id for f in self.mets.alto_files()], ["ALTO_0001"])
        self.assertEqual(self.mets.files["ALTO_0001"].href, "alto/0001.xml")

    def test_metadata_from_mods(self):
        self.assertEqual(self.mets.metadata.get("title"), "Baltijas Wehſtneſis")
        self.assertEqual(self.mets.metadata.get("date"), "1899-05-01")

    def test_physical_page_order(self):
        self.assertEqual(self.mets.page_order().get("ALTO_0001"), 1)

    def test_articles_are_split_by_logical_structure(self):
        articles = assemble_articles(self.mets, self.pages)
        self.assertEqual([a.id for a in articles], ["DIVL2", "DIVL3"])
        first, second = articles
        self.assertEqual(first.title, "Rihgas Latweeschu Beedriba")
        self.assertIn("Beedriba", first.text)
        self.assertIn("ſapulzejahs", first.text)
        # sludinājums nedrīkst ielīst rakstā
        self.assertNotIn("Sludinajumi", first.text)
        self.assertIn("Sludinajumi", second.text)
        self.assertEqual(first.page_numbers, [1])

    def test_type_filter(self):
        only_ads = assemble_articles(self.mets, self.pages, include_types=["ADVERTISEMENT"])
        self.assertEqual([a.id for a in only_ads], ["DIVL3"])

    def test_fallback_to_pages_without_logical_map(self):
        mets = parse_mets(b'<mets xmlns="http://www.loc.gov/METS/"><fileSec/></mets>')
        articles = assemble_articles(mets, self.pages)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].type, "PAGE")
        self.assertIn("Sludinajumi", articles[0].text)


class TestTei(unittest.TestCase):
    def test_articles_from_tei_divs(self):
        articles = parse_tei((FIXTURES / "tei_issue.xml").read_bytes())
        self.assertEqual([a.id for a in articles], ["DIVL75", "DIVL76"])
        self.assertEqual(articles[0].title, "Weſtis no Widſemes")
        self.assertIn("Otrā rindkopa", articles[0].text)
        self.assertEqual(articles[0].metadata.get("title"), "Mahjas Weesis")


class TestRobustness(unittest.TestCase):
    def test_control_characters_do_not_break_parsing(self):
        raw = (FIXTURES / "alto_page1.xml").read_bytes().replace(b"Rihgas", b"Rih\x0bgas")
        page = parse_alto(raw)
        self.assertTrue(page.blocks)

    def test_alto_v2_namespace(self):
        raw = (FIXTURES / "alto_page1.xml").read_bytes().replace(
            b"ns-v3#", b"ns-v2#"
        )
        page = parse_alto(raw)
        self.assertEqual(len(page.blocks), 3)


if __name__ == "__main__":
    unittest.main()
