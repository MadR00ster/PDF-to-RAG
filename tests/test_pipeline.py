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
    overwrite better information already there, whichever branch it takes.
    This is what protects the page-accurate entity breadcrumbs
    rebuild_reference.py writes.
  * entity attribution -- the command level is found from the TOC, and its
    >=20-entry threshold means a small document silently attributes nothing.
    A chapter that is not a command must end the command before it, or the
    last command in a chapter owns the appendices.
  * the output contract -- build_index.py and build_search_db.py read what the
    converters write, so a manifest field rename breaks retrieval, not a test.
  * the server -- driven over stdio as an editor drives it, including the
    AND->OR fallback on an identifier, which reaches FTS5 as a phrase.
  * figures -- extracted from the PDF without touching what the converter
    wrote, tied to a section only by evidence, and served as images. A figure
    OCR could not read stays unread, so a rerun retries it.
  * optional-dependency gating -- convert_docling.py must fail with
    instructions, not a traceback, when Docling is absent.
  * the contract checker -- check_corpus.py passes the pipeline's own output,
    passes a document written by hand to the contract with warnings counted,
    and catches each defect it claims to when that defect is planted alone.
  * editions -- a PDF dropped in new_docs/ is filed under source/ with its
    release in the manifest, a second release of the same filename does not
    overwrite the first, search answers from one edition per manual, a version
    that is not there is refused rather than approximated, a cover that says
    "and later" answers for later releases and no other does, and a
    comparison reads the whole entry, past the point where a lookup is cut.
"""
from __future__ import annotations

import json
import re
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


def inverted_fixture(path: Path) -> None:
    """A preface set larger than the chapter after it, as real manuals do.
    The chapter heading comes out as ### inside the preface's chunk, so a walk
    by heading level keeps the preface as the parent of the whole chapter."""
    pages, toc = [], []
    for title, size, level in [("About This Guide", 22, 1), ("Normalized Formats", 14, 1),
                               ("Data Loading", 16, 2), ("Parametric Data", 16, 2)]:
        pages.append([(title, size),
                      (f"This part explains {title.lower()} and how its pages are laid out.", 11)])
        toc.append([level, title, len(pages)])
    write_pdf(path, pages, toc)


def command_page(name: str) -> list[tuple[str, int]]:
    return [(name, 20),
            ("SYNTAX", 14), (f"{name} -value <int>", 11),
            ("ARGUMENTS", 14), ("-value  the value to set", 11)]


def reference_fixture(path: Path, n_commands: int) -> None:
    """A command dictionary: one entry per command, listed at TOC level 1."""
    pages, toc = [], []
    for i in range(n_commands):
        name = f"set_widget_option_{i:02d}"
        pages.append(command_page(name))
        toc.append([1, name, len(pages)])
    write_pdf(path, pages, toc)


def nested_reference_fixture(path: Path, n_commands: int) -> None:
    """Commands at TOC level 2 under a chapter, then chapters that are not
    commands -- the tshell-ref shape, with an appendix and a licence after the
    last entry. Neither belongs to any command."""
    pages = [[("Command Reference", 22), ("This chapter lists every widget command.", 11)]]
    toc = [[1, "Command Reference", 1]]
    for i in range(n_commands):
        name = f"set_widget_option_{i:02d}"
        pages.append(command_page(name))
        toc.append([2, name, len(pages)])
    trailing = [("Appendix A Troubleshooting", "If the widget will not power on, check the fuse."),
                ("End-User License Agreement", "You may not redistribute this software.")]
    for chapter, text in trailing:
        pages.append([(chapter, 22), (text, 11)])
        toc.append([1, chapter, len(pages)])
    write_pdf(path, pages, toc)


def figure_fixture(path: Path) -> None:
    """Two captioned figures of the two kinds real manuals mix: a vector block
    diagram whose labels are real text (Synopsys draws its figures) and a
    raster image whose words are only pixels (Tessent embeds them)."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Wiring", fontsize=20)
    page.insert_text((72, 104), "The widget connects to the controller through the harness shown below.", fontsize=11)
    page.insert_text((72, 124), "Seat every connector before applying power to the assembly.", fontsize=11)
    page.insert_text((72, 160), "Figure 1. Widget Wiring Diagram", fontsize=10)
    shape = page.new_shape()
    for i in range(3):
        shape.draw_rect(pymupdf.Rect(90 + i * 160, 200, 200 + i * 160, 270))
    for i in range(2):
        x = 200 + i * 160
        shape.draw_line((x, 235), (x + 50, 235))
        shape.draw_polyline([(x + 42, 230), (x + 50, 235), (x + 42, 240)])
    shape.draw_circle((305, 320), 22)
    shape.draw_line((305, 270), (305, 298))
    shape.finish(color=(0, 0, 0), width=1.2)
    shape.commit()
    for i, name in enumerate(("CPU", "BUS", "WIDGET")):
        page.insert_text((105 + i * 160, 240), name, fontsize=10)
    page.insert_text((292, 324), "PSU", fontsize=9)
    page.insert_text((72, 380), "After wiring, continue with calibration as the next chapter describes.", fontsize=11)

    page = doc.new_page()
    page.insert_text((72, 72), "Front Panel", fontsize=20)
    page.insert_text((72, 104), "The front panel carries the status lamps and the reset switch.", fontsize=11)
    art = pymupdf.open()
    canvas = art.new_page(width=360, height=200)
    canvas.draw_rect(canvas.rect, color=None, fill=(0.88, 0.91, 0.94))
    for i, rgb in enumerate([(0.8, 0.15, 0.15), (0.15, 0.6, 0.25), (0.95, 0.8, 0.15)]):
        canvas.draw_rect(pymupdf.Rect(40 + i * 100, 30, 100 + i * 100, 90), color=None, fill=rgb)
    canvas.insert_text((60, 150), "RESET LAMP", fontsize=30)
    page.insert_image(pymupdf.Rect(110, 130, 470, 330), pixmap=canvas.get_pixmap(dpi=144))
    page.insert_text((72, 350), "Figure 2. Front Panel Lamps", fontsize=10)
    page.insert_text((72, 390), "Press reset for two seconds to clear a fault.", fontsize=11)
    doc.set_toc([[1, "Wiring", 1], [1, "Front Panel", 2]])
    doc.save(str(path))
    doc.close()


