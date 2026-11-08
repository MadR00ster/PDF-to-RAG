"""check_script: a Tcl script's commands and options against the reference entries."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _support import WS, load_script, run, write_pdf  # noqa: E402


def gizmo_reference(path: Path, release: str, extra: dict[str, list[str]], core: dict[str, list[str]] | None = None) -> None:
    """A command reference with the options of every entry written out the way
    a vendor's manual does: a syntax line, then an Arguments list."""
    entries = {
        **(core if core is not None else {
            "report_gizmo": ["-verbose", "-max_paths", "-max_slack", "-nworst"],
            "set_gizmo_mode": ["-mode"],
            **{f"fill_gizmo_{n:02d}": ["-value"] for n in range(1, 21)},   # enough entries to be a reference
        }),
        **extra,
    }
    pages, toc = [], []
    for name, options in entries.items():
        pages.append([(name, 20), ("SYNTAX", 14), (f"{name} [{'] ['.join(options)}]", 11), ("ARGUMENTS", 14),
                      *[(f"{o}  sets the {o.lstrip('-')} of {name} in release {release}", 11) for o in options]])
        toc.append([1, name, len(pages)])
    write_pdf(path, pages, toc)


class TclParseTest(unittest.TestCase):
    """What the scanner finds in a script, with no index in sight."""

    @classmethod
    def setUpClass(cls):
        cls.server = load_script("mcp_server")

    def scan(self, script: str):
        scan = self.server.TclScan()
        scan.script(script)
        return scan

    def names(self, script: str) -> list[str]:
        return [c[0] for c in self.scan(script).calls]

    def test_57_the_scanner_reads_tcl_the_way_tcl_does(self):
        # Comments, including one after a `;` and one that continues; not a `#` inside a command.
        self.assertEqual(self.names("# get_cells a\nreport_a ; # report_b\nreport_c #report_d \\\n  -x\n# one \\\n report_e\n"),
                         ["report_a", "report_c"])
        # Words that are data are not scanned: braced, and a `\[`.
        self.assertEqual(self.names("puts {[report_a]}\nputs \\[report_b]\nset x {get_cells a}\n"), [])
        # Words that are run: bare and quoted, nested, and inside an expression.
        self.assertEqual(sorted(self.names('set a [list [get_cells x] [get_pins "y [get_nets z]"]]\nputs "n: [report_a]"\n')),
                         ["get_cells", "get_nets", "get_pins", "report_a"])
        self.assertEqual(self.names("expr {[sizeof_collection $x] > 0}\n"), ["sizeof_collection"])

        # Control commands: their bodies are scripts, their conditions expressions.
        script = """\
if {[count_a] > 1} {
    body_a
} elseif {[count_b]} then {
    body_b
} else {
    body_c -x
}
while {[cond_w]} { body_w }
for {init_f} {[cond_f]} {next_f} { body_f }
foreach c [get_items] { each_item -from $c }
foreach_in_collection c [get_more] { each_more }
proc helper {a} { in_helper -opt $a }
helper 1
catch { risky_cmd } err
switch -glob -- $x { a* { sw_a } default { sw_d -z } }
switch $y x { sw_x } y { sw_y }
namespace eval ns { in_ns }
try { try_body } on error {e} { try_err } finally { try_fin }
"""
        scan = self.scan(script)
        self.assertEqual(sorted(c[0] for c in scan.calls),
                         sorted(["count_a", "body_a", "count_b", "body_b", "body_c", "cond_w", "body_w", "init_f",
                                 "cond_f", "next_f", "body_f", "get_items", "each_item", "get_more", "each_more",
                                 "foreach_in_collection", "in_helper", "helper", "risky_cmd", "sw_a", "sw_d", "sw_x",
                                 "sw_y", "in_ns", "try_body", "try_err", "try_fin"]))
        self.assertEqual(scan.procs, {"helper"})

        # A command named by a variable cannot be checked, and is counted.
        scan = self.scan("$cmd -x\nset n [$other 1]\nputs ok\n")
        self.assertEqual((scan.calls, sorted(scan.dynamic)), ([], [1, 2]))

    def test_57b_options_and_lines_are_what_the_script_wrote(self):
        script = ("report_a -from $x \\\n"
                  "    -through [get_pins -hier a*] -nworst 3 -5 -- -after\n"
                  "if {1} {\n"
                  "    set_b -mode {-notopt} \"-quoted\"\n"
                  "}\n")
        calls = {c[0]: c for c in self.scan(script).calls}
        self.assertEqual(calls["report_a"], ("report_a", 1, [("-from", 1), ("-through", 2), ("-nworst", 2)]))
        self.assertEqual(calls["get_pins"], ("get_pins", 2, [("-hier", 2)]))
        self.assertEqual(calls["set_b"], ("set_b", 4, [("-mode", 4), ("-quoted", 4)]))
        # Unbalanced input is read as far as it goes, not refused.
        self.assertEqual(self.names("report_a {never closed\nreport_b"), ["report_a"])
        self.assertEqual(self.names('report_a "never closed [report_b'), ["report_a", "report_b"])


