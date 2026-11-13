"""backfill_pages.py: page numbers for chunks a converter wrote without them."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _support import SCRIPTS, WS, run, write_pdf, load_script, schema_errors  # noqa: E402


def page_lines(tag: str, n: int = 6) -> list[tuple[str, int]]:
    """n distinct lines of ordinary text for a page."""
    return [(f"{tag} line {i} explains how the {tag} unit keeps its settings in step", 10) for i in range(n)]


def hand_document(root: Path, pages: list[list[tuple[str, int]]], chunks: list[tuple[str, str]]) -> Path:
    """A collection holding doc.pdf and docs/doc/ whose chunks are the given
    (heading, body) pairs, with no page numbers anywhere."""
    (root / "docs" / "doc" / "sections").mkdir(parents=True)
    write_pdf(root / "doc.pdf", pages, [[1, "Doc", 1]])
    sections = []
    for n, (heading, body) in enumerate(chunks, start=1):
        name = f"sections/{n:04d}-part.md"
        (root / "docs" / "doc" / name).write_text(f"## {heading}\n\n{body}\n" if heading else body + "\n",
                                                  encoding="utf-8")
        sections.append({"file": name, "heading": heading, "level": 2})
    (root / "docs" / "doc" / "full.md").write_text("x", encoding="utf-8")
    (root / "docs" / "doc" / "manifest.json").write_text(json.dumps({
        "source_pdf": "doc.pdf", "title": "Doc", "slug": "doc", "page_count": len(pages),
        "toc": [{"level": 1, "title": "Doc", "page": 1}], "sections": sections,
    }, indent=2), encoding="utf-8")
    return root


def text_of(pages: list[list[tuple[str, int]]], first: int, last: int) -> str:
    return "\n\n".join(t for p in pages[first - 1:last] for t, _ in p)


class BackfillTest(unittest.TestCase):
    """Pages found in the PDF for chunks that have none."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.tmp = Path(cls._tmp.name)
        cls.mod = load_script("backfill_pages")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def manifest(self, root: Path) -> dict:
        return json.loads((root / "docs" / "doc" / "manifest.json").read_text(encoding="utf-8"))

    def test_61_pages_come_back_as_the_converter_wrote_them_and_nothing_else_changes(self):
        """A converted fixture with its pages taken away gets them back, equal
        to the converter's own, with every body, name and heading as it was."""
        coll = WS.collection("backfill", "prose")
        r = run("convert_manual.py", str(coll / "widget-guide.pdf"), "--title", "Widget Guide", "--slug", "prose")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = coll / "docs" / "prose"
        before = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        stripped = json.loads(json.dumps(before))
        for s in stripped["sections"]:
            s.pop("page_start"), s.pop("page_end")
        (d / "manifest.json").write_text(json.dumps(stripped, indent=2), encoding="utf-8")
        files = {p: p.read_bytes() for p in d.rglob("*") if p.is_file() and p.name != "manifest.json"}

        r = run("backfill_pages.py", str(coll))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([(s["page_start"], s["page_end"]) for s in after["sections"]],
                         [(s["page_start"], s["page_end"]) for s in before["sections"]])
        for key in ("file", "heading", "level"):
            self.assertEqual([s[key] for s in after["sections"]], [s[key] for s in before["sections"]])
        self.assertEqual(files, {p: p.read_bytes() for p in d.rglob("*") if p.is_file() and p.name != "manifest.json"},
                         "a body, full.md or figure was rewritten")
        self.assertEqual(after["pages_backfill"]["unplaced"], 0)
        self.assertEqual(schema_errors(after, json.loads((SCRIPTS / "manifest.schema.json").read_text(encoding="utf-8"))), [])
        # the manifest as it was is kept, and the PDF is not read as a source of text twice over
        kept = list((coll / ".rebuild-backup").glob("manifests-*-pre-pages/prose.manifest.json"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(json.loads(kept[0].read_text(encoding="utf-8")), stripped)

        # a document that has pages is left alone, and so is one this script gave pages, unless --redo
        again = run("backfill_pages.py", str(coll))
        self.assertIn("already has pages", again.stdout)
        self.assertEqual(json.loads((d / "manifest.json").read_text(encoding="utf-8")), after)
        redo = run("backfill_pages.py", str(coll), "--redo")
        self.assertEqual(redo.returncode, 0, redo.stdout + redo.stderr)
        self.assertEqual(len(list((coll / ".rebuild-backup").glob("manifests-*-pre-pages/*.json"))), 1,
                         "a second run replaced the backup of the manifest as it was")
        self.assertEqual(json.loads(kept[0].read_text(encoding="utf-8")), stripped)

    def test_61b_dry_run_writes_nothing(self):
        coll = WS.collection("backfill-dry", "prose")
        run("convert_manual.py", str(coll / "widget-guide.pdf"), "--title", "Widget Guide", "--slug", "prose")
        d = coll / "docs" / "prose"
        m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        for s in m["sections"]:
            s.pop("page_start"), s.pop("page_end")
        (d / "manifest.json").write_text(json.dumps(m, indent=2), encoding="utf-8")
        before = (d / "manifest.json").read_bytes()
        r = run("backfill_pages.py", str(coll), "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DRY RUN", r.stdout)
        self.assertEqual((d / "manifest.json").read_bytes(), before)
        self.assertFalse(list(coll.glob(".rebuild-backup/manifests-*-pre-pages")), "a dry run kept a backup")

    def test_61c_a_page_taken_from_a_neighbour_is_flagged_and_counted(self):
        """A chapter title too short to find sits between a chunk ending on
        page 2 and one starting on page 3: it takes page 3, and says it was a
        guess. Between two chunks on one page it is not a guess."""
        gamma, delta = (text_of([page_lines(t)], 1, 1) for t in ("gamma", "delta"))
        pages_for = [page_lines("alpha"), page_lines("beta"), page_lines("gamma") + page_lines("delta"),
                     page_lines("epsilon")]
        chunks = [("Alpha", text_of(pages_for, 1, 2)), ("Part Two", ""), ("Gamma", gamma),
                  ("Interlude", ""), ("Delta", delta), ("Epsilon", text_of(pages_for, 4, 4))]
        coll = hand_document(self.tmp / "Flag", pages_for, chunks)
        r = run("backfill_pages.py", str(coll))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        m = self.manifest(coll)
        got = [(s["page_start"], s["page_end"], s.get("pages_from")) for s in m["sections"]]
        self.assertEqual(got[1], (3, 3, "neighbour"), got)
        self.assertEqual(got[3], (3, 3, None), "a stub between two chunks on one page is not a guess")
        self.assertEqual([g[2] for g in got], [None, "neighbour", None, None, None, None])
        self.assertEqual((m["pages_backfill"]["neighbour"], m["pages_backfill"]["bounded"]), (1, 1))
        self.assertIn("1 chunk(s) took their pages from a neighbour", r.stdout)

    def test_61d_a_repeated_phrase_is_placed_where_the_chunks_before_it_leave_off(self):
        """Two chunks open with the same sentence, and a third closes on a line
        that an earlier page also prints. Each is placed in reading order, not
        at the first place its words occur."""
        same = "Before you begin make sure the unit is switched off and unplugged from the wall"
        pages = [[(same, 10)] + page_lines("alpha"),
                 page_lines("beta"),
                 [(same, 10)] + page_lines("gamma"),
                 page_lines("delta") + [(same + " again", 10)],
                 page_lines("alpha")[:2] + page_lines("omega")]
        chunks = [("One", text_of(pages, 1, 2)), ("Two", text_of(pages, 3, 4)), ("Three", text_of(pages, 5, 5))]
        coll = hand_document(self.tmp / "Repeat", pages, chunks)
        r = run("backfill_pages.py", str(coll))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        got = [(s["page_start"], s["page_end"]) for s in self.manifest(coll)["sections"]]
        self.assertEqual(got, [(1, 2), (3, 4), (5, 5)])

    def test_61e_a_garbled_text_layer_still_matches(self):
        """Words run together and doubled letters dropped, as one real manual's
        text layer has them."""
        chunks = ["## Setup\n\nRun the command to set the attribute for every controller in the assembly",
                  "## Next\n\nThe message appears when the connection to the address is lost during the transfer"]
        pages = ["Setup\nRun the comand to set the atribute for every controler in the asembly\n",
                 "Page furniture\nThe messageappears when the conection to the adress is lost during the transfer\n"]
        out = self.mod.place(chunks, pages)
        self.assertEqual([(p.start, p.end, p.state) for p in out], [(1, 1, "matched"), (2, 2, "matched")])

    def test_61f_no_chunk_is_matched_on_words_the_pdf_does_not_have(self):
        """Nothing is invented: a chunk whose words are nowhere in the PDF,
        with no neighbour to take a page from, is left without pages."""
        out = self.mod.place(["## Nowhere\n\nthese words are not printed anywhere in this document at all"],
                             ["Something else entirely on the only page of the file\n"])
        self.assertEqual((out[0].start, out[0].state), (None, "unplaced"))

    def test_61g_a_chunk_does_not_end_on_an_earlier_repeat_of_its_closing_words(self):
        """A long chunk closing on a sentence an earlier page also prints ends
        on the later page: a chunk spans about as much of the PDF as it has text."""
        closing = "Refer to the chapter on maintenance for the full schedule of checks"
        pages = [page_lines("alpha", 3) + [(closing, 10)], page_lines("beta", 6), page_lines("gamma", 6),
                 page_lines("delta", 6) + [(closing, 10)], page_lines("omega", 4)]
        chunks = ["## Long\n\n" + text_of(pages, 1, 4), "## After\n\n" + text_of(pages, 5, 5)]
        out = self.mod.place(chunks, ["\n".join(t for t, _ in p) for p in pages])
        self.assertEqual((out[0].start, out[0].end), (1, 4))

    def test_61h_a_phrase_two_probes_do_not_agree_on_is_not_taken_on_one_probe(self):
        """A table header is the same in every table: the chunk opens with it
        and the page prints it after the running title. The place the rest of
        the chunk matches is the start."""
        header = "Property Name Definition Columns In The Database Mandatory Or Optional Comments"
        pages = ["Intro\n" + header + "\nrow of table one with its own long distinct cells alpha beta\n",
                 "Running title\nTable\nSecond\n" + "Header\n" + header.replace("Property ", "") + "\nrow two cells gamma delta\n",
                 "Third page\n" + header + "\nrow of table three with its own long distinct cells epsilon zeta\n"]
        chunks = ["## Header\n\n" + header + " row of table one with its own long distinct cells alpha beta",
                  "## Header\n\nProperty " + header.replace("Property ", "") + " row two cells gamma delta",
                  "## Header\n\n" + header + " row of table three with its own long distinct cells epsilon zeta"]
        out = self.mod.place(chunks, pages)
        self.assertEqual([p.start for p in out], [1, 2, 3])


if __name__ == "__main__":
    unittest.main(verbosity=2)
