"""Converters, enrichment and the pre-flight: what a PDF becomes."""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _support import SCRIPTS, WS, run, inverted_fixture, gadget_fixture, write_pdf, load_script, edition_reference_fixture, mixed_fixture  # noqa: E402
import pymupdf  # noqa: E402


class ConverterTest(unittest.TestCase):
    """The converters, enrich_chunks.py and the pre-flight, on fixture PDFs."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.tmp = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def manifest(self, slug: str) -> dict:
        return json.loads((self.corpus / "docs" / slug / "manifest.json").read_text(encoding="utf-8"))

    def manifest_of(self, coll: Path, slug: str) -> dict:
        return json.loads((coll / "docs" / slug / "manifest.json").read_text(encoding="utf-8"))

    def test_01_prose_conversion_contract(self):
        self.corpus = WS.collection("prose", "prose")
        r = run("convert_manual.py", str(self.corpus / "widget-guide.pdf"), "--title", "Widget Guide", "--slug", "prose")
        self.assertEqual(r.returncode, 0, r.stderr)
        m = self.manifest("prose")
        for key in ("source_pdf", "title", "slug", "page_count", "toc", "sections", "full_md_chars"):
            self.assertIn(key, m, f"manifest lost the '{key}' field that downstream tools read")
        self.assertTrue(m["sections"], "no sections produced")
        self.assertEqual(len(m["toc"]), 6, "bookmark TOC not carried into the manifest")
        for s in m["sections"]:
            f = self.corpus / "docs" / "prose" / s["file"]
            self.assertTrue(f.is_file(), f"manifest names a missing file: {s['file']}")
            self.assertNotIn("chars", s, "a section's length is not stored: the file is the copy")
        self.assertTrue(m["sections"][0]["file"].startswith("sections/0001-"), m["sections"][0]["file"])
        pages = [(s["page_start"], s["page_end"]) for s in m["sections"]]
        for first, last in pages:
            self.assertTrue(isinstance(first, int) and isinstance(last, int) and 1 <= first <= last <= m["page_count"],
                            f"a chunk's pages {first}-{last} are outside 1..{m['page_count']}")
        self.assertEqual([p[0] for p in pages], sorted(p[0] for p in pages), "page_start went backwards")

    def test_02_enrich_is_idempotent(self):
        # A collection nothing has enriched yet: on one already enriched,
        # a first run that changes nothing would pass as well.
        self.corpus = WS.collection("enrich", "prose")
        r = run("convert_manual.py", str(self.corpus / "widget-guide.pdf"), "--title", "Widget Guide", "--slug", "prose")
        self.assertEqual(r.returncode, 0, r.stderr)
        first = run("enrich_chunks.py", str(self.corpus))
        self.assertEqual(first.returncode, 0, first.stderr)
        changed = re.search(r"(\d+) changed", first.stdout)
        self.assertTrue(changed and int(changed.group(1)) > 0,
                        "the first enrich run changed nothing, so the second proves nothing")
        second = run("enrich_chunks.py", str(self.corpus))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("0 changed", second.stdout,
                      "second enrich run changed something; the pass is not idempotent")

    def test_03_enrich_gives_every_chunk_a_breadcrumb(self):
        self.corpus = WS.corpus()
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
        self.corpus = WS.fresh(WS.corpus(), "enrich-parent")
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

    def test_32_an_interrupted_build_leaves_nothing_behind(self):
        """A conversion is built outside docs/, from an empty folder, and
        moved in whole.

        rebuild_reference.py built in docs/<slug>.new/ and never cleared it,
        so section files an interrupted run left there, named for another
        chunk count, shipped with the next run; and every script that read
        docs/*/ could meet the half-built folder."""
        for script, pdf, slug, title in (("rebuild_reference.py", "ref", "ref", "Widget Commands"),
                                         ("convert_manual.py", "prose", "prose", "Widget Guide")):
            with self.subTest(script=script):
                coll = WS.collection(f"staged-{slug}", pdf)
                places = [coll / ".rebuild-backup" / ".staging" / slug]
                if script == "rebuild_reference.py":
                    places.append(coll / "docs" / f"{slug}.new")   # where it used to build
                for place in places:
                    (place / "sections").mkdir(parents=True)
                    (place / "sections" / "9999-orphan.md").write_text("left by an interrupted run\n",
                                                                       encoding="utf-8")
                r = run(script, str(coll / WS.PDFS[pdf][0]), "--title", title, "--slug", slug)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertFalse((coll / "docs" / slug / "sections" / "9999-orphan.md").exists(),
                                 "a file an interrupted run left behind shipped with this one")
                manifest = json.loads((coll / "docs" / slug / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(sorted(p.name for p in (coll / "docs" / slug / "sections").iterdir()),
                                 sorted(s["file"].split("/", 1)[1] for s in manifest["sections"]))

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

    def test_05_reference_attributes_entities(self):
        self.corpus = WS.collection("ref", "ref")
        r = run("rebuild_reference.py", str(self.corpus / "widget-commands.pdf"),
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
        self.corpus = WS.collection("tiny", "tiny")
        r = run("rebuild_reference.py", str(self.corpus / "tiny-commands.pdf"),
                "--title", "Tiny", "--slug", "tiny")
        self.assertEqual(r.returncode, 0, r.stderr)
        m = self.manifest("tiny")
        self.assertFalse([s for s in m["sections"] if s.get("command")],
                         "attributed commands from below-threshold evidence")
        self.assertEqual(m["attribution"], {"status": "declined", "command_level": None, "chosen_by": "toc"})
        self.assertRegex(r.stdout, r"!! no TOC level has 20 titles", "a declined attribution passed quietly")

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

    def test_07_pick_extractor_classifies_both_shapes(self):
        r = run("pick_extractor.py", str(WS.pdf("prose")), str(WS.pdf("ref")))
        self.assertEqual(r.returncode, 0, r.stderr)
        prose_block = r.stdout.split("widget-guide.pdf")[1].split("widget-commands.pdf")[0]
        ref_block = r.stdout.split("widget-commands.pdf")[1]
        self.assertIn("shape: prose", prose_block)
        self.assertIn("shape: reference", ref_block)
        self.assertIn("rebuild_reference.py", ref_block)

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
            c = {"headings": heading if isinstance(heading, list) else [heading], "pages": pages, "text": ""}
            return cd.resolve_ancestors(c, toc, toc_pos, by_title, spans, pdf)

        self.assertEqual(resolve("Syntax", [4]), (["Commands", "beta"], "anchored"),
                         "the Syntax entry covering the page should anchor it")
        self.assertEqual(resolve("Syntax", [2]), (["Commands", "alpha"], "anchored"))
        ancestors, how = resolve("HyperGrid", [2])
        self.assertEqual(how, "page", "anchored to a same-titled entry six pages on")
        self.assertEqual(ancestors, ["Commands", "alpha", "Syntax"])
        self.assertEqual(resolve("Setup", [9])[1], "page", "anchored to a bookmark with no page")
        # Docling lists headings outermost first, the document's title among
        # them once it has labelled the cover: the chunk's own is the last.
        self.assertEqual(resolve(["Widget Guide", "Syntax"], [4]), (["Commands", "beta"], "anchored"),
                         "a title ahead of the chunk's own heading hid it")
        self.assertEqual(resolve(["Widget Guide"], [2])[1], "page")
        pdf.close()

    def test_09c_docling_records_the_edition_and_files_the_pdf(self):
        """The Docling converter takes a PDF from new_docs/ like the others.
        Runs only where Docling is installed; the other two converters are
        covered wherever the suite runs."""
        try:
            import docling  # noqa: F401
        except ImportError:
            self.skipTest("Docling is not installed")
        coll = self.tmp / "DoclingCollection"
        for folder in ("new_docs", "docs"):
            (coll / folder).mkdir(parents=True)
        gadget_fixture(coll / "new_docs" / "dl_guide.pdf", "2026.1", ["Setup", "Use"])
        r = run("convert_docling.py", str(coll / "new_docs" / "dl_guide.pdf"), "--title", "DL Guide")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        m = json.loads((coll / "docs" / "dl-guide-2026-1" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((m["doc_id"], m["version"], m["source_pdf"]), ("dl-guide", "2026.1", "dl_guide.pdf"))
        self.assertTrue((coll / "source" / "dl_guide.pdf").is_file(), "the converted PDF was not moved to source/")
        self.assertFalse((coll / "new_docs" / "dl_guide.pdf").exists())

    def test_09_docling_path_gates_cleanly(self):
        try:
            import docling  # noqa: F401
            self.skipTest("Docling is installed; the missing-dependency path cannot run here")
        except ImportError:
            pass
        r = run("convert_docling.py", str(WS.pdf("prose")), "--title", "x", "--slug", "dl")
        self.assertNotEqual(r.returncode, 0, "should refuse without Docling")
        out = r.stdout + r.stderr
        self.assertIn("pip install docling", out, "no install instruction")
        self.assertIn("convert_manual.py", out, "did not point at the light path")
        self.assertNotIn("Traceback", out, "gated with a traceback instead of a message")

    def test_11_entity_regions_end_at_chapters(self):
        """Text that follows a command but is not one must belong to nothing.

        Regions used to end only where the next command began, so the last
        command in a chapter owned whatever came after it -- the next chapter,
        the appendices, the licence -- and lookup_entity served it all as that
        command's entry. Checked by what each chunk contains, not by the share
        of chunks carrying a label: coverage is not correctness.
        """
        self.corpus = WS.collection("nested", "nested")
        r = run("rebuild_reference.py", str(self.corpus / "nested-commands.pdf"),
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

    def test_12_enrich_leaves_converter_breadcrumbs_alone_in_walk_mode(self):
        """The no-downgrade guard used to hold for flat documents only.

        enrich_chunks takes the heading walk when a document's levels vary, and
        that branch refreshed every breadcrumb in place -- replacing the
        page-accurate `Title › command` rebuild_reference.py writes. Force the
        walk on real rebuild_reference output and check nothing moves.
        """
        corpus = self.tmp / "WalkCorpus"
        slug_dir = corpus / "docs" / "walkref"
        r = run("rebuild_reference.py", str(WS.pdf("ref")), "--title", "Widget Commands",
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


    def test_27_page_numbers_come_from_pymupdf4llms_own_key(self):
        """pymupdf4llm 1.28 says `page_number`; rebuild_reference read `page`,
        so every number was its list position, wrong from the first dropped page."""
        cm = load_script("rebuild_reference").cm
        pdf = self.tmp / "three.pdf"
        write_pdf(pdf, [[(f"Page {n}", 11)] for n in (1, 2, 3)], [])

        def pages(*numbers, key="page_number"):
            return lambda *a, **k: [{"text": f"p{n}", "metadata": {key: n} if key else {}} for n in numbers]

        def numbers_for(fake):
            with mock.patch.object(cm.pymupdf4llm, "to_markdown", fake):
                return cm.page_markdown(pdf)[1]

        self.assertEqual(numbers_for(pages(1, 2, 3)), [1, 2, 3])
        self.assertEqual(numbers_for(pages(1, 2, 3, key="page")), [1, 2, 3])
        self.assertEqual(numbers_for(pages(1, 2, 3, key=None)), [1, 2, 3])
        for bad in ((1, 3), (0, 1, 2)):
            with self.assertRaises(SystemExit, msg=f"pages {bad} were accepted"):
                numbers_for(pages(*bad))
        # the real library, end to end: the key it actually writes is read
        self.assertEqual(cm.page_markdown(pdf)[1], [1, 2, 3])


    def test_29_docling_merge_names_its_sub_headings(self):
        """merge_sections read the outermost heading, the document's title once
        Docling had labelled the cover, so no `### sub-heading` was ever
        written. Runs without Docling: merging is plain list work."""
        cd = load_script("convert_docling")
        ancestors = ["Commands", "alpha"]

        def chunk(headings, text):
            return {"headings": headings, "ancestors": ancestors, "confidence": "anchored",
                    "pages": [1], "text": text}

        merged = cd.merge_sections([chunk(["Widget Guide", "Setup"], "one"),
                                    chunk(["Widget Guide", "Setup"], "two"),
                                    chunk(["Widget Guide", "Use"], "three")], 9000)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["heading"], "Setup")
        self.assertIn("\n\n### Use\n\n", merged[0]["text"])
        self.assertNotIn("### Setup", merged[0]["text"], "a heading repeated within one section was written twice")


    def test_30_a_declined_attribution_is_said_and_recorded(self):
        """A Tcl-style reference (entries are plain words) was sent to
        rebuild_reference.py by pick_extractor and came out with no command
        on any chunk, exit code 0 and a line saying `command level LNone`."""
        self.corpus = WS.collection("tcl", "tcl")
        pdf = str(self.corpus / "tcl-commands.pdf")

        r = run("pick_extractor.py", pdf)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("shape: reference", r.stdout)
        self.assertIn("--command-level 1", r.stdout)

        r = run("rebuild_reference.py", pdf, "--title", "Tcl Commands", "--slug", "tcl")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"!! no TOC level has 20 titles.*--command-level N")
        m = self.manifest("tcl")
        self.assertEqual(m["attribution"], {"status": "declined", "command_level": None, "chosen_by": "toc"})
        self.assertFalse([s for s in m["sections"] if s.get("command")])

        r = run("rebuild_reference.py", pdf, "--title", "Tcl Commands", "--slug", "tcl",
                "--replace", "--command-level", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        m = self.manifest("tcl")
        self.assertEqual(m["attribution"], {"status": "attributed", "command_level": 1, "chosen_by": "option"})
        self.assertEqual({s["command"] for s in m["sections"] if s.get("command")},
                         {t["title"] for t in m["toc"]}, "not every command was attributed")
        self.assertEqual(len({t["title"] for t in m["toc"]}), 30)

        r = run("rebuild_reference.py", pdf, "--title", "Tcl Commands", "--slug", "tcl",
                "--replace", "--command-level", "7")
        self.assertNotEqual(r.returncode, 0)
        self.assertRegex(r.stderr, r"level 7.*levels here: 1")


    def test_34_a_line_that_only_begins_the_title_is_kept(self):
        """The running-title rule matched any prefix of the title, so a plain
        "Design Compiler" line in three sections was deleted as furniture."""
        ec = load_script("enrich_chunks")
        title = "Design Compiler User Guide"

        def furniture_for(line: str) -> set:
            texts = [f"## Part {n}\n\nBody of part {n}.\n\n{line}\n" for n in range(4)]
            return ec.detect_furniture(texts, title)

        self.assertEqual(furniture_for("Design Compiler"), set(), "the product name alone was taken for furniture")
        for line in ("Design Compiler User Guide", "Design Compiler User Guide V-2024.06",
                     "Design Compiler User"):       # 77% of the title
            self.assertIn(line, furniture_for(line), f"{line!r} is the running title")

        # --list-furniture names every distinct line, with or without --dry-run.
        coll = WS.collection("furniture", "prose")
        r = run("convert_manual.py", str(coll / "widget-guide.pdf"), "--title", "Widget Guide", "--slug", "prose")
        self.assertEqual(r.returncode, 0, r.stderr)
        # The converter strips the page footer itself; put it back, as a document
        # converted by another tool would have it.
        for section in sorted((coll / "docs" / "prose" / "sections").glob("*.md")):
            section.write_text(section.read_text(encoding="utf-8") + "\nFeedback\n", encoding="utf-8")
        r = run("enrich_chunks.py", str(coll), "--dry-run", "--list-furniture")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"would be deleted")
        self.assertRegex(r.stdout, r"\d+x  'Feedback'")
        r = run("enrich_chunks.py", str(coll), "--list-furniture")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"\d+x  'Feedback'")
        self.assertNotIn("would be", r.stdout)


    def test_39_chunks_are_exact_spans_of_the_text(self):
        """A chunk's offsets come from where the chunker cut, not from finding
        its text again. That holds only if every chunk is a slice of the text:
        packing used to join siblings with nothing, dropping the blank line
        between two paragraphs."""
        cm = load_script("convert_manual")

        short_lines = "\n".join(f"Line {n:03d} of the long paragraph." for n in range(290))   # about 9,500 characters
        texts = {
            "tail of a split heading": (
                "## A\n\n" + "Intro of A.\n\n" + "### B\n\n" + short_lines + "\n\nShort tail of B.\n\n"
                "### C\n\nText of C.\n"),
            "one line, no newline": "## A\n\n" + "word " * 6000,
            "dictionary entries": "".join(
                f"**cmd_{n:02d}**\n\n" + ("\n\n".join(f"Paragraph {k} of entry {n}. " * 12 for k in range(40) if n == 5 or k < 2))
                + "\n\n" for n in range(12)),
        }
        for name in ("prose", "ref", "tiny", "nested"):
            texts[f"full.md of {name}"] = (WS.corpus() / "docs" / name / "full.md").read_text(encoding="utf-8")
        long_entry = WS.collection("longentry")
        edition_reference_fixture(long_entry / "long.pdf", list(range(8)), "End of the long option.")
        r = run("rebuild_reference.py", str(long_entry / "long.pdf"), "--title", "Long", "--slug", "long")
        self.assertEqual(r.returncode, 0, r.stderr)
        texts["full.md of a reference with an entry over the limit"] = (
            long_entry / "docs" / "long" / "full.md").read_text(encoding="utf-8")

        strip = lambda t: re.sub(r"\s", "", t)
        for name, text in texts.items():
            for dictionary in (False, True):
                with self.subTest(text=name, dictionary=dictionary):
                    spans = cm.chunk_spans(text, dictionary)
                    self.assertTrue(spans)
                    edge = 0
                    for sp in spans:
                        self.assertTrue(edge <= sp.start < sp.end <= len(text), f"{sp} is out of order or outside the text")
                        self.assertLessEqual(sp.end - sp.start, cm.MAX_CHUNK)
                        edge = sp.end
                    bodies = [b for _h, _l, b in cm.chunk_markdown(text, dictionary)]
                    self.assertEqual(bodies, [text[sp.start:sp.end] for sp in spans])
                    self.assertEqual(strip("".join(bodies)), strip(text), "text was lost or repeated")

        tail = [sp for sp in cm.chunk_spans(texts["tail of a split heading"], False)
                if "Short tail of B." in texts["tail of a split heading"][sp.start:sp.end]]
        self.assertEqual(len(tail), 1)
        self.assertIn("\n\nShort tail of B.", texts["tail of a split heading"][tail[0].start:tail[0].end])
        self.assertGreater(len(cm.chunk_spans(texts["one line, no newline"], False)), 1, "the long line was not wrapped")

    def test_39b_a_page_is_where_a_chunks_first_word_is(self):
        """A piece cut at a newline starts with it, and that newline can be the
        last character of the page before."""
        cm = load_script("convert_manual")
        text = "aaa\nbbb\n" + "ccc\nddd\n"
        starts, numbers = [0, 8], [4, 5]
        self.assertEqual(cm.page_range(starts, numbers, 0, 8), (4, 4))
        self.assertEqual(cm.page_range(starts, numbers, 4, 12), (4, 5))
        start, end = cm.trim_span(text, 7, 12)       # "\nccc\n"
        self.assertEqual(text[start:end], "ccc")
        self.assertEqual(cm.page_range(starts, numbers, start, end), (5, 5))


    def test_41b_joined_pages_are_the_whole_document(self):
        """The prose path chunks the pages joined together. pymupdf4llm 1.28.2
        builds a page's markdown the same way with and without page_chunks;
        a release that stops doing so changes prose chunks quietly, so it
        fails here instead."""
        import pymupdf4llm
        cm = load_script("convert_manual")
        for name in ("prose", "ref"):
            pdf = WS.pdf(name)
            self.assertEqual("".join(cm.page_markdown(pdf)[0]), pymupdf4llm.to_markdown(str(pdf)), name)


    def test_41c_the_reference_path_is_one_implementation(self):
        """`convert_manual.py --shape reference` and `rebuild_reference.py`
        are the same code: the same PDF gives the same files."""
        out = {}
        for script, extra in (("rebuild_reference.py", []), ("convert_manual.py", ["--shape", "reference"])):
            coll = WS.collection(f"same-{script[:7]}", "ref")
            r = run(script, str(coll / "widget-commands.pdf"), "--title", "Widget Commands", "--slug", "ref", *extra)
            self.assertEqual(r.returncode, 0, r.stderr)
            out[script] = coll / "docs" / "ref"
        trees = [{str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}
                 for root in out.values()]
        self.assertTrue(trees[0])
        self.assertEqual(trees[0].keys(), trees[1].keys())
        for tree in trees:
            # the one record of which command ran, and when
            m = json.loads(tree["manifest.json"])
            m["converter"].pop("script"), m["converter"].pop("converted_at")
            tree["manifest.json"] = json.dumps(m, indent=2).encode()
        for name in trees[0]:
            self.assertEqual(trees[0][name], trees[1][name], f"{name} differs between the two commands")

    def test_41d_shape_is_decided_from_the_outline_and_a_mixed_one_is_refused(self):
        def convert(name, pdf, *extra):
            coll = WS.collection(name, pdf) if pdf in WS.PDFS else self.tmp / name
            return coll, run("convert_manual.py", str((coll / WS.PDFS[pdf][0]) if pdf in WS.PDFS else coll / f"{pdf}.pdf"),
                             "--title", "T", "--slug", "doc", *extra)

        coll, r = convert("auto-ref", "ref")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("shape: reference", r.stdout)
        self.assertTrue(json.loads((coll / "docs" / "doc" / "manifest.json").read_text(encoding="utf-8"))["attribution"])

        coll, r = convert("auto-prose", "prose")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("shape: prose", r.stdout)
        self.assertNotIn("attribution", json.loads((coll / "docs" / "doc" / "manifest.json").read_text(encoding="utf-8")))

        mixed = self.tmp / "auto-mixed"
        (mixed / "docs").mkdir(parents=True)
        mixed_fixture(mixed / "mixed.pdf")
        r = run("convert_manual.py", str(mixed / "mixed.pdf"), "--title", "T", "--slug", "doc")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--shape", r.stderr)
        self.assertIn("shape: mixed", r.stdout)
        self.assertFalse((mixed / "docs" / "doc").exists(), "a document of uncertain shape was written")
        r = run("convert_manual.py", str(mixed / "mixed.pdf"), "--title", "T", "--slug", "doc", "--shape", "prose")
        self.assertEqual(r.returncode, 0, r.stderr)

        r = run("convert_manual.py", str(coll / "widget-guide.pdf"), "--title", "T", "--slug", "doc",
                "--shape", "prose", "--command-level", "1", "--replace")
        self.assertNotEqual(r.returncode, 0, "--command-level was accepted for a prose conversion")


    def test_41e_reference_breadcrumbs_follow_the_toc_chain(self):
        """A reference's breadcrumb was `Title › command`, though the TOC says
        which chapter holds the entry and every region starts at a known one."""
        coll = WS.collection("chain", "nested")
        r = run("rebuild_reference.py", str(coll / "nested-commands.pdf"), "--title", "Widget Commands", "--slug", "nested")
        self.assertEqual(r.returncode, 0, r.stderr)
        sections = self.manifest_of(coll, "nested")["sections"]
        crumbs = {s["file"]: s["breadcrumb"] for s in sections}
        commands = [s for s in sections if s["command"]]
        self.assertEqual(len(commands), 22)
        for s in commands:
            self.assertEqual(s["breadcrumb"], f"Widget Commands › Command Reference › {s['command']}")
        self.assertIn("Widget Commands › Appendix A Troubleshooting", crumbs.values())
        self.assertIn("Widget Commands › End-User License Agreement", crumbs.values())
        self.assertIn("Widget Commands › Command Reference", crumbs.values())     # the chapter's own introduction
        text = (coll / "docs" / "nested" / commands[0]["file"]).read_text(encoding="utf-8")
        self.assertTrue(text.startswith(f"*{commands[0]['breadcrumb']}*"))

        out = self.tmp / "chain-check.json"
        r = run("check_corpus.py", str(coll), "--only", "nested", "--json", str(out))
        report = json.loads(out.read_text(encoding="utf-8"))
        raised = {p["check"] for d in report["documents"] for p in d["problems"]}
        self.assertFalse({c for c in raised if c.split("-")[0] in ("ancestor", "entity", "breadcrumb")},
                         f"chain breadcrumbs raised {raised}\n{r.stdout}")

    def test_41f_prose_outside_entries_is_opt_in(self):
        """In a document that is part reference, a chapter title at the command
        level is not a command, if asked."""
        coll = WS.collection("proseout")
        mixed_fixture(coll / "mixed.pdf")

        def commands(*extra):
            r = run("convert_manual.py", str(coll / "mixed.pdf"), "--title", "Mixed", "--slug", "mixed",
                    "--shape", "reference", "--replace", *extra)
            self.assertEqual(r.returncode, 0, r.stderr)
            return {s["command"] for s in self.manifest_of(coll, "mixed")["sections"] if s["command"]}

        plain = commands()
        self.assertTrue(any(c.startswith("Chapter") for c in plain), "the default changed what it attributes")
        chosen = commands("--prose-outside-entries")
        self.assertEqual(chosen, {c for c in plain if c.startswith("set_gadget_option_")})
        self.assertEqual(len(chosen), 20)


    def test_50_merged_helpers_agree_with_the_copies_they_replaced(self):
        """T24 removed duplicated code. Each merge is only safe if the old copy
        and the new one give the same answer on everything they were fed, so the
        old copies are kept here, as they were, and held to the new ones."""
        common = load_script("_common")
        manual, docling, build = load_script("convert_manual"), load_script("convert_docling"), load_script("build_search_db")
        editions, enrich, checker = load_script("editions"), load_script("enrich_chunks"), load_script("check_corpus")
        inputs = ["", "   ", "Hello World", "**Bold** Heading", "__Under_score__", "`code`", "# Title", "### # x",
                  "1.2.3 Numbered", "1. Intro", "Chapter 4: Foo", "Appendix A  Bar", "Section 12. Baz", "Ünïcode Straße",
                  "\u0130stanbul", "Kelvin \u212a", "a" * 100, "x" * 59 + " y", "---", "set_scan_configuration -chain_count",
                  "  spaced   out  ", "**", "****", "* *", "## **Bold heading**", "_a_ b _c_", "\uff21\uff22\uff23", "tab\there", "\n"]

        def old_manual(text, maxlen=60):
            text = re.sub(r"[*_`]", "", text)
            s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
            s = s[:maxlen].strip("-")
            return s or "section"

        def old_normalize(s):
            s = re.sub(r"^#{1,6}\s*", "", (s or "").strip())
            for _ in range(3):
                t = re.sub(r"^(\*\*|__|\*|_|`)(.*?)\1$", r"\2", s.strip())
                if t == s:
                    break
                s = t
            s = re.compile(r"\s+").sub(" ", s).strip()
            s = re.sub(r"^(chapter|appendix|section)\s+\w{1,4}\s*[:.]?\s*", "", s, flags=re.I)
            s = re.sub(r"^\d+(\.\d+)*\s*", "", s)
            return s.lower().strip()

        def old_docling(text, maxlen=60):
            s = re.sub(r"[^0-9A-Za-z]+", "-", old_normalize(text)).strip("-").lower()
            return s[:maxlen].rstrip("-") or "section"

        def old_editions(text):
            return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")

        def old_collection(name):
            return re.sub(r"[^0-9A-Za-z]+", "-", name).strip("-").lower() or "corpus"

        for text in inputs + [None]:
            with self.subTest(text=text):
                self.assertEqual(docling.normalize(text), old_normalize(text))
                if text is not None:
                    self.assertEqual(manual.slugify(text), old_manual(text))
                    self.assertEqual(manual.slugify(text, 10), old_manual(text, 10))
                    self.assertEqual(docling.slugify(text), old_docling(text))
                    self.assertEqual(docling.slugify(text, 10), old_docling(text, 10))
                    self.assertEqual(editions.slugify(text), old_editions(text))
                    self.assertEqual(build.slugify(text), old_collection(text))
        # the one place the shared core, as the other callers use it, is not the old collection key
        self.assertNotEqual(common.slugify("Kelvin \u212a", default="corpus"), old_collection("Kelvin \u212a"))

        # the separator every tool writes and reads
        for module in (enrich, docling, checker):
            self.assertEqual(module.BREADCRUMB_SEP, common.BREADCRUMB_SEP)

        # the console block: what it did to each stream, and to one that cannot be reconfigured
        class Stream:
            def __init__(self, fail=None):
                self.calls, self.fail = [], fail

            def reconfigure(self, **kwargs):
                self.calls.append(kwargs)
                if self.fail:
                    raise self.fail
        real = sys.stdout, sys.stderr
        try:
            sys.stdout, sys.stderr = Stream(), Stream(ValueError("detached"))
            common.utf8_console()
            self.assertEqual(sys.stdout.calls, [{"encoding": "utf-8", "errors": "replace"}])
            self.assertEqual(sys.stderr.calls, [{"encoding": "utf-8", "errors": "replace"}])
            sys.stdout, sys.stderr = object(), Stream()
            common.utf8_console()
        finally:
            sys.stdout, sys.stderr = real

    def test_50b_one_rule_finds_collections(self):
        """build_search_db.py and editions.py each had a rule for what a
        collection is. The one that is left gives the index builder the answer
        it always gave."""
        build, editions = load_script("build_search_db"), load_script("editions")

        def old_build(root):
            found = []
            if editions.document_dirs(root / "docs"):
                found.append((build.slugify(root.name), ".", root / "docs"))
                return found
            for sub in sorted(p for p in root.iterdir() if p.is_dir()):
                if editions.document_dirs(sub / "docs"):
                    found.append((build.slugify(sub.name), sub.name, sub / "docs"))
            return found

        def doc(folder):
            folder.mkdir(parents=True)
            (folder / "manifest.json").write_text("{}", encoding="utf-8")

        layouts = {"one collection at the root": [("docs", "a")],
                   "two subfolders": [("A/docs", "a"), ("B Vendor/docs", "b")],
                   "root and a subfolder": [("docs", "a"), ("A/docs", "a")],
                   "only a backup in docs": [("docs", "x.old")],
                   "empty docs beside a collection": [("A/docs", "a")],
                   "a hidden folder": [(".rebuild-backup/docs", "a"), ("A/docs", "a")]}
        for name, docs in layouts.items():
            with self.subTest(layout=name):
                root = self.tmp / f"layout-{name.replace(' ', '-')}"
                for where, slug in docs:
                    doc(root / where / slug)
                if name == "empty docs beside a collection":
                    (root / "docs").mkdir()
                self.assertEqual(build.find_collections(root), old_build(root))
                self.assertEqual([c for c in editions.find_collections(root)], [p.parent for _k, _d, p in old_build(root)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
