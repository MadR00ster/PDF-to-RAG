"""The search index and the MCP server built on it."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _support import WS, run, handmade_document, load_script, edition_reference_fixture, LONG_ENTRY  # noqa: E402


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

        # --exclude takes a question file with quote answers, and an answer
        # naming another collection excludes nothing here
        ref = json.loads((docs / "ref" / "manifest.json").read_text(encoding="utf-8"))["sections"][2]["file"]
        prose = json.loads((docs / "prose" / "manifest.json").read_text(encoding="utf-8"))["sections"][1]["file"]
        exclude = self.tmp / "exclude.jsonl"
        exclude.write_text("\n".join(json.dumps(q) for q in (
            {"id": "a", "answers": [{"doc_id": "prose", "quote": "anything"}]},
            {"id": "b", "answers": [{"slug": "ref", "file": ref}]},
            {"id": "c", "answers": [{"slug": "prose", "file": prose, "collection": "elsewhere"}]})), encoding="utf-8")
        r = run("sample_sections.py", "--root", str(corpus), "--n", "500", "--min-chars", "1", "--exclude", str(exclude))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(f"ref · {ref}", r.stdout, "an answered section was sampled")
        self.assertIn(f"prose · {prose}", r.stdout, "an answer in another collection excluded this one")

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

        # questions without an id are told apart by their text
        def idless(name, ranks):
            path = self.tmp / name
            path.write_text(json.dumps({"db": "x", "summary": {}, "errors": [], "results": [
                {"id": None, "kind": "concept", "question": q, "rank": rank, "top": []} for q, rank in ranks]}),
                encoding="utf-8")
            return path
        r = run("eval_search.py", "--compare", str(idless("na.json", [("first", 1), ("second", 2)])),
                str(idless("nb.json", [("first", 4), ("second", 2)])))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"concept\s+1 -> 4\s+first")
        self.assertIn("Rank changes (rank 11+ shown as -): 1", r.stdout)
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


    def test_45_remap_answers_follows_the_text_across_a_reconversion(self):
        """Answers name section files, which a reconversion renames. Simulate
        one so the right result is known: a renamed section, a split one, and
        one whose text was replaced."""
        corpus = WS.fresh(WS.corpus(), "remap")
        docs, backup = corpus / "docs" / "prose", corpus / ".rebuild-backup" / "prose"
        manifest_path = docs / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        def text(tag: str, n: int, start: int = 0) -> str:
            return " ".join(f"{tag}{i}word" for i in range(start, start + n)) + "\n"

        old = {}
        for i, tag in ((0, "alpha"), (1, "bravo"), (2, "charlie")):
            old[i] = manifest["sections"][i]["file"]
            (docs / old[i]).write_text(text(tag, 60), encoding="utf-8")
        shutil.copytree(docs, backup)

        def rename(i: int, new_name: str, body: str) -> str:
            (docs / old[i]).unlink()
            (docs / new_name).write_text(body, encoding="utf-8")
            return new_name
        renamed = rename(0, "sections/0100-renamed.md", text("alpha", 60))
        manifest["sections"][0]["file"] = renamed
        # the second section's text now sits in two files that overlap where it was cut
        first, second = rename(1, "sections/0101-part-one.md", text("bravo", 35)), "sections/0102-part-two.md"
        (docs / second).write_text(text("bravo", 33, 27), encoding="utf-8")
        manifest["sections"][1]["file"] = first
        manifest["sections"].insert(2, {**manifest["sections"][1], "file": second, "heading": "Part two"})
        manifest["sections"][3]["file"] = rename(2, "sections/0103-replaced.md", text("zulu", 60))
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        questions = self.tmp / "remap-questions.jsonl"
        lines = [json.dumps({"id": f"q{i}", "kind": "concept", "question": f"question {i}", "note": "kept",
                             "answers": [{"slug": "prose", "file": old[i]}]}) for i in range(3)]
        lines.insert(1, "// a comment line")
        questions.write_text("\n".join(lines) + "\n", encoding="utf-8")
        before = questions.read_bytes()

        out = self.tmp / "remap-questions.remapped.jsonl"
        r = run("remap_answers.py", "--root", str(corpus), "--questions", str(questions))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(questions.read_bytes(), before, "the input was changed")
        self.assertIn("1 remapped 1:1, 1 split 1:n, 1 unresolved", r.stdout)
        self.assertIn(old[2], r.stdout)
        got = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l and not l.startswith("//")]
        self.assertIn("// a comment line", out.read_text(encoding="utf-8").splitlines())
        self.assertEqual([x["id"] for x in got], ["q0", "q1", "q2"])
        self.assertEqual(got[0]["answers"], [{"slug": "prose", "file": renamed}])
        self.assertEqual({a["file"] for a in got[1]["answers"]}, {first, second})
        self.assertEqual(got[1]["note"], f"kept; remapped from {old[1]} to 2 sections")
        self.assertEqual(got[2]["answers"], [{"slug": "prose", "file": old[2]}], "an unresolved answer was rewritten")
        for x in got:
            self.assertEqual({k: x[k] for k in ("kind", "question")}, {"kind": "concept", "question": f"question {x['id'][1:]}"})

    def test_46_an_answer_given_as_a_quote_survives_a_rebuild(self):
        """`quote` answers name the text, not the file, so they need no remap.
        A phrase that is nowhere, or in too many sections to be one answer, is
        stale, like an answer whose file is gone."""
        questions = self.tmp / "quote-questions.jsonl"

        def write(*qs):
            questions.write_text("\n".join(json.dumps(q) for q in qs) + "\n", encoding="utf-8")

        def ask(doc_id, quote, qid, kind="concept"):
            return {"id": qid, "kind": kind, "question": "what does set_widget_option_05 do",
                    "answers": [{"doc_id": doc_id, "quote": quote}]}

        write(ask("ref", "set_widget_option_05 -value", "q1", "identifier"),
              # across a blank line: only the Installation section has this
              ask("prose", "Installation This section explains the widget", "q2"))
        r = run("eval_search.py", "--db", str(WS.index()), "--questions", str(questions))
        self.assertIn(r.returncode, (0, 3), r.stdout + r.stderr)
        self.assertNotIn("not in this index", r.stdout)
        self.assertRegex(r.stdout, r"all\s+2\s")

        write(ask("prose", "Torque the bolts evenly to avoid warping the bracket.", "q3"),    # in all six
              ask("prose", "words found nowhere in the document", "q4"),
              ask("no-such-manual", "set_widget_option_05", "q5"))
        r = run("eval_search.py", "--db", str(WS.index()), "--questions", str(questions))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        for qid in ("q3", "q4", "q5"):
            self.assertIn(qid, r.stdout)


    def test_47_the_search_experiments_are_flags_and_off_by_default(self):
        """--body-without-breadcrumb and --ident-index change what is indexed,
        and nothing about the default."""
        corpus = WS.corpus()
        built = {}
        for name, flags in (("plain", ()), ("nocrumb", ("--body-without-breadcrumb",)), ("idents", ("--ident-index",))):
            db = self.tmp / f"experiment-{name}.sqlite3"
            r = run("build_search_db.py", "--root", str(corpus), "--out", str(db), *flags)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            built[name] = db

        def rows(name, sql):
            con = sqlite3.connect(built[name])
            try:
                return con.execute(sql).fetchall()
            finally:
                con.close()

        def starting_with_own_breadcrumb(name):
            return [1 for crumb, body in rows(name, "SELECT breadcrumb, body FROM chunks")
                    if crumb and body.split("\n", 1)[0].strip("*").strip() == crumb.strip("*").strip()]
        self.assertTrue(starting_with_own_breadcrumb("plain"), "the control index has no breadcrumb lines to remove")
        self.assertFalse(starting_with_own_breadcrumb("nocrumb"))
        self.assertEqual(dict(rows("nocrumb", "SELECT key, value FROM meta"))["body_without_breadcrumb"], "1")
        self.assertEqual(dict(rows("plain", "SELECT key, value FROM meta"))["body_without_breadcrumb"], "0")
        r = run("mcp_smoke_test.py", "--db", str(built["nocrumb"]))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(rows("plain", "SELECT name FROM sqlite_master WHERE name = 'idents'"), [])

        # the server's rule for an identifier and the index's are the same rule
        build, server = load_script("build_search_db"), load_script("mcp_server")
        for sample in ("set_widget_option", "`set_widget_option`,", "-value", "-", "ADES-002", "ades-002", "X-1",
                       "ADES-002x", "set", "word.", "a_b."):
            self.assertEqual(bool(build.identifiers(sample)), any(ident for _t, ident in server.query_tokens(sample)), sample)
            self.assertEqual(build.identifiers(sample), [t for t, ident in server.query_tokens(sample) if ident], sample)

        query = "what does set_widget_option_05 do"
        for name in ("plain", "idents"):
            server.CORPUS = server.Corpus(built[name])
            try:
                self.assertEqual(server.search(query)[0]["entity"], "set_widget_option_05", name)
                self.assertEqual(bool(server.ident_rows(query)), name == "idents")
                if name == "idents":
                    self.assertEqual(server.ident_rows("how do I set the clock"), frozenset())
                    held = server.ident_rows(query)
                    # Asked only about the candidates being ranked.
                    one = next(iter(held))
                    self.assertEqual(server.ident_rows(query, [one, -1]), frozenset({one}))
                    self.assertEqual(server.ident_rows(query, []), frozenset())
                    row = {"score": 0.0, "entity": "", "heading": "", "chars": 1000, "noise": 0, "id": next(iter(held))}
                    self.assertEqual(server.rank_adjust(row, query, held), -3.0)
                    self.assertEqual(server.rank_adjust(row, query), 0.0)
            finally:
                server.CORPUS._db.close()


    def test_49_export_writes_every_chunk_once_and_stops_on_a_hole(self):
        corpus = WS.corpus()
        out = self.tmp / "chunks.jsonl"
        r = run("export_chunks.py", "--root", str(corpus), "--out", str(out))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
        wanted = []
        for manifest in sorted((corpus / "docs").glob("*/manifest.json")):
            wanted += [(manifest.parent.name, s["file"]) for s in json.loads(manifest.read_text(encoding="utf-8"))["sections"]]
        self.assertEqual(len(rows), len(wanted))
        self.assertEqual(len({x["id"] for x in rows}), len(rows), "an id repeats")
        self.assertEqual({(x["slug"], x["file"]) for x in rows}, set(wanted))
        for x in rows:
            self.assertEqual(x["text"], (corpus / "docs" / x["slug"] / x["file"]).read_text(encoding="utf-8"))
            self.assertTrue(x["id"].startswith(f"{x['collection']}/{x['slug']}/"))
        ref = next(x for x in rows if x["slug"] == "ref")
        self.assertEqual(ref["entity"], ref["breadcrumb"].rsplit(" › ", 1)[-1])
        self.assertIsNone(next(x for x in rows if x["slug"] == "prose")["entity"])
        self.assertTrue(all(x["is_current"] for x in rows))

        # two editions of one manual: --current-only keeps the one search answers from
        root, _coll = WS.gadgets()
        root = WS.fresh(root, "export-gadgets")
        r = run("export_chunks.py", "--root", str(root), "--out", str(out), "--current-only")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual({json.loads(l)["slug"] for l in out.read_text(encoding="utf-8").splitlines()}, {"gadget-2026-1"})
        r = run("export_chunks.py", "--root", str(root), "--out", str(out))
        self.assertEqual({json.loads(l)["slug"] for l in out.read_text(encoding="utf-8").splitlines()},
                         {"gadget-2025-1", "gadget-2026-1"})

        # a section that cannot be read leaves the old export alone and says which
        before = out.read_bytes()
        (root / "Gadgets" / "docs" / "gadget-2026-1" / "sections" / "0002-unpacking.md").unlink()
        r = run("export_chunks.py", "--root", str(root), "--out", str(out))
        self.assertEqual(r.returncode, 1)
        self.assertIn("0002-unpacking.md", r.stderr)
        self.assertEqual(out.read_bytes(), before)


    def test_42_a_stale_index_says_so(self):
        """An index nobody rebuilt answers confidently from a corpus that has
        moved on; the server says so, the way it does for a partial one."""
        corpus = WS.fresh(WS.corpus(), "stale")
        db = corpus / "mcp-index.sqlite3"
        r = run("build_search_db.py", "--root", str(corpus), "--out", str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        server = load_script("mcp_server")

        def warning(path=db) -> str:
            c = server.Corpus(path)
            try:
                return c.staleness_warning()
            finally:
                c._db.close()

        self.assertEqual(warning(), "")
        manifest = corpus / "docs" / "prose" / "manifest.json"
        original = manifest.read_bytes()
        changed = json.loads(original)
        changed["title"] = "Widget Guide, Second Edition"
        manifest.write_text(json.dumps(changed, indent=2), encoding="utf-8")
        said = warning()
        self.assertIn("Stale index", said)
        self.assertIn("prose", said)

        # the same bytes again, under a newer mtime, as a synced copy has
        manifest.write_bytes(original)
        later = manifest.stat().st_mtime + 100
        os.utime(manifest, (later, later))
        self.assertEqual(warning(), "", "an unchanged file with a new mtime was reported")

        shutil.copytree(corpus / "docs" / "prose", corpus / "docs" / "prose-copy")
        self.assertIn("prose-copy", warning())
        shutil.rmtree(corpus / "docs" / "prose-copy")
        shutil.copytree(corpus / "docs" / "prose", corpus / "docs" / "prose.old")
        shutil.copytree(corpus / "docs" / "prose", corpus / "docs" / "_scratch")
        self.assertEqual(warning(), "", "a backup folder was taken for a new document")
        shutil.rmtree(corpus / "docs" / "prose.old")
        shutil.rmtree(corpus / "docs" / "_scratch")

        (corpus / "current_versions.json").write_text("{}", encoding="utf-8")
        self.assertIn("current_versions.json", warning())
        (corpus / "current_versions.json").unlink()
        (corpus / "docs" / "ref" / "manifest.json").unlink()
        self.assertIn("ref", warning(), "a manifest that is gone was not reported")

        # an index made before the table existed, or copied away from its corpus
        old = self.tmp / "before-sources.sqlite3"
        shutil.copy(db, old)
        con = sqlite3.connect(old)
        con.execute("DROP TABLE sources")
        con.commit()
        con.close()
        self.assertEqual(warning(old), "")
        away = self.tmp / "away"
        away.mkdir()
        shutil.copy(db, away / "mcp-index.sqlite3")
        c = server.Corpus(away / "mcp-index.sqlite3")
        c._root = away
        self.assertEqual(c.staleness_warning(), "")
        c._db.close()

        # one server, as an editor keeps it: the first answer is not kept for good
        manifest.write_bytes(original)
        shutil.rmtree(corpus / "docs" / "ref")
        r = run("build_search_db.py", "--root", str(corpus), "--out", str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        live = server.Corpus(db)
        self.addCleanup(lambda: live._db and live._db.close())
        self.assertEqual(live.staleness_warning(), "")
        manifest.write_text(json.dumps(changed, indent=2), encoding="utf-8")
        self.assertEqual(live.staleness_warning(), "", "rechecked within STALE_RECHECK")
        live._stale_checked -= server.STALE_RECHECK
        self.assertIn("Stale index", live.staleness_warning(), "a change after the first check was never seen")
        live._db.close()   # Windows will not replace a file that is open
        live._db = None

        # figures extracted after the build
        manifest.write_bytes(original)
        figures = corpus / "docs" / "prose" / "figures.json"
        figures.unlink(missing_ok=True)
        r = run("build_search_db.py", "--root", str(corpus), "--out", str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(warning(), "")
        figures.write_text('{"figures": []}', encoding="utf-8")
        self.assertIn("prose", warning(), "a figures.json the index never saw was not reported")
        figures.unlink()

        # and it reaches an answer, wherever the partial-index warning would
        manifest.write_text(json.dumps(changed, indent=2), encoding="utf-8")
        server.CORPUS = server.Corpus(db)
        self.addCleanup(lambda: server.CORPUS._db and server.CORPUS._db.close())
        self.assertIn("Stale index", server.tool_list_documents({}))
        self.assertIn("Stale index", server.tool_search_docs({"query": "widget"}))


    def test_43_a_file_name_is_not_a_pattern(self):
        """`_` is a wildcard in LIKE and nearly every section file has one, so a
        request for sections/0001-set_x.md was answered with 0001-setax.md."""
        root = self.tmp / "Underscore"
        doc = root / "docs" / "one"
        (doc / "sections").mkdir(parents=True)
        text = "*One › Setting*\n\n## Setting\n\nThe setax option sets a value.\n"
        (doc / "sections" / "0001-setax.md").write_text(text, encoding="utf-8")
        (doc / "full.md").write_text(text, encoding="utf-8")
        (doc / "manifest.json").write_text(json.dumps({
            "slug": "one", "title": "One", "source_pdf": "one.pdf", "page_count": 1,
            "sections": [{"file": "sections/0001-setax.md", "heading": "Setting", "level": 2,
                          "breadcrumb": "One › Setting"}]}), encoding="utf-8")
        db = self.tmp / "underscore.sqlite3"
        r = run("build_search_db.py", "--root", str(root), "--out", str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        server = load_script("mcp_server")
        server.CORPUS = server.Corpus(db)
        self.addCleanup(lambda: server.CORPUS._db and server.CORPUS._db.close())
        self.assertIn("setax option", server.tool_get_section({"file": "sections/0001-setax.md"}))
        self.assertIn("setax option", server.tool_get_section({"file": "0001-setax.md"}))
        for wrong in ("sections/0001-set_x.md", "sections/0001-set%x.md", "001-setax.md", "1-setax.md"):
            with self.assertRaises(ValueError, msg=wrong) as raised:
                server.tool_get_section({"file": wrong})
            self.assertIn("No section matching", str(raised.exception))
        self.assertEqual(server.like_escape(r"a_b%c\d"), r"a\_b\%c\\d")

    def test_44_lookup_budget_covers_the_whole_response(self):
        """The 40,000-character budget was reset for each document that has the
        entry, so an entry in two documents came back at twice the size."""
        coll = WS.collection("twice")
        for name in ("a", "b"):
            edition_reference_fixture(coll / f"ref-{name}.pdf", list(range(25)), f"The last setting of {name}.")
            r = run("rebuild_reference.py", str(coll / f"ref-{name}.pdf"), "--title", f"Reference {name}",
                    "--slug", f"ref-{name}", "--doc-id", f"ref-{name}")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        db = self.tmp / "twice.sqlite3"
        r = run("build_search_db.py", str(coll), "--out", str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        server = load_script("mcp_server")
        server.CORPUS = server.Corpus(db)
        self.addCleanup(lambda: server.CORPUS._db and server.CORPUS._db.close())
        answer = server.tool_lookup_entity({"name": LONG_ENTRY})
        self.assertLessEqual(len(answer), server.MAX_SECTION_CHARS + 1000, "the budget was spent twice")
        self.assertIn("1 more document(s) have this entry: ref-b", answer)
        self.assertIn("ref-a", answer)
        # asked for by name, the second one is read
        self.assertIn("ref-b", server.tool_lookup_entity({"name": LONG_ENTRY, "document": "ref-b"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
