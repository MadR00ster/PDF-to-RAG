"""The search index and the MCP server built on it."""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _support import WS, run, handmade_document, load_script  # noqa: E402


class ServerTest(unittest.TestCase):
    """The index builders and the MCP server, on what the converters write."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.tmp = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_08_index_and_search_db_consume_the_output(self):
        self.corpus = WS.fresh(WS.corpus(), "index-build")
        r = run("build_index.py", str(self.corpus))
        self.assertEqual(r.returncode, 0, r.stderr)
        index = json.loads((self.corpus / "docs" / "index.json").read_text(encoding="utf-8"))
        self.assertTrue(index.get("manuals"), "index.json has no manuals")

        db = self.tmp / "index.sqlite3"
        r = run("build_search_db.py", "--root", str(self.corpus), "--out", str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(db.is_file(), "search index not written")

        import sqlite3
        con = sqlite3.connect(db)
        chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        nocrumb = con.execute("SELECT COUNT(*) FROM chunks WHERE breadcrumb = ''").fetchone()[0]
        con.close()
        self.assertGreater(chunks, 0, "no chunks indexed")
        self.assertEqual(nocrumb, 0, "chunks reached the index with no attribution")

    def test_31_folders_every_tool_skips_are_not_read(self):
        """A backup or an interrupted build left in docs/ is not a document.

        build_index.py skipped docs/<slug>.old and .new, while the search
        index, enrich_chunks.py and the OCR pass read every
        docs/*/manifest.json. A copy left there was a second document with
        the same slug, which stopped the index build, and enrich rewrote it.
        """
        corpus = WS.fresh(WS.corpus(), "hidden")
        docs = corpus / "docs"
        for name in ("prose.new", "_scratch"):
            shutil.copytree(docs / "prose", docs / name)
        db = self.tmp / "hidden.sqlite3"
        r = run("build_search_db.py", "--root", str(corpus), "--out", str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        con = sqlite3.connect(db)
        slugs = {row[0] for row in con.execute("SELECT slug FROM documents")}
        con.close()
        self.assertEqual(slugs, {"prose", "ref", "tiny", "nested"}, "a folder every tool skips was indexed")
        r = run("enrich_chunks.py", str(corpus))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for name in ("prose.new", "_scratch"):
            self.assertNotIn(name, r.stdout, f"enrich_chunks.py processed docs/{name}")
        r = run("sample_sections.py", "--root", str(corpus), "--n", "10", "--min-chars", "40")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for name in ("prose.new", "_scratch"):
            self.assertNotIn(name, r.stdout, f"sample_sections.py drew from docs/{name}")

    def test_33_the_folder_names_the_document(self):
        """The folder name is the document's slug in every tool. The index
        used the manifest's copy while build_index.py used the folder, so one
        document could be two different names."""
        corpus = WS.fresh(WS.corpus(), "renamed")
        path = corpus / "docs" / "tiny" / "manifest.json"
        m = json.loads(path.read_text(encoding="utf-8"))
        m["slug"] = "other"
        path.write_text(json.dumps(m, indent=2), encoding="utf-8")
        db = self.tmp / "renamed.sqlite3"
        r = run("build_search_db.py", "--root", str(corpus), "--out", str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("indexed as 'tiny'", r.stderr, "a manifest disagreeing with its folder went unreported")
        con = sqlite3.connect(db)
        slugs = {row[0] for row in con.execute("SELECT slug FROM documents")}
        con.close()
        self.assertEqual(slugs, {"prose", "ref", "tiny", "nested"})

    def test_35_two_collections_may_share_a_slug(self):
        """Two vendors may each have a user-guide.

        The index keyed documents by slug alone, so the second stopped the
        build; and a lookup narrowed by slug alone could serve one vendor's
        text as the other's -- the wrong answer that looks right.
        """
        root = self.tmp / "TwoVendors"
        for coll in ("A", "B"):
            handmade_document(root / coll, slug="user-guide")
        b = root / "B" / "docs" / "user-guide"
        m = json.loads((b / "manifest.json").read_text(encoding="utf-8"))
        first = b / m["sections"][0]["file"]
        first.write_text(first.read_text(encoding="utf-8") + "zebraword appears only in collection B.\n",
                         encoding="utf-8")
        m["sections"][0]["chars"] = len(first.read_text(encoding="utf-8"))
        (b / "manifest.json").write_text(json.dumps(m, indent=2), encoding="utf-8")
        db = self.tmp / "two-vendors.sqlite3"
        r = run("build_search_db.py", "--root", str(root), "--out", str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        s = load_script("mcp_server")
        s.CORPUS = s.Corpus(db)
        self.addCleanup(lambda: s.CORPUS._db and s.CORPUS._db.close())
        self.assertEqual(s.CORPUS.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 2)
        hits = s.search("zebraword")
        self.assertEqual({(h["collection"], h["slug"]) for h in hits}, {("b", "user-guide")})
        with self.assertRaises(ValueError) as ambiguous:
            s.tool_get_toc({"document": "user-guide"})
        self.assertIn("more than one collection", str(ambiguous.exception))
        self.assertIn("Hand Guide", s.tool_get_toc({"document": "user-guide", "collection": "b"}))
        self.assertIn("No matches", s.tool_search_docs({"query": "zebraword", "document": "user-guide",
                                                        "collection": "a"}),
                      "a search of collection A's user-guide returned B's text")
        around = s.tool_get_section({"section_id": hits[0]["id"], "context": 1})
        self.assertNotIn("A/docs/", around, "the sections around one of B's came from A as well")
        listing = s.tool_list_documents({})
        self.assertIn("## a", listing)
        self.assertIn("## b", listing)
        s.CORPUS._db.close()
        s.CORPUS._db = None

        r = run("mcp_smoke_test.py", "--db", str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_35b_collection_folders_that_clash_stop_the_build(self):
        """Two collection folders whose names give one key would merge two
        vendors' documents into one collection. The build stops instead, and
        writes nothing. Names that differ in more than case, so this runs
        on any filesystem."""
        root = self.tmp / "Clash"
        handmade_document(root / "Tools Manual", slug="one")
        handmade_document(root / "tools-manual", slug="two")
        db = self.tmp / "clash.sqlite3"
        r = run("build_search_db.py", "--root", str(root), "--out", str(db))
        self.assertNotEqual(r.returncode, 0, "two folders sharing a collection key were indexed as one")
        self.assertIn("tools-manual", r.stderr)
        self.assertFalse(db.exists(), "a build that stopped wrote an index")

    def test_10_mcp_server_passes_its_smoke_test(self):
        """The server, driven over stdio the way an editor drives it.

        The smoke test includes the AND->OR fallback on an identifier. An
        identifier reaches FTS5 as a multi-word phrase, and the fallback used to
        rewrite the spaces inside it as well, turning "set widget option 00"
        into a phrase containing the word "or" that could never match.
        """
        db = WS.index()
        r = run("mcp_smoke_test.py", "--db", str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("SKIP", r.stdout,
                         "the index has no entities, so the identifier probes never ran")


    def test_36_front_matter_is_a_whole_heading(self):
        """is_noise took any heading that began with a front-matter word, so
        "Index Types" and "Feedback Loops in PLLs" were demoted below the
        text that answers them."""
        noise = load_script("build_search_db").is_noise
        for real in ("Indexing Options", "Index Types", "Contents of the Install Kit", "Feedback Loops in PLLs"):
            self.assertFalse(noise(real, "Real text.\n"), f"{real!r} is a real section")
        for front in ("Contents", "TABLE OF CONTENTS", "Index", "Index (cont.)", "Contents (intro) (cont.)",
                      "Feedback", "List of Figures", "List of Tables", "About This Manual", "  List  of   Tables "):
            self.assertTrue(noise(front, "Real text.\n"), f"{front!r} is front matter")
        leaders = "".join(f"Rule K{n} . . . . {100 + n}\n" for n in range(6))
        self.assertTrue(noise("Overview", leaders), "a page of dot leaders is a contents page whatever it is called")


    def test_37_an_entity_named_in_a_question_is_boosted(self):
        """The boost fired only where the query was the bare identifier, so
        "what does set_scan_configuration do" got none; and an entity called
        `set` was boosted by "set the clock"."""
        server = load_script("mcp_server")

        def adjust(entity, query, heading=""):
            row = {"score": 0.0, "entity": entity, "heading": heading, "chars": 1000, "noise": 0}
            return server.rank_adjust(row, query)

        scan = "set_scan_configuration"
        for entity, query, want in (
                (scan, scan, -6.0),
                (scan, "set_scan_configuration -chain_count", -6.0),
                (scan, "what does set_scan_configuration do?", -6.0),
                (scan, "-chain_count option of set_scan_configuration", -6.0),
                (scan, "set scan configuration", -6.0),
                (scan, "scan configuration", 0.0),
                ("set", "set", -6.0),
                ("set", "how do I set the clock", 0.0),
                ("set", "options of `set`", -6.0),
                ("tessent -shell", "how to start tessent -shell in batch", -6.0),
                ("ADES-002", "what does ADES-002 mean", -6.0)):
            with self.subTest(entity=entity, query=query):
                self.assertEqual(adjust(entity, query), want)
        for heading, query, want in (("Timing Analysis", "i a", 0.0),
                                     ("Timing Analysis", "analysis of timing", -1.5),
                                     ("Timing Analysis", "timing analysis options", 0.0),
                                     ("Reporting", "port", 0.0)):
            with self.subTest(heading=heading, query=query):
                self.assertEqual(adjust("", query, heading), want)

        # the server writes the message-code pattern out; it must agree with the shared one
        common = load_script("_common")
        for sample in ("ADES-002", "CMD-082", "ades-002", "X-1", "ADES-002x", "AB-12345", "ABCDEFGHIJK-12"):
            self.assertEqual(bool(common.MESSAGE_CODE_RE.match(sample)),
                             bool(server.MESSAGE_CODE_RE.fullmatch(sample)), sample)

        server.CORPUS = server.Corpus(WS.index())
        self.addCleanup(lambda: server.CORPUS._db and server.CORPUS._db.close())
        top = server.search("what does set_widget_option_05 do")[0]
        self.assertEqual(top["entity"], "set_widget_option_05")


    def test_38_eval_compare_lists_each_rank_that_moved(self):
        """Two saved runs can be compared without an index, and only the
        questions whose rank differs are listed."""
        def run_file(name, ranks, commit, built):
            results = [{"id": i, "kind": kind, "question": f"question {i}", "rank": rank, "top": []}
                       for (i, kind), rank in ranks.items()]
            summary = {}
            for kind in {k for _i, k in ranks} | {"all"}:
                mine = [r for r in results if kind == "all" or r["kind"] == kind]
                summary[kind] = {"n": len(mine), **{f"hit@{k}": sum(1 for r in mine if r["rank"] and r["rank"] <= k) / len(mine)
                                                    for k in (1, 3, 5, 10)},
                                 "mrr": sum(1 / r["rank"] for r in mine if r["rank"]) / len(mine)}
            path = self.tmp / name
            path.write_text(json.dumps({"db": "x", "git_commit": commit, "meta": {"built_at": built, "figure_text": "1"},
                                        "summary": summary, "results": results, "errors": []}), encoding="utf-8")
            return path

        a = run_file("run-a.json", {("q1", "identifier"): 3, ("q2", "concept"): 2, ("q3", "concept"): None,
                                    ("q7", "concept"): 1}, "0e4dd59", "2026-10-01T10:00:00")
        b = run_file("run-b.json", {("q1", "identifier"): 1, ("q2", "concept"): 2, ("q3", "concept"): 7},
                     "1a2b3c4", "2026-10-07T12:00:00")
        r = run("eval_search.py", "--compare", str(a), str(b))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("code 0e4dd59", r.stdout)
        self.assertIn("code 1a2b3c4", r.stdout)
        self.assertRegex(r.stdout, r"\[q1\] identifier\s+3 -> 1\s+question q1")
        self.assertRegex(r.stdout, r"\[q3\] concept\s+- -> 7\s+question q3")
        self.assertIn("Only in A: q7", r.stdout)
        self.assertNotIn("[q2]", r.stdout, "a question whose rank did not move was listed")

        questions = self.tmp / "questions.jsonl"
        manifest = json.loads((WS.corpus() / "docs" / "ref" / "manifest.json").read_text(encoding="utf-8"))
        wanted = manifest["sections"][3]
        questions.write_text("\n".join(json.dumps({
            "id": f"q{n}", "kind": "identifier", "question": text,
            "answers": [{"slug": "ref", "file": wanted["file"]}]})
            for n, text in enumerate(("what does set_widget_option_03 do", "set_widget_option_04"), 1)),
            encoding="utf-8")
        out = self.tmp / "real-run.json"
        r = run("eval_search.py", "--db", str(WS.index()), "--questions", str(questions), "--json", str(out))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        saved = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual([x["question"] for x in saved["results"]],
                         ["what does set_widget_option_03 do", "set_widget_option_04"])
        self.assertIn("git_commit", saved)
        self.assertTrue(saved["git_commit"] is None or isinstance(saved["git_commit"], str))


if __name__ == "__main__":
    unittest.main(verbosity=2)
