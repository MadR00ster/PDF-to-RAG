"""A tool log in, the reference entries for what it names out."""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _support import run, write_pdf, load_script  # noqa: E402


def drc_rules_fixture(path: Path, n_rules: int) -> None:
    """A DRC rules manual: one "DRC Rule Cn" entry per rule under a chapter,
    then a chapter of message codes. Each rule's advice names its own number,
    so an entry served for the wrong rule shows."""
    pages = [[("1 Category C - Clock Rules", 22), ("Rules about clocks.", 11)]]
    toc = [[1, "1 Category C - Clock Rules", 1]]
    for n in range(1, n_rules + 1):
        pages.append([(f"DRC Rule C{n}", 18), ("Message Text", 14),
                      (f"Clock Rule C{n}: clock input failed to hold, case {n}.", 11),
                      ("What Next", 14), (f"Constrain the test-mode port for clock case {n}.", 11)])
        toc.append([2, f"DRC Rule C{n}", len(pages)])
    pages.append([("2 Messages", 22), ("Messages the rule checker prints.", 11)])
    toc.append([1, "2 Messages", len(pages)])
    pages.append([("SEL-003", 18), ("Selection failed for the given object.", 11)])
    toc.append([2, "SEL-003", len(pages)])
    write_pdf(path, pages, toc)


class LogTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.tmp = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_50_drc_rules_are_entries_found_by_their_id(self):
        """A rules manual titles its entries "DRC Rule C1"; logs say "Rule C1".

        Neither entry test took a rule title for an entry, so a 457-page rules
        manual went down the prose path: 118 chunks for 447 rules, half of them
        bare "Description" fragments, and no rule could be looked up at all.
        """
        common, server = load_script("_common"), load_script("mcp_server")
        for sample in ("DRC Rule C1", "Rule S29", "rule K80", "DRC Rule ABC1234", "DRC Rule C", "DRC Rules C1",
                       "Rule c1", "Clock Rules"):
            self.assertEqual(bool(common.RULE_TITLE_RE.match(sample)),
                             bool(server.RULE_TITLE_RE.fullmatch(sample)), sample)
        self.assertTrue(common.looks_like_entry("DRC Rule C1"))

        coll = self.tmp / "Root" / "Vendor"
        (coll / "docs").mkdir(parents=True)
        pdf = coll / "rules.pdf"
        drc_rules_fixture(pdf, 22)
        r = run("pick_extractor.py", str(pdf))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("shape: reference", r.stdout)
        r = run("rebuild_reference.py", str(pdf), "--title", "Widget DRC Rules", "--slug", "rules")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = coll / "docs" / "rules"
        m = json.loads((doc / "manifest.json").read_text(encoding="utf-8"))
        owners = {s["command"] for s in m["sections"] if s.get("command")}
        self.assertEqual(owners, {f"DRC Rule C{n}" for n in range(1, 23)} | {"SEL-003"})
        for s in m["sections"]:
            case = re.search(r"clock case (\d+)", (doc / s["file"]).read_text(encoding="utf-8"))
            if case:
                self.assertEqual(s["command"], f"DRC Rule C{case.group(1)}", f"{s['file']} has another rule's advice")

        db = self.tmp / "rules.sqlite3"
        r = run("build_search_db.py", "--root", str(self.tmp / "Root"), "--out", str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        server.CORPUS = corpus = server.Corpus(db)
        self.addCleanup(lambda: corpus._db and corpus._db.close())

        for asked in ("C1", "Rule C1", "drc rule c1", "DRC Rule C1"):
            out = server.tool_lookup_entity({"name": asked})
            self.assertIn("clock case 1.", out, f"{asked!r} did not find rule C1")
            self.assertNotIn("clock case 10", out, f"{asked!r} also served rule C10")
        self.assertNotIn("# `", server.tool_lookup_entity({"name": "C99"}), "a rule that is not there was served")

        log = "\n".join([
            "Warning: Rule C3 (clock not held) was violated 4 times.",
            "Information: Selection failed (SEL-003)",
            "Error: Rule C12 (clock input failed) was violated 1 times.",
            "Warning: Rule C3 (clock not held) was violated 4 times.",
            "Warning: something else (ZZZ-123)",
            "net D5 is floating",
        ])
        out = server.tool_explain_log({"text": log})
        self.assertIn("4 distinct", out, out)
        self.assertLess(out.index("`C12`"), out.index("`C3`"), "an error was not put before a warning")
        self.assertIn("`C3` · 2 line(s) · warning · first at line 1", out)
        self.assertIn("clock case 12.", out)
        self.assertIn("clock case 3.", out)
        self.assertIn("Selection failed for the given object", out)
        self.assertIn("`ZZZ-123`", out.split("Not an entry")[1], "an unknown code was not reported as such")
        self.assertNotIn("`D5`", out, "a bare id with no 'Rule' before it was taken for a rule")
        self.assertIn("No message codes", server.tool_explain_log({"text": "all clean, 0 violations"}))
        corpus._db.close()

        r = run("mcp_smoke_test.py", "--db", str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("explain_log({\"text\": \"Warning: Rule", r.stdout, "the smoke test did not probe a rule")


if __name__ == "__main__":
    unittest.main(verbosity=2)
