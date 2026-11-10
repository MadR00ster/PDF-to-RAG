"""Measuring real use: the call log's searches become test questions
(triage_log.py), and a question set is answered end to end (eval_answers.py)."""
from __future__ import annotations

import json
import os
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
             "sections": [7], "refs": [["c", "doc", "sections/001-x.md"]], "chars": 3000, "ms": 8.0, "error": False,
             "token_id": "1a2b3c4d"},
            {"t": "2026-11-08T09:03:00.000+00:00", "tool": "get_section", "args": {},
             "sections": [], "refs": [], "chars": 40, "ms": 1.0, "error": True, "token_id": "1a2b3c4d"},
        ]
        log.write_text("\n".join(json.dumps(x) for x in lines) + '\n{"t": "2026-11-08T09:04', encoding="utf-8")
        r = triage("--log", str(log), "--summary")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("4 call(s)", r.stdout)
        self.assertIn("1 unreadable line(s)", r.stdout)
        self.assertIn("tokens: 1a2b3c4d 2", r.stdout)
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


STUB = r"""
import json, os, subprocess, sys
argv = sys.argv[1:]
prompt = sys.stdin.read()
try:
    cfg = json.load(open(argv[argv.index("--mcp-config") + 1]))["mcpServers"]
except (ValueError, KeyError):
    cfg = {}
with open(os.path.join(os.environ["STUB_RECORD"], "calls.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "prompt": prompt, "servers": sorted(cfg)}) + "\n")


def usage(i, o, cr, cw):
    return {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}


if "You are grading" in prompt:
    answer = prompt.split("Answer to grade:\n", 1)[1].split("\n\nGive exactly", 1)[0]
    gold = prompt.split("Gold answer:\n", 1)[1].split("\n\nGold sources", 1)[0]
    verdict = ("declined" if "does not cover" in answer else
               "correct" if gold.lower().rstrip(".") in answer.lower() else "unsupported")
    reply = "Here is my verdict: " + json.dumps({"verdict": verdict, "reason": "stub"})
    print(json.dumps({"type": "result", "is_error": False, "result": reply, "usage": usage(300, 20, 0, 0),
                      "total_cost_usd": 0.002, "num_turns": 1}))
    sys.exit(0)

# An answering run: ask the server named in the config, as Claude Code would.
server = cfg["docs"]
proc = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        text=True, encoding="utf-8", env={**os.environ, **server.get("env", {})})
for i, (method, params) in enumerate([
        ("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "stub-claude"}}),
        ("tools/call", {"name": "search_docs", "arguments": {"query": prompt.strip(), "limit": 10}})], 1):
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\n")
    proc.stdin.flush()
    proc.stdout.readline()
proc.stdin.close()
proc.wait(timeout=10)

answers = json.loads(os.environ["STUB_ANSWERS"])
text = next(a for key, a in answers.items() if key in prompt)
if text == "FAIL":
    print(json.dumps({"type": "result", "is_error": True, "result": "boom", "usage": usage(1000, 200, 5000, 400),
                      "total_cost_usd": 0.01, "num_turns": 2}))
    sys.exit(1)
print(json.dumps({"type": "result", "is_error": False, "result": text, "usage": usage(1000, 200, 5000, 400),
                  "total_cost_usd": 0.01, "num_turns": 3}))
"""


