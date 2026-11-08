"""update_corpus.py: one command from the PDFs in new_docs/ to a checked,
indexed corpus, stopping where a step would have to guess."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _support import (SCRIPTS, WS, gadget_fixture, load_script, mixed_fixture, prose_fixture,  # noqa: E402
                      reference_fixture, run, tcl_reference_fixture, write_pdf)
import pymupdf  # noqa: E402

# Prose goes through convert_manual.py in every test. Docling is installed on
# some machines and not in CI, and the default follows whichever this is.
MANUAL = ("--prose-converter", "convert_manual")


def titled(path: Path, write, title: str) -> None:
    """A fixture PDF whose metadata carries `title` and whose first page
    prints it, the way a vendor's PDF does."""
    write(path)
    doc = pymupdf.open(path)
    doc.set_metadata({"title": title})
    doc[0].insert_text((72, 40), title, fontsize=9)
    doc.saveIncr()
    doc.close()


def tree(root: Path) -> dict[str, str]:
    """Every file under `root`, by content."""
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def indexed(db: Path) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return sorted(r[0] for r in con.execute("SELECT slug FROM documents"))
    finally:
        con.close()


class UpdateTest(unittest.TestCase):
    """update_corpus.py on fixture PDFs, each test in a corpus of its own."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.tmp = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def inbox_corpus(self, name: str) -> tuple[Path, Path]:
        """(root, collection): a corpus with nothing converted and three PDFs
        waiting: a prose manual and a reference whose metadata titles their
        covers print, and a manual whose cover title nothing confirms."""
        root = self.tmp / name
        inbox = root / "Widgets" / "new_docs"
        inbox.mkdir(parents=True)
        titled(inbox / "widget-guide.pdf", prose_fixture, "Widget Installation Guide")
        titled(inbox / "widget-commands.pdf", lambda p: reference_fixture(p, 25), "Widget Command Reference")
        gadget_fixture(inbox / "gadget.pdf", "2026.1", ["Unpacking", "Mounting"])
        return root, root / "Widgets"

    def test_53_one_command_brings_a_corpus_up_to_date(self):
        root, coll = self.inbox_corpus("t53")
        waiting = (coll / "new_docs" / "gadget.pdf").read_bytes()
        r = run("update_corpus.py", str(root), *MANUAL)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 2, "a PDF still waiting on a decision should exit 2:\n" + out)

        # Each step is shown as the command line it is, so it can be rerun by hand.
        self.assertRegex(out, r'\$ python \S*convert_manual\.py \S*widget-commands\.pdf --title '
                              r'"Widget Command Reference" --slug widget-commands --shape reference')
        self.assertRegex(out, r'\$ python \S*convert_manual\.py \S*widget-guide\.pdf --title '
                              r'"Widget Installation Guide" --slug widget-guide --shape prose')
        where = []
        for step in ("convert_manual.py", "enrich_chunks.py", "extract_figures.py", "build_index.py",
                     "check_corpus.py", "build_search_db.py", "mcp_smoke_test.py"):
            found = re.search(rf"\$ python \S*{re.escape(step)}", out)
            self.assertIsNotNone(found, f"{step} was not run, or not shown")
            where.append(found.start())
        self.assertEqual(where, sorted(where), "the steps ran out of the long form's order")
        self.assertRegex(out, r"\$ python \S*enrich_chunks\.py \S+ --only widget-guide\n",
                         "enrich_chunks.py should run on the prose document alone")

        self.assertEqual(sorted(p.name for p in (coll / "source").iterdir()),
                         ["widget-commands.pdf", "widget-guide.pdf"])
        self.assertEqual((coll / "new_docs" / "gadget.pdf").read_bytes(), waiting,
                         "a PDF with no title was touched")
        self.assertFalse((coll / "docs" / "gadget-2026-1").exists(), "a document was made without a title")
        for slug, title in (("widget-guide", "Widget Installation Guide"), ("widget-commands", "Widget Command Reference")):
            m = json.loads((coll / "docs" / slug / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(m["title"], title)
        db = root / "mcp-index.sqlite3"
        self.assertEqual(indexed(db), ["widget-commands", "widget-guide"])
        self.assertFalse((root / "mcp-index.candidate.sqlite3").exists(), "the candidate was left behind")

        report = out[out.rindex("update_corpus:"):]
        self.assertIn("Documents added: 2 converted now", report)
        self.assertRegex(report, r"Widgets/widget-commands\s+convert_manual \(reference\)\s+\d+\s+100%\s+\d+%")
        self.assertRegex(report, r"Widgets/widget-guide\s+convert_manual \(prose\)\s+\d+\s+100%\s+-")
        self.assertRegex(report, r"Checker: 0 failing, \d+ warning")
        self.assertIn("Smoke test: passed", report)
        # As a path, not as text: Windows hands out temp folders in 8.3 short
        # form (RUNNER~1), and the script prints the resolved name.
        replaced = re.search(r"^Index: replaced: (.+)$", report, re.M)
        self.assertIsNotNone(replaced, "the index was not reported replaced")
        self.assertEqual(os.path.normcase(os.path.realpath(replaced.group(1))),
                         os.path.normcase(os.path.realpath(db)))
        self.assertRegex(report, r"Widgets/new_docs/gadget\.pdf\n\s+- no title: .*Gadget Guide.*--titles")

        # Nothing new: nothing is written, the index included.
        before = tree(root)
        r = run("update_corpus.py", str(root), *MANUAL)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("Index: unchanged", r.stdout)
        self.assertEqual(tree(root), before, "a run with nothing to do changed files")

        # The decision, once given, converts the PDF that waited.
        titles = self.tmp / "t53-titles.json"
        titles.write_text(json.dumps({"gadget.pdf": "Gadget Guide"}), encoding="utf-8")
        r = run("update_corpus.py", str(root), *MANUAL, "--titles", str(titles))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(indexed(db), ["gadget-2026-1", "widget-commands", "widget-guide"])
        self.assertEqual(list((coll / "new_docs").iterdir()), [])

    def test_53b_a_dry_run_changes_nothing(self):
        root, _coll = self.inbox_corpus("t53b")
        before = tree(root)
        r = run("update_corpus.py", str(root), "--dry-run", *MANUAL)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(tree(root), before, "--dry-run wrote something")
        self.assertRegex(out, r"=> python \S*convert_manual\.py \S*widget-guide\.pdf .*--shape prose")
        self.assertRegex(out, r"=> python \S*convert_manual\.py \S*widget-commands\.pdf .*--shape reference")
        self.assertRegex(out, r"gadget\.pdf\n(.*\n)*?  WAITS: no title")
        self.assertRegex(out, r"enrich_chunks\.py \S+ --only widget-guide\n")
        self.assertRegex(out, r"check_corpus\.py \S+ --strict --only widget-commands --only widget-guide\n")
        self.assertIn("Dry run: nothing was changed.", out)

    def test_53c_the_index_is_replaced_only_when_search_does_not_get_worse(self):
        root, coll = self.inbox_corpus("t53c")
        (coll / "new_docs" / "gadget.pdf").unlink()
        r = run("update_corpus.py", str(root), *MANUAL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        db = root / "mcp-index.sqlite3"

        def section(slug: str, test) -> str:
            m = json.loads((coll / "docs" / slug / "manifest.json").read_text(encoding="utf-8"))
            return next(s["file"] for s in m["sections"] if test(s))
        questions = self.tmp / "t53c-questions.jsonl"
        questions.write_text("\n".join(json.dumps(q) for q in [
            {"id": "q1", "kind": "concept", "question": "mounting the widget",
             "answers": [{"slug": "widget-guide", "file": section("widget-guide", lambda s: s["heading"].strip("# ") == "Mounting")}]},
            {"id": "q2", "kind": "identifier", "question": "set_widget_option_07",
             "answers": [{"slug": "widget-commands", "file": section("widget-commands", lambda s: s.get("command") == "set_widget_option_07")}]},
        ]) + "\n", encoding="utf-8")
        r = run("eval_search.py", "--db", str(db), "--questions", str(questions))
        self.assertRegex(r.stdout, r"all\s+2\s+100%", "the questions should both be answered first to begin with")

        # A manual that answers the first question better than its answer does.
        pages = [[("Widget Mounting Notes", 24), ("Notes on mounting the widget.", 11)]]
        toc = [[1, "Overview", 1]]
        for heading in ("Mounting the Widget", "Widget Mounting Checks"):
            # Two lines, past the 100 characters a page pick_extractor.py takes for a text layer.
            pages.append([(heading, 18), ("Mounting the widget: mount the widget level when mounting the widget.", 11),
                          ("Check the widget mounting twice before the widget is mounted for use.", 11)])
            toc.append([1, heading, len(pages)])
        notes = coll / "new_docs" / "mounting-notes.pdf"
        titled(notes, lambda p: write_pdf(p, pages, toc), "Widget Mounting Notes")
        # The server copy beside the index goes with it: a candidate that is
        # refused leaves the copy that matched the old one.
        server_copy = root / "mcp_server.py"
        server_copy.write_text("# the copy that served the old index\n", encoding="utf-8")
        old = db.read_bytes()

        r = run("update_corpus.py", str(root), *MANUAL, "--questions", str(questions))
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 1, out)
        self.assertEqual(db.read_bytes(), old, "a candidate that scores lower replaced the index")
        self.assertTrue((root / "mcp-index.candidate.sqlite3").is_file(), "the refused candidate was not kept")
        self.assertRegex(out, r"\[q1\]\s+concept\s+1 -> (?!1 )", "the question that moved was not listed")
        self.assertRegex(out, r"Eval: .*hit@5 100% -> 100%, MRR 1\.000 -> 0\.\d+")
        self.assertIn("Index: kept as it was", out)
        self.assertEqual(server_copy.read_text(encoding="utf-8"), "# the copy that served the old index\n",
                         "the server copy no longer matches the index it serves")
        self.assertEqual(list((coll / "new_docs").iterdir()), [], "the PDF was converted and should be filed")

        # The next run carries on from there: the document is new to the index.
        r = run("update_corpus.py", str(root), *MANUAL, "--questions", str(questions), "--accept-regression")
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("Widgets/mounting-notes *", out)
        self.assertIn("taken with --accept-regression", out)
        self.assertNotRegex(out, r"\$ python \S*(enrich_chunks|extract_figures)\.py",
                            "a document finished by the run before was processed again")
        self.assertEqual(indexed(db), ["mounting-notes", "widget-commands", "widget-guide"])
        self.assertIn("A copy, placed here", server_copy.read_text(encoding="utf-8"))
        self.assertFalse((root / "mcp-index.candidate.sqlite3").exists())

    def test_53d_a_failing_check_stops_before_the_index(self):
        """A document the index does not hold is checked before an index is
        built, and one FAIL stops the run there. A document converted earlier
        is not rewritten: enrich_chunks.py edits in place."""
        root = WS.fresh(WS.corpus(), "t53d")
        manifest_path = root / "docs" / "prose" / "manifest.json"
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
        m["sections"][0]["page_start"] = 9999
        manifest_path.write_text(json.dumps(m, indent=2), encoding="utf-8")
        sections = {slug: tree(root / "docs" / slug / "sections") for slug in ("prose", "ref")}

        r = run("update_corpus.py", str(root), *MANUAL)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("page-invalid", out)
        self.assertRegex(out, r"STOPPED: check_corpus\.py found \d+ failing check")
        self.assertNotRegex(out, r"\$ python \S*build_search_db\.py", "an index was built after a FAIL")
        self.assertFalse((root / "mcp-index.sqlite3").exists())
        self.assertFalse((root / "mcp-index.candidate.sqlite3").exists())
        self.assertNotRegex(out, r"\$ python \S*enrich_chunks\.py", "an enriched document was enriched again")
        # The report tells a reference that owns no chunk from prose, which owns none by design.
        self.assertRegex(out, r"/tiny \*\s+rebuild_reference \(reference\)\s+\d+\s+\d+%\s+0%\s",
                         "a reference with its attribution declined should show 0% owned")
        self.assertRegex(out, r"/prose \*\s+convert_manual \(prose\)\s+\d+\s+\d+%\s+-\s")
        for slug, files in sections.items():
            self.assertEqual(tree(root / "docs" / slug / "sections"), files, f"{slug}'s sections were rewritten")

    def test_53e_what_waits_and_why(self):
        """Every PDF this cannot decide about waits, with what to decide, and
        the decision given in --titles is what the converter is told."""
        uc = load_script("update_corpus")
        coll = self.tmp / "t53e" / "Widgets"
        (coll / "new_docs").mkdir(parents=True)

        def plan(name, write, decision=None, prose_converter="convert_manual", docling=False, title="A Widget Manual"):
            pdf = coll / "new_docs" / name
            if not pdf.exists():
                titled(pdf, write, title) if title else write(pdf)
            return uc.plan_pdf(coll, pdf, decision or {}, prose_converter, docling)

        it = plan("guide.pdf", prose_fixture, prose_converter=None)
        self.assertIsNone(it.command)
        self.assertRegex(" ".join(it.held), r"pip install docling.*--prose-converter convert_manual")
        it = plan("guide.pdf", prose_fixture, {"converter": "convert_docling"})
        self.assertRegex(" ".join(it.held), r"Docling is not installed")
        it = plan("guide.pdf", prose_fixture, prose_converter=None, docling=True)
        self.assertEqual((it.held, Path(it.command[1]).name), ([], "convert_docling.py"))
        self.assertNotIn("--shape", it.command, "convert_docling.py takes no --shape")
        it = plan("guide.pdf", prose_fixture)
        self.assertEqual(it.held, [])
        self.assertEqual(it.command[2:], [str(coll / "new_docs" / "guide.pdf"), "--title", "A Widget Manual",
                                          "--slug", "guide", "--shape", "prose"])

        # A new edition takes its manual's converter, over the default.
        shelf = self.tmp / "t53e" / "Gadgets Shelf"
        (shelf / "new_docs").mkdir(parents=True)
        (shelf / "docs" / "gadget-2025-1").mkdir(parents=True)
        (shelf / "docs" / "gadget-2025-1" / "manifest.json").write_text(json.dumps(
            {"slug": "gadget-2025-1", "doc_id": "gadget", "version": "2025.1",
             "converter": {"script": "convert_manual.py"}}), encoding="utf-8")
        titled(shelf / "new_docs" / "gadget.pdf", lambda p: gadget_fixture(p, "2026.1", ["Unpacking", "Mounting"]),
               "Gadget Guide")
        for docling in (False, True):
            it = uc.plan_pdf(shelf, shelf / "new_docs" / "gadget.pdf", {}, None, docling)
            self.assertEqual((it.held, Path(it.command[1]).name, it.command[-2:]),
                             ([], "convert_manual.py", ["--shape", "prose"]), f"docling={docling}")
            self.assertIn("converter: convert_manual.py, as for this manual's other editions", it.edition)
        it = uc.plan_pdf(shelf, shelf / "new_docs" / "gadget.pdf", {"converter": "convert_docling"}, None, True)
        self.assertEqual(Path(it.command[1]).name, "convert_docling.py", "the PDF's own decision should win")

        it = plan("mixed.pdf", mixed_fixture)
        self.assertRegex(" ".join(it.held), r"part prose, part reference")
        it = plan("mixed.pdf", mixed_fixture, {"shape": "prose", "title": "Gadget Book"})
        self.assertEqual(it.held, [])
        self.assertEqual(it.command[3:7], ["--title", "Gadget Book", "--slug", "mixed"])
        self.assertEqual(it.command[-2:], ["--shape", "prose"])

        it = plan("tcl.pdf", lambda p: tcl_reference_fixture(p, 30))
        self.assertRegex(" ".join(it.held), r"no TOC level has 20 titles.*L1.*command_level")
        it = plan("tcl.pdf", None, {"command_level": 1, "version": "2026.1"})
        self.assertEqual(it.held, [])
        self.assertEqual(it.command[-6:], ["--shape", "reference", "--command-level", "1", "--version", "2026.1"])
        self.assertEqual(it.slug, "tcl-2026-1", "the slug should carry the version given")

        it = plan("bare.pdf", prose_fixture, title=None)
        self.assertRegex(" ".join(it.held), r"no title: the PDF's metadata has no title")
        (coll / "docs" / "taken").mkdir(parents=True)
        (coll / "docs" / "taken" / "manifest.json").write_text("{}", encoding="utf-8")
        it = plan("taken.pdf", prose_fixture)
        self.assertRegex(" ".join(it.held), r"docs/taken already exists.*--replace")

        # One name waiting in two collections needs saying which.
        other = self.tmp / "t53e" / "Gadgets"
        (other / "new_docs").mkdir(parents=True)
        shutil.copy(coll / "new_docs" / "guide.pdf", other / "new_docs" / "guide.pdf")
        intakes, unused = uc.plan_intake([other, coll], {"guide.pdf": {"title": "Shared Guide"},
                                                         "nope.pdf": {"title": "x"}}, "convert_manual", False)
        guides = [i for i in intakes if i.pdf.name == "guide.pdf"]
        self.assertTrue(all(re.search(r'waits in more than one collection.*"(Gadgets|Widgets)/guide.pdf"',
                                      " ".join(i.held)) for i in guides), [i.held for i in guides])
        self.assertEqual(unused, ["nope.pdf"])
        shutil.copy(coll / "new_docs" / "guide.pdf", coll / "new_docs" / "guide-copy.pdf")
        intakes, _ = uc.plan_intake([coll], {}, "convert_manual", False)
        held = {i.pdf.name: " ".join(i.held) for i in intakes}
        self.assertIn("byte for byte guide-copy.pdf", held["guide.pdf"])
        self.assertNotIn("byte for byte", held["guide-copy.pdf"], "both copies were held")
        (coll / "new_docs" / "guide-copy.pdf").unlink()
        intakes, _ = uc.plan_intake([other, coll], {"Gadgets/guide.pdf": {"title": "Shared Guide"}},
                                    "convert_manual", False)
        self.assertEqual([i.title for i in intakes if i.pdf.name == "guide.pdf" and i.collection == other],
                         ["Shared Guide"])

        # Two releases of one manual waiting together are checked against
        # each other, not only against what is filed: one with no version, or
        # the same release twice, waits with that reason.
        releases = self.tmp / "t53e" / "Releases"
        (releases / "new_docs").mkdir(parents=True)
        titled(releases / "new_docs" / "gadget_2025_1.pdf",
               lambda p: gadget_fixture(p, "2025.1", ["Unpacking", "Mounting"]), "Gadget Guide")
        titled(releases / "new_docs" / "gadget_2026_1.pdf", prose_fixture, "Gadget Guide")   # no version on it
        intakes, _ = uc.plan_intake([releases], {}, "convert_manual", False)
        held = {i.pdf.name: " ".join(i.held) for i in intakes}
        self.assertEqual(held["gadget_2025_1.pdf"], "")
        self.assertRegex(held["gadget_2026_1.pdf"], r"gadget_2025_1\.pdf, waiting beside it, is another edition "
                                                    r"of gadget, and this one has no version")
        copies = self.tmp / "t53e" / "Copies"
        (copies / "new_docs").mkdir(parents=True)
        for name, sections in (("gadget.pdf", ["Unpacking", "Mounting"]), ("gadget_2026_1.pdf", ["Mounting", "Storage"])):
            titled(copies / "new_docs" / name, lambda p, s=sections: gadget_fixture(p, "2026.1", s), "Gadget Guide")
        intakes, _ = uc.plan_intake([copies], {}, "convert_manual", False)
        held = {i.pdf.name: " ".join(i.held) for i in intakes}
        self.assertEqual(held["gadget.pdf"], "")
        self.assertIn("gadget.pdf, waiting beside it, is the same release of gadget (2026.1)", held["gadget_2026_1.pdf"])

        bad = self.tmp / "t53e-bad.json"
        for wrong, why in (([], "object keyed"), ({"a.pdf": {"titel": "x"}}, "titel"),
                           ({"a.pdf": {"command_level": "2"}}, "command_level"),
                           ({"a.pdf": {"shape": "reference", "converter": "convert_manual"}}, "converter is for prose"),
                           ({"a.pdf": {"converter": "docling"}}, "converter must be"),
                           ({"a.pdf": {"title": " "}}, "title")):
            bad.write_text(json.dumps(wrong), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, why):
                uc.load_decisions(bad)
        bad.write_text(json.dumps({"a.pdf": "A Title", "b.pdf": {"shape": "prose"}}), encoding="utf-8")
        self.assertEqual(uc.load_decisions(bad), {"a.pdf": {"title": "A Title"}, "b.pdf": {"shape": "prose"}})

    def test_53f_a_title_is_taken_only_where_the_pdf_prints_it(self):
        """A wrong title does more than mislabel: the converters strip the
        lines that repeat it as page furniture. So the metadata title counts
        when the first pages print it, the cover's largest text when later
        pages print it as a line of its own, as a running title, and nothing
        else does."""
        uc = load_script("update_corpus")
        base = self.tmp / "t53f"
        base.mkdir()

        def pdf(name, pages, title=None):
            path = base / name
            write_pdf(path, pages, [[1, "Start", 1]])
            if title:
                doc = pymupdf.open(path)
                doc.set_metadata({"title": title})
                doc.saveIncr()
                doc.close()
            return pymupdf.open(path)

        body = [("Chapter Heading", 18), ("Some text about the widget and its parts.", 11)]
        cover = [("Widget Field Manual", 24), ("Software Version 2026.1", 11)]
        with pdf("meta.pdf", [cover] + [body] * 4, "Widget Field Manual") as doc:
            self.assertEqual(uc.find_title(doc), ("Widget Field Manual", "the PDF's metadata, printed on its cover"))
        with pdf("running.pdf", [cover] + [[("Widget Field Manual, v2026.1", 8)] + body] * 4,
                 "Microsoft Word - draft7.docx") as doc:
            self.assertEqual(uc.find_title(doc), ("Widget Field Manual", "the cover, repeated as the running title"),
                             "a running title with its release beside it is still the running title")
        with pdf("neither.pdf", [cover] + [body] * 4, "Microsoft Word - draft7.docx") as doc:
            title, why = uc.find_title(doc)
            self.assertIsNone(title)
            self.assertIn("'Microsoft Word - draft7.docx' is not printed on its first pages", why)
            self.assertIn("'Widget Field Manual' is not a line of its own on later pages", why)
        # A product name set largest above the real title is in every footer
        # that names the manual, and is still not its title: it waits.
        banner = [("Gizmo", 40), ("Gizmo Field Manual", 20), ("Software Version 2026.1", 11)]
        with pdf("banner.pdf", [banner] + [[("Gizmo Field Manual", 8)] + body] * 4) as doc:
            title, why = uc.find_title(doc)
            self.assertIsNone(title, "a banner that is part of the running title was taken for the title")
            self.assertIn("'Gizmo' is not a line of its own on later pages", why)
        with pdf("marks.pdf", [[("Widget® Field Manual", 24)]] + [body] * 2, "Widget(R) Field Manual") as doc:
            self.assertEqual(uc.find_title(doc)[0], "Widget(R) Field Manual",
                             "a typed (R) should read as the mark the cover prints")

    def test_53i_the_one_command_runs_every_step_the_long_form_does(self):
        """The wrapper this replaces, update.ps1, fell four features behind the
        scripts it wrapped and nobody noticed. Every script the documented
        long form runs is one update_corpus.py runs, in the same order."""
        uc = load_script("update_corpus")
        root = Path(__file__).resolve().parent.parent
        same = {"rebuild_reference.py": "convert_manual.py"}   # one reference conversion (test_41c)

        def long_form(doc: str, heading: str) -> list[str]:
            text = (root / doc).read_text(encoding="utf-8")
            start = text.index(heading)
            end = text.find("\n## ", start + len(heading))
            section = text[start:end if end != -1 else None]
            names = []
            for block in re.findall(r"```bash\n(.*?)```", section, re.S):
                for name in re.findall(r"\b(\w+\.py)\b", block):
                    name = same.get(name, name)
                    if name != "update_corpus.py" and name not in names:
                        names.append(name)
            return names

        for doc, heading in (("README.md", "### The same, step by step"),
                             ("SKILL.md", "## Adding one document later")):
            with self.subTest(doc=doc):
                names = long_form(doc, heading)
                self.assertGreaterEqual(len(names), 8, f"{doc}'s long form was not found")
                self.assertEqual([n for n in names if n not in uc.PIPELINE], [],
                                 f"{doc} runs a step update_corpus.py does not")
                self.assertEqual(names, [n for n in uc.PIPELINE if n in names], f"{doc} orders its steps otherwise")
        for name in uc.PIPELINE:
            self.assertTrue((SCRIPTS / name).is_file(), name)

    def stopped_corpus(self, name: str) -> Path:
        """A corpus of two collections. Alpha waits on a reference. Beta waits
        on a prose manual and holds one document converted earlier the way
        Docling writes it: breadcrumbs its converter owns, and page furniture
        that only enrich_chunks.py strips."""
        root = self.tmp / name
        titled_ref = lambda p: titled(p, lambda q: reference_fixture(q, 25), "Widget Command Reference")  # noqa: E731
        (root / "Alpha" / "new_docs").mkdir(parents=True)
        titled_ref(root / "Alpha" / "new_docs" / "widget-commands.pdf")
        beta = root / "Beta"
        (beta / "new_docs").mkdir(parents=True)
        titled(beta / "new_docs" / "gadget.pdf", lambda p: gadget_fixture(p, "2026.1", ["Unpacking", "Mounting"]),
               "Gadget Guide")
        shutil.copytree(WS.corpus() / "docs" / "prose", beta / "docs" / "prose")
        (beta / "source").mkdir()
        shutil.copy(WS.corpus() / "widget-guide.pdf", beta / "source" / "widget-guide.pdf")
        manifest_path = beta / "docs" / "prose" / "manifest.json"
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
        m["converter"] = {"script": "convert_docling.py", "extractor": "docling", "extractor_version": "2.0",
                          "converted_at": "2026-01-01T00:00:00", "owns_breadcrumbs": True}
        manifest_path.write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
        for section in m["sections"]:
            path = beta / "docs" / "prose" / section["file"]
            path.write_text(path.read_text(encoding="utf-8") + "\nFeedback\n", encoding="utf-8")
        return root

    @staticmethod
    def documents(root: Path) -> dict[str, str]:
        """Every file of every document, the manifests without the time they
        were converted at, which is all two runs of one conversion differ in."""
        out = {}
        for path in sorted(root.glob("*/docs/*/**/*")):
            if not path.is_file():
                continue
            text = path.read_bytes()
            if path.name == "manifest.json":
                m = json.loads(text)
                (m.get("converter") or {}).pop("converted_at", None)
                text = json.dumps(m, sort_keys=True).encode()
            out[path.relative_to(root).as_posix()] = hashlib.sha256(text).hexdigest()
        return out

    def test_53j_a_stopped_run_and_its_rerun_end_where_one_run_does(self):
        """A run that stops after converting leaves documents the index does
        not hold. The next run finishes them, and ends where one run that
        never stopped does: every step that document missed, and nothing
        twice. A document whose converter owns its breadcrumbs carries no
        mark of enrichment, and still has its furniture stripped."""
        once = self.stopped_corpus("t53j-once")
        r = run("update_corpus.py", str(once), *MANUAL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        twice = self.stopped_corpus("t53j-twice")
        # build_index.py stops on Alpha, so Beta's documents are converted
        # and nothing after that.
        bad = twice / "Alpha" / "superseded.json"
        bad.write_text('[{"file": "x.pdf"}]', encoding="utf-8")
        r = run("update_corpus.py", str(twice), *MANUAL)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 1, out)
        self.assertRegex(out, r"STOPPED: build_index\.py failed in Alpha")
        self.assertTrue((twice / "Beta" / "docs" / "gadget-2026-1").is_dir())
        self.assertNotRegex(out, r"\$ python \S*enrich_chunks\.py \S*Beta", "the run went on past the stop")
        bad.unlink()
        r = run("update_corpus.py", str(twice), *MANUAL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        self.assertEqual(self.documents(twice), self.documents(once),
                         "a run that stopped and its rerun did not end where one run does")
        self.assertEqual(indexed(twice / "mcp-index.sqlite3"), indexed(once / "mcp-index.sqlite3"))
        planted = json.loads((WS.corpus() / "docs" / "prose" / "manifest.json").read_text(encoding="utf-8"))
        for root in (once, twice):
            m = json.loads((root / "Beta" / "docs" / "prose" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual([s["breadcrumb"] for s in m["sections"]], [s["breadcrumb"] for s in planted["sections"]],
                             "the converter's own breadcrumbs were rewritten")
            for s in m["sections"]:
                body = (root / "Beta" / "docs" / "prose" / s["file"]).read_text(encoding="utf-8")
                self.assertNotIn("Feedback", body, f"{root.name}: page furniture was left in {s['file']}")
        # And passing it again changes nothing, which is what makes it safe to
        # pass every such document the index does not hold.
        before = tree(twice / "Beta" / "docs")
        r = run("enrich_chunks.py", str(twice / "Beta"), "--only", "prose", "--only", "gadget-2026-1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(tree(twice / "Beta" / "docs"), before, "a second enrich_chunks.py pass changed files")

    def test_53k_a_failed_build_names_no_candidate_it_did_not_write(self):
        """A candidate an earlier run refused is kept to be looked at. A later
        build that fails must not name it as its own."""
        uc = load_script("update_corpus")
        root = self.tmp / "t53k"
        root.mkdir()
        stale = uc.candidate_path(root / "mcp-index.sqlite3")
        stale.write_bytes(b"refused by an earlier run")
        rep = uc.Report(root, root / "mcp-index.sqlite3")
        uc.build_and_swap(rep, None, False, False)          # nothing to index: the build fails
        self.assertIsNotNone(rep.stopped)
        self.assertIn("build_search_db.py failed", rep.index)
        self.assertNotIn("candidate is at", rep.index, "the report named a candidate this run did not build")
        self.assertFalse(stale.exists(), "an old candidate was left to be mistaken for this run's")

    def test_53l_a_locked_old_candidate_stops_the_run_with_a_message(self):
        """On Windows an old candidate held open (a SQLite browser, say)
        cannot be deleted. That stops the run in its report, before anything
        is built, and the report names no candidate this run wrote."""
        uc = load_script("update_corpus")
        root = self.tmp / "t53l"
        root.mkdir()
        db = root / "mcp-index.sqlite3"
        stale = uc.candidate_path(db)
        stale.write_bytes(b"refused by an earlier run")

        def locked(path, *args, **kwargs):
            raise PermissionError(13, "The process cannot access the file because it is being used by "
                                      "another process", str(path))

        rep = uc.Report(root, db)
        with unittest.mock.patch.object(Path, "unlink", locked), \
                unittest.mock.patch.object(uc, "run_step", side_effect=AssertionError("a step ran")):
            uc.build_and_swap(rep, None, False, False)
        self.assertIsNotNone(rep.stopped, "the run went on past a candidate it could not remove")
        self.assertIn(stale.name, rep.stopped)
        self.assertIn("being used by another process", rep.stopped)
        self.assertIn("kept as it was", rep.index)
        self.assertNotIn("candidate is at", rep.index, "the report named a candidate this run did not build")
        self.assertEqual(stale.read_bytes(), b"refused by an earlier run")
        self.assertFalse(db.exists(), "an index appeared from a run that built nothing")

    def test_53g_a_path_that_would_hide_collections_is_refused(self):
        """PDFs waiting at a corpus root would make it one collection and hide
        the others from every tool; one collection of a corpus would be
        indexed alone. Both stop before anything is planned."""
        root = self.tmp / "t53g"
        (root / "Old" / "docs").mkdir(parents=True)
        shutil.copytree(WS.corpus() / "docs" / "prose", root / "Old" / "docs" / "prose")
        (root / "new_docs").mkdir()
        shutil.copy(WS.pdf("prose"), root / "new_docs" / "stray.pdf")
        r = run("update_corpus.py", str(root), "--dry-run", *MANUAL)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("every tool would stop seeing Old", r.stderr)

        # The same on a corpus with nothing converted yet: a collection only
        # waiting below the root is hidden just as surely.
        fresh = self.tmp / "t53g-fresh"
        for folder in (fresh, fresh / "Synopsys"):
            (folder / "new_docs").mkdir(parents=True)
            shutil.copy(WS.pdf("prose"), folder / "new_docs" / "guide.pdf")
        r = run("update_corpus.py", str(fresh), "--dry-run", *MANUAL)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("every tool would stop seeing Synopsys", r.stderr)

        shutil.rmtree(root / "new_docs")
        (root / "mcp-index.sqlite3").write_bytes(b"")
        r = run("update_corpus.py", str(root / "Old"), "--dry-run", *MANUAL)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("is one collection, and the index beside it is", r.stderr)


if __name__ == "__main__":
    unittest.main()
