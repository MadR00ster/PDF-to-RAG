"""The output-contract checker."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _support import SCRIPTS, WS, run, handmade_document, words, load_script, schema_errors  # noqa: E402


class CheckerTest(unittest.TestCase):
    """check_corpus.py: passes what the pipeline writes, catches what is planted."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.tmp = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def check(self, path: Path, *extra: str) -> tuple[subprocess.CompletedProcess, set[str]]:
        """Run check_corpus.py and return the names of the checks it raised."""
        out = self.tmp / "check.json"
        out.unlink(missing_ok=True)
        r = run("check_corpus.py", str(path), "--json", str(out), *extra)
        if not out.is_file():
            self.fail(f"check_corpus.py wrote no report:\n{r.stdout}{r.stderr}")
        report = json.loads(out.read_text(encoding="utf-8"))
        raised = {p["check"] for d in report["documents"] for p in d["problems"]}
        raised |= {p["check"] for ps in report["collections"].values() for p in ps}
        return r, raised

    def test_16_checker_accepts_what_the_pipeline_writes(self):
        """Everything the earlier tests converted, enriched and linked passes.

        Warnings are allowed here -- the tiny reference declines to attribute --
        failures are not.
        """
        r, raised = self.check(WS.corpus())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("no-pages", raised, "the prose path wrote no page numbers")

    def test_17_checker_accepts_a_handmade_document_strictly(self):
        """A document no script here wrote, following only the contract, passes
        with warnings counted as failures. This is the control for test_18:
        each planted defect below starts from exactly this."""
        corpus = self.tmp / "HandCorpus"
        handmade_document(corpus)
        r = run("build_index.py", str(corpus))
        self.assertEqual(r.returncode, 0, r.stderr)
        r, raised = self.check(corpus, "--strict")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(raised, f"a clean document raised {raised}")

    def test_18_checker_catches_planted_defects(self):
        """Each defect the checker claims to catch, planted one at a time.

        Most are ones that happened here: a breadcrumb naming the wrong parent,
        an owner from the wrong page, a crumb written twice by a pass that was
        not idempotent, a backup left in docs/, footers left in chunks.
        """
        import shutil
        clean = WS.hand_corpus()

        def relabel(d, m, i, crumb):
            s = m["sections"][i]
            s["breadcrumb"] = crumb
            f = d / s["file"]
            lines = f.read_text(encoding="utf-8").split("\n")
            lines[0] = f"*{crumb}*"
            f.write_text("\n".join(lines), encoding="utf-8")
            s["chars"] = len("\n".join(lines))

        def wrong_owner(d, m):
            m["sections"][1]["command"] = "Gamma Limits"
            relabel(d, m, 1, "Hand Guide › Gamma › Gamma Limits")

        def unnested(d, m):
            for s in m["sections"]:
                s.pop("page_start"), s.pop("page_end")
            relabel(d, m, 3, "Hand Guide › Alpha › Beta Tuning")

        def doubled(d, m):
            f = d / m["sections"][2]["file"]
            f.write_text(f"*{m['sections'][2]['breadcrumb']}*\n\n" + f.read_text(encoding="utf-8"),
                         encoding="utf-8")

        def shifted(d, m):
            for s in m["sections"]:
                s["page_start"] = s["page_end"] = min(6, s["page_start"] + 2)

        def dropped(d, m):
            s = m["sections"][2]
            (d / s["file"]).write_text(f"*{s['breadcrumb']}*\n\n## Beta\n\nSee the next page.\n",
                                       encoding="utf-8")

        def feedback(d, m):
            f = d / m["sections"][1]["file"]
            f.write_text(f.read_text(encoding="utf-8") + "\n**Feedback**\n", encoding="utf-8")

        def backup(d, m):
            shutil.copytree(d, d.with_name("hand.old"))

        def stale_index(d, m):
            index = d.parent / "index.json"
            data = json.loads(index.read_text(encoding="utf-8"))
            data["manuals"][0]["title"] = "Hand Guide, First Edition"
            index.write_text(json.dumps(data), encoding="utf-8")

        plants = [
            # name, damage, check expected, whether it must fail the run
            ("parent from another chapter", lambda d, m: relabel(d, m, 1, "Hand Guide › Beta › Beta Tuning"),
             "ancestor-contradicts-pages", True),
            ("owner from another page", wrong_owner, "entity-contradicts-pages", True),
            ("parent that does not contain the child", unnested, "ancestor-not-nested", True),
            ("breadcrumb written twice", doubled, "breadcrumb-doubled", True),
            ("section file gone", lambda d, m: (d / m["sections"][3]["file"]).unlink(),
             "section-missing-file", True),
            ("page past the end", lambda d, m: m["sections"][5].update(page_end=9), "page-invalid", True),
            # A warning, not a failure: no tool reads it any more (test_31).
            ("backup left in docs/", backup, "hidden-document", False),
            # Caught twice over: the PDF's words, and the TOC spans (which fail it).
            ("pages numbered two too high", shifted, "content-gap", True),
            ("pages numbered two too high", shifted, "ancestor-contradicts-pages", True),
            ("a page's text dropped", dropped, "content-gap", False),
            ("Feedback link left in", feedback, "furniture", False),
            # Present is not valid: each of these passed a key check once.
            ("slug set to null", lambda d, m: m.update(slug=None), "manifest-invalid-field", True),
            ("sections not a list", lambda d, m: m.update(sections={}), "manifest-invalid-field", True),
            ("section path leaving the document", lambda d, m: m["sections"][0].update(file="../../hand.pdf"),
             "section-bad-path", True),
            ("section path a number", lambda d, m: m["sections"][0].update(file=5), "section-bad-path", True),
            ("page count not the PDF's", lambda d, m: m.update(page_count=7), "page-count-mismatch", True),
            ("source PDF that will not open", lambda d, m: (d.parents[1] / "hand.pdf").write_bytes(b"not a pdf"),
             "content-unchecked", False),
            ("index.json title out of date", stale_index, "index-stale", False),
            ("attribution declined",
             lambda d, m: m.update(attribution={"status": "declined", "command_level": None, "chosen_by": "toc"}),
             "attribution-declined", False),
            ("a converted PDF still listed as set aside",
             lambda d, m: (d.parents[1] / "superseded.json").write_text(
                 json.dumps([{"file": "hand.pdf", "superseded_by": "hand"}]), encoding="utf-8"),
             "superseded-invalid", False),
        ]
        for i, (name, damage, expected, fails) in enumerate(plants):
            with self.subTest(defect=name, check=expected):
                corpus = self.tmp / f"Planted{i}"
                shutil.copytree(clean, corpus)
                d = corpus / "docs" / "hand"
                m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
                damage(d, m)
                (d / "manifest.json").write_text(json.dumps(m, indent=2), encoding="utf-8")
                r, raised = self.check(corpus)
                self.assertIn(expected, raised, f"{name} went unreported; raised {raised}\n{r.stdout}")
                self.assertEqual(r.returncode, 1 if fails else 0, r.stdout)

    def test_19_checker_order_prefers_the_enclosing_entry(self):
        """A repeated title must not send the order check ahead to a later
        copy when the one enclosing the reading position fits. Breadcrumbs
        name parents only, so a chunk's deepest ancestor is often shallower
        than the last chunk's; resolving it to the next "Simulating the Design"
        1,300 entries on flagged 195 correct chunks in one manual."""
        corpus = self.tmp / "RepeatCorpus"
        doc_dir = corpus / "docs" / "rep"
        (doc_dir / "sections").mkdir(parents=True)
        toc = [[1, "Simulating", 1], [2, "Setup", 1], [3, "Options", 1], [2, "Runtime", 2],
               [3, "Tuning", 2], [1, "Reference", 3], [2, "Simulating", 3]]
        # Chunks opening with: Simulating, Setup, Options, Runtime, Tuning.
        crumbs = ["Guide", "Guide › Simulating", "Guide › Simulating › Setup",
                  "Guide › Simulating", "Guide › Simulating › Runtime"]
        sections = []
        for n, crumb in enumerate(crumbs, start=1):
            text = f"*{crumb}*\n\n## Part {n}\n\nText of part {n}.\n"
            name = f"sections/{n:03d}.md"
            (doc_dir / name).write_text(text, encoding="utf-8")
            sections.append({"file": name, "heading": f"Part {n}", "level": 2,
                             "chars": len(text), "breadcrumb": crumb})
        (doc_dir / "full.md").write_text("x", encoding="utf-8")
        (doc_dir / "manifest.json").write_text(json.dumps({
            "source_pdf": "rep.pdf", "title": "Guide", "slug": "rep", "page_count": 3,
            "toc": [{"level": l, "title": t, "page": p} for l, t, p in toc],
            "sections": sections}), encoding="utf-8")
        r, raised = self.check(corpus, "--no-pdf")
        self.assertNotIn("ancestor-out-of-order", raised, r.stdout)

    def test_31b_every_copy_of_the_skip_rule_agrees(self):
        """Which folders under docs/ are not documents is decided in three
        places that must not import one another: editions.py for the
        scripts, check_corpus.py, and (from T22) the server."""
        editions, checker = load_script("editions"), load_script("check_corpus")
        server = load_script("mcp_server")
        for name in ("x.old", "x.new", ".x", "_x", "x", "a.b", "x.older", "x.new.bak", "docs"):
            self.assertEqual(checker.index_skips(name), editions.skips_name(name), name)
            self.assertEqual(server.skips_folder(name), editions.skips_name(name), name)

    def test_35c_the_checker_fails_what_stops_the_build_and_passes_what_does_not(self):
        """Two collections may each hold a document of one name: the index
        keys documents by collection too (test_35). Two collection folders
        whose names give one key still stop the build, so the checker fails
        those, by the same rule build_search_db.py uses."""
        root = self.tmp / "TwoHands"
        for coll in ("A", "B"):
            shutil.copytree(WS.hand_corpus(), root / coll)
        r, raised = self.check(root, "--no-pdf")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("collection-clash", raised)
        shutil.copytree(WS.hand_corpus(), root / "a-")      # gives the key "a" too
        r, raised = self.check(root, "--no-pdf")
        self.assertIn("collection-clash", raised, r.stdout)
        self.assertEqual(r.returncode, 1, r.stdout)
        db = load_script("build_search_db")
        checker = load_script("check_corpus")
        for name in ("Tessent Manual", "tessent-manual", "A", "a-", "Ünïcode", "___", "x.y_z"):
            self.assertEqual(checker.collection_key(name), db.slugify(name), name)

    def test_23_checker_catches_edition_defects(self):
        """Each way a set of editions can be undecidable, planted alone."""
        import shutil
        clean = self.tmp / "HandClean"
        handmade_document(clean)

        def plant(name: str, first: dict, second: dict, pins: dict | None = None) -> tuple[int, str]:
            corpus = self.tmp / f"Hand-{name}"
            shutil.copytree(clean, corpus)
            docs = corpus / "docs"
            shutil.copytree(docs / "hand", docs / "hand2")
            for slug, extra in (("hand", first), ("hand2", {"slug": "hand2", **second})):
                path = docs / slug / "manifest.json"
                m = json.loads(path.read_text(encoding="utf-8"))
                m.update(extra)
                path.write_text(json.dumps(m, indent=2), encoding="utf-8")
            if pins is not None:
                (corpus / "current_versions.json").write_text(json.dumps(pins), encoding="utf-8")
            r = run("check_corpus.py", str(corpus), "--no-pdf")
            built = run("build_search_db.py", "--root", str(corpus), "--stats-only")
            return r.returncode, r.stdout, built.returncode

        both = {"doc_id": "hand"}
        cases = [
            ("ungrouped", {}, {}, None, "edition-ungrouped", 0),
            ("unordered", {**both, "version": "1.0"}, both, None, "edition-unordered", 1),
            ("duplicate", {**both, "version": "1.0"}, {**both, "version": "1.0"}, None, "edition-duplicate-version", 1),
            ("badpin", {**both, "version": "1.0"}, {**both, "version": "2.0"}, {"hand": "3.0"}, "pin-invalid", 1),
            # A pin is a version as text: 2.0 as a JSON number stops the build.
            ("numberpin", {**both, "version": "1.0"}, {**both, "version": "2.0"}, {"hand": 2.0}, "pin-invalid", 1),
        ]
        for name, first, second, pins, expected, code in cases:
            with self.subTest(defect=name):
                rc, out, build_rc = plant(name, first, second, pins)
                self.assertIn(expected, out, f"{name} went unreported\n{out}")
                self.assertEqual(rc, code, out)
                self.assertEqual(build_rc, code, f"the checker and the index build disagree about {name}")
        # What the checker passes, the index build must accept -- including a
        # release written without its letter.
        fine = [("clean", {**both, "version": "1.0"}, {**both, "version": "2.0"}, {"hand": "1.0"}),
                ("samepin", {**both, "version": "Y-2026.03"}, {**both, "version": "Y-2026.06"}, {"hand": "2026.3"})]
        for name, first, second, pins in fine:
            with self.subTest(accepted=name):
                rc, out, build_rc = plant(name, first, second, pins)
                for check in ("edition-", "pin-invalid"):
                    self.assertNotIn(check, out, f"two well-formed editions raised {check}\n{out}")
                self.assertEqual(build_rc, 0, f"the index build rejects a pin the checker accepts ({name})")


    def test_28_build_index_refuses_a_malformed_superseded_entry(self):
        """A superseded.json entry without its fields crashed with a KeyError;
        it now stops before anything is written, and a name that is not here
        is a warning only."""
        corpus = WS.fresh(WS.hand_corpus(), "superseded")
        catalog = corpus / "docs" / "index.json"
        slug = json.loads(catalog.read_text(encoding="utf-8"))["manuals"][0]["slug"]
        sup = corpus / "superseded.json"

        for bad in ([{"file": "x.pdf"}], [{"file": "", "superseded_by": slug}], ["x.pdf"], {"file": "x.pdf"}):
            with self.subTest(bad=bad):
                sup.write_text(json.dumps(bad), encoding="utf-8")
                catalog.unlink(missing_ok=True)
                r = run("build_index.py", str(corpus))
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertNotIn("Traceback", r.stdout + r.stderr)
                self.assertIn("superseded.json", r.stdout + r.stderr)
                self.assertFalse(catalog.exists(), "an index was written from a malformed list")

        sup.write_text("[{", encoding="utf-8")
        r = run("build_index.py", str(corpus))
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIn("superseded.json", r.stdout + r.stderr)

        sup.write_text(json.dumps([{"file": "x.pdf", "superseded_by": "nope"}]), encoding="utf-8")
        r = run("build_index.py", str(corpus))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"!!.*nope")
        self.assertTrue(catalog.exists())


    def test_40_light_prose_conversion_passes_the_strict_check(self):
        """What convert_manual.py and enrich_chunks.py write passes every check,
        warnings included, now that the chunks carry pages."""
        coll = WS.collection("strictprose", "prose")
        for script, args in (("convert_manual.py", [str(coll / "widget-guide.pdf"), "--title", "Widget Guide",
                                                    "--slug", "prose"]),
                             ("enrich_chunks.py", [str(coll)]), ("build_index.py", [str(coll)])):
            r = run(script, *args)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r, raised = self.check(coll, "--strict", "--only", "prose")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(raised, f"a light prose conversion raised {raised}")


    def test_48_every_manifest_the_suite_makes_fits_the_schema(self):
        """scripts/manifest.schema.json describes what the converters write, and
        every one of them says what wrote it."""
        schema = json.loads((SCRIPTS / "manifest.schema.json").read_text(encoding="utf-8"))
        checker = load_script("check_corpus")
        self.assertEqual(tuple(schema["required"]), checker.REQUIRED_FIELDS)

        roots = [WS.corpus(), WS.fig_corpus(), WS.gadgets()[0], WS.hand_corpus()]
        seen = 0
        for root in roots:
            for path in sorted(root.rglob("manifest.json")):
                if ".rebuild-backup" in path.parts:
                    continue
                m = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(schema_errors(m, schema), [], path.parent.name)
                seen += 1
                if root is not roots[3]:
                    self.assertEqual(m["schema_version"], 1)
                    conv = m["converter"]
                    self.assertIn(conv["script"], ("convert_manual.py", "rebuild_reference.py"))
                    self.assertEqual(conv["extractor"], "pymupdf4llm")
                    self.assertRegex(conv["extractor_version"], r"^\d")
                    self.assertEqual(conv["owns_breadcrumbs"], "command" in m["sections"][0], path.parent.name)
        self.assertGreaterEqual(seen, 8)
        self.assertEqual(schema_errors({"slug": "a", "title": "t", "source_pdf": "x", "page_count": 0, "sections": []}, schema),
                         ["$.page_count: 0 is below 1"], "the validator accepts what the schema forbids")

        # enrich reads the manifest's own word for who wrote a breadcrumb, and
        # reads the sections where an older manifest has none
        corpus = WS.fresh(WS.corpus(), "owns")
        ref = corpus / "docs" / "ref" / "manifest.json"
        m = json.loads(ref.read_text(encoding="utf-8"))
        del m["converter"]
        ref.write_text(json.dumps(m, indent=2), encoding="utf-8")
        before = {p: p.read_bytes() for p in (corpus / "docs" / "ref").rglob("*.md")}
        r = run("enrich_chunks.py", str(corpus), "--only", "ref")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual({p: p.read_bytes() for p in before}, before, "enrich rewrote a reference's breadcrumbs")
        m["converter"] = {"script": "x", "extractor": "x", "extractor_version": "1", "converted_at": "now",
                          "owns_breadcrumbs": False}
        ref.write_text(json.dumps(m, indent=2), encoding="utf-8")
        run("enrich_chunks.py", str(corpus), "--only", "ref")
        self.assertNotEqual({p: p.read_bytes() for p in before}, before,
                            "a manifest saying it owns no breadcrumbs was not rewritten")


if __name__ == "__main__":
    unittest.main(verbosity=2)