class AnswersTest(unittest.TestCase):
    """eval_answers.py against a stand-in for `claude -p`: the same flags in,
    the same JSON out, no network. The stand-in starts the corpus's own server
    from the MCP config it is handed, so the call log is the real one."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.tmp = Path(cls._tmp.name)
        cls.stub = cls.tmp / "claude_stub.py"
        cls.stub.write_text(STUB, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def questions(self, db: Path, name: str) -> Path:
        prose = section_files(db, "prose")
        rows = [
            {"id": "t1", "kind": "real", "question": "torque bolts", "ask": "How should the bolts be torqued?",
             "gold": "Torque the bolts evenly.", "answers": [{"slug": "prose", "file": prose[0]}]},
            {"id": "t2", "kind": "concept", "question": "flux capacitor setting",
             "gold": "Set the flux capacitor to on.", "answers": [{"slug": "prose", "file": prose[1]}]},
            {"id": "t3", "kind": "real", "question": "what about drift", "gold": "Recalibrate every month.",
             "answers": [{"slug": "prose", "file": prose[5]}]},
            {"id": "t4", "kind": "concept", "question": "this one will ERROR",
             "gold": "Never graded.", "answers": [{"slug": "prose", "file": prose[2]}]},
            {"id": "t5", "kind": "real", "question": "torque bolts", "answers": [{"slug": "prose", "file": prose[3]}]},
        ]
        path = self.tmp / name
        path.write_text("// the first line is a comment\n" + "\n".join(json.dumps(q) for q in rows) + "\n",
                        encoding="utf-8")
        return path

    def evaluate(self, db: Path, questions: Path, *extra: str, name: str = "run") -> tuple:
        record = self.tmp / f"{name}-record"
        record.mkdir()
        answers = {"How should the bolts be torqued?": "Torque the bolts evenly, as the Widget Guide says on p. 1.",
                   "what about drift": "Use a hammer.",
                   "flux capacitor": "The documentation does not cover that.",
                   "ERROR": "FAIL"}
        env = {**os.environ, "STUB_RECORD": str(record), "STUB_ANSWERS": json.dumps(answers)}
        env.pop("PDF_TO_RAG_LOG", None)
        out = self.tmp / f"{name}.json"
        r = subprocess.run([PY, str(SCRIPTS / "eval_answers.py"), "--db", str(db), "--questions", str(questions),
                            "--claude", PY, "--claude-arg", str(self.stub), "--json", str(out), *extra],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        calls = []
        if (record / "calls.jsonl").is_file():
            calls = [json.loads(x) for x in (record / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        return r, calls, out

    def test_56_questions_are_answered_graded_and_counted(self):
        db = WS.index()
        questions = self.questions(db, "answers.jsonl")

        r, calls, _ = self.evaluate(db, questions, "--dry-run", name="dry")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(calls, [], "a dry run called claude")
        self.assertIn("4 question(s) to run (1 left out for having no gold answer)", r.stdout)
        self.assertIn("How should the bolts be torqued?", r.stdout)

        r, calls, out = self.evaluate(db, questions)
        self.assertEqual(r.returncode, 3, "a run with an error in it should say so\n" + r.stdout + r.stderr)
        done = {x["id"]: x for x in json.loads(out.read_text(encoding="utf-8"))["results"]}
        self.assertEqual({k: v["verdict"] for k, v in done.items()},
                         {"t1": "correct", "t2": "declined", "t3": "unsupported", "t4": "error"})
        self.assertEqual(done["t3"]["reason"], "stub")
        self.assertIn("claude reported an error: boom", done["t4"]["error"])

        # What claude was asked: the wording a person would use, else the question.
        answering = [c for c in calls if "You are grading" not in c["prompt"]]
        grading = [c for c in calls if "You are grading" in c["prompt"]]
        self.assertEqual([c["prompt"] for c in answering],
                         ["How should the bolts be torqued?", "flux capacitor setting", "what about drift",
                          "this one will ERROR"])
        self.assertEqual(done["t1"]["asked"], "How should the bolts be torqued?")
        self.assertEqual(len(grading), 3, "an answer that failed was graded anyway")
        self.assertIn("Widget Guide", grading[0]["prompt"])           # the gold sources, from the index
        self.assertIn('section "Installation"', grading[0]["prompt"])

        # Only the corpus's server, no built-in tools, nothing kept, and not from the repo.
        for c in answering:
            argv = c["argv"]
            self.assertEqual(c["servers"], ["docs"])
            self.assertIn("--strict-mcp-config", argv)
            self.assertEqual(argv[argv.index("--tools") + 1], "")
            self.assertEqual(argv[argv.index("--allowedTools") + 1], "mcp__docs")
            self.assertIn("--no-session-persistence", argv)
            self.assertEqual(argv[argv.index("--output-format") + 1], "json")
            self.assertFalse(Path(c["cwd"]).resolve().is_relative_to(SCRIPTS.parent), "claude ran in the checkout")
        for c in grading:
            self.assertEqual(c["servers"], [], "the grader was given the documentation")
            self.assertNotIn("--allowedTools", c["argv"])

        # The server's own call log says what each run was handed.
        self.assertTrue(done["t1"]["returned_gold"])
        self.assertEqual(done["t1"]["tools"], {"search_docs": 1})

        # Tokens: four answering runs (the failed one counts) and three gradings.
        self.assertIn("in 4,000 · cache read 20,000 · cache write 1,600 · out 800  = 26,400", r.stdout)
        self.assertIn("in 900 · cache read 0 · cache write 0 · out 60  = 960", r.stdout)
        self.assertIn("cost reported by claude: $0.0460", r.stdout)
        self.assertRegex(r.stdout, r"all\s+4\s+1\s+33%\s+1\s+33%\s+1\s+33%\s+1\s")
        self.assertRegex(r.stdout, r"real\s+2\s+1\s+50%\s+1\s+50%\s+0\s+0%\s+0\s")
        self.assertIn("[t3] unsupported", r.stdout)
        self.assertIn("answer: Use a hammer.", r.stdout)

        # Filters: one kind, one id, a limit.
        r, calls, _ = self.evaluate(db, questions, "--kind", "real", "--dry-run", name="kind")
        self.assertIn("2 question(s) to run (1 left out", r.stdout)
        r, calls, _ = self.evaluate(db, questions, "--ids", "t3,t4", "--limit", "1", "--dry-run", name="ids")
        self.assertIn("1 question(s) to run", r.stdout)
        self.assertIn("what about drift", r.stdout)

    def test_56b_a_graders_reply_is_read_leniently_and_never_guessed(self):
        answers = load_script("eval_answers")
        self.assertEqual(answers.parse_verdict('{"verdict": "correct", "reason": "it cites p. 4"}'),
                         ("correct", "it cites p. 4"))
        self.assertEqual(answers.parse_verdict('```json\n{"verdict": "Declined", "reason": "no answer"}\n```'),
                         ("declined", "no answer"))
        self.assertEqual(answers.parse_verdict('My verdict: "verdict": "unsupported", but not valid JSON {')[0],
                         "unsupported")
        for reply in ('{"verdict": "mostly right"}', "It is correct.", "", "{}"):
            verdict, why = answers.parse_verdict(reply)
            self.assertIsNone(verdict, reply)
            self.assertIn("not a verdict", why)

    def test_56c_a_run_that_cannot_start_says_why(self):
        db = WS.index()
        questions = self.questions(db, "answers-c.jsonl")

        def eval_answers(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run([PY, str(SCRIPTS / "eval_answers.py"), "--db", str(db), *args],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace")

        r = eval_answers("--questions", str(questions), "--claude", "claude-is-not-installed-here")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Cannot find `claude-is-not-installed-here`", r.stdout + r.stderr)

        nogold = self.tmp / "nogold.jsonl"
        nogold.write_text(json.dumps({"id": "x", "question": "torque bolts",
                                      "answers": [{"slug": "prose", "file": section_files(db, "prose")[0]}]}) + "\n",
                          encoding="utf-8")
        r = eval_answers("--questions", str(nogold), "--dry-run")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("1 had no `gold` answer", r.stderr)

        stale = self.tmp / "stale.jsonl"
        stale.write_text(json.dumps({"id": "y", "question": "torque bolts", "gold": "g",
                                     "answers": [{"slug": "prose", "file": "sections/9999-gone.md"}]}) + "\n",
                         encoding="utf-8")
        r = eval_answers("--questions", str(stale), "--dry-run")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("not in this index", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
