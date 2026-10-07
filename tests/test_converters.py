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
from _support import SCRIPTS, WS, run, inverted_fixture, gadget_fixture, write_pdf, load_script  # noqa: E402
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
            self.assertEqual(s["chars"], len(f.read_text(encoding="utf-8")),
                             "manifest 'chars' disagrees with the file on disk")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