def handmade_document(collection: Path, slug: str = "hand") -> None:
    """A document written the way any converter might write one, with no
    script from this repo involved, so check_corpus.py is tested on the
    contract alone. Six pages with their own vocabulary, one chunk per page,
    two chapters' worth of TOC nesting per pair."""
    toc = [[1, "Alpha", 1], [2, "Alpha Setup", 2], [1, "Beta", 3],
           [2, "Beta Tuning", 4], [1, "Gamma", 5], [2, "Gamma Limits", 6]]
    doc_dir = collection / "docs" / slug
    (doc_dir / "sections").mkdir(parents=True)
    pdf = pymupdf.open()
    sections, texts, chapter = [], [], None
    for n, (level, heading, page_no) in enumerate(toc, start=1):
        vocab = [f"{heading.split()[0].lower()}{n}term{chr(97 + i % 26)}{chr(97 + i * 7 % 26)}"
                 for i in range(36)]
        lines = [" ".join(vocab[i:i + 6]) for i in range(0, len(vocab), 6)]
        page = pdf.new_page()
        page.insert_text((72, 72), heading, fontsize=18)
        for i, line in enumerate(lines):
            page.insert_text((72, 110 + i * 18), line, fontsize=11)
        chapter = heading if level == 1 else chapter
        crumb = " › ".join(["Hand Guide", chapter] + ([heading] if level == 2 else []))
        text = f"*{crumb}*\n\n## {heading}\n\n" + "\n".join(lines) + "\n"
        name = f"sections/{n:03d}-{heading.lower().replace(' ', '-')}.md"
        (doc_dir / name).write_text(text, encoding="utf-8")
        texts.append(text)
        sections.append({"file": name, "heading": heading, "level": level, "chars": len(text),
                         "page_start": page_no, "page_end": page_no, "breadcrumb": crumb})
    pdf.set_toc(toc)
    pdf.save(str(collection / "hand.pdf"))
    pdf.close()
    full = "\n\n".join(texts)
    (doc_dir / "full.md").write_text(full, encoding="utf-8")
    manifest = {"source_pdf": "hand.pdf", "title": "Hand Guide", "slug": slug, "page_count": 6,
                "toc": [{"level": l, "title": t, "page": p} for l, t, p in toc],
                "sections": sections, "full_md_chars": len(full)}
    (doc_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def words(text: str) -> str:
    """Lowercase alphanumerics, so markdown emphasis cannot hide a caption."""
    return " ".join(re.findall(r"[0-9a-z]+", text.lower()))


def gadget_fixture(path: Path, version: str, sections: list[str]) -> None:
    """One release of a prose manual, its version printed on the cover the
    way a vendor prints it and nowhere in the filename."""
    pages = [[("Gadget Guide", 24), (f"Software Version {version}", 11)]]
    toc = [[1, "Gadget Guide", 1]]
    for sec in sections:
        pages.append([(sec, 18), (f"This section covers {sec.lower()} for the gadget in release {version}.", 11),
                      ("Keep the gadget dry and check the seals before every use.", 11)])
        toc.append([1, sec, len(pages)])
    write_pdf(path, pages, toc)


LONG_ENTRY = "set_widget_option_05"
LONG_LINES = 600      # at ~75 characters a line, well past the server's 40,000-character cut


def edition_reference_fixture(path: Path, numbers: list[int], last_line: str) -> None:
    """One release of a command reference. One entry runs to twenty pages, and
    `last_line` is its final line: the only place two releases' text differs."""
    pages, toc = [], []
    for i in numbers:
        name = f"set_widget_option_{i:02d}"
        toc.append([1, name, len(pages) + 1])
        if name != LONG_ENTRY:
            pages.append(command_page(name))
            continue
        lines = [f"Setting {n:03d} of the long option is described on this line in full detail."
                 for n in range(LONG_LINES)]
        lines[-1] = last_line
        for start in range(0, LONG_LINES, 30):
            pages.append(([(name, 20)] if start == 0 else []) + [(l, 11) for l in lines[start:start + 30]])
    write_pdf(path, pages, toc)


def load_script(name: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        cls.nested_ref_pdf = cls.corpus / "nested-commands.pdf"
        prose_fixture(cls.prose_pdf)
        reference_fixture(cls.ref_pdf, 25)      # clears the >=20 command threshold
        reference_fixture(cls.small_ref_pdf, 8)  # deliberately below it
        nested_reference_fixture(cls.nested_ref_pdf, 22)

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

    def test_04_enrich_replaces_a_parent_the_toc_does_not_give(self):
        """A breadcrumb enrich did not get from a converter is rewritten from
        the TOC, however many ancestors it names.

        This used to assert the opposite: never trade a breadcrumb naming
        ancestors for a title-only one. That guard dated from before converter
        breadcrumbs were marked in the manifest (test_12 covers those), and it
        would have kept every wrong parent the old heading walk wrote.
        """
        slug_dir = self.corpus / "docs" / "prose"
        m = self.manifest("prose")
        target = slug_dir / m["sections"][0]["file"]
        body = target.read_text(encoding="utf-8").split("\n")
        body[0] = "*Widget Guide › Installation › Unpacking*"   # Installation has no parent
        target.write_text("\n".join(body), encoding="utf-8")

        r = run("enrich_chunks.py", str(self.corpus))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(target.read_text(encoding="utf-8").split("\n")[0], "*Widget Guide*",
                         "a parent the TOC does not give survived")
        self.assertEqual(self.manifest("prose")["sections"][0]["breadcrumb"], "Widget Guide")

    def test_04b_enrich_takes_parents_from_the_toc_not_font_sizes(self):
        """The heading walk kept a larger-set preface as the parent of the
        chapter after it: measured against located pages, 14-23% of the
        parents it gave across 13 real manuals were right.

        First the shape of silicon-da-loc-data-intg, written by hand: heading
        levels from font size put the preface at 2 and the chapter's own
        opening chunk, marked (intro), at 4, so the walk never popped the
        preface. Then the same inversion end to end, where the small-set
        chapter heading lands inside the preface's chunk.
        """
        corpus = self.tmp / "SiliconCorpus"
        doc_dir = corpus / "docs" / "silicon"
        (doc_dir / "sections").mkdir(parents=True)
        shape = [("About This Guide", 2), ("1 Normalized File Formats (intro)", 4),
                 ("Data Loading Modes", 4), ("Example", 5), ("Parametric Data", 4),
                 ("Header", 5), ("Table", 6)]
        sections = []
        for n, (heading, level) in enumerate(shape, start=1):
            name = f"sections/{n:03d}.md"
            text = f"## {heading.replace(' (intro)', '')}\n\nText of {heading}.\n"
            (doc_dir / name).write_text(text, encoding="utf-8")
            sections.append({"file": name, "heading": heading, "level": level, "chars": len(text)})
        toc = [[1, "About This Guide", 1], [1, "1 Normalized File Formats", 2],
               [2, "Data Loading Modes", 3], [2, "Parametric Data", 4], [3, "Header", 4]]
        (doc_dir / "manifest.json").write_text(json.dumps({
            "source_pdf": "silicon.pdf", "title": "Data Integration Guide", "slug": "silicon",
            "page_count": 4, "toc": [{"level": l, "title": t, "page": p} for l, t, p in toc],
            "sections": sections}), encoding="utf-8")
        r = run("enrich_chunks.py", str(corpus))
        self.assertEqual(r.returncode, 0, r.stderr)
        crumbs = [s["breadcrumb"] for s in
                  json.loads((doc_dir / "manifest.json").read_text(encoding="utf-8"))["sections"]]
        self.assertFalse([c for c in crumbs if "About This Guide" in c],
                         f"the preface is still a parent: {crumbs}")
        self.assertEqual(crumbs[2], "Data Integration Guide › 1 Normalized File Formats")
        self.assertEqual(crumbs[5], "Data Integration Guide › 1 Normalized File Formats › Parametric Data")
        self.assertEqual(crumbs[3], "Data Integration Guide",
                         "a heading the TOC does not list inherited a parent")

        # The same entry matched again further on must not displace the chunk
        # that opens it: a sub-heading repeating its section's title, or a
        # passing mention before the section starts.
        import importlib.util
        spec = importlib.util.spec_from_file_location("enrich_chunks", SCRIPTS / "enrich_chunks.py")
        ec = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ec)
        two = [{"level": 1, "title": "Setup", "page": 1}, {"level": 2, "title": "Topic", "page": 2}]
        for texts in (["## Setup\n\nIntro.\n", "## Topic\n\nText.\n\n### Topic\n\nMore.\n"],
                      ["## Setup\n\nIntro.\n", "## Topic\n\nText.\n", "More.\n\n**Topic**\n\nLater.\n"],
                      ["## Setup\n\nSee below.\n\n**Topic**\n\nx\n", "## Topic\n\nText.\n"]):
            self.assertEqual(ec.build_breadcrumbs(texts, "Guide", two)[1], "Guide › Setup",
                             f"a repeated match displaced the opener: {texts}")

        corpus = self.tmp / "InvertedCorpus"
        (corpus / "docs").mkdir(parents=True)
        pdf = corpus / "formats.pdf"
        inverted_fixture(pdf)
        r = run("convert_manual.py", str(pdf), "--title", "Formats Guide", "--slug", "formats")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run("enrich_chunks.py", str(corpus))
        self.assertEqual(r.returncode, 0, r.stderr)

        m = json.loads((corpus / "docs" / "formats" / "manifest.json").read_text(encoding="utf-8"))
        crumbs = {s["heading"]: s["breadcrumb"] for s in m["sections"]}
        self.assertEqual(crumbs.get("Data Loading"), "Formats Guide › Normalized Formats",
                         f"parent not taken from the TOC: {crumbs}")
        self.assertEqual(crumbs.get("Parametric Data"), "Formats Guide › Normalized Formats")
        self.assertEqual(crumbs.get("About This Guide"), "Formats Guide")
        r = run("enrich_chunks.py", str(corpus))
        self.assertIn("0 changed", r.stdout, "a second run changed something")

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

    def test_06b_message_codes_are_entries(self):
        """An error-message catalogue (ADES-002, CMD-082) is a reference too.

        Its TOC titles have no underscore, so an identifier-only test found no
        command level and a 3,776-page catalogue came out with 0% attributed.
        """
        import importlib.util
        mods = {}
        for name in ("rebuild_reference", "pick_extractor"):
            spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
            mods[name] = importlib.util.module_from_spec(spec)
            sys.modules[name] = mods[name]
            spec.loader.exec_module(mods[name])
        toc = [[1, "Messages", 3], [2, "ADES", 3]] + [
            [3, f"ADES-{n:03d}", 3 + n // 2] for n in range(2, 40)]
        self.assertEqual(mods["rebuild_reference"].pick_command_level(toc), 3)
        looks = mods["pick_extractor"].looks_like_entry
        self.assertTrue(looks("CMD-082"))
        self.assertFalse(looks("Chapter 4"))
        self.assertFalse(looks("Getting Started"))

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

    def test_09b_docling_anchors_only_to_an_entry_on_its_pages(self):
        """A heading that matches a TOC title elsewhere in the document anchors
        nothing. Titles repeat ("Syntax" under every construct) and overviews
        name features chapters before their own section; the nearest-title
        rule labelled 114 production chunks with ancestry from up to 1,200
        pages away, as `anchored`. Runs without Docling: resolution is plain
        TOC and page arithmetic."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("convert_docling", SCRIPTS / "convert_docling.py")
        cd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cd)

        toc = [[1, "Commands", 1], [2, "alpha", 1], [3, "Syntax", 1], [2, "beta", 3],
               [3, "Syntax", 3], [1, "HyperGrid", 8], [2, "Setup", -1]]
        pdf = pymupdf.open()
        for _ in range(9):
            pdf.new_page()
        by_title: dict = {}
        for i, (_l, t, _p) in enumerate(toc):
            by_title.setdefault(cd.normalize(t), []).append(i)
        spans = cd.toc_spans(toc, pdf.page_count)
        toc_pos = cd.build_toc_positions(pdf, toc)

        def resolve(heading, pages):
            c = {"headings": [heading], "pages": pages, "text": ""}
            return cd.resolve_ancestors(c, toc, toc_pos, by_title, spans, pdf)

        self.assertEqual(resolve("Syntax", [4]), (["Commands", "beta"], "anchored"),
                         "the Syntax entry covering the page should anchor it")
        self.assertEqual(resolve("Syntax", [2]), (["Commands", "alpha"], "anchored"))
        ancestors, how = resolve("HyperGrid", [2])
        self.assertEqual(how, "page", "anchored to a same-titled entry six pages on")
        self.assertEqual(ancestors, ["Commands", "alpha", "Syntax"])
        self.assertEqual(resolve("Setup", [9])[1], "page", "anchored to a bookmark with no page")
        pdf.close()

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

    # ---------------------------------------------------------------- server

    def test_10_mcp_server_passes_its_smoke_test(self):
        """The server, driven over stdio the way an editor drives it.

        The smoke test includes the AND->OR fallback on an identifier. An
        identifier reaches FTS5 as a multi-word phrase, and the fallback used to
        rewrite the spaces inside it as well, turning "set widget option 00"
        into a phrase containing the word "or" that could never match.
        """
        db = self.tmp / "index.sqlite3"
        self.assertTrue(db.is_file(), "test_08 builds the index this test serves")
        r = run("mcp_smoke_test.py", "--db", str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("SKIP", r.stdout,
                         "the index has no entities, so the identifier probes never ran")

    # ------------------------------------------------------ entity boundaries

    def test_11_entity_regions_end_at_chapters(self):
        """Text that follows a command but is not one must belong to nothing.

        Regions used to end only where the next command began, so the last
        command in a chapter owned whatever came after it -- the next chapter,
        the appendices, the licence -- and lookup_entity served it all as that
        command's entry. Checked by what each chunk contains, not by the share
        of chunks carrying a label: coverage is not correctness.
        """
        r = run("rebuild_reference.py", str(self.nested_ref_pdf),
                "--title", "Widget Commands", "--slug", "nested")
        self.assertEqual(r.returncode, 0, r.stderr)
        slug_dir = self.corpus / "docs" / "nested"
        sections = self.manifest("nested")["sections"]
        for s in sections:
            body = (slug_dir / s["file"]).read_text(encoding="utf-8")
            if "fuse" in body or "redistribute" in body:
                self.assertIsNone(s["command"],
                                  f"{s['file']} is an appendix or licence, labelled {s['command']}")
            owner = re.search(r"(set_widget_option_\d+) -value", body)
            if owner:
                self.assertEqual(s["command"], owner.group(1),
                                 f"{s['file']} documents {owner.group(1)}")
        self.assertEqual({s["command"] for s in sections if s["command"]},
                         {f"set_widget_option_{i:02d}" for i in range(22)},
                         "bounding the regions cost a command its attribution")

    # ------------------------------------------------- breadcrumb provenance

    def test_12_enrich_leaves_converter_breadcrumbs_alone_in_walk_mode(self):
        """The no-downgrade guard used to hold for flat documents only.

        enrich_chunks takes the heading walk when a document's levels vary, and
        that branch refreshed every breadcrumb in place -- replacing the
        page-accurate `Title › command` rebuild_reference.py writes. Force the
        walk on real rebuild_reference output and check nothing moves.
        """
        corpus = self.tmp / "WalkCorpus"
        slug_dir = corpus / "docs" / "walkref"
        r = run("rebuild_reference.py", str(self.ref_pdf), "--title", "Widget Commands",
                "--slug", "walkref", "--out-root", str(corpus / "docs"))
        self.assertEqual(r.returncode, 0, r.stderr)

        manifest_path = slug_dir / "manifest.json"
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
        for i, s in enumerate(m["sections"]):
            s["level"] = 1 + i % 3      # modal level at ~1/3, so enrich walks
        manifest_path.write_text(json.dumps(m, indent=2), encoding="utf-8")

        def crumbs():
            cur = json.loads(manifest_path.read_text(encoding="utf-8"))
            return [(s["breadcrumb"],
                     (slug_dir / s["file"]).read_text(encoding="utf-8").split("\n")[0])
                    for s in cur["sections"]]

        before = crumbs()
        r = run("enrich_chunks.py", str(corpus))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"walk\s+walkref",
                         "enrich did not take the walk branch, so this proves nothing")
        self.assertEqual(crumbs(), before, "enrich rewrote breadcrumbs a converter wrote")

    # --------------------------------------------------------------- figures

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

        Runs on test_13's corpus. The vector figure already has drawn labels and
        must be left alone; the raster one's "RESET LAMP" must reach search.
        """
        sys.path.insert(0, str(SCRIPTS))
        from ocr_figures import find_tessdata
        if not find_tessdata():
            self.skipTest("no Tesseract language data here; OCR cannot run")
        corpus = self.tmp / "FigCorpus"
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
        corpus = self.tmp / "FigCorpus"      # built by test_13, OCR'd by test_14
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

    # --------------------------------------------------------- contract check

    def check(self, path: Path, *extra: str) -> tuple[subprocess.CompletedProcess, set[str]]:
        """Run check_corpus.py and return the names of the checks it raised."""
        out = self.tmp / "check.json"
        out.unlink(missing_ok=True)
        r = run("check_corpus.py", str(path), "--json", str(out), *extra)
        if not out.is_file():
            self.fail(f"check_corpus.py wrote no report:\n{r.stdout}{r.stderr}")
        report = json.loads(out.read_text(encoding="utf-8"))
        raised = {p["check"] for d in report["documents"] for p in d["problems"]}
        raised |= {p["check"] for ps in report["collections"].values() for p in ps}
        return r, raised

    def test_16_checker_accepts_what_the_pipeline_writes(self):
        """Everything the earlier tests converted, enriched and linked passes.

        Warnings are allowed here -- the prose path writes no page numbers, and
        test_11 added a document after test_08 built index.json -- failures
        are not.
        """
        r, raised = self.check(self.corpus)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("no-pages", raised, "the prose path's missing pages went unreported")

    def test_17_checker_accepts_a_handmade_document_strictly(self):
        """A document no script here wrote, following only the contract, passes
        with warnings counted as failures. This is the control for test_18:
        each planted defect below starts from exactly this."""
        corpus = self.tmp / "HandCorpus"
        handmade_document(corpus)
        r = run("build_index.py", str(corpus))
        self.assertEqual(r.returncode, 0, r.stderr)
        r, raised = self.check(corpus, "--strict")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(raised, f"a clean document raised {raised}")

    def test_18_checker_catches_planted_defects(self):
        """Each defect the checker claims to catch, planted one at a time.

        Most are ones that happened here: a breadcrumb naming the wrong parent,
        an owner from the wrong page, a crumb written twice by a pass that was
        not idempotent, a backup left in docs/, footers left in chunks.
        """
        import shutil
        clean = self.tmp / "HandCorpus"
        self.assertTrue(clean.is_dir(), "test_17 builds the document this test damages")

        def relabel(d, m, i, crumb):
            s = m["sections"][i]
            s["breadcrumb"] = crumb
            f = d / s["file"]
            lines = f.read_text(encoding="utf-8").split("\n")
            lines[0] = f"*{crumb}*"
            f.write_text("\n".join(lines), encoding="utf-8")
            s["chars"] = len("\n".join(lines))

        def wrong_owner(d, m):
            m["sections"][1]["command"] = "Gamma Limits"
            relabel(d, m, 1, "Hand Guide › Gamma › Gamma Limits")

        def unnested(d, m):
            for s in m["sections"]:
                s.pop("page_start"), s.pop("page_end")
            relabel(d, m, 3, "Hand Guide › Alpha › Beta Tuning")

        def doubled(d, m):
            f = d / m["sections"][2]["file"]
            f.write_text(f"*{m['sections'][2]['breadcrumb']}*\n\n" + f.read_text(encoding="utf-8"),
                         encoding="utf-8")

        def shifted(d, m):
            for s in m["sections"]:
                s["page_start"] = s["page_end"] = min(6, s["page_start"] + 2)

        def dropped(d, m):
            s = m["sections"][2]
            (d / s["file"]).write_text(f"*{s['breadcrumb']}*\n\n## Beta\n\nSee the next page.\n",
                                       encoding="utf-8")

        def feedback(d, m):
            f = d / m["sections"][1]["file"]
            f.write_text(f.read_text(encoding="utf-8") + "\n**Feedback**\n", encoding="utf-8")

        def backup(d, m):
            shutil.copytree(d, d.with_name("hand.old"))

        def stale_index(d, m):
            index = d.parent / "index.json"
            data = json.loads(index.read_text(encoding="utf-8"))
            data["manuals"][0]["title"] = "Hand Guide, First Edition"
            index.write_text(json.dumps(data), encoding="utf-8")

        plants = [
            # name, damage, check expected, whether it must fail the run
            ("parent from another chapter", lambda d, m: relabel(d, m, 1, "Hand Guide › Beta › Beta Tuning"),
             "ancestor-contradicts-pages", True),
            ("owner from another page", wrong_owner, "entity-contradicts-pages", True),
            ("parent that does not contain the child", unnested, "ancestor-not-nested", True),
            ("breadcrumb written twice", doubled, "breadcrumb-doubled", True),
            ("section file gone", lambda d, m: (d / m["sections"][3]["file"]).unlink(),
             "section-missing-file", True),
            ("page past the end", lambda d, m: m["sections"][5].update(page_end=9), "page-invalid", True),
            ("backup left in docs/", backup, "hidden-document", True),
            ("backup left in docs/", backup, "duplicate-slug", True),
            # Caught twice over: the PDF's words, and the TOC spans (which fail it).
            ("pages numbered two too high", shifted, "content-gap", True),
            ("pages numbered two too high", shifted, "ancestor-contradicts-pages", True),
            ("a page's text dropped", dropped, "content-gap", False),
            ("Feedback link left in", feedback, "furniture", False),
            # Present is not valid: each of these passed a key check once.
            ("slug set to null", lambda d, m: m.update(slug=None), "manifest-invalid-field", True),
            ("sections not a list", lambda d, m: m.update(sections={}), "manifest-invalid-field", True),
            ("section path leaving the document", lambda d, m: m["sections"][0].update(file="../../hand.pdf"),
             "section-bad-path", True),
            ("section path a number", lambda d, m: m["sections"][0].update(file=5), "section-bad-path", True),
            ("page count not the PDF's", lambda d, m: m.update(page_count=7), "page-count-mismatch", True),
            ("source PDF that will not open", lambda d, m: (d.parents[1] / "hand.pdf").write_bytes(b"not a pdf"),
             "content-unchecked", False),
            ("index.json title out of date", stale_index, "index-stale", False),
        ]
        for i, (name, damage, expected, fails) in enumerate(plants):
            with self.subTest(defect=name, check=expected):
                corpus = self.tmp / f"Planted{i}"
                shutil.copytree(clean, corpus)
                d = corpus / "docs" / "hand"
                m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
                damage(d, m)
                (d / "manifest.json").write_text(json.dumps(m, indent=2), encoding="utf-8")
                r, raised = self.check(corpus)
                self.assertIn(expected, raised, f"{name} went unreported; raised {raised}\n{r.stdout}")
                self.assertEqual(r.returncode, 1 if fails else 0, r.stdout)

    def test_19_checker_order_prefers_the_enclosing_entry(self):
        """A repeated title must not send the order check ahead to a later
        copy when the one enclosing the reading position fits. Breadcrumbs
        name parents only, so a chunk's deepest ancestor is often shallower
        than the last chunk's; resolving it to the next "Simulating the Design"
        1,300 entries on flagged 195 correct chunks in one manual."""
        corpus = self.tmp / "RepeatCorpus"
        doc_dir = corpus / "docs" / "rep"
        (doc_dir / "sections").mkdir(parents=True)
        toc = [[1, "Simulating", 1], [2, "Setup", 1], [3, "Options", 1], [2, "Runtime", 2],
               [3, "Tuning", 2], [1, "Reference", 3], [2, "Simulating", 3]]
        # Chunks opening with: Simulating, Setup, Options, Runtime, Tuning.
        crumbs = ["Guide", "Guide › Simulating", "Guide › Simulating › Setup",
                  "Guide › Simulating", "Guide › Simulating › Runtime"]
        sections = []
        for n, crumb in enumerate(crumbs, start=1):
            text = f"*{crumb}*\n\n## Part {n}\n\nText of part {n}.\n"
            name = f"sections/{n:03d}.md"
            (doc_dir / name).write_text(text, encoding="utf-8")
            sections.append({"file": name, "heading": f"Part {n}", "level": 2,
                             "chars": len(text), "breadcrumb": crumb})
        (doc_dir / "full.md").write_text("x", encoding="utf-8")
        (doc_dir / "manifest.json").write_text(json.dumps({
            "source_pdf": "rep.pdf", "title": "Guide", "slug": "rep", "page_count": 3,
            "toc": [{"level": l, "title": t, "page": p} for l, t, p in toc],
            "sections": sections}), encoding="utf-8")
        r, raised = self.check(corpus, "--no-pdf")
        self.assertNotIn("ancestor-out-of-order", raised, r.stdout)


class EditionsTest(unittest.TestCase):
    """Several releases of one manual in one collection."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.root = cls.tmp / "Root"
        cls.coll = cls.root / "Gadgets"
        for folder in ("new_docs", "source", "docs"):
            (cls.coll / folder).mkdir(parents=True)
        cls.db = cls.tmp / "editions.sqlite3"
        cls.server = load_script("mcp_server")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def manifest(self, slug: str) -> dict:
        return json.loads((self.coll / "docs" / slug / "manifest.json").read_text(encoding="utf-8"))

    def build(self) -> subprocess.CompletedProcess:
        return run("build_search_db.py", "--root", str(self.root), "--out", str(self.db))

    def serve(self):
        """Point the in-process server at the index, closed again afterwards:
        an open connection would stop the next rebuild replacing the file."""
        corpus = self.server.Corpus(self.db)
        self.server.CORPUS = corpus
        self.addCleanup(lambda: corpus._db and corpus._db.close())
        return self.server

    def test_20_a_pdf_from_new_docs_is_filed_under_source(self):
        """The inbox empties into source/, and the release comes off the cover.

        Vendors reuse a filename from one release to the next (ptug.pdf), so
        the second gadget.pdf must not overwrite the first: it takes its
        version on the end. The same file dropped in twice is not an edition.
        """
        inbox, source = self.coll / "new_docs", self.coll / "source"
        gadget_fixture(inbox / "gadget.pdf", "2025.1", ["Unpacking", "Mounting", "Legacy Mode"])
        r = run("convert_manual.py", str(inbox / "gadget.pdf"), "--title", "Gadget Guide")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        m = self.manifest("gadget-2025-1")
        self.assertEqual((m["doc_id"], m["version"], m["source_pdf"]), ("gadget", "2025.1", "gadget.pdf"))
        self.assertTrue((source / "gadget.pdf").is_file(), "the converted PDF was not moved to source/")
        self.assertFalse((inbox / "gadget.pdf").exists(), "the converted PDF is still in new_docs/")

        gadget_fixture(inbox / "gadget.pdf", "2026.1", ["Unpacking", "Mounting", "Cloud Sync"])
        r = run("convert_manual.py", str(inbox / "gadget.pdf"), "--title", "Gadget Guide")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        m = self.manifest("gadget-2026-1")
        self.assertEqual((m["doc_id"], m["version"], m["source_pdf"]), ("gadget", "2026.1", "gadget_2026.1.pdf"))
        self.assertTrue((source / "gadget_2026.1.pdf").is_file(), "the second release was not filed by version")
        self.assertEqual(self.manifest("gadget-2025-1")["source_pdf"], "gadget.pdf")
        first = pymupdf.open(str(source / "gadget.pdf"))
        self.assertIn("2025.1", first[0].get_text(), "the first release's PDF was overwritten")
        first.close()

        import shutil
        shutil.copy(source / "gadget.pdf", inbox / "again.pdf")
        r = run("convert_manual.py", str(inbox / "again.pdf"), "--title", "Gadget Guide")
        self.assertNotEqual(r.returncode, 0, "a PDF already filed was converted a second time")
        self.assertIn("byte for byte", r.stdout + r.stderr)
        (inbox / "again.pdf").unlink()

        for step in ("enrich_chunks.py", "build_index.py"):
            r = run(step, str(self.coll))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        index = json.loads((self.coll / "docs" / "index.json").read_text(encoding="utf-8"))
        self.assertEqual({e["slug"]: e["current"] for e in index["manuals"]},
                         {"gadget-2025-1": False, "gadget-2026-1": True})
        r = run("check_corpus.py", str(self.coll))
        self.assertEqual(r.returncode, 0, "the checker failed a corpus whose PDFs are in source/:\n" + r.stdout)
        self.assertNotIn("source-pdf-missing", r.stdout)
        self.assertNotIn("pdf-unaccounted", r.stdout)

    def test_20b_any_version_groups_editions_and_two_on_a_cover_are_refused(self):
        """4.1 and 4.2 of guide.pdf are one manual, not two.

        The shared name used to be found by taking a year-style ending off the
        slug, so `guide-4-1` and `guide-4-2` stayed two unrelated manuals that
        both answered every search. And a cover naming two versions is
        ambiguous however often each is printed: a running header repeats.
        """
        editions = load_script("editions")
        self.assertEqual(editions.default_slug("guide", "4.1"), "guide-4-1")
        self.assertEqual(editions.default_slug("guide-4-1", "4.1"), "guide-4-1", "the version was appended twice")
        self.assertEqual(editions.default_doc_id("guide-4-1", "4.1"), "guide")
        self.assertEqual(editions.default_doc_id("guide-42", "4.1"), "guide-42",
                         "a number in the manual's name was taken for its version")
        self.assertEqual(editions.default_doc_id("tshell-ref-2026-2", "2026.2"), "tshell-ref")
        self.assertEqual(editions.default_doc_id("mbist-useref-2025-2", "2023.1"), "mbist-useref")

        coll = self.tmp / "Numbered" / "Tools"
        for folder in ("new_docs", "docs"):
            (coll / folder).mkdir(parents=True)
        for version in ("4.1", "4.2"):
            gadget_fixture(coll / "new_docs" / "guide.pdf", version, ["Setup", "Use"])
            r = run("convert_manual.py", str(coll / "new_docs" / "guide.pdf"), "--title", "Tool Guide")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        ids = {slug: json.loads((coll / "docs" / slug / "manifest.json").read_text(encoding="utf-8"))["doc_id"]
               for slug in ("guide-4-1", "guide-4-2")}
        self.assertEqual(ids, {"guide-4-1": "guide", "guide-4-2": "guide"},
                         "two releases of one manual were given different doc_ids")

        pdf = self.tmp / "two-versions.pdf"
        write_pdf(pdf, [[("Tool Guide", 24), ("Software Version 4.1", 11), ("Software Version 4.1", 11),
                         ("Version 4.2", 11)]], [[1, "Tool Guide", 1]])
        cover = editions.read_cover(pdf)
        self.assertIsNone(cover.version, "the version printed more often was taken as the manual's")
        self.assertIn("4.2", cover.note)
        # The same release written two ways is one version, not two.
        self.assertTrue(editions.same_release("2026.3", "Y-2026.03"))
        self.assertFalse(editions.same_release("Y-2026.03", "Y-2026.03-SP2"))

    def test_21_search_answers_from_one_edition_per_manual(self):
        """Two releases indexed, one answering -- and never a stand-in.

        Both editions say almost the same thing, so without the filter every
        question is answered twice with nothing to say which hit is in use.
        """
        r = self.build()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        s = self.serve()
        hits = s.search("gadget seals")
        self.assertTrue(hits, "nothing found in the current edition")
        self.assertEqual({h["slug"] for h in hits}, {"gadget-2026-1"}, "an older edition answered a plain search")

        old = s.tool_search_docs({"query": "gadget seals", "document": "gadget", "version": "2025.1"})
        self.assertIn("gadget-2025-1", old)
        self.assertIn("not the current edition", old, "an older edition was served without saying so")
        self.assertNotIn("gadget-2026-1", old, "a search of one edition returned another's sections")
        self.assertIn("gadget-2025-1", s.tool_search_docs({"query": "seals", "document": "gadget", "version": "v2025_1"}),
                      "a version typed differently was not recognised")

        with self.assertRaises(ValueError) as missing:
            s.tool_search_docs({"query": "seals", "document": "gadget", "version": "2024.1"})
        self.assertIn("2025.1", str(missing.exception), "the refusal does not list the editions that exist")
        self.assertIn("2026.1", str(missing.exception))
        with self.assertRaises(ValueError):
            s.tool_search_docs({"query": "seals", "version": "2025.1"})

        headings = s.tool_compare_versions({"document": "gadget"})
        added, removed = headings.split("## Removed since")
        self.assertIn("Cloud Sync", added)
        self.assertIn("Legacy Mode", removed)
        self.assertNotIn("Mounting", added.split("## Added in")[1] + removed, "a heading in both was listed")
        # A template that renumbers its chapters has not renamed them.
        self.assertEqual(s.heading_key("Chapter 3 A Typical Flow"), s.heading_key("3. A Typical Flow"))
        self.assertEqual(s.heading_key("Appendix A  Getting Help"), s.heading_key("A. Getting Help"))
        self.assertNotEqual(s.heading_key("A Typical Flow"), s.heading_key("Typical Flow"))
        s.CORPUS._db.close()

        # A pin makes the older release the one in use; a pin on a release
        # that is not here stops the build and leaves the old index standing.
        pins = self.coll / "current_versions.json"
        pins.write_text(json.dumps({"gadget": "2025.1"}), encoding="utf-8")
        r = self.build()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        s = self.serve()
        self.assertEqual({h["slug"] for h in s.search("gadget seals")}, {"gadget-2025-1"}, "the pin was ignored")
        s.CORPUS._db.close()

        before = self.db.read_bytes()
        pins.write_text(json.dumps({"gadget": "2024.1"}), encoding="utf-8")
        r = self.build()
        self.assertNotEqual(r.returncode, 0, "a pin on a version that is not here was accepted")
        self.assertEqual(self.db.read_bytes(), before, "a failed build replaced the index")
        r = run("check_corpus.py", str(self.coll), "--no-pdf")
        self.assertIn("pin-invalid", r.stdout)
        pins.unlink()

    def test_22_comparison_reads_the_whole_entry(self):
        """A difference past the lookup's cut must still be found.

        lookup_entity returns the first 40,000 characters of an entry. Two
        releases of a long entry that differ only in its last line look the
        same that far in, so a comparison made by reading both lookups says
        "no difference". compare_versions reads every line.
        """
        source = self.coll / "source"
        edition_reference_fixture(source / "widget-ref-1.pdf", list(range(25)),
                                  "The final setting accepts values from 1 to 8.")
        edition_reference_fixture(source / "widget-ref-2.pdf", [n for n in range(27) if n != 3],
                                  "The final setting accepts values from 1 to 64.")
        for n in ("1", "2"):
            r = run("rebuild_reference.py", str(source / f"widget-ref-{n}.pdf"), "--title", "Widget Reference",
                    "--slug", f"widget-ref-v{n}", "--doc-id", "widget-ref", "--version", f"{n}.0")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((source / f"widget-ref-{n}.pdf").is_file(), "a PDF already in source/ was moved")
        m = self.manifest("widget-ref-v2")
        self.assertEqual((m["doc_id"], m["version"]), ("widget-ref", "2.0"))
        long_chars = sum(sec["chars"] for sec in m["sections"] if sec.get("command") == LONG_ENTRY)
        self.assertGreater(long_chars, 40_000, "the fixture's long entry is not past the lookup cut")

        r = self.build()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        s = self.serve()

        looked_up = s.tool_lookup_entity({"name": LONG_ENTRY})
        self.assertIn("widget-ref-v2", looked_up)
        self.assertNotIn("widget-ref-v1", looked_up, "lookup returned an older edition's entry as well")
        self.assertNotIn("from 1 to 64", looked_up, "the fixture's difference is inside the lookup cut")

        diff = s.tool_compare_versions({"document": "widget-ref", "name": LONG_ENTRY})
        self.assertIn("from 1 to 8.", diff, "the old wording of the changed line is missing")
        self.assertIn("from 1 to 64.", diff, "a difference past the lookup cut was not found")
        self.assertNotIn("Setting 010", diff, "a line that is in both editions was reported")

        same = s.tool_compare_versions({"document": "widget-ref", "name": "set_widget_option_07"})
        self.assertIn("No differences", same)

        # What is ignored is Markdown emphasis, and only that. Deleting every
        # `_` and `*` made `data_*` and `data*` the same line, and two entries
        # matching different names "identical".
        key = s.diff_key
        self.assertNotEqual(key("set_mode -pattern data_*"), key("set_mode -pattern data*"),
                            "a pattern's own characters were dropped as if they were emphasis")
        self.assertNotEqual(key("use scan_en"), key("use scanen"))
        self.assertEqual(key("**_chain_name group_name_**"), key("chain_name group_name"))
        self.assertEqual(key("_Note:_ The _cell_em_ value"), key("Note: The cell_em value"))
        self.assertEqual(key("- `-from` _`from_list`_"), key("- -from from_list"))
        self.assertEqual(key(r"data\_\*"), key("data_*"))

        listing = s.tool_compare_versions({"document": "widget-ref", "from_version": "1.0", "to_version": "2.0"})
        added, removed = listing.split("## Removed since")
        self.assertIn("set_widget_option_25", added)
        self.assertIn("set_widget_option_26", added)
        self.assertIn("set_widget_option_03", removed)
        self.assertNotIn("set_widget_option_07", listing, "an entry in both editions was listed")

        self.assertIn("removed between them",
                      s.tool_compare_versions({"document": "widget-ref", "name": "set_widget_option_03"}))
        gone = s.tool_lookup_entity({"name": "set_widget_option_03"})
        self.assertIn("widget-ref-v1", gone, "an entry dropped in the current edition was not traced to the older one")
        self.assertNotIn("SYNTAX", gone, "an older edition's entry was served as if it were current")
        self.assertIn("SYNTAX", s.tool_lookup_entity({"name": "set_widget_option_03", "document": "widget-ref",
                                                      "version": "1.0"}).upper())
        s.CORPUS._db.close()

        r = run("mcp_smoke_test.py", "--db", str(self.db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("compare_versions", r.stdout)

    def test_22b_the_corpus_folder_serves_itself_from_anywhere(self):
        """No path in the editor config names the machine that built the index.

        A config pointing at this checkout works on one machine, and a corpus
        in a synced folder is opened on several. --emit-vscode-config copies
        the server beside the index and writes workspace-relative paths, so
        the folder can be moved or synced and still be served.
        """
        import shutil
        config = self.root / ".vscode" / "mcp.json"
        config.parent.mkdir(exist_ok=True)
        # A server someone registered earlier, by absolute path, under their own name.
        config.write_text(json.dumps({"servers": {"my-manuals": {
            "type": "stdio", "command": "python",
            "args": ["X:\\old\\checkout\\scripts\\mcp_server.py", "--db", "X:\\old\\index.sqlite3"]}}}),
            encoding="utf-8")
        for _ in range(2):                      # emitting twice must not add a second entry
            r = run("build_search_db.py", "--root", str(self.root), "--emit-vscode-config")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        servers = json.loads(config.read_text(encoding="utf-8"))["servers"]
        self.assertEqual(list(servers), ["my-manuals"], "the existing entry was not reused")
        self.assertEqual(servers["my-manuals"]["args"],
                         ["${workspaceFolder}/mcp_server.py", "--db", "${workspaceFolder}/mcp-index.sqlite3"])
        self.assertNotIn(self.tmp.name, config.read_text(encoding="utf-8"), "the config names this machine's paths")
        self.assertTrue((self.root / "mcp_server.py").is_file(), "the server was not copied beside the index")

        moved = self.tmp / "Elsewhere"
        shutil.copytree(self.root, moved)
        r = run("mcp_smoke_test.py", "--db", str(moved / "mcp-index.sqlite3"),
                "--server", str(moved / "mcp_server.py"))
        self.assertEqual(r.returncode, 0, "the copied corpus could not serve itself:\n" + r.stdout + r.stderr)
        corpus = self.server.Corpus(moved / "mcp-index.sqlite3")
        self.assertEqual(corpus.root, moved, "a moved corpus still reads figures and PDFs from where it was built")
        corpus._db.close()

    def test_23_checker_catches_edition_defects(self):
        """Each way a set of editions can be undecidable, planted alone."""
        import shutil
        clean = self.tmp / "HandClean"
        handmade_document(clean)

        def plant(name: str, first: dict, second: dict, pins: dict | None = None) -> tuple[int, str]:
            corpus = self.tmp / f"Hand-{name}"
            shutil.copytree(clean, corpus)
            docs = corpus / "docs"
            shutil.copytree(docs / "hand", docs / "hand2")
            for slug, extra in (("hand", first), ("hand2", {"slug": "hand2", **second})):
                path = docs / slug / "manifest.json"
                m = json.loads(path.read_text(encoding="utf-8"))
                m.update(extra)
                path.write_text(json.dumps(m, indent=2), encoding="utf-8")
            if pins is not None:
                (corpus / "current_versions.json").write_text(json.dumps(pins), encoding="utf-8")
            r = run("check_corpus.py", str(corpus), "--no-pdf")
            built = run("build_search_db.py", "--root", str(corpus), "--stats-only")
            return r.returncode, r.stdout, built.returncode

        both = {"doc_id": "hand"}
        cases = [
            ("ungrouped", {}, {}, None, "edition-ungrouped", 0),
            ("unordered", {**both, "version": "1.0"}, both, None, "edition-unordered", 1),
            ("duplicate", {**both, "version": "1.0"}, {**both, "version": "1.0"}, None, "edition-duplicate-version", 1),
            ("badpin", {**both, "version": "1.0"}, {**both, "version": "2.0"}, {"hand": "3.0"}, "pin-invalid", 1),
            # A pin is a version as text: 2.0 as a JSON number stops the build.
            ("numberpin", {**both, "version": "1.0"}, {**both, "version": "2.0"}, {"hand": 2.0}, "pin-invalid", 1),
        ]
        for name, first, second, pins, expected, code in cases:
            with self.subTest(defect=name):
                rc, out, build_rc = plant(name, first, second, pins)
                self.assertIn(expected, out, f"{name} went unreported\n{out}")
                self.assertEqual(rc, code, out)
                self.assertEqual(build_rc, code, f"the checker and the index build disagree about {name}")
        # What the checker passes, the index build must accept -- including a
        # release written without its letter.
        fine = [("clean", {**both, "version": "1.0"}, {**both, "version": "2.0"}, {"hand": "1.0"}),
                ("samepin", {**both, "version": "Y-2026.03"}, {**both, "version": "Y-2026.06"}, {"hand": "2026.3"})]
        for name, first, second, pins in fine:
            with self.subTest(accepted=name):
                rc, out, build_rc = plant(name, first, second, pins)
                for check in ("edition-", "pin-invalid"):
                    self.assertNotIn(check, out, f"two well-formed editions raised {check}\n{out}")
                self.assertEqual(build_rc, 0, f"the index build rejects a pin the checker accepts ({name})")

    def test_24_an_older_corpus_is_stamped_and_migrated(self):
        """A corpus converted before editions existed: PDFs beside docs/, no
        doc_id or version in any manifest. `stamp` adds them and keeps the
        originals; `migrate` moves the PDFs; nothing downstream breaks."""
        corpus = self.tmp / "Legacy"
        handmade_document(corpus)
        manifest = corpus / "docs" / "hand" / "manifest.json"
        original = manifest.read_text(encoding="utf-8")

        r = run("editions.py", "stamp", str(corpus), "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(manifest.read_text(encoding="utf-8"), original, "--dry-run wrote a manifest")

        r = run("editions.py", "stamp", str(corpus), "--set", "hand=3.1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        m = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual((m["doc_id"], m["version"]), ("hand", "3.1"))
        kept = list((corpus / ".rebuild-backup").glob("manifests-*/hand.manifest.json"))
        self.assertTrue(kept, "the manifest was rewritten without keeping the original")
        self.assertEqual(kept[0].read_text(encoding="utf-8"), original)

        r = run("editions.py", "migrate", str(corpus))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((corpus / "source" / "hand.pdf").is_file())
        self.assertFalse((corpus / "hand.pdf").exists())
        self.assertTrue((corpus / "new_docs").is_dir(), "migrate did not leave an inbox")

        r = run("build_index.py", str(corpus))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = run("check_corpus.py", str(corpus), "--strict")
        self.assertEqual(r.returncode, 0, "a migrated corpus no longer passes strictly:\n" + r.stdout)


    def test_25_a_cover_that_says_and_later_covers_later_releases(self):
        """A manual the vendor ships unchanged says "2023.1 and later" on its
        cover and takes each release's filename. It is the edition for every
        release from 2023.1 until the next edition, because it says so. An
        edition that makes no such claim answers for its own release only."""
        root = self.tmp / "LaterRoot"
        coll = root / "Tools"
        for folder in ("new_docs", "docs"):
            (coll / folder).mkdir(parents=True)
        gadget_fixture(coll / "new_docs" / "probe_guide_2025_2.pdf", "2023.1 and later", ["Probing", "Limits"])
        gadget_fixture(coll / "new_docs" / "probe_guide_2026_1.pdf", "2026.1", ["Probing", "Limits", "Remote Probing"])
        for name in ("probe_guide_2025_2.pdf", "probe_guide_2026_1.pdf"):
            r = run("convert_manual.py", str(coll / "new_docs" / name), "--title", "Probe Guide")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        old = json.loads((coll / "docs" / "probe-guide-2023-1" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((old["version"], old.get("version_and_later")), ("2023.1", True),
                         "the cover's version was dropped because the filename names a later release")
        new = json.loads((coll / "docs" / "probe-guide-2026-1" / "manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn("version_and_later", new)

        # A filename naming an earlier release than the cover contradicts it.
        editions = load_script("editions")
        gadget_fixture(self.tmp / "probe_guide_2022_1.pdf", "2023.1 and later", ["Probing"])
        self.assertIsNone(editions.read_cover(self.tmp / "probe_guide_2022_1.pdf").version)

        db = root / "mcp-index.sqlite3"
        r = run("build_search_db.py", "--root", str(root))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.server.CORPUS = corpus = self.server.Corpus(db)
        self.addCleanup(lambda: corpus._db and corpus._db.close())
        resolve = self.server.resolve_document
        self.assertEqual(resolve("probe-guide", "2025.2")["slug"], "probe-guide-2023-1")
        self.assertEqual(resolve("probe-guide", "2023.1")["slug"], "probe-guide-2023-1")
        self.assertEqual(resolve("probe-guide", "2026.1")["slug"], "probe-guide-2026-1")
        self.assertEqual(resolve("probe-guide")["slug"], "probe-guide-2026-1")
        for release, why in (("2022.4", "a release before the first edition"),
                             ("2026.2", "a release after an edition that does not say 'and later'")):
            with self.assertRaises(ValueError, msg=f"{why} was answered"):
                resolve("probe-guide", release)
        self.assertIn("2023.1 and later", self.server.tool_search_docs(
            {"query": "probing", "document": "probe-guide", "version": "2025.2"}))
        corpus._db.close()

        # A pin names the release in use, and resolves the same way.
        (coll / "current_versions.json").write_text(json.dumps({"probe-guide": "2025.2"}), encoding="utf-8")
        r = run("build_search_db.py", "--root", str(root))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.server.CORPUS = corpus = self.server.Corpus(db)
        self.addCleanup(lambda: corpus._db and corpus._db.close())
        self.assertEqual({h["slug"] for h in self.server.search("probing")}, {"probe-guide-2023-1"},
                         "a pin on a release the older edition covers did not select it")
        corpus._db.close()
        r = run("check_corpus.py", str(coll), "--no-pdf")
        self.assertNotIn("pin-invalid", r.stdout, "the checker rejects a pin the index build accepts")

        # The pin makes the earliest edition current. That is a healthy
        # corpus, and the smoke test has to say so.
        r = run("mcp_smoke_test.py", "--db", str(db))
        self.assertEqual(r.returncode, 0, "a pin on the earliest edition failed the smoke test:\n" + r.stdout)

        # A section the index could not read is not evidence it was removed.
        self.server.CORPUS = corpus = self.server.Corpus(db)
        self.addCleanup(lambda: corpus._db and corpus._db.close())
        asked = {"document": "probe-guide", "from_version": "2023.1", "to_version": "2026.1"}
        self.assertIn("Remote Probing", self.server.tool_compare_versions(asked))
        corpus._db.close()
        (coll / "docs" / "probe-guide-2026-1" / new["sections"][-1]["file"]).unlink()
        r = run("build_search_db.py", "--root", str(root))
        self.assertEqual(r.returncode, 2, "a section file that is gone did not mark the build partial")
        self.server.CORPUS = corpus = self.server.Corpus(db)
        self.addCleanup(lambda: corpus._db and corpus._db.close())
        with self.assertRaises(ValueError) as partial:
            self.server.tool_compare_versions(asked)
        self.assertIn("not in the index", str(partial.exception))
        corpus._db.close()

    def test_26_a_name_means_what_it_does_in_the_collection_asked_for(self):
        """One collection's slug can be another's doc_id.

        `hand` is a document in Alpha and a manual with two editions in Beta.
        Asked for in Beta, it has to be Beta's manual -- and every tool that
        takes a document has to be able to say which collection it means.
        """
        root = self.tmp / "TwoCollections"
        handmade_document(root / "Alpha", "hand")
        for n in ("1", "2"):
            handmade_document(root / "Beta", f"hand-{n}")
            path = root / "Beta" / "docs" / f"hand-{n}" / "manifest.json"
            m = json.loads(path.read_text(encoding="utf-8"))
            m.update(doc_id="hand", version=f"{n}.0")
            path.write_text(json.dumps(m, indent=2), encoding="utf-8")
        r = run("build_search_db.py", "--root", str(root))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.server.CORPUS = corpus = self.server.Corpus(root / "mcp-index.sqlite3")
        self.addCleanup(lambda: corpus._db and corpus._db.close())
        resolve = self.server.resolve_document
        self.assertEqual(resolve("hand")["slug"], "hand")
        self.assertEqual(resolve("hand", collection="beta")["slug"], "hand-2",
                         "another collection's slug answered for this collection's manual")
        self.assertEqual(resolve("hand", "1.0", "beta")["slug"], "hand-1")
        with self.assertRaises(ValueError):
            resolve("hand-1", collection="alpha")
        self.assertIn("hand-2", self.server.tool_get_toc({"document": "hand", "collection": "beta"}))
        schemas = {t["name"]: t["inputSchema"]["properties"] for t in self.server.build_tools()}
        for tool in ("search_docs", "lookup_entity", "get_toc", "compare_versions", "get_page_image"):
            self.assertIn("collection", schemas[tool], f"{tool} takes a document and cannot say which collection")
        corpus._db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
