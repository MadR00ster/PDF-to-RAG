"""Figures from extraction to get_figure."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _support import SCRIPTS, WS, run, figure_fixture, words  # noqa: E402


class FigureTest(unittest.TestCase):
    """Figures: extracted, tied to sections on evidence, OCR'd, and served."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.tmp = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_13_figures_are_extracted_linked_and_served(self):
        """Figures come out of the PDF, attach to the section carrying their
        caption, and reach an agent as images -- without the pass touching a
        byte the converter wrote, and changing nothing when run twice.
        """
        corpus = self.tmp / "FigCorpus"
        (corpus / "docs").mkdir(parents=True)
        pdf = corpus / "widget-figures.pdf"
        figure_fixture(pdf)
        r = run("convert_manual.py", str(pdf), "--title", "Widget Figures", "--slug", "figs")
        self.assertEqual(r.returncode, 0, r.stderr)
        slug_dir = corpus / "docs" / "figs"
        converted = {p: p.read_bytes() for p in slug_dir.rglob("*") if p.is_file()}

        r = run("extract_figures.py", str(corpus))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for p, data in converted.items():
            self.assertEqual(p.read_bytes(), data, f"figure extraction rewrote {p.name}")

        figures_json = slug_dir / "figures.json"
        figures = json.loads(figures_json.read_text(encoding="utf-8"))["figures"]
        self.assertEqual([f["caption"] for f in figures],
                         ["Figure 1. Widget Wiring Diagram", "Figure 2. Front Panel Lamps"])
        for f in figures:
            self.assertEqual(f["link"], "caption", f"{f['caption']} was not tied by its caption")
            section = (slug_dir / f["section"]).read_text(encoding="utf-8")
            self.assertIn(words(f["caption"]), words(section),
                          f"{f['caption']} tied to a section that does not carry it")
            self.assertEqual((slug_dir / f["file"]).read_bytes()[:8], b"\x89PNG\r\n\x1a\n",
                             f"{f['file']} is not a PNG")
        self.assertIn("WIDGET", figures[0]["labels"] or "", "the vector figure's drawn labels were lost")

        before = figures_json.read_bytes()
        r = run("extract_figures.py", str(corpus))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(figures_json.read_bytes(), before, "a second extraction changed figures.json")

        db = corpus / "index.sqlite3"
        r = run("build_search_db.py", "--root", str(corpus), "--out", str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = run("mcp_smoke_test.py", "--db", str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PASS  get_figure", r.stdout, "the smoke test never reached get_figure")
        self.assertIn("PASS  get_page_image", r.stdout, "the smoke test never reached get_page_image")

    def test_14_ocr_makes_a_raster_figures_words_searchable(self):
        """A raster figure's words are pixels: only OCR puts them in the index.

        Runs on a copy of the shared figure corpus. The vector figure already has drawn labels and
        must be left alone; the raster one's "RESET LAMP" must reach search.
        """
        sys.path.insert(0, str(SCRIPTS))
        from ocr_figures import find_tessdata
        if not find_tessdata():
            self.skipTest("no Tesseract language data here; OCR cannot run")
        corpus = WS.fresh(WS.fig_corpus(), "ocr")
        r = run("ocr_figures.py", str(corpus))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        figures = json.loads((corpus / "docs" / "figs" / "figures.json").read_text(encoding="utf-8"))["figures"]
        self.assertNotIn("ocr", figures[0], "OCR ran on a figure that already had drawn labels")
        self.assertIn("RESET", (figures[1].get("ocr") or "").upper(), "OCR did not read the raster figure")

        db = self.tmp / "figocr.sqlite3"
        r = run("build_search_db.py", "--root", str(corpus), "--out", str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        import sqlite3
        con = sqlite3.connect(db)
        hit = con.execute("SELECT file FROM chunks WHERE chunks MATCH 'figures:reset'").fetchone()
        con.close()
        self.assertTrue(hit, "the OCR text never reached the search index")

    def test_15_a_failed_ocr_read_is_left_to_retry(self):
        """Tesseract failing on a figure must not be recorded as reading nothing.

        The pass skips any figure that already carries an `ocr` field, so an
        empty string written after a failure makes that figure unsearchable for
        good -- and the run would still exit 0, which is how a corpus ends up
        with a hole nobody sees.
        """
        corpus = WS.fresh(WS.fig_corpus(), "ocr-retry")
        index = corpus / "docs" / "figs" / "figures.json"
        data = json.loads(index.read_text(encoding="utf-8"))
        for f in data["figures"]:
            f.pop("ocr", None)               # back to "never read"
        index.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        broken = self.tmp / "broken-tessdata"
        broken.mkdir(exist_ok=True)
        (broken / "eng.traineddata").write_bytes(b"not a model")
        r = run("ocr_figures.py", str(corpus), "--tessdata", str(broken))

        self.assertNotEqual(r.returncode, 0, "a pass that read nothing reported success")
        after = json.loads(index.read_text(encoding="utf-8"))["figures"]
        self.assertFalse([f for f in after if "ocr" in f],
                         "a figure Tesseract could not read was recorded as read")


if __name__ == "__main__":
    unittest.main(verbosity=2)
