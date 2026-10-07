"""Editions: several releases of one manual, filed, indexed, served and compared."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _support import (WS, run, write_pdf, handmade_document, gadget_fixture, LONG_ENTRY,
                      edition_reference_fixture, load_script)  # noqa: E402
import pymupdf  # noqa: E402


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

    def use_gadgets(self, name: str) -> None:
        """Work on a copy of the two gadget editions test_20 converts."""
        self.root = WS.fresh(WS.gadgets()[0], name)
        self.coll = self.root / "Gadgets"
        self.db = self.root.parent / f"{self.root.name}.sqlite3"

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
        # Two releases are two releases in whichever style each is printed.
        mixed = self.tmp / "mixed-styles.pdf"
        write_pdf(mixed, [[("Tool Guide", 24), ("Version Y-2026.03", 11), ("Software Version 2025.1", 11)]],
                  [[1, "Tool Guide", 1]])
        self.assertIsNone(editions.read_cover(mixed).version,
                          "a lettered version hid a different one printed beside it")
        agreeing = self.tmp / "agreeing-styles.pdf"
        write_pdf(agreeing, [[("Tool Guide", 24), ("Version Y-2026.03", 11), ("Release 2026.03", 11)]],
                  [[1, "Tool Guide", 1]])
        self.assertEqual(editions.read_cover(agreeing).version, "Y-2026.03")
        # The same release written two ways is one version, not two.
        self.assertTrue(editions.same_release("2026.3", "Y-2026.03"))
        self.assertFalse(editions.same_release("Y-2026.03", "Y-2026.03-SP2"))
        # Every dotted component counts: with the third dropped, 2026.1.1 and
        # 2026.1.2 were one release, and a request for one got the other.
        self.assertFalse(editions.same_release("2026.1.1", "2026.1.2"))
        self.assertFalse(editions.same_release("2026.1", "2026.1.1"))
        ordered = ["2026.1", "2026.1-SP1", "2026.1.1", "2026.1.2", "2026.2"]
        self.assertEqual(sorted(reversed(ordered), key=editions.version_key), ordered)
        self.assertEqual(sorted(reversed(ordered), key=editions.version_sort), ordered)
        # The rule is written three times -- here, in the server that is
        # copied beside indexes, and in the checker that shares no code with
        # what it checks. They have to say the same thing.
        checker = load_script("check_corpus")
        for v in ordered + ["2026.2", "Y-2026.03", "Y-2026.03-SP2", "V-2023.12-SP1-2", "4.1", "4.1.2",
                            "v2025_2", "RevB"]:
            self.assertEqual(checker.version_numbers(v), editions.version_key(v), v)
            self.assertEqual(self.server.version_sort(v), editions.version_sort(v), v)

        # A filename's version is read whole, service pack included.
        packed = self.tmp / "guide_2026.1-SP2.pdf"
        write_pdf(packed, [[("Tool Guide", 24), ("Software Version 2026.1-SP2", 11)]], [[1, "Tool Guide", 1]])
        self.assertEqual(editions.read_cover(packed).version, "2026.1-SP2",
                         "a filename read without its service pack contradicted the cover")
        self.assertEqual(editions.filename_version("guide_2026.1.3.pdf"), "2026.1.3")
        self.assertEqual(editions.filename_version("tshell_ref_2026_2.pdf"), "2026.2")
        # Digits after the release in a filename are a revision counter or a
        # date as often as part of the version. They do not contradict a cover
        # that gives the release they follow; another release does.
        for name in ("guide_2026_2_2.pdf", "ug_2026_2_001.pdf", "guide_2026_2_20260301.pdf", "guide_2026.2-SP1.pdf"):
            numbered = self.tmp / name
            write_pdf(numbered, [[("Tool Guide", 24), ("Software Version 2026.2", 11)]], [[1, "Tool Guide", 1]])
            self.assertEqual(editions.read_cover(numbered).version, "2026.2", f"{name} contradicted its own cover")
        other = self.tmp / "guide_2025_2.pdf"
        write_pdf(other, [[("Tool Guide", 24), ("Software Version 2026.2", 11)]], [[1, "Tool Guide", 1]])
        self.assertIsNone(editions.read_cover(other).version, "a filename naming another release passed")

        # A document's own revision is not the release of the tool it describes.
        revised = self.tmp / "revised.pdf"
        write_pdf(revised, [[("Tool Guide", 24), ("Document Version 1.3", 11)]], [[1, "Tool Guide", 1]])
        self.assertIsNone(editions.read_cover(revised).version, "a document revision was recorded as the release")
        titled = self.tmp / "titled.pdf"
        write_pdf(titled, [[("Tool User Guide Version 4.1", 24)]], [[1, "Tool Guide", 1]])
        self.assertEqual(editions.read_cover(titled).version, "4.1")

        # A filename with nothing to make a slug from still gets one.
        odd = self.tmp / "Odd" / "new_docs"
        odd.mkdir(parents=True)
        write_pdf(odd / "\u624b\u518c.pdf", [[("Guide", 24), ("Nothing here names a release.", 11)]], [[1, "Guide", 1]])
        plan = editions.plan(odd / "\u624b\u518c.pdf", None, self.tmp / "odd-out", None, None)
        self.assertEqual((plan.slug, plan.doc_id), ("document", "document"), "an empty slug is docs/ itself")

    def test_21_search_answers_from_one_edition_per_manual(self):
        """Two releases indexed, one answering -- and never a stand-in.

        Both editions say almost the same thing, so without the filter every
        question is answered twice with nothing to say which hit is in use.
        """
        self.use_gadgets("one-edition")
        r = self.build()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # Questions are written against what search can find: the sampler
        # draws from the edition in use, not from every one on disk.
        r = run("sample_sections.py", "--root", str(self.root), "--n", "20", "--min-chars", "40")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("gadget-2026-1", r.stdout)
        self.assertNotIn("gadget-2025-1", r.stdout, "a section of an edition search never reads was sampled")
        s = self.serve()
        # Both editions have sections/002-unpacking.md. Without the edition's
        # folder in the path, it is the current one that is meant.
        self.assertIn("gadget-2026-1", s.tool_get_section({"file": "sections/002-unpacking.md"}),
                      "a path two editions share returned the older one")
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

        # The catalog follows the pin as well, and until it is rebuilt the
        # checker says it does not.
        catalog = self.coll / "docs" / "index.json"
        r = run("check_corpus.py", str(self.coll), "--no-pdf")
        self.assertIn("index-stale", r.stdout, "a catalog naming the wrong current edition went unreported")
        r = run("build_index.py", str(self.coll))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pinned = catalog.read_bytes()
        self.assertEqual({e["slug"]: e["current"] for e in json.loads(pinned)["manuals"]},
                         {"gadget-2025-1": True, "gadget-2026-1": False})
        r = run("check_corpus.py", str(self.coll), "--no-pdf")
        self.assertNotIn("index-stale", r.stdout, r.stdout)

        before = self.db.read_bytes()
        pins.write_text(json.dumps({"gadget": "2024.1"}), encoding="utf-8")
        r = self.build()
        self.assertNotEqual(r.returncode, 0, "a pin on a version that is not here was accepted")
        self.assertEqual(self.db.read_bytes(), before, "a failed build replaced the index")
        # ...and like the index, is left as it was when the pin matches
        # nothing, not rewritten to advertise the newest edition.
        r = run("build_index.py", str(self.coll))
        self.assertNotEqual(r.returncode, 0, "build_index.py accepted a pin that matches nothing")
        self.assertEqual(catalog.read_bytes(), pinned, "a catalog was overwritten by a build that failed")
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
        self.use_gadgets("compare")
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
        # A rebuild keeps what the document is: no --version, no --doc-id, no --title.
        r = run("rebuild_reference.py", str(source / "widget-ref-2.pdf"), "--slug", "widget-ref-v2", "--replace")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        m = self.manifest("widget-ref-v2")
        self.assertEqual((m["doc_id"], m["version"], m["title"]), ("widget-ref", "2.0", "Widget Reference"),
                         "--replace lost the edition a document was converted as")
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
        # Code is compared as written: inside a code span or a fenced block a
        # `*`, a `_` or a backslash is the command's, not Markdown's.
        for code, other in (("`*foo*`", "`foo`"), ("`_foo_`", "`foo`"), ("`__init__`", "`init`"),
                            (r"`a\_b`", "`a_b`"), ("`**kwargs**`", "`kwargs`")):
            self.assertNotEqual(key(f"set_pattern -pattern {code}"), key(f"set_pattern -pattern {other}"),
                                f"{code} and {other} compare equal")
        self.assertEqual(key("**`foo`**"), key("foo"), "emphasis around a code span was kept")
        fenced = "Run it:\n```\nset_pattern *foo*\n```\nThen use *foo* here.\n"
        plain = "Run it:\n```\nset_pattern foo\n```\nThen use foo here.\n"
        changed = [a for a, b in zip(s.keyed_lines(fenced), s.keyed_lines(plain)) if a.key != b.key]
        self.assertEqual([line.text for line in changed], ["set_pattern *foo*"],
                         "only the line inside the fence differs; emphasis in the prose line does not")
        # Exactly as written: spacing and quote style inside code are the code's.
        self.assertNotEqual(key('print("a  b")', literal=True), key('print("a b")', literal=True))
        self.assertNotEqual(key('call `f("a  b")` now'), key('call `f("a b")` now'))
        self.assertNotEqual(key("print(\u201ca\u201d)", literal=True), key('print("a")', literal=True))
        self.assertEqual(key('print("a  b")', literal=True, exact=False), key('print("a b")', literal=True, exact=False))
        # A line of code with no letter in it is still a line of code.
        self.assertEqual([line.key for line in s.keyed_lines("```\n[\n+\n]\n```\n---\n")], ["[", "+", "]"])

        listing = s.tool_compare_versions({"document": "widget-ref", "from_version": "1.0", "to_version": "2.0"})
        added, removed = listing.split("## Removed since")
        self.assertIn("set_widget_option_25", added)
        self.assertIn("set_widget_option_26", added)
        self.assertIn("set_widget_option_03", removed)
        self.assertNotIn("set_widget_option_07", listing, "an entry in both editions was listed")

        self.assertIn("removed between them",
                      s.tool_compare_versions({"document": "widget-ref", "name": "set_widget_option_03"}))
        # A typo is answered from the edition that was asked, not from one
        # that happens to hold the closest name.
        typo = s.tool_lookup_entity({"name": "set_widget_optoin_03"})
        self.assertIn("Did you mean", typo)
        self.assertNotIn("set_widget_option_03", typo, "a typo was corrected to an entry only an older edition has")
        self.assertIn("set_widget_option_03", s.tool_lookup_entity(
            {"name": "set_widget_optoin_03", "document": "widget-ref", "version": "1.0"}))

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
        self.use_gadgets("serve-anywhere")
        import shutil
        config = self.root / ".vscode" / "mcp.json"
        config.parent.mkdir(exist_ok=True)
        # This corpus, registered earlier by absolute path under someone's own
        # name -- and another corpus registered the same way, which is not
        # this build's to touch just because it runs a file of the same name.
        other = {"type": "stdio", "command": "python",
                 "args": ["X:\\old\\checkout\\scripts\\mcp_server.py", "--db", "X:\\other\\index.sqlite3"]}
        config.write_text(json.dumps({"servers": {
            "my-manuals": {"type": "stdio", "command": "python",
                           "args": ["X:\\old\\checkout\\scripts\\mcp_server.py", "--db",
                                    str(self.root / "mcp-index.sqlite3")]},
            "other-manuals": other}}), encoding="utf-8")
        for _ in range(2):                      # emitting twice must not add a second entry
            r = run("build_search_db.py", "--root", str(self.root), "--emit-vscode-config")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        servers = json.loads(config.read_text(encoding="utf-8"))["servers"]
        self.assertEqual(list(servers), ["my-manuals", "other-manuals"], "this corpus's entry was not reused")
        self.assertEqual(servers["my-manuals"]["args"],
                         ["${workspaceFolder}/mcp_server.py", "--db", "${workspaceFolder}/mcp-index.sqlite3"])
        self.assertEqual(servers["other-manuals"], other, "another corpus's server was pointed at this index")
        self.assertNotIn(self.root.parent.name, config.read_text(encoding="utf-8"), "the config names this machine's paths")
        self.assertTrue((self.root / "mcp_server.py").is_file(), "the server was not copied beside the index")

        moved = self.tmp / "Elsewhere"
        shutil.copytree(self.root, moved)
        r = run("mcp_smoke_test.py", "--db", str(moved / "mcp-index.sqlite3"),
                "--server", str(moved / "mcp_server.py"))
        self.assertEqual(r.returncode, 0, "the copied corpus could not serve itself:\n" + r.stdout + r.stderr)
        corpus = self.server.Corpus(moved / "mcp-index.sqlite3")
        self.assertEqual(corpus.root, moved, "a moved corpus still reads figures and PDFs from where it was built")
        corpus._db.close()

        # The same for an index kept in a subfolder of its corpus: it has to
        # find its way back up, not fall through to the path it was built at.
        r = run("build_search_db.py", "--root", str(self.root), "--out", str(self.root / "indexes" / "nested.sqlite3"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        nested = self.tmp / "ElsewhereNested"
        shutil.copytree(self.root, nested)
        corpus = self.server.Corpus(nested / "indexes" / "nested.sqlite3")
        self.assertEqual(corpus.root, nested, "an index in a subfolder of a moved corpus reads the old corpus")
        corpus._db.close()

    def test_22c_an_entry_naming_the_index_another_way_is_reused(self):
        """An editor entry that reaches this index by another path is still
        this index's entry.

        The build resolves its root, while an entry written earlier may name
        the same folder through a symlink, a junction or a Windows 8.3 short
        name. Compared as text, those looked like another corpus's entry, and
        every build added a second server for the same index. Windows CI
        caught it through the runner's short temp path; a symlink is the same
        failure wherever symlinks can be made."""
        real = self.tmp / "RealRoot"
        coll = real / "Gadgets"
        for folder in ("new_docs", "docs"):
            (coll / folder).mkdir(parents=True)
        gadget_fixture(coll / "new_docs" / "g.pdf", "2025.1", ["Setup"])
        r = run("convert_manual.py", str(coll / "new_docs" / "g.pdf"), "--title", "G")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        link = self.tmp / "LinkedRoot"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks cannot be made here; test_22b covers Windows short names")
        config = real / ".vscode" / "mcp.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"servers": {"my-manuals": {
            "type": "stdio", "command": "python",
            "args": ["X:\\old\\checkout\\scripts\\mcp_server.py", "--db", str(link / "mcp-index.sqlite3")]}}}),
            encoding="utf-8")
        r = run("build_search_db.py", "--root", str(link), "--emit-vscode-config")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        servers = json.loads(config.read_text(encoding="utf-8"))["servers"]
        self.assertEqual(list(servers), ["my-manuals"], "a second server was added for the same index")

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

    def test_25b_an_unreadable_manifest_stops_the_build(self):
        """A manifest that cannot be read is not a document to leave out.

        Skipped, the newest edition of a manual drops out of the inventory,
        the one before it is marked current, and every default answer comes
        from an older release with nothing to say so.
        """
        import shutil
        root = self.tmp / "BrokenManifest"
        handmade_document(root, "hand-1")
        shutil.copytree(root / "docs" / "hand-1", root / "docs" / "hand-2")
        for n in ("1", "2"):
            path = root / "docs" / f"hand-{n}" / "manifest.json"
            m = json.loads(path.read_text(encoding="utf-8"))
            m.update(slug=f"hand-{n}", doc_id="hand", version=f"{n}.0")
            path.write_text(json.dumps(m, indent=2), encoding="utf-8")
        db = root / "mcp-index.sqlite3"
        r = run("build_search_db.py", "--root", str(root))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        before = db.read_bytes()

        newest = root / "docs" / "hand-2" / "manifest.json"
        newest.write_text(newest.read_text(encoding="utf-8")[:-40], encoding="utf-8")   # cut off mid-write
        r = run("build_search_db.py", "--root", str(root))
        self.assertNotEqual(r.returncode, 0, "the build went ahead without the newest edition's manifest")
        self.assertIn("hand-2/manifest.json", r.stdout + r.stderr)
        self.assertEqual(db.read_bytes(), before, "a build that could not read every manifest replaced the index")

    def test_25c_a_filed_pdf_is_never_overwritten(self):
        """PTUG.PDF is a PDF, and a filed one is never replaced.

        A glob for *.pdf does not see it where filenames are case-sensitive,
        so the next release of the same name looked free to file -- and a
        rename replaces an existing file silently on POSIX.
        """
        coll = self.tmp / "Shouting" / "Tools"
        for folder in ("new_docs", "source", "docs"):
            (coll / folder).mkdir(parents=True)
        gadget_fixture(coll / "source" / "PTUG.PDF", "2025.1", ["Setup"])
        editions = load_script("editions")
        self.assertEqual([p.name for p in editions.filed_pdfs(coll)], ["PTUG.PDF"])

        gadget_fixture(coll / "new_docs" / "PTUG.PDF", "2026.1", ["Setup", "Extras"])
        r = run("convert_manual.py", str(coll / "new_docs" / "PTUG.PDF"), "--title", "Tool Guide")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        m = json.loads((coll / "docs" / "ptug-2026-1" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(m["source_pdf"], "PTUG_2026.1.PDF")
        first = pymupdf.open(str(coll / "source" / "PTUG.PDF"))
        self.assertIn("2025.1", first[0].get_text(), "the PDF already filed was replaced by the new release")
        first.close()

        # And if the name is taken by the time the conversion ends, the PDF
        # is filed under another, and the manifest -- written before, with
        # the name that was free then -- is corrected. Left alone it would
        # name the other PDF, and this edition's pages would come from it.
        waiting = coll / "new_docs" / "late.pdf"
        waiting.write_bytes(b"new")
        (coll / "source" / "late.pdf").write_bytes(b"already filed")
        (coll / "docs" / "late").mkdir()
        manifest = coll / "docs" / "late" / "manifest.json"
        manifest.write_text(json.dumps({"slug": "late", "source_pdf": "late.pdf"}), encoding="utf-8")
        editions.finish(editions.Plan(waiting, coll, coll / "docs", "late", "late", None, False, "late.pdf", True))
        self.assertEqual((coll / "source" / "late.pdf").read_bytes(), b"already filed")
        filed = json.loads(manifest.read_text(encoding="utf-8"))["source_pdf"]
        self.assertNotEqual(filed, "late.pdf", "the manifest still names the PDF another edition filed")
        self.assertEqual((coll / "source" / filed).read_bytes(), b"new")
        self.assertEqual(editions.find_source_pdf(coll, filed).read_bytes(), b"new")
        self.assertFalse(waiting.exists())

    def test_25d_code_in_an_entry_is_compared_as_code(self):
        """An entry whose example changed is not "No differences".

        Two releases of one entry, the same but for a fenced example that
        runs across a chunk boundary. In the second chunk a wildcard loses
        its asterisks, a string loses a space, and a `+` becomes a `-`. Each
        was invisible once: emphasis was stripped inside code, spacing was
        collapsed in it, lines without a letter were dropped, and a chunk
        that began inside a fence was read as prose.
        """
        import shutil
        root = self.tmp / "CodeEntry"
        handmade_document(root, "hand-1")
        shutil.copytree(root / "docs" / "hand-1", root / "docs" / "hand-2")
        variants = {"1": ("set_pattern *foo*", 'print("a  b")', "+"),
                    "2": ("set_pattern foo", 'print("a b")', "-")}
        for n, (pattern, text, sign) in variants.items():
            doc = root / "docs" / f"hand-{n}"
            m = json.loads((doc / "manifest.json").read_text(encoding="utf-8"))
            m.update(slug=f"hand-{n}", doc_id="hand", version=f"{n}.0")
            first, second = m["sections"][1], m["sections"][2]
            first["command"] = second["command"] = "Alpha Setup"
            (doc / first["file"]).write_text(
                f"*{first['breadcrumb']}*\n\n## Alpha Setup\n\nRun it:\n```\n[\nset_pattern *foo*\n", encoding="utf-8")
            (doc / second["file"]).write_text(
                f"*{second['breadcrumb']}*\n\n{pattern}\n{text}\n{sign}\n]\n```\nThen use *foo* here.\n",
                encoding="utf-8")
            (doc / "manifest.json").write_text(json.dumps(m, indent=2), encoding="utf-8")
        r = run("build_search_db.py", "--root", str(root))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.server.CORPUS = corpus = self.server.Corpus(root / "mcp-index.sqlite3")
        self.addCleanup(lambda: corpus._db and corpus._db.close())
        out = self.server.tool_compare_versions({"document": "hand", "name": "Alpha Setup"})
        corpus._db.close()
        self.assertNotIn("No differences", out)
        only_old, rest = out.split("## Only in 2.0")
        only_new, spacing = rest.split("## Code that differs only in spacing")
        self.assertIn("- set_pattern *foo*", only_old, "a wildcard in a chunk that starts inside a fence was read as emphasis")
        self.assertIn("- set_pattern foo", only_new)
        self.assertIn("- +", only_old, "a line of code with no letter in it was dropped")
        self.assertIn("- -", only_new)
        self.assertIn('1.0: print("a  b")', spacing, "a change of spacing inside code was not reported")
        self.assertIn('2.0: print("a b")', spacing)
        self.assertNotIn("Then use", out, "prose that differs in nothing was reported")
        self.assertNotIn("- [", out, "a line that is in both editions was reported")

    def test_25e_an_edition_that_names_no_entries_proves_nothing_about_one(self):
        """Absent from an edition with no entity rows is not "removed".

        rebuild_reference.py attributes entries only where it finds enough of
        them. A reference that shrinks below that still has every command and
        no entity rows, and an entry compared against it used to be reported
        as removed.
        """
        import shutil
        root = self.tmp / "Unattributed"
        handmade_document(root, "hand-1")
        shutil.copytree(root / "docs" / "hand-1", root / "docs" / "hand-2")
        for n in ("1", "2"):
            path = root / "docs" / f"hand-{n}" / "manifest.json"
            m = json.loads(path.read_text(encoding="utf-8"))
            m.update(slug=f"hand-{n}", doc_id="hand", version=f"{n}.0")
            if n == "1":
                m["sections"][1]["command"] = "Alpha Setup"
            else:                               # a second section with a title the manual already has
                m["toc"].append({"level": 2, "title": "Alpha Setup", "page": 6})
            path.write_text(json.dumps(m, indent=2), encoding="utf-8")
        r = run("build_search_db.py", "--root", str(root))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.server.CORPUS = corpus = self.server.Corpus(root / "mcp-index.sqlite3")
        self.addCleanup(lambda: corpus._db and corpus._db.close())
        with self.assertRaises(ValueError) as unknown:
            self.server.tool_compare_versions({"document": "hand", "name": "Alpha Setup"})
        self.assertIn("no per-entry attribution", str(unknown.exception))
        listing = self.server.tool_compare_versions({"document": "hand"})
        corpus._db.close()
        self.assertIn("Only one of these editions names its entries", listing)
        self.assertIn("Alpha Setup (1 more than in 1.0)", listing,
                      "a heading added under a title the manual already had was not reported")
        self.assertNotIn("## Removed since 1.0\n- Alpha Setup", listing,
                         "an entry was reported removed from an edition that attributes nothing")

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

        # Within one collection too: `guide` is the doc_id of guide-1 and
        # guide-2, and the slug of a document that has nothing to do with
        # them. Comparing guide-2 with its 1.0 must stay inside the manual.
        shared = self.tmp / "SharedName"
        for slug, doc_id, version in (("guide", "unrelated", "1.0"), ("guide-1", "guide", "1.0"),
                                      ("guide-2", "guide", "2.0")):
            handmade_document(shared, slug)
            path = shared / "docs" / slug / "manifest.json"
            m = json.loads(path.read_text(encoding="utf-8"))
            m.update(doc_id=doc_id, version=version)
            if doc_id == "guide":
                m["toc"] = []                   # a manual whose PDFs have no bookmark outline
            path.write_text(json.dumps(m, indent=2), encoding="utf-8")
        r = run("build_search_db.py", "--root", str(shared))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.server.CORPUS = corpus = self.server.Corpus(shared / "mcp-index.sqlite3")
        self.addCleanup(lambda: corpus._db and corpus._db.close())
        out = self.server.tool_compare_versions({"document": "guide-2", "from_version": "1.0"})
        corpus._db.close()
        self.assertIn("(guide-1)", out, "the earlier edition of the manual was not the one compared")
        self.assertNotIn("(guide)", out, "a document that only shares the manual's name was compared instead")
        # No outline means no headings to compare, not a broken corpus.
        r = run("mcp_smoke_test.py", "--db", str(shared / "mcp-index.sqlite3"))
        self.assertEqual(r.returncode, 0, "a manual with no bookmark outline failed the smoke test:\n" + r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