class CheckScriptTest(unittest.TestCase):
    """The tool against a reference in two releases."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.tmp = Path(cls._tmp.name)
        coll = WS.collection("gizmo")
        # `shared_gizmo` is in two manuals with different options; `-beta_` is how a PDF
        # that broke a name across lines leaves it in an entry's text.
        shared = {"shared_gizmo": ["-alpha", "-beta", "-beta_"]}
        for n, release, extra in (("1", "1.0", {"old_gizmo_cmd": ["-legacy"], "bare_gizmo": [], **shared}),
                                  ("2", "2.0", {"bare_gizmo": [], **shared})):
            pdf = coll / f"gizmo-ref-{n}.pdf"
            gizmo_reference(pdf, release, extra)
            r = run("rebuild_reference.py", str(pdf), "--title", "Gizmo Reference", "--slug", f"gizmo-ref-v{n}",
                    "--doc-id", "gizmo-ref", "--version", release)
            assert r.returncode == 0, r.stdout + r.stderr
        lite = coll / "gizmo-lite.pdf"
        gizmo_reference(lite, "1.0", {"shared_gizmo": ["-alpha", "-gamma"]},
                        core={f"fill_lite_{n:02d}": ["-value"] for n in range(1, 21)})
        r = run("rebuild_reference.py", str(lite), "--title", "Gizmo Lite Reference", "--slug", "gizmo-lite")
        assert r.returncode == 0, r.stdout + r.stderr
        # A reference whose entries are variables: a name in it is not a command.
        names = ["gizmo_timeout"] + [f"gizmo_var_{n:02d}" for n in range(1, 22)]
        pages = [[(name, 20), ("Default: 5", 11), (f"The {name} variable limits what the tool does.", 11)]
                 for name in names]
        write_pdf(coll / "gizmo-vars.pdf", pages, [[1, name, i + 1] for i, name in enumerate(names)])
        r = run("rebuild_reference.py", str(coll / "gizmo-vars.pdf"), "--title", "Gizmo Variables Reference",
                "--slug", "gizmo-vars")
        assert r.returncode == 0, r.stdout + r.stderr
        db = cls.tmp / "gizmo.sqlite3"
        r = run("build_search_db.py", str(coll), "--out", str(db))
        assert r.returncode == 0, r.stdout + r.stderr
        cls.server = load_script("mcp_server")
        cls.server.CORPUS = cls.server.Corpus(db)

    @classmethod
    def tearDownClass(cls):
        if cls.server.CORPUS._db:
            cls.server.CORPUS._db.close()
        cls._tmp.cleanup()

    def check(self, script: str, **args) -> str:
        return self.server.tool_check_script({"script": script, **args})

    def test_58_commands_and_options_are_checked_against_the_entries(self):
        out = self.check(
            "set clocks [get_clocks *]\n"                                       # Tcl's own, and one no entry has
            "report_gizmo -verbose -max_paths 3\n"                              # all documented
            "report_gizmo -nw 2\n"                                              # the start of one option
            "report_gizmo -max 5\n"                                             # the start of two
            "report_gizmo -verbos3\n"                                           # a typo
            "set_gizmo_mode -mod fast\n"
            "report_gizmoo\n"                                                   # a typo of a command
            "old_gizmo_cmd -legacy\n"                                           # only in release 1.0
            "bare_gizmo -anything\n"                                            # an entry that shows no options
            "puts hello\n")
        self.assertIn("6 commands checked, used 9 times, against the current edition of each manual: "
                      "3 documented, 3 not.", out)
        unknown = out.split("## Unknown commands")[1].split("##")[0]
        self.assertIn("`get_clocks` (line 1)", unknown)
        self.assertIn("`report_gizmoo` (line 7): not an entry", unknown)
        self.assertIn("Did you mean `report_gizmo`?", unknown)
        self.assertIn("`old_gizmo_cmd` (line 8): not an entry in the current edition of each manual. It is an entry in "
                      "Gizmo Reference, 1.0 (gizmo-ref-v1)", unknown)

        options = out.split("## Unknown options")[1].split("##")[0]
        self.assertIn("`report_gizmo` -verbos3 (line 5)", options)
        self.assertIn("Did you mean `-verbose`?", options)
        self.assertIn("`report_gizmo` -max (line 4): ambiguous, the start of `-max_paths`, `-max_slack`", options)
        self.assertNotIn("-verbose (", options, "a documented option was reported")
        self.assertNotIn("-max_paths (", options)
        abbreviated = out.split("## Abbreviated options")[1].split("##")[0]
        self.assertIn("`report_gizmo` -nw (line 3) is the start of `-nworst`", abbreviated)
        self.assertIn("`set_gizmo_mode` -mod (line 6) is the start of `-mode`", abbreviated)

        documented = out.split("## Documented")[1].split("##")[0]
        self.assertRegex(documented, r"`report_gizmo` -- Gizmo Reference, 2\.0 \(gizmo-ref-v2\) · p\. \d+ \(lines 2, 3, 4, 5\)")
        self.assertIn("Options not checked, because the entry shows none: `bare_gizmo`", out)
        self.assertNotIn("`puts`", out, "a Tcl command was looked up")
        self.assertNotIn("bare_gizmo -anything (", options)

    def test_58b_what_is_not_a_tool_command_is_said_not_guessed(self):
        self.assertIn("No tool commands in this script", self.check("set a 1\nputs $a\nforeach x $a { puts $x }\n"))
        out = self.check("proc mine {} { report_gizmo -verbose }\nmine\n$cmd -x\nset_gizmo_mode -mode a\n")
        self.assertIn("2 commands checked, used 2 times", out)
        self.assertIn("procs the script defines (mine)", out)
        self.assertIn("1 command named by a variable or a substitution (line 3)", out)
        self.assertNotIn("`mine`", out.split("## Documented")[1].split("Not checked")[0])

    def test_58e_a_variable_is_not_a_command(self):
        """`gizmo_timeout` has an entry, in a reference of variables. Used as a
        command it is a mistake, and saying "documented" would hide it."""
        out = self.check("gizmo_timeout 5\n")
        self.assertIn("0 documented, 1 not", out)
        self.assertIn("It is an entry only in Gizmo Variables Reference (gizmo-vars), which documents variables, "
                      "messages or rules, not commands.", out)

    def test_58f_a_command_several_manuals_share_is_checked_against_each(self):
        script = "shared_gizmo -alpha -beta -gamma -bet\n"
        out = self.check(script)
        self.assertIn("1 command checked, used 1 time", out)
        # -beta is only in one manual, -gamma only in the other; -alpha is in both.
        partial = out.split("## Options only some manuals show")[1].split("##")[0]
        self.assertIn("`shared_gizmo` -beta (line 1): in gizmo-ref-v2, not in gizmo-lite", partial)
        self.assertIn("`shared_gizmo` -gamma (line 1): in gizmo-lite, not in gizmo-ref-v2", partial)
        self.assertNotIn("-alpha", partial)
        self.assertNotIn("## Unknown options", out)
        # `-beta_` in the entry's text is not an option of its own, so `-bet` is not ambiguous.
        self.assertIn("`shared_gizmo` -bet (line 1) is the start of `-beta`", out)

        # Scoped to one manual, what only the other shows is unknown.
        out = self.check(script, document="gizmo-lite")
        self.assertIn("## Unknown options", out)
        self.assertIn("`shared_gizmo` -beta (line 1): not an option its entry shows", out)
        self.assertNotIn("## Options only some manuals show", out)
        self.assertNotIn("`shared_gizmo` -gamma (", out)

    def test_58c_the_edition_is_the_one_asked_for(self):
        out = self.check("old_gizmo_cmd -legacy\nold_gizmo_cmd -nope\n", document="gizmo-ref", version="1.0")
        self.assertIn("1 command checked, used 2 times", out)
        self.assertIn("`old_gizmo_cmd` -nope (line 2)", out)
        self.assertNotIn("`old_gizmo_cmd` -legacy (", out)
        self.assertIn("(gizmo-ref-v1)", out)
        with self.assertRaises(ValueError):
            self.check("report_gizmo", version="1.0")                 # a version names a document
        with self.assertRaises(ValueError):
            self.check("report_gizmo", document="gizmo-ref", version="9.9")
        with self.assertRaises(ValueError):
            self.check("   ")
        with self.assertRaises(ValueError):
            self.check("x " * self.server.MAX_SCRIPT_CHARS)

    def test_58d_a_long_report_is_cut_with_its_count(self):
        script = "\n".join(f"made_up_command_{n:03d}" for n in range(self.server.CHECK_LISTED + 7))
        out = self.check(script)
        self.assertIn(f"## Unknown commands ({self.server.CHECK_LISTED + 7})", out)
        self.assertIn("…and 7 more", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
