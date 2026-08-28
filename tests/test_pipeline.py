#!/usr/bin/env python3
"""Regression tests for the conversion pipeline.

Run:  python tests/test_pipeline.py

Plain unittest, no pytest, no fixtures checked in: the PDFs are generated with
PyMuPDF so the ground truth (bookmark TOC, command names, page numbers) is known
exactly rather than asserted against whatever a real manual happens to contain.

What is covered is deliberately the class of thing that has actually broken
here, not a coverage percentage:

  * idempotency -- enrich_chunks must change nothing on a second run. A
    breadcrumb detector keyed on the separator once re-prepended a crumb every
    run, and the title-only fallback reintroduced exactly that risk.
  * no metadata downgrade -- a pass that can only add information must not
    overwrite better information already there. This is what protects the
    page-accurate entity breadcrumbs rebuild_reference.py writes.
  * entity attribution -- the command level is found from the TOC, and its
    >=20-entry threshold means a small document silently attributes nothing.
  * the output contract -- build_index.py and build_search_db.py read what the
    converters write, so a manifest field rename breaks retrieval, not a test.
  * optional-dependency gating -- convert_docling.py must fail with
    instructions, not a traceback, when Docling is absent.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import pymupdf
except ImportError:
    sys.exit("Tests need pymupdf. Run: pip install -r scripts/requirements.txt")

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
PY = sys.executable

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(SCRIPTS / script), *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def write_pdf(path: Path, pages: list[list[tuple[str, int]]], toc: list) -> None:
    """Build a PDF from [(text, fontsize), ...] per page, plus a bookmark TOC.

    Font size carries the heading signal, because that is what pymupdf4llm
    infers structure from -- the fixture has to exercise the same weakness the
    real documents do.
    """
    doc = pymupdf.open()
    for blocks in pages:
        page = doc.new_page()
        y = 72
        for text, size in blocks:
            page.insert_text((72, y), text, fontsize=size)
            y += size + 10
        page.insert_text((72, 760), "Feedback", fontsize=8)   # page furniture
    doc.set_toc(toc)
    doc.save(str(path))
    doc.close()


def prose_fixture(path: Path) -> None:
    body = [("This section explains the widget and how to seat it correctly.", 11),
            ("Torque the bolts evenly to avoid warping the bracket.", 11)]
    pages, toc = [], []
    chapters = [("Installation", ["Unpacking", "Mounting"]),
                ("Calibration", ["First Run", "Drift"])]
    page_no = 1
    for chap, sections in chapters:
        pages.append([(chap, 22)] + body)
        toc.append([1, chap, page_no])
        page_no += 1
        for sec in sections:
            pages.append([(sec, 16)] + body)
            toc.append([2, sec, page_no])
            page_no += 1
    write_pdf(path, pages, toc)


def reference_fixture(path: Path, n_commands: int) -> None:
    """A command dictionary: one entry per command, listed at TOC level 1."""
    pages, toc = [], []
    for i in range(n_commands):
        name = f"set_widget_option_{i:02d}"
        pages.append([(name, 20),
                      ("SYNTAX", 14), (f"{name} -value <int>", 11),
                      ("ARGUMENTS", 14), ("-value  the value to set", 11)])
        toc.append([1, name, len(pages)])
    write_pdf(path, pages, toc)


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.corpus = cls.tmp / "Corpus"
        (cls.corpus / "docs").mkdir(parents=True)
        cls.prose_pdf = cls.corpus / "widget-guide.pdf"
        cls.ref_pdf = cls.corpus / "widget-commands.pdf"
        cls.small_ref_pdf = cls.corpus / "tiny-commands.pdf"
        prose_fixture(cls.prose_pdf)
        reference_fixture(cls.ref_pdf, 25)      # clears the >=20 command threshold
        reference_fixture(cls.small_ref_pdf, 8)  # deliberately below it

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def manifest(self, slug: str) -> dict:
        return json.loads((self.corpus / "docs" / slug / "manifest.json").read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- prose

    def test_01_prose_conversion_contract(self):
        r = run("convert_manual.py", str(self.prose_pdf), "--title", "Widget Guide", "--slug", "prose")
        self.assertEqual(r.returncode, 0, r.stderr)
        m = self.manifest("prose")
        for key in ("source_pdf", "title", "slug", "page_count", "toc", "sections", "full_md_chars"):
            self.assertIn(key, m, f"manifest lost the '{key}' field that downstream tools read")
        self.assertTrue(m["sections"], "no sections produced")
        self.assertEqual(len(m["toc"]), 6, "bookmark TOC not carried into the manifest")
        for s in m["sections"]:
            f = self.corpus / "docs" / "prose" / s["file"]
            self.assertTrue(f.is_file(), f"manifest names a missing file: {s['file']}")
            self.assertEqual(s["chars"], len(f.read_text(encoding="utf-8")),
                             "manifest 'chars' disagrees with the file on disk")

    def test_02_enrich_is_idempotent(self):
        first = run("enrich_chunks.py", str(self.corpus))
        self.assertEqual(first.returncode, 0, first.stderr)
        second = run("enrich_chunks.py", str(self.corpus))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("0 changed", second.stdout,
                      "second enrich run changed something; the pass is not idempotent")

    def test_03_enrich_gives_every_chunk_a_breadcrumb(self):
        m = self.manifest("prose")
        missing = [s["file"] for s in m["sections"] if not s.get("breadcrumb")]
        self.assertFalse(missing, f"chunks left with no attribution: {missing[:3]}")

    def test_04_enrich_never_downgrades_a_richer_breadcrumb(self):
        """The guard that protects rebuild_reference.py's entity breadcrumbs."""
        slug_dir = self.corpus / "docs" / "prose"
        m = self.manifest("prose")
        target = slug_dir / m["sections"][0]["file"]
        rich = "*Widget Guide › Installation › Unpacking*"
        body = target.read_text(encoding="utf-8").split("\n")
        body[0] = rich
        target.write_text("\n".join(body), encoding="utf-8")

        r = run("enrich_chunks.py", str(self.corpus))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(target.read_text(encoding="utf-8").split("\n")[0], rich,
                         "a richer breadcrumb naming ancestors was overwritten")

    # ------------------------------------------------------------ reference

    def test_05_reference_attributes_entities(self):
        r = run("rebuild_reference.py", str(self.ref_pdf),
                "--title", "Widget Commands", "--slug", "ref")
        self.assertEqual(r.returncode, 0, r.stderr)
        m = self.manifest("ref")
        attributed = [s for s in m["sections"] if s.get("command")]
        self.assertTrue(attributed, "no chunk was attributed to a command")
        share = len(attributed) / len(m["sections"])
        self.assertGreater(share, 0.8, f"only {share:.0%} of chunks attributed")
        for s in attributed:
            self.assertIsNotNone(s.get("page_start"), "attributed chunk has no page")

    def test_06_small_reference_declines_rather_than_guessing(self):
        """Below the command-level threshold it must attribute nothing.

        Silently attributing from too little evidence is worse than declining:
        every miss inherits the previous command. A 60-page benchmark slice hit
        exactly this and reported 0% for a manual that really achieves 90%+.
        """
        r = run("rebuild_reference.py", str(self.small_ref_pdf),
                "--title", "Tiny", "--slug", "tiny")
        self.assertEqual(r.returncode, 0, r.stderr)
        m = self.manifest("tiny")
        self.assertFalse([s for s in m["sections"] if s.get("command")],
                         "attributed commands from below-threshold evidence")

    # ----------------------------------------------------------- pre-flight

    def test_07_pick_extractor_classifies_both_shapes(self):
        r = run("pick_extractor.py", str(self.prose_pdf), str(self.ref_pdf))
        self.assertEqual(r.returncode, 0, r.stderr)
        prose_block = r.stdout.split("widget-guide.pdf")[1].split("widget-commands.pdf")[0]
        ref_block = r.stdout.split("widget-commands.pdf")[1]
        self.assertIn("shape: prose", prose_block)
        self.assertIn("shape: reference", ref_block)
        self.assertIn("rebuild_reference.py", ref_block)

    # ------------------------------------------------------------ downstream

    def test_08_index_and_search_db_consume_the_output(self):
        r = run("build_index.py", str(self.corpus))
        self.assertEqual(r.returncode, 0, r.stderr)
        index = json.loads((self.corpus / "docs" / "index.json").read_text(encoding="utf-8"))
        self.assertTrue(index.get("manuals"), "index.json has no manuals")

        db = self.tmp / "index.sqlite3"
        r = run("build_search_db.py", "--root", str(self.tmp), "--out", str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(db.is_file(), "search index not written")

        import sqlite3
        con = sqlite3.connect(db)
        chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        nocrumb = con.execute("SELECT COUNT(*) FROM chunks WHERE breadcrumb = ''").fetchone()[0]
        con.close()
        self.assertGreater(chunks, 0, "no chunks indexed")
        self.assertEqual(nocrumb, 0, "chunks reached the index with no attribution")

    # ---------------------------------------------------- optional dependency

    def test_09_docling_path_gates_cleanly(self):
        try:
            import docling  # noqa: F401
            self.skipTest("Docling is installed; the missing-dependency path cannot run here")
        except ImportError:
            pass
        r = run("convert_docling.py", str(self.prose_pdf), "--title", "x", "--slug", "dl")
        self.assertNotEqual(r.returncode, 0, "should refuse without Docling")
        out = r.stdout + r.stderr
        self.assertIn("pip install docling", out, "no install instruction")
        self.assertIn("convert_manual.py", out, "did not point at the light path")
        self.assertNotIn("Traceback", out, "gated with a traceback instead of a message")


if __name__ == "__main__":
    unittest.main(verbosity=2)
