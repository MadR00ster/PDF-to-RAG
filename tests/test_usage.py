"""Measuring real use: the call log's searches become test questions
(triage_log.py), and a question set is answered end to end (eval_answers.py)."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _support import PY, SCRIPTS, WS, load_script, run  # noqa: E402


def serve_and_log(db: Path, log: Path, searches: list[str]) -> None:
    """Drive the real server over stdio with the call log on, as an editor would."""
    smoke = load_script("mcp_smoke_test")
    env = {**{k: v for k, v in os.environ.items() if k != "PDF_TO_RAG_LOG"}, "PDF_TO_RAG_LOG": str(log)}
    client = smoke.Client(db, env=env)
    try:
        client.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "triage-test"}})
        for query in searches:
            client.call("tools/call", {"name": "search_docs", "arguments": {"query": query, "limit": 4}})
    finally:
        client.close()


def triage(*args: str, answers: str = "") -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(SCRIPTS / "triage_log.py"), *args], input=answers, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def section_files(db: Path, slug: str) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return ["sections/" + r[0].rsplit("/", 1)[-1]
                for r in con.execute("SELECT file FROM chunks WHERE slug = ? ORDER BY ord", (slug,))]
    finally:
        con.close()


class TriageTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.tmp = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_55_logged_searches_become_real_questions(self):
        db = WS.index()
        log = self.tmp / "calls.jsonl"
        # The same search twice is one search to judge; the last is nothing the corpus answers.
        serve_and_log(db, log, ["torque the bolts evenly", "set_widget_option_03", "torque the bolts evenly",
                                "bracket warping", "calibration drift"])
        prose = section_files(db, "prose")
        header = "// A header the user wrote. It must come through untouched.\n"
        first = json.dumps({"id": "q01", "kind": "concept", "question": "how are the bolts torqued",
                            "answers": [{"slug": "prose", "file": prose[1]}]})
        questions = self.tmp / "questions.jsonl"
        questions.write_text(header + first, encoding="utf-8")        # no newline at the end

        script = "\n".join([
            "h", "1", "How tight should the bolts be?", "Torque them evenly.",     # 1: a hit on result 1
            "m", f"prose {prose[2]}", "", "", "",                                   # 2: a miss; the right section by name
            "i",                                                                    # 3: not a documentation question
            "s",                                                                    # 4: later
        ]) + "\n"
        r = triage("--log", str(log), "--questions", str(questions), "--db", str(db), answers=script)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("4 search(es) to triage", r.stdout)
        self.assertIn("asked 2 times", r.stdout)
        self.assertIn("triage-test", r.stdout)

        text = questions.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(header + first + "\n"), "what the file held was changed")
        added = [json.loads(x) for x in text.splitlines()[2:]]
        self.assertEqual([q["id"] for q in added], ["r01", "r02"])
        hit, miss = added
        calls = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
        top = calls[2]["refs"][0]                       # the latest of the two identical searches
        self.assertEqual(hit["kind"], "real")
        self.assertEqual(hit["question"], "torque the bolts evenly")
        self.assertEqual(hit["answers"], [{"slug": top[1], "file": top[2], "collection": top[0]}])
        self.assertEqual(hit["ask"], "How tight should the bolts be?")
        self.assertEqual(hit["gold"], "Torque them evenly.")
        self.assertIn("marked hit", hit["note"])
        self.assertIn("rank 1", hit["note"])
        self.assertEqual(miss["question"], "set_widget_option_03")
        self.assertEqual(miss["answers"][0]["slug"], "prose")
        self.assertEqual(miss["answers"][0]["file"], prose[2])
        self.assertTrue(miss["answers"][0]["collection"], "the collection was not filled in from the index")
        self.assertNotIn("ask", miss)
        self.assertNotIn("gold", miss)
        self.assertIn("marked miss", miss["note"])
        self.assertIn("nowhere in its results", miss["note"])

        # Done is remembered beside the log: a second run shows only what was skipped.
        r = triage("--log", str(log), "--questions", str(questions), "--db", str(db), answers="q\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("1 search(es) to triage", r.stdout)
        self.assertIn("calibration drift", r.stdout)
        self.assertEqual(questions.read_text(encoding="utf-8"), text, "quitting changed the question file")

        # And what it wrote is a question file eval_search.py scores, `real` on its own row.
        r = run("eval_search.py", "--db", str(db), "--questions", str(questions))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"real\s+2\s")

    def test_55b_the_log_is_summarised_and_listed_without_asking(self):
        log = self.tmp / "hand.jsonl"
        lines = [
            {"t": "2026-11-08T09:00:00.000+00:00", "tool": "search_docs", "args": {"query": "no such thing"},
             "sections": [], "refs": [], "chars": 120, "ms": 4.0, "error": False, "client": "a"},
            {"t": "2026-11-08T09:01:00.000+00:00", "tool": "search_docs", "args": {"query": "clock gating"},
             "sections": [7], "refs": [["c", "doc", "sections/001-x.md"]], "chars": 900, "ms": 20.0,
             "error": False, "client": "a"},
            {"t": "2026-11-08T09:02:00.000+00:00", "tool": "get_section", "args": {"section_id": 7},
             "sections": [7], "refs": [["c", "doc", "sections/001-x.md"]], "chars": 3000, "ms": 8.0, "error": False},
            {"t": "2026-11-08T09:03:00.000+00:00", "tool": "get_section", "args": {},
             "sections": [], "refs": [], "chars": 40, "ms": 1.0, "error": True},
        ]
        log.write_text("\n".join(json.dumps(x) for x in lines) + '\n{"t": "2026-11-08T09:04', encoding="utf-8")
        r = triage("--log", str(log), "--summary")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("4 call(s)", r.stdout)
        self.assertIn("1 unreadable line(s)", r.stdout)
        self.assertRegex(r.stdout, r"get_section\s+2\s+1\s")
        self.assertIn("2 distinct", r.stdout)
        self.assertIn("1 returned nothing", r.stdout)
        self.assertIn("no results: no such thing", r.stdout)

        r = triage("--log", str(log), "--list")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("2 search(es) to triage", r.stdout)
        self.assertIn("doc · 001-x.md", r.stdout)
        self.assertFalse(log.with_name(log.name + ".triage.json").exists(), "listing recorded something as done")

    def test_55c_a_section_can_be_named_without_an_index(self):
        """With no --db a miss is answered by `slug file`, taken as written; an
        answer no one could place is left out of the question file."""
        log = self.tmp / "nodb.jsonl"
        log.write_text(json.dumps({"t": "2026-11-08T09:00:00.000+00:00", "tool": "search_docs",
                                   "args": {"query": "clock gating", "collection": "c"}, "sections": [],
                                   "refs": [], "chars": 10, "ms": 1.0, "error": False}) + "\n", encoding="utf-8")
        questions = self.tmp / "nodb-questions.jsonl"
        r = triage("--log", str(log), "--questions", str(questions),
                   answers="m\n12\nwords\nguide sections/004-gating.md\n\n\nGating stops the clock.\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("a section id needs --db", r.stdout)
        self.assertIn("words to search for need --db", r.stdout)
        q = json.loads(questions.read_text(encoding="utf-8"))
        self.assertEqual(q["id"], "r01")
        self.assertEqual(q["answers"], [{"slug": "guide", "file": "sections/004-gating.md"}])
        self.assertEqual(q["gold"], "Gating stops the clock.")

        # Nothing named, nothing added: the search stays for next time.
        other = self.tmp / "nodb-questions-2.jsonl"
        r = triage("--log", str(log.with_name("nodb2.jsonl")), "--questions", str(other))
        self.assertEqual(r.returncode, 1, "a missing log should be said, not read as empty")
        self.assertIn("No call log", r.stderr)
        self.assertFalse(other.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
