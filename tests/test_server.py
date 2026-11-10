"""The search index and the MCP server built on it."""
from __future__ import annotations

import json
import os
import re
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
        self.assertNotIn("cost", saved["results"][0], "reading cost was counted without --cost")

    def test_60g_cost_counts_what_a_client_reads_to_reach_the_answer(self):
        server = load_script("mcp_server")
        server.CORPUS = server.Corpus(WS.index())
        self.addCleanup(lambda: server.CORPUS._db and server.CORPUS._db.close())
        # A question every fixture section can answer: its own heading, kept where it ranks in the top ten.
        asked = []
        for row in server.CORPUS.db.execute("SELECT slug, file, heading FROM chunks WHERE heading != '' ORDER BY rowid"):
            if any(r["slug"] == row["slug"] and r["file"] == row["file"] for r in server.search(row["heading"], limit=10)):
                asked.append({"id": f"c{len(asked)}", "kind": "concept", "question": row["heading"],
                              "answers": [{"slug": row["slug"], "file": "sections/" + row["file"].rsplit("/", 1)[-1]}]})
            if len(asked) == 3:
                break
        self.assertEqual(len(asked), 3, "the fixture has no sections that answer their own heading")
        questions = self.tmp / "cost-questions.jsonl"
        questions.write_text(chr(10).join(json.dumps(q) for q in asked), encoding="utf-8")
        out = self.tmp / "cost-run.json"
        r = run("eval_search.py", "--db", str(WS.index()), "--questions", str(questions), "--cost", "--json", str(out))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Characters a client reads to reach the answer", r.stdout)
        self.assertIn("page + answer section, passages", r.stdout)
        for result in json.loads(out.read_text(encoding="utf-8"))["results"]:
            cost = result["cost"]
            self.assertGreater(cost["page"], 0)
            self.assertEqual(cost["direct_whole"], cost["page"] + cost["section_whole"])
            self.assertLessEqual(cost["section_passages"], cost["section_whole"])
            self.assertGreaterEqual(cost["down_whole"], cost["direct_whole"])
            self.assertEqual(cost["terms_kept"], 1.0, "a short fixture section was narrowed")

    def test_60f_terms_kept_counts_the_questions_words_the_passages_still_hold(self):
        evaluator = load_script("eval_search")
        whole = "The zorbulate option flushes buffers. Clocks gate the reset."
        self.assertEqual(evaluator.terms_kept(whole, whole, "zorbulate buffers reset"), 1.0)
        # Cut down to one sentence: "zorbulate" and "buffers" stay, "reset" is gone.
        cut = "The zorbulate option flushes buffers." + chr(10) * 2 + "Passages matching `reset`: 3 of 9 characters."
        self.assertAlmostEqual(evaluator.terms_kept(whole, cut, "zorbulate buffers reset"), 2 / 3)
        self.assertEqual(evaluator.terms_kept("nothing here", "nothing here", "zorbulate"), 1.0)


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

    def test_43b_only_a_dot_slash_prefix_is_dropped_from_a_file_name(self):
        """The request was stripped with lstrip("./"), which removes any run of
        dots and slashes, so a section file whose name begins with a dot could
        not be asked for by that name."""
        root = self.tmp / "Dotted"
        doc = root / "docs" / "one"
        (doc / "sections").mkdir(parents=True)
        text = "*One › Setting*" + chr(10) * 2 + "## Setting" + chr(10) * 2 + "The dotted option sets a value." + chr(10)
        (doc / "sections" / ".0001-dotted.md").write_text(text, encoding="utf-8")
        (doc / "full.md").write_text(text, encoding="utf-8")
        (doc / "manifest.json").write_text(json.dumps({
            "slug": "one", "title": "One", "source_pdf": "one.pdf", "page_count": 1,
            "sections": [{"file": "sections/.0001-dotted.md", "heading": "Setting", "level": 2,
                          "breadcrumb": "One › Setting"}]}), encoding="utf-8")
        db = self.tmp / "dotted.sqlite3"
        r = run("build_search_db.py", "--root", str(root), "--out", str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        server = load_script("mcp_server")
        server.CORPUS = server.Corpus(db)
        self.addCleanup(lambda: server.CORPUS._db and server.CORPUS._db.close())
        for asked in (".0001-dotted.md", "./.0001-dotted.md", "sections/.0001-dotted.md", "./sections/.0001-dotted.md"):
            self.assertIn("dotted option", server.tool_get_section({"file": asked}), asked)

    def long_section(self, name: str, blocks: list[str]):
        """A server over a one-section document made of `blocks`, and the
        section_id of that section."""
        root = self.tmp / name
        doc = root / "docs" / "one"
        (doc / "sections").mkdir(parents=True)
        nl = chr(10)
        text = "*One › Long*" + nl * 2 + "## Long" + nl * 2 + (nl * 2).join(blocks) + nl
        (doc / "sections" / "0001-long.md").write_text(text, encoding="utf-8")
        (doc / "full.md").write_text(text, encoding="utf-8")
        (doc / "manifest.json").write_text(json.dumps({
            "slug": "one", "title": "One", "source_pdf": "one.pdf", "page_count": 1,
            "sections": [{"file": "sections/0001-long.md", "heading": "Long", "level": 2,
                          "breadcrumb": "One › Long"}]}), encoding="utf-8")
        db = self.tmp / f"{name}.sqlite3"
        r = run("build_search_db.py", "--root", str(root), "--out", str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        server = load_script("mcp_server")
        server.CORPUS = server.Corpus(db)
        self.addCleanup(lambda: server.CORPUS._db and server.CORPUS._db.close())
        return server, server.CORPUS.db.execute("SELECT rowid FROM chunks").fetchone()[0]

    def test_60_a_query_returns_the_passages_that_match_it(self):
        """A long section read for one answer cost all of it. With a `query`,
        the passages that hold the query's words come back, whole, in their
        order, with the gaps said and the way to the rest."""
        filler = [f"Paragraph {n} of the guide describes unrelated housekeeping for the gadget in some detail. " * 4
                  for n in range(40)]
        needle = "The zorbulate option controls how the gadget flushes its buffers after a reset. " * 3
        blocks = filler[:20] + [needle.strip()] + filler[20:]
        server, sid = self.long_section("passages", blocks)
        whole = server.tool_get_section({"section_id": sid})
        part = server.tool_get_section({"section_id": sid, "query": "how does zorbulate flush buffers"})
        self.assertGreater(len(whole), 10_000)
        self.assertLess(len(part), len(whole) // 2, "the section was not narrowed")
        self.assertIn(needle.strip(), part, "the matching passage came back cut or not at all")
        self.assertIn("characters omitted", part)
        self.assertIn("Call get_section without `query` for the whole section.", part)
        self.assertTrue(part.startswith("# Long"), "the section's own heading and citation are kept")
        # With no query, or a blank one, nothing changes.
        self.assertEqual(server.tool_get_section({"section_id": sid, "query": "  "}), whole)
        self.assertNotIn("characters omitted", whole)

    def test_60b_a_passage_is_never_cut_inside_and_keeps_its_order(self):
        server, sid = self.long_section("passages-order", [
            ("Alpha " + f"filler number {n} " * 60 + "end.") for n in range(30)]
            + ["The quokka setting is described here. " * 12, "A fenced example follows." + chr(10) + "```" + chr(10)
               + "set_quokka 1" + chr(10) * 2 + "set_quokka 2" + chr(10) + "```", "The quokka setting ends here. " * 12])
        part = server.tool_get_section({"section_id": sid, "query": "quokka"})
        body = part.split("file: ", 1)[1].split(chr(10), 1)[1]
        whole = server.CORPUS.db.execute("SELECT body FROM chunks WHERE rowid = ?", (sid,)).fetchone()[0]
        pieces = [p for p in body.split(chr(10) * 2) if not p.startswith(("[…", "Passages matching"))]
        # a fenced block holding a blank line is one passage: its two halves are both there, and in order
        self.assertIn("set_quokka 1" + chr(10) * 2 + "set_quokka 2", part)
        position = -1
        for piece in pieces:
            found = whole.find(piece, position + 1)
            self.assertGreater(found, -1, f"text that is not in the section, in this order: {piece[:60]!r}")
            position = found

    def test_60c_a_query_that_matches_nothing_or_a_short_section_gives_the_whole(self):
        server, sid = self.long_section("passages-none", [f"Paragraph {n} is about the gadget. " * 20 for n in range(30)])
        whole = server.tool_get_section({"section_id": sid})
        self.assertEqual(server.tool_get_section({"section_id": sid, "query": "xylophone"}), whole)
        self.assertEqual(server.tool_get_section({"section_id": sid, "query": "the and of"}), whole,
                         "a query of stopwords alone narrowed the section by words that mean nothing")
        short, short_id = self.long_section("passages-short", ["The only paragraph mentions zorbulate."])
        self.assertEqual(short.tool_get_section({"section_id": short_id, "query": "zorbulate"}),
                         short.tool_get_section({"section_id": short_id}))

    def test_60d_a_small_saving_is_not_taken(self):
        """A section a little over the budget, whose five passages each hold
        one of the query's words, would lose only its last one: not worth the
        risk of leaving out what answers, so it is returned whole."""
        words = ["alpha", "bravo", "charlie", "delta", "echo"]
        blocks = [f"Gadget paragraph about {w} " + f"filler number {n} " * 60 for n, w in enumerate(words)]
        server, sid = self.long_section("passages-small", blocks)
        whole = server.tool_get_section({"section_id": sid})
        self.assertGreater(len(whole), server.PASSAGE_BUDGET)
        self.assertEqual(server.tool_get_section({"section_id": sid, "query": " ".join(words)}), whole)
        # One word of the five picks one passage and its neighbours: that is a real saving.
        self.assertLess(len(server.tool_get_section({"section_id": sid, "query": "charlie"})), len(whole))

    def test_60e_the_query_is_part_of_the_tools_schema(self):
        server, _ = self.long_section("passages-schema", ["One paragraph."])
        tool = next(t for t in server.build_tools() if t["name"] == "get_section")
        self.assertIn("query", tool["inputSchema"]["properties"])
        self.assertIn("query", tool["description"])

    QUESTION = "what kinds of widgets are supported"

    def kinds_section(self, first_explanation: int) -> str:
        """A section laid out the way a converted manual lays out a list of
        kinds: a lead-in, bullets with a blank line between every one and a
        paragraph that explains each, sub-bullets with no indentation worth the
        name, and paragraphs that match the question and paragraphs that do not
        around it. The explanations share none of the question's words."""
        nl = chr(10)

        def explain(text: str, size: int) -> str:
            return (text * (size // len(text) + 1))[:size].rsplit(" ", 1)[0] + "."
        filler = [f"Section {n} covers housekeeping for the sprocket in some detail and mentions nothing else. " * 5
                  for n in range(3)]
        strong = [("The supported kinds of widgets differ in how the tool treats them, and each kind of widget is "
                   "described below. " * 8).strip() for _ in range(3)]
        blocks = (filler + strong[:2] + [
            ("The guide names the kinds of widgets that are supported before it explains each one. " * 3).strip(),
            "The following kinds of widgets are supported:",
            "- Opaque", explain("These cells hold contents the tool cannot see. ", first_explanation),
            "   - Sealed housing", "   - Empty shell",
            "- Linked", explain("These cells can be opened and read by the tool. ", 300),
            "- Remote", explain("These cells sit on another machine. ", 300)] + strong[2:] + filler)
        return (nl * 2).join(blocks)

    def test_60h_a_list_comes_back_without_a_hole_in_it(self):
        """The converter puts a blank line between every bullet and the
        paragraph that explains it, so a list is many small blocks and the
        scorer kept some and dropped the ones between: the answer to "what
        kinds" came back with the explanation of its first kind missing."""
        server = load_script("mcp_server")
        part = server.matching_passages(self.kinds_section(300), self.QUESTION)
        self.assertIsNotNone(part)
        for item in ("- Opaque", "- Linked", "- Remote", "sit on another machine"):
            self.assertIn(item, part, f"the list is missing {item!r}")
        listed = part[part.index("- Opaque"):part.index("- Remote")]
        self.assertNotIn("characters omitted", listed, "a gap was left inside the list")

    def test_60i_a_whole_list_is_kept_though_one_explanation_is_longer_than_a_gap(self):
        server = load_script("mcp_server")
        part = server.matching_passages(self.kinds_section(server.PASSAGE_GAP + 100), self.QUESTION)
        self.assertIsNotNone(part)
        for item in ("- Opaque", "- Linked", "- Remote", "sit on another machine"):
            self.assertIn(item, part, f"the list is missing {item!r}")
        listed = part[part.index("- Opaque"):part.index("- Remote")]
        self.assertNotIn("characters omitted", listed, "the list was cut although it is shorter than PASSAGE_LIST_MAX")

    def test_60j_a_short_gap_between_two_passages_is_returned_and_a_long_one_is_marked(self):
        server = load_script("mcp_server")
        nl = chr(10)
        strong = ("The supported kinds of widgets differ in how the tool treats them, and each kind of widget is "
                  "described below. " * 8).strip()

        def section(middle: int) -> str:
            plain = ("Nothing here concerns the sprocket housing or its calibration. " * 30)[:middle].rsplit(" ", 1)[0]
            return (nl * 2).join([strong, plain, strong, strong, strong,
                                  "Unrelated closing note about housekeeping. " * 30, "More housekeeping text. " * 60])
        short = server.matching_passages(section(server.PASSAGE_GAP - 100), self.QUESTION)
        self.assertIsNotNone(short)
        self.assertIn("Nothing here concerns", short)
        long = server.matching_passages(section(server.PASSAGE_GAP + 400), self.QUESTION)
        self.assertIsNotNone(long)
        self.assertNotIn("Nothing here concerns", long)
        self.assertIn("characters omitted", long)

    def test_60k_a_passage_does_not_end_on_a_heading_or_a_lead_in(self):
        """Closing a passage on a heading or "the options are:" put the gap
        straight after it, and the client read a promise with nothing under it."""
        server = load_script("mcp_server")
        nl = chr(10)
        for ending in ("##### **Options**", "The options are:"):
            text = (nl * 2).join(["Some text that is nearly long enough. " * 7, ending,
                                  "- first option", "- second option", "Text that follows. " * 40])
            for passage in server.split_passages(text)[:-1]:
                last = passage.split(nl * 2)[-1]
                self.assertNotEqual(server.block_kind(last), "heading", passage[-60:])
                self.assertNotEqual(server.block_kind(last), "lead", passage[-60:])

    def test_60l_which_blocks_make_a_list(self):
        server = load_script("mcp_server")
        nl = chr(10)
        kinds = lambda blocks: [server.block_kind(b) for b in blocks]  # noqa: E731
        self.assertEqual(kinds(["- a", "1. b", "2) c", "   - d", "## H", "```", "Lead:", "Plain."]),
                         ["item", "item", "item", "item", "heading", "code", "lead", "text"])
        # A lead-in, items with their explanations between, and the one explanation after the last item are one unit;
        # the heading after it is not.
        blocks = ["Intro.", "The kinds are:", "- one", "Explains one.", "- two", "Explains two.", "More about two.",
                  "- three", "Explains three.", "Closing remark.", "# Next"]
        units = server.group_lists(blocks)
        self.assertEqual(units[0], ["Intro."])
        self.assertEqual(units[1], blocks[1:9])
        self.assertEqual(units[2:], [["Closing remark."], ["# Next"]])
        # Too many paragraphs between two items end the list.
        self.assertEqual(len(server.group_lists(["- a", "p1.", "p2.", "p3.", "- b"])), 4)
        # A list longer than the limit is left as it was: narrowing has to cut inside it.
        long = [f"- item {n} " + "x" * 100 for n in range(40)]
        self.assertGreater(len(nl.join(long)), server.PASSAGE_LIST_MAX)
        self.assertEqual(server.group_lists(long), [[b] for b in long])

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

    def served(self, db: Path, log=None):
        """The server over stdio, with $PDF_TO_RAG_LOG set to `log` or not at all."""
        smoke = load_script("mcp_smoke_test")
        env = {k: v for k, v in os.environ.items() if k != "PDF_TO_RAG_LOG"}
        if log is not None:
            env["PDF_TO_RAG_LOG"] = str(log)
        return smoke.Client(db, env=env)

    @staticmethod
    def answer(reply: dict) -> str:
        return reply["result"]["content"][0]["text"]

    def test_54_the_call_log_is_opt_in_and_records_each_call(self):
        """The call log is how real questions are collected, so it has to be
        off until asked for, say what each call returned, and keep stdout to
        the protocol: the client reads every line of it as a reply."""
        db = WS.index()
        quiet = self.tmp / "unset" / "calls.jsonl"
        client = self.served(db)
        try:
            client.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
            self.assertFalse(client.call("tools/call", {"name": "search_docs", "arguments": {
                "query": "torque the bolts"}})["result"]["isError"])
        finally:
            client.close()
        self.assertFalse(quiet.parent.exists(), "a log was written without being asked for")

        log = self.tmp / "calls" / "real.jsonl"           # its folder does not exist yet
        long_text = "x" * 5000
        client = self.served(db, log)
        try:
            client.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "usage-test", "version": "1"}})
            client.notify("notifications/initialized")
            found = self.answer(client.call("tools/call", {"name": "search_docs", "arguments": {
                "query": "torque the bolts evenly", "limit": 3}}))
            ids = [int(n) for n in re.findall(r"section_id: (\d+)", found)]
            self.assertTrue(ids, found)
            read = self.answer(client.call("tools/call", {"name": "get_section", "arguments": {
                "section_id": ids[0]}}))
            entry = self.answer(client.call("tools/call", {"name": "lookup_entity", "arguments": {
                "name": "set_widget_option_03"}}))
            bad = client.call("tools/call", {"name": "get_section", "arguments": {}})
            client.call("tools/call", {"name": "explain_log", "arguments": {"text": long_text}})
            client.call("tools/call", {"name": "nope", "arguments": {}})        # not a tool: no line
        finally:
            client.close()

        lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([x["tool"] for x in lines],
                         ["search_docs", "get_section", "lookup_entity", "get_section", "explain_log"])
        search, section, lookup, failed, explain = lines
        self.assertEqual(search["args"], {"query": "torque the bolts evenly", "limit": 3})
        self.assertEqual(search["sections"], ids, "the search's section ids were not logged in order")
        self.assertEqual(search["chars"], len(found))
        self.assertEqual(search["client"], "usage-test")
        self.assertFalse(search["error"])
        self.assertIsInstance(search["ms"], (int, float))
        self.assertTrue(search["t"].endswith("+00:00"), search["t"])
        # refs name the same sections without depending on this index's ids
        con = sqlite3.connect(db)
        try:
            for sid, ref in zip(search["sections"], search["refs"]):
                collection, slug, file = con.execute(
                    "SELECT collection, slug, file FROM chunks WHERE rowid = ?", (sid,)).fetchone()
                self.assertEqual(ref, [collection, slug, "sections/" + file.rsplit("/", 1)[-1]])
        finally:
            con.close()
        self.assertEqual(section["sections"], [ids[0]])
        self.assertEqual(section["chars"], len(read))
        self.assertEqual(lookup["sections"], [int(n) for n in re.findall(r"section_id: (\d+)", entry)])
        self.assertTrue(lookup["sections"], "an entry's sections were not logged")
        self.assertTrue(failed["error"])
        self.assertEqual((failed["sections"], failed["refs"]), ([], []))
        self.assertTrue(failed["chars"] and bad["result"]["isError"])
        self.assertEqual(explain["args"]["text"], "x" * 2000 + "…[5000 chars]",
                         "a long argument was logged whole")

    def test_54b_a_log_that_cannot_be_written_is_dropped_not_fatal(self):
        db = WS.index()
        blocked = self.tmp / "blocked"
        blocked.mkdir()                       # a folder, so the file cannot be opened
        client = self.served(db, blocked)
        try:
            client.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
            for _ in range(2):
                reply = client.call("tools/call", {"name": "search_docs", "arguments": {"query": "torque the bolts"}})
                self.assertFalse(reply["result"]["isError"], reply)
            client.proc.stdin.close()
            client.proc.wait(timeout=10)
            err = client.proc.stderr.read()
        finally:
            client.close()
        self.assertEqual(err.count("call log dropped"), 1, "the failure was not said once:\n" + err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
