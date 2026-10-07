# TODO: implementing IMPROVEMENT.md

This is the execution plan for every item in `IMPROVEMENT.md`, written
2026-10-07 against `main` at `0e4dd59`. IMPROVEMENT.md is in this branch,
amended so that it and this file agree: where planning found it wrong or left
a question open, it now says **Amended** or **Decided**.

IMPROVEMENT.md says what is wrong and why. This file says what to change, in
what order, how to test it, and when each task is done. You should not need
IMPROVEMENT.md to carry out a task, but read the matching section when a task
says why something matters. References like **[1.4]** point to its sections.

Every claim marked "verified" below was checked while this plan was written:
by running the code, by reading a dependency's source, or with the test suite.

---

## 0. Read this first

### How to work through this file

- Do the tasks in order (see §2). Each task is one commit, or a few small
  ones, and the full suite passes before every commit.
- Before editing, find the code **by the quoted snippet, not the line
  number**. Line numbers are as of `0e4dd59` and move as tasks land.
- Each task ends with a **Done when** list. Tick the boxes (`[x]`) in this
  file in the same commit as the work, and set the task's **Status** line.
- Some steps need the user's real corpus, Docling, Tesseract, or a Windows
  machine. If you cannot do one, don't skip it silently and don't invent a
  result. Fill in its row in §4 "Measurements owed" (add one if it has
  none), and carry on with the rest of the task.
- If you find a problem a task doesn't cover, add it to §5 "Found along the
  way" instead of fixing it.
- CI runs on every push (T02 is done). After each push, read the run with
  the GitHub tools. A red cell is yours to fix before the next task.

### Decisions already made: nothing here waits on the user

Every gate this plan had is settled. Don't stop to ask about any of them.

| item | decision | where |
|---|---|---|
| CI push permission | Tested: a workflow file pushes fine. T02 is done | T02 |
| Merge the two entry tests (1.5) | Don't. Report the disagreement instead | T06 |
| Joined pages vs whole-document markdown (2.1) | Identical by construction in pymupdf4llm 1.28.2; pinned by a fixture test | T14 |
| One pymupdf4llm converter | Not gated; do it after T14 | T15 |
| Defaults for the search experiments | Ship each as a flag, off. Choosing a default is not part of this work | T18 |
| `collection.json` (3.6) | Not now. Skip T23 | T23 |

What §4 lists are measurements on the user's corpus. Record them where you
can. None blocks a task.

### Ground rules

1. **The repo's rule: metadata is right or absent, never guessed.** A change
   that could put a wrong label, a wrong page or a deleted line into a corpus
   is wrong, even if every test passes. Prefer declining to guessing.
2. **Keep what works** (IMPROVEMENT.md, "What to keep"): every decision has a
   number behind it; `check_corpus.py` shares no code with the converters;
   editions refuse rather than approximate; the server is one
   standard-library file.
3. **Independence.** `check_corpus.py` and `mcp_server.py` import nothing
   from `scripts/`. `mcp_server.py` is copied beside indexes and must run on
   its own. When a rule has to exist in one of them and somewhere else too,
   write it twice and add a test that holds the copies together. The model is
   test_20b, which checks `check_corpus.version_numbers` and
   `mcp_server.version_sort` against `editions.version_key` on a list of
   versions.
4. **Change only what the task changes.** Outside that, a converter run on
   the same PDF must produce byte-identical output. When a task touches code
   every converter uses, prove it: convert the fixture PDFs before and after,
   and `diff -r` the two `docs/` trees.
5. **JSON:** write with `json.dumps(obj, indent=2)` (keep the default
   `ensure_ascii=True`), and read with `encoding="utf-8-sig"`. SKILL.md's
   "Platform notes" say why.
6. **Style.** Match the code around you. Docstrings and comments in this repo
   explain *why*, often citing a measurement. Don't write comments that
   narrate the code, and never put an unmeasured number in one.
7. **Tests:** `unittest` only, no pytest, no new dependencies. Fixture PDFs
   are generated with PyMuPDF (see `tests/_support.py` after T01). A bug fix
   comes with a test that fails without the fix. Check that it does fail.
8. **Docs move with code.** When behaviour changes, update SKILL.md,
   README.md, `references/*.md`, module docstrings and `--help` text in the
   same commit. `grep -rn` for the old wording.
9. **Never fabricate a measurement.** "Not measured: no corpus here" is an
   acceptable outcome. A made-up number is not.
10. **Commits.** Write an imperative subject that says what changed for the
    user, as the history does ("Stop on an unreadable manifest, never
    overwrite a filed PDF"). Put no model names in commits, code or docs.

### Environment and baseline

```bash
pip install -r scripts/requirements.txt          # pymupdf4llm 1.28.2, PyMuPDF 1.28.2
python -m unittest discover -s tests -v          # the suite
python tests/run_each.py                         # every test alone, each in its own process
```

Baseline at `0e4dd59` (Python 3.13.16, Linux): **36 tests, OK, 2 skipped**
(no Tesseract language data, no Docling), about 70 s. With T01, T02, T07
and T09 done, the branch is at **44 tests, OK, 2 skipped**, about 85 s.

Optional tools: Tesseract (test_14), Docling (test_09c and the check in T05).
Neither is needed for the suite to pass.

---

## 1. Corrections to IMPROVEMENT.md

Found while writing this plan, and now made in IMPROVEMENT.md itself. They
are listed here so each task's reasoning is in one place.

1. **[4.1] The minimum Python is 3.10, not 3.9.** pymupdf4llm 1.28.2 and
   PyMuPDF 1.28.2 both declare `Requires-Python >=3.10`, and
   `requirements.txt` asks for pymupdf4llm 1.28 or later. Verified with
   `importlib.metadata`.
2. **[1.5] Wrong line numbers.** `pick_extractor.py` has 223 lines. The entry
   test is `looks_like_entry` at lines 56–65, and the level choice is in
   `detect_shape` at lines 74–101, not 264–273.
3. **[1.1] Matching the whole heading would miss split headings.** The chunker
   names the later pieces of a split block `"<heading> (cont.)"` and an intro
   piece `"<heading> (intro)"`. Matching the whole heading would stop demoting
   `Index (cont.)`, which the prefix rule catches today. T10 allows those
   suffixes.
4. **[1.2] Plain-word entity names still need the whole-query rule.** A Tcl
   command called `set` or `foreach` is not identifier-shaped. If only
   identifier-shaped tokens are boosted, the bare query `set` loses its −6.0.
   T11 keeps the "the whole query is the entity" case and drops only the
   prefix branch.
5. **[1.5] Unifying the two entry tests is not safe to do blind.** The
   reference converter's rule keeps a looser `" -"` clause on purpose: its
   docstring says tightening it "would move which level is picked for manuals
   that already convert correctly". pick_extractor's rule was measured on 38
   manuals. Changing either one moves measured results. **Decided:** keep
   both. T06 makes the decline loud, and has pick_extractor report the level
   the converter would pick.
6. **[1.6] Skipping hidden folders changes two checker rules.** Once
   `build_search_db.py` skips `.old`/`.new`/dot/underscore folders, the
   checker's `hidden-document` no longer describes something that breaks the
   build. Its `duplicate-slug` rule must also stop counting those folders.
   test_18 plants both defects and has to change with them (T07).
7. **[2.1] test_16 asserts the prose path has no pages**
   (`assertIn("no-pages", raised)`). T14 reverses that assertion.
8. **[2.1] Joined pages equal the whole-document markdown, by construction.**
   In pymupdf4llm 1.28.2, `helpers/document_layout.py`'s `to_markdown`
   builds each page's string the same way in both modes. The
   whole-document call concatenates them (`document_output += md_string`),
   and `page_chunks` is never passed to the parser. IMPROVEMENT.md asked for
   this to be checked on real manuals before relying on it; the source
   settles it, and T14 pins it with a test.
9. **[4.5] `get_section`'s unescaped `LIKE` is not harmless.** Verified: with
   no exact match, a request for `sections/0001-set_x.md` returns
   `…/sections/0001-setax.md`.
10. **[2.2] Chunk bodies are not always exact slices of the text today.**
   Verified: when a heading split's children include pieces from paragraph
   packing, `pack_adjacent` joins them with `""`. That drops the blank line
   between two paragraphs, so `"…word Short tail of B."` appears in a chunk
   but not in `full.md`. Exact offsets need the chunker to work in spans
   (T13). That also restores the blank line in those rare chunks.
11. **[2.1] Telling reference output apart without provenance.**
   `rebuild_reference.py` writes a `"command"` key on *every* section, with
   `null` where nothing is attributed. Before T20 records provenance, the
   presence of that key, not its value, is what identifies a rebuilt
   reference.

---

## 2. Order and dependencies

The order follows IMPROVEMENT.md's "Suggested order" with one change: 2.2
(exact offsets) moves ahead of 2.1, because 2.1's page numbers depend on it.

| # | task | IMPROVEMENT | depends on | size |
|---|---|---|---|---|
| **Phase 1: foundations** |||||
| T01 | Split the tests; make each runnable alone (**done**) | 4.2 | — | L |
| T02 | CI on Linux and Windows (**done**) | 4.1 | — | S |
| T03 | Read pymupdf4llm's page number from the right key (**done**) | 1.4 | T01 | S |
| T04 | Docs and code that disagree; dead code (**done**) | 1.7 | T01 | S |
| **Phase 2: the corpus** |||||
| T05 | Docling chunks: use their own heading (**done**) | 1.3 | T01 | S |
| T06 | Make a declined attribution loud; `--command-level` (**done**) | 1.5 | T01 | M |
| T07 | Stage conversions outside `docs/`; skip hidden folders everywhere (**done**) | 1.6 | T01 | M |
| T08 | Stop deleting short lines that begin the title (**done**) | 1.8 | T04 | S |
| T09 | Key documents by (collection, slug) (**done**) | 3.5 | T07 | L |
| **Phase 3: search** |||||
| T10 | Demote front matter by whole heading (**done**) | 1.1 | T01 | S |
| T11 | Entity boost for identifiers inside a question (**done**) | 1.2 | T01 | M |
| T12 | `eval_search.py --compare` (**done**) | 2.6 | T01 | S |
| **Phase 4: coverage** |||||
| T13 | Chunk in spans, so offsets are exact (**done**) | 2.2 | T01 | M |
| T14 | Page numbers on the light prose path (**done**) | 2.1 | T03, T13 | L |
| T15 | One pymupdf4llm converter (**done**) | 2.1 "next step" | T14 | L |
| **Phase 5: later** |||||
| T16 | Reference breadcrumbs from the TOC chain (**done**) | 2.3 | T06 | M |
| T17 | Section identity across reconversions (**done**) | 2.4 | T12 | M |
| T18 | Retrieval experiments behind flags (**done**) | 2.5 | T11, T12 | M |
| T19 | Say which copy of a field is canonical; stop writing `chars` | 3.1 | T07 | M |
| T20 | Manifest schema and provenance | 3.2 | T06 | M |
| T21 | Export chunks as JSONL | 3.3 | T07 | S |
| T22 | Warn when the index is stale | 3.4 | T07 | M |
| T23 | One `collection.json` (**not now: skip**) | 3.6 | — | — |
| T24 | Remove accidental duplication | 4.3 | T06, T07 | M |
| T25 | Consistent command lines | 4.4 | T07 | M |
| T26 | Server robustness and tool metadata | 4.5 | T01 | S |

T26 is independent and small, so it can be done any time after T01. The
`--slug` part of T25 can come earlier too. T02 is already done, and T23 is
not part of this work.

---

## 3. Tasks

Most tasks have the same parts: **Why**, **Files**, **Steps**, **Tests** and
**Done when**. Doc updates are listed under Steps. New tests are numbered from
`test_27` upward, in task order; the names are suggestions.

---

### T01: Split the tests; make each runnable alone [4.2]

**Status:** done. The steps below are the record of what was built. In the
end, `tests/_support.py` holds the fixtures and `WS`, and each test file
imports what it uses by name. test_02 now also asserts that its first enrich
run changed something. The full suite takes about 77 s, against 64 s before.

**Why.** Verified: `python -m unittest tests.test_pipeline.PipelineTest.test_10_mcp_server_passes_its_smoke_test`
fails alone, because test_08 builds the index it serves, and test_03 errors
alone. Later tasks add about 30 tests, and they need a suite whose tests pass
in any order.

**Files.** `tests/test_pipeline.py` (deleted), plus these new files:
`tests/_support.py`, `tests/test_converters.py`, `tests/test_server.py`,
`tests/test_figures.py`, `tests/test_checker.py`, `tests/test_editions.py`
and `tests/run_each.py`. Also update `README.md` ("Tests"), `SKILL.md`, and
the `scripts/check_corpus.py` docstring. Run
`grep -rn "test_pipeline" .` to find every mention.

**Steps.**

1. Create `tests/_support.py` with everything in `test_pipeline.py` above
   `class PipelineTest`:
   - the imports and the PyMuPDF import guard
   - `ROOT`, `SCRIPTS`, `PY` and the console `reconfigure` loop
   - `run()`, `write_pdf()` and every `*_fixture` function
   - `handmade_document()`, `words()`, `LONG_ENTRY`, `LONG_LINES` and
     `load_script()`

   Move the module docstring's list of what the suite covers here as well.
2. Add a process-wide, lazily built workspace to `_support.py`:

   ```python
   class Workspace:
       """Fixture PDFs and what the pipeline makes of them, built the first
       time a test asks and shared by every test in the process after that.

       A test that checks a step runs that step itself, in a folder of its
       own. A test that only needs a step's output takes it from here. A test
       that changes what it gets copies it first, with fresh().
       """
   ```

   - Hold the folder as
     `tempfile.TemporaryDirectory(ignore_cleanup_errors=True)`. That argument
     needs Python 3.10, and it stops an open SQLite handle on Windows from
     failing the cleanup. Register the cleanup with `atexit`. Expose a
     module-level instance `WS = Workspace()`.
   - Each builder runs once, caches its result, and raises `AssertionError`
     with stdout and stderr in the message if a step exits non-zero:

   | method | builds (once) | today built by |
   |---|---|---|
   | `pdf(name)` | `prose`, `ref` (25 commands), `tiny` (8), `nested` (22) fixture PDFs, under today's file names (`widget-guide.pdf`, `widget-commands.pdf`, `tiny-commands.pdf`, `nested-commands.pdf`). test_07 splits its output on those names | setUpClass |
   | `corpus()` | `<tmp>/Corpus`. The four PDFs are copied into it. prose (`--slug prose`, "Widget Guide"), ref (`ref`, "Widget Commands"), tiny (`tiny`, "Tiny") and nested (`nested`, "Widget Commands") are converted, then `enrich_chunks.py` and `build_index.py` are run | tests 01–11 |
   | `index()` | `build_search_db.py --root <corpus()> --out <tmp>/index.sqlite3` | test_08 |
   | `fig_corpus()` | `<tmp>/FigCorpus`: figure fixture, `convert_manual.py --slug figs`, then `extract_figures.py` | test_13 |
   | `hand_corpus()` | `<tmp>/HandCorpus`: `handmade_document()`, then `build_index.py` | test_17 |
   | `gadgets()` | `(root, coll)` = `<tmp>/Root/Gadgets`: gadget 2025.1 and 2026.1 converted from `new_docs/`, then `enrich_chunks.py` and `build_index.py`. This is test_20's setup without its assertions | test_20 |
   | `fresh(src, name)` | `shutil.copytree(src, <tmp>/<name>-<n>)` with a unique `n`. Returns the copy | — |

3. Move the tests into files by area. Keep every test's name, docstring and
   assertions. Change only where each test gets its inputs:

   | file | class | tests |
   |---|---|---|
   | `test_converters.py` | `ConverterTest` | 01, 02, 03, 04, 04b, 05, 06, 06b, 07, 09, 09b, 09c, 11, 12 |
   | `test_server.py` | `ServerTest` | 08, 10 |
   | `test_figures.py` | `FigureTest` | 13, 14, 15 |
   | `test_checker.py` | `CheckerTest` | 16, 17, 18, 19, 23 |
   | `test_editions.py` | `EditionsTest` | 20, 20b, 21, 22, 22b, 22c, 24, 25, 25b, 25c, 25d, 25e, 26 |

   Input changes, and only these:
   - **test_01, 05, 06, 11** test a conversion. Each one converts into its own
     new collection, with the PDF copied in from `WS.pdf(...)`.
   - **test_02** converts the prose PDF into a new collection of its own, so
     nothing has enriched it yet. It asserts that the first enrich run changed
     at least one chunk (parse `(\d+) changed` from stdout and check it is
     above 0), then keeps its existing "second run: 0 changed" assertion.
     Today a first run that changes nothing would pass.
   - **test_03** reads `WS.corpus()`. **test_04** works on
     `WS.fresh(WS.corpus(), "enrich-parent")`.
   - **test_07 and test_09** use `WS.pdf(...)`. **test_12** is unchanged
     apart from `WS.pdf("ref")`.
   - **test_08** tests the build, so it builds its own index from
     `WS.fresh(WS.corpus(), ...)`. **test_10** serves `WS.index()`.
   - **test_13** tests extraction, so it keeps building its own FigCorpus.
     **test_14 and test_15** each use `WS.fresh(WS.fig_corpus(), ...)`.
     test_15 already removes `ocr` first, so it no longer needs test_14 to
     have run.
   - **test_16** reads `WS.corpus()`. Its docstring says test_11 added a
     document after test_08 built `index.json`. That is no longer true, since
     the shared corpus's `index.json` is built last, so fix the docstring.
     **test_17** keeps building its own
     HandCorpus. **test_18** uses `WS.hand_corpus()` as `clean`; it already
     copies the corpus for every plant.
   - **test_21, 22 and 22b** each use `WS.fresh(WS.gadgets()[0], ...)` as
     their root. test_22 adds its widget-ref editions to that copy.
   - **Every other editions test**, test_22c included, already builds its
     own folders under `self.tmp`. Keep a per-class `TemporaryDirectory` for
     those.
4. Each test file starts by putting its own folder on `sys.path`, so all
   three ways of running it work: `discover`, `python tests/test_x.py`, and
   `python -m unittest test_x...` run from `tests/`.

   ```python
   import sys
   from pathlib import Path
   sys.path.insert(0, str(Path(__file__).resolve().parent))
   from _support import WS, run, load_script, ...   # explicit names, no *
   ```

   End each file with `if __name__ == "__main__": unittest.main(verbosity=2)`.
5. Create `tests/run_each.py`. It runs every test id in a process of its own
   and exits 1, listing the failures, if any test fails alone:

   ```python
   """Run every test alone, in a process of its own. A test that passes only
   after another one has run is a test of the order, not of the code."""
   import subprocess, sys, unittest
   from pathlib import Path

   HERE = Path(__file__).resolve().parent

   def ids(suite):
       for t in suite:
           yield from ids(t) if isinstance(t, unittest.TestSuite) else [t.id()]

   def main() -> int:
       failed = [i for i in ids(unittest.defaultTestLoader.discover(str(HERE)))
                 if subprocess.run([sys.executable, "-m", "unittest", i], cwd=HERE,
                                   capture_output=True).returncode]
       print("\n".join(failed) or "every test passes alone")
       return 1 if failed else 0

   if __name__ == "__main__":
       sys.exit(main())
   ```
6. Delete `tests/test_pipeline.py`. Wherever the docs say
   `python tests/test_pipeline.py`, change it to
   `python -m unittest discover -s tests -v`.

**Done when**
- [x] `python -m unittest discover -s tests -v` runs 37 tests, OK, with skips
      only for Tesseract and Docling.
- [x] `python tests/run_each.py` reports every test passing alone
      ("37 of 37 tests pass alone").
- [x] `discover` takes no more than about twice the 70 s baseline.
- [x] `grep -rn test_pipeline --exclude-dir=.git .` finds nothing outside
      TODO.md and IMPROVEMENT.md, which describe the history.

---

### T02: CI on Linux and Windows [4.1]

**Status:** done in commits `d7305cf` and `b5c3172`. The push that added the
workflow was accepted, so the token can push workflow files. This section is
kept as the record of what was done.

The first run passed on Linux with 3.10 and 3.13, which confirms the 3.10
floor. Both Windows cells failed one test, test_22b, which found a real bug:

- `build_search_db.emit_vscode_config` compared an existing entry's `--db`
  path with the index's as text, after `abspath`, while the build resolves
  its root.
- On the runner, the temp folder came back in its 8.3 short form
  (`C:\Users\RUNNER~1\...`), so the build didn't recognise this corpus's
  own entry and added a second server for the same index.
- A user who registered the corpus through a symlink, a junction or a mapped
  drive would hit the same thing.

`b5c3172` compares resolved paths (`os.path.realpath`) and adds test_22c,
which reproduces the bug through a symlink, so every CI cell covers it. The
CI command, `python -m unittest discover -s tests -v`, runs
`tests/test_pipeline.py` today and must keep working through T01's split.

**Why.** Verified: there is no `.github/` folder. Half of SKILL.md's platform
notes came from Windows, and nothing states the minimum Python version.

**Files.** `.github/workflows/tests.yml` (new), `README.md`, `SKILL.md`
("Bundled scripts", the install line).

**Steps.**

1. Add this workflow:

   ```yaml
   name: tests
   on: [push, pull_request]
   jobs:
     test:
       strategy:
         fail-fast: false
         matrix:
           os: [ubuntu-latest, windows-latest]
           python: ["3.10", "3.13"]
       runs-on: ${{ matrix.os }}
       timeout-minutes: 20
       steps:
         - uses: actions/checkout@v4
         - uses: actions/setup-python@v5
           with:
             python-version: ${{ matrix.python }}
         - run: python -m pip install -r scripts/requirements.txt
         - run: python -m unittest discover -s tests -v
   ```

   Don't set `PYTHONUTF8`. The console encoding on Windows is one of the
   things the suite should exercise.
2. In README.md, next to the install line, add "Python 3.10 or later
   (pymupdf4llm 1.28 requires it)". Add the same to SKILL.md's "Bundled
   scripts" install line.
3. Push, then read the workflow run with the GitHub tools. Fix any Windows
   failure that is a real bug in scope. If one can't be fixed in this task,
   record it in §5.
4. **Push permission:** a push that adds a workflow file can be refused when
   the token lacks the `workflow` scope. If that happens, say so to the user.
   Don't remove the file to get the push through.

**Done when**
- [x] The workflow has run on all four matrix cells, or a refused push is
      reported to the user.
- [x] README and SKILL.md state Python 3.10+.

---

### T03: Read pymupdf4llm's page number from the right key [1.4]

**Status:** done

**Why.** Verified: pymupdf4llm 1.28.2 puts the page number in
`metadata["page_number"]`. `rebuild_reference.py` reads `metadata["page"]`,
so every page number comes from the fallback `i + 1`. That is right only
while every page comes back, in order. A page that is dropped misnumbers
every page after it, and nothing reports it.

**Files.** `scripts/convert_manual.py` (new helper),
`scripts/rebuild_reference.py` (`build_pages`), `tests/test_converters.py`.

**Steps.**

1. Add this helper to `convert_manual.py` (T14 uses it there as well), next
   to `chunk_markdown`:

   ```python
   def page_markdown(pdf_path: Path) -> tuple[list[str], list[int]]:
       """Each page's markdown and its 1-based page number.

       pymupdf4llm 1.28 names the number `page_number`; earlier releases
       called it `page`. Where neither is there, a page's place in the list
       is its number -- true only while every page comes back in order, so
       that is checked: one page dropped would misnumber every page after it,
       and a citation to the wrong page is worse than none.
       """
       raw = pymupdf4llm.to_markdown(str(pdf_path), page_chunks=True)
       with pymupdf.open(str(pdf_path)) as doc:
           expected = doc.page_count
       texts, numbers = [], []
       for i, page in enumerate(raw):
           meta = page.get("metadata") or {}
           n = meta.get("page_number", meta.get("page"))
           texts.append(page.get("text") or "")
           numbers.append(i + 1 if n is None else n)
       if numbers != list(range(1, expected + 1)):
           sys.exit(f"pymupdf4llm returned pages {numbers[:5]}... ({len(numbers)} of them) for the "
                    f"{expected}-page {pdf_path.name}. Page numbers taken from that would be wrong, "
                    "so nothing was written.")
       return texts, numbers
   ```

   Use explicit `None` checks, not `or`. A page numbered `0` must fail the
   check, not turn into `1`.
2. In `rebuild_reference.build_pages`, replace the `to_markdown` call and the
   `numbers = [...]` comprehension with
   `page_texts, numbers = cm.page_markdown(pdf_path)`.

**Tests** (`test_converters.py`):
`test_27_page_numbers_come_from_pymupdf4llms_own_key`. Load
`rebuild_reference` with `load_script`; its `cm` attribute is the
`convert_manual` module. Write a 3-page PDF with `write_pdf`. Then use
`unittest.mock.patch.object(cm.pymupdf4llm, "to_markdown", fake)` to check:
- `fake` returns 3 pages with `page_number` 1, 2 and 3: the numbers are
  `[1, 2, 3]`.
- `fake` returns pages 1 and 3 only: `SystemExit`.
- `fake` returns 3 pages with no metadata: the numbers are `[1, 2, 3]`.
- `fake` returns `page_number` 0, 1 and 2: `SystemExit`.

**Done when**
- [x] The new test fails on `0e4dd59`'s `build_pages` and passes now. To see
      the old code fail, apply the test alone first.
- [x] Converting `ref` with and without the change gives identical `docs/`
      trees (`diff -r`).

---

### T04: Docs and code that disagree; dead code [1.7]

**Status:** done

**Files.** `SKILL.md`, `scripts/enrich_chunks.py`,
`scripts/rebuild_reference.py`, `scripts/convert_manual.py`,
`scripts/build_index.py` and the test files.

**Steps.**

1. **SKILL.md, "When the input does not look like this":** the bullet that
   begins "`enrich_chunks.py` rewrites section files" says the furniture pass
   "deletes any line repeated across 5% of sections (at least 10)". It does
   not. Rewrite the sentence to say what the code does. It deletes:
   - `Feedback` lines
   - lines matching the document's own title that recur in at least 3
     sections
   - bare page numbers and `Chapter N:` headers within 3 lines of either of
     those

   The risk to name on text another tool produced is a short line that starts
   the title (T08 narrows it). Keep the advice to run `--dry-run` first and
   back up.
2. **`enrich_chunks.detect_furniture`:** the `min_count` parameter is never
   read. Remove it: `detect_furniture(section_texts, title)`. Update both
   callers, `enrich_chunks.process_manual` (also delete its `min_count = ...`
   line) and `rebuild_reference.build_pages`. The comment inside
   `detect_furniture` that begins "Precision over recall" quotes the earlier
   rule as "any line repeated >= min_count times". Reword that to "any line
   repeated often enough", so the name no longer appears.
3. **Dead code in `enrich_chunks.process_manual`:** delete `chars_before`,
   `chars_after` and `samples`, and the `if len(samples) < 3 and dropped:`
   block.
4. **`convert_manual.py` docstring:** change "(see each vendor folder's
   CLAUDE.md for the prose spec this implements)" to "(SKILL.md's "Chunking"
   section is the spec this implements)".
5. **`build_index.py`:**
   - `write_readme`: change "Converted from the {vendor_label} EDA tool PDFs"
     to "Converted from the {vendor_label} PDFs".
   - `load_vendor_label`: return `vendor_dir.name` unchanged (drop
     `.replace(" Manual", "")`), and fix its docstring. `vendor.json` still
     overrides it.
6. **`build_index.py`, `superseded.json`:** `load_superseded` crashes with
   `KeyError` on an entry missing `file` or `superseded_by`. Validate it the
   way `check_corpus.check_collection` does:
   - The file is not a list, or an entry is not a dict with non-empty string
     `file` and `superseded_by`: print one `!!` line per bad entry and
     `sys.exit(1)` before anything is written. Before this it crashed, so
     stopping changes nothing that worked.
   - The entry is well formed but `superseded_by` names a slug that is not
     here: print an `!!` warning and continue.
   - Unreadable JSON: exit 1 with the file named.

**Tests.**
- `test_28_build_index_refuses_a_malformed_superseded_entry`: use
  `WS.fresh(WS.hand_corpus(), ...)` and write
  `superseded.json = [{"file": "x.pdf"}]`. `build_index.py` exits 1, with no
  `Traceback` in its output and the entry named. With
  `[{"file": "x.pdf", "superseded_by": "nope"}]` it exits 0 and prints an
  `!!` line naming `nope`.
- Update any test that calls `detect_furniture` with three arguments.

**Done when**
- [x] `grep -n "5% of" SKILL.md` finds nothing (the furniture sentence is gone; the
      grep still matches two unrelated lines, "95% of" and "82.5% of").
- [x] `grep -n "min_count\|chars_before\|samples" scripts/enrich_chunks.py`
      finds nothing.
- [x] Converting the fixtures before and after gives identical `docs/` trees.
      `index.json` and `README.md` differ only in the label wording.

---

### T05: Docling chunks use their own heading, not the outermost [1.3]

**Status:** done

**Why.** Verified in the source of docling-core 2.100.0
(`transforms/chunker/hierarchical_chunker.py`, lines 221–248 and 269–270).
Headings are kept in `heading_by_level` and emitted as
`[heading_by_level[k].text for k in sorted(heading_by_level)]`, outermost
first. A `TitleItem` takes level 0, and a new heading removes only levels at
or below its own. So once Docling labels the cover as the title,
`headings[0]` is the document title for every chunk after it.
`convert_docling.py` reads `headings[0]` in two places. The effects:
- No chunk is anchored by its own heading.
- Every section file is named after the title.
- `merge_sections` never writes its `### sub-heading` lines.

**Files.** `scripts/convert_docling.py`, `tests/test_converters.py`.

**Steps.**

1. In `resolve_ancestors`, change
   `norm = normalize((c["headings"] or [""])[0])` to use `[-1]`.
2. In `merge_sections`, change `head = (r["headings"] or [""])[0]` to use
   `[-1]`.
3. Add a sentence to `resolve_ancestors`'s docstring: Docling lists headings
   outermost first, so the chunk's own heading is the last one.

**Tests.**
- Extend test_09b's `resolve()` so it can pass a full heading list.
  `resolve(["Widget Guide", "Syntax"], [4])` must give
  `(["Commands", "beta"], "anchored")`, the same as `["Syntax"]` alone.
  `["Widget Guide"]` alone, on page 2, must give `"page"`.
- `test_29_docling_merge_names_its_sub_headings`: call `merge_sections`
  directly. Pass three chunks with the same ancestors and headings
  `["Widget Guide", "Setup"]`, `["Widget Guide", "Setup"]` and
  `["Widget Guide", "Use"]`. Expect one merged chunk with heading `"Setup"`
  whose text contains `"\n\n### Use\n\n"`. This runs without Docling.

**Confirm on a real document (optional, needs Docling).** IMPROVEMENT.md
asks for this before trusting the fix, and Docling is not installed here.
Where it can be installed (`pip install docling`, several GB), convert the
gadget fixture, or better a real manual, and print each chunk's
`meta.headings`:

```python
from docling.document_converter import DocumentConverter
from docling.chunking import HybridChunker
doc = DocumentConverter().convert("guide.pdf").document
for ch in list(HybridChunker().chunk(doc))[:15]:
    print(ch.meta.headings)
```

If the title comes first in the later chunks' lists, the problem is
confirmed. Record the result in §4 either way. Re-running the benchmark
(`references/extractor-benchmark.md` reports 16–87% anchored) is also owed.
Read that file's method section. If those numbers came from
`convert_docling.py`, add a dated note under its table saying they were
measured before this fix and have not been re-measured.

**Done when**
- [x] Both `[0]` reads are `[-1]`. `grep -n 'headings"\] or \[""\])\[0\]' scripts/convert_docling.py`
      finds nothing.
- [x] The new assertions fail on the old code and pass now.
- [x] The Docling confirmation and the benchmark rerun are done, or their
      §4 rows say why not.

---

### T06: Make a declined attribution loud; `--command-level` [1.5]

**Status:** done, as specified below.

**Why.** `pick_extractor.py` counts a lowercase single word (`after`,
`foreach`) as an entry. `rebuild_reference.py` needs an `_`, a `" -"`, or a
message code. So pick_extractor can send a Tcl-style reference to
`rebuild_reference.py`, which prints `command level LNone: 0 commands` and
exits 0. Declining is correct, and test_06 expects it, but it is easy to miss.
The benchmark once reported 0% for a manual that reaches 90%.

**Files.**
- `scripts/_common.py` (new; T24 adds more to it)
- `scripts/rebuild_reference.py`
- `scripts/pick_extractor.py`
- `scripts/check_corpus.py`
- `SKILL.md`
- `tests/_support.py` (new fixture), `tests/test_converters.py`,
  `tests/test_checker.py`

**Part A (do now).**

1. Create `scripts/_common.py`. It imports only the standard library, never
   pymupdf: pick_extractor must stay fast, and importing pymupdf4llm starts
   its layout model. Move these from `rebuild_reference.py` unchanged:
   - `IDENTIFIER_RE` and `MESSAGE_CODE_RE`
   - `pick_command_level`, with its docstring and its comment about the
     `" -"` clause

   Name the 20 as a constant, `MIN_COMMANDS = 20`. In `rebuild_reference.py`,
   add `sys.path.insert(0, str(Path(__file__).resolve().parent))` if missing,
   then
   `from _common import IDENTIFIER_RE, MESSAGE_CODE_RE, pick_command_level  # noqa: E402`.
   test_06b reads `rebuild_reference.pick_command_level`, and the import keeps
   that name available.
2. **`rebuild_reference.py`:**
   - Add `--command-level N` (int). When given, check that some TOC entry has
     that level, else `sys.exit("no TOC entry is at level N; levels here: ...")`,
     and use it instead of the picked level.
   - When no level is picked and none is given, keep declining and keep exit
     code 0. Replace the `command level LNone` line with:
     `!! no TOC level has 20 titles shaped like commands (an underscore, " -", or a message code), so no chunk is attributed to one. If this is a reference, pass --command-level N with the TOC level its entries are at.`
   - Record what happened in the manifest. Use one object with fixed types,
     so T20's schema can describe it. Place it after `"toc"`:

     ```json
     "attribution": {"status": "attributed", "command_level": 3, "chosen_by": "toc"}
     "attribution": {"status": "attributed", "command_level": 1, "chosen_by": "option"}
     "attribution": {"status": "declined",   "command_level": null, "chosen_by": "toc"}
     ```

     Pass the dict through `editions.with_edition_fields` like the other
     fields. Check it is not in the list of keys that function rewrites.
3. **`pick_extractor.py`:**
   - `from _common import pick_command_level` (with the `sys.path` insert).
   - Make `detect_shape` return `(shape, why, level)`, where `level` is the
     TOC level it judged to hold the entries, or `None`. Update its caller.
   - In `report()`, when the shape is `reference` or `mixed`, compute
     `picked = pick_command_level(toc)`:
     - `picked is None`: print
       `!! rebuild_reference.py would attribute nothing here: it takes no TOC level for commands. Pass --command-level {level}.`
     - `picked != level`: print
       `!! rebuild_reference.py would take L{picked} for commands, where this check found entries at L{level}. Check which is right; pass --command-level if needed.`
4. **`check_corpus.py`:** add the check
   `"attribution-declined": (WARN, "a reference conversion attributed no chunk to an entry (manifest attribution.status is declined): pass --command-level if it is a reference")`.
   Raise it when `m.get("attribution", {}).get("status") == "declined"`.
   Guard against the field not being a dict. It reads only the manifest, so
   the checker's independence holds.
5. **SKILL.md:** in "Choosing an extractor" or "Entity attribution", add one
   sentence each about the `!!` decline line, the `attribution` manifest
   field, and `--command-level`. Add the option to the `rebuild_reference.py`
   row of the scripts table.

**Tests.**
- Add a fixture, `tcl_reference_fixture(path, n)`, to `_support.py`: `n` L1
  TOC entries named with lowercase words that have no underscore (`verbaa`,
  `verbab`, …), one per page, each page built with `command_page(name)`.
- `test_30_a_declined_attribution_is_said_and_recorded`, with a 30-entry
  fixture:
  - `pick_extractor.py` says `shape: reference` and prints
    `--command-level 1`.
  - `rebuild_reference.py` without the option exits 0. Its stdout has an
    `!!` line, the manifest has `attribution.status == "declined"`, and no
    section has a command.
  - With `--command-level 1`, all 30 commands are attributed and the manifest
    has `attribution == {"status": "attributed", "command_level": 1, "chosen_by": "option"}`.
  - `--command-level 7` exits non-zero and says which levels exist.
- Extend test_06: tiny now records `"declined"`, and its stdout has `!!`.
- In `test_checker.py`, add to test_18's plants
  `("attribution declined", lambda d, m: m.update(attribution={"status": "declined", "command_level": None, "chosen_by": "toc"}), "attribution-declined", False)`.

**Part B: decided, nothing to build.** IMPROVEMENT.md first asked for one
entry test in both scripts; it now records the decision not to (§1, item
5). The two tests answer different questions, whether a document is a
reference and which level to attribute from, and each was measured as it
is. Write that in `_common.py`'s docstring, next to `pick_command_level`, in
two or three sentences, so nobody merges them later without measuring.

**Done when**
- [x] `_common.py` exists, imports only the standard library, and holds
      `pick_command_level`.
- [x] The declined, attributed and given-level cases are all recorded in the
      manifest and tested.
- [x] Converting `ref` before and after differs only by the new
      `attribution` field.
- [x] `_common.py`'s docstring says why the two entry tests stay separate.

---

### T07: Stage conversions outside `docs/`; skip hidden folders everywhere [1.6]

**Status:** done, as specified below. Additions:
- `editions.document_dirs(docs)` lists one `docs/` folder's documents, and
  every walker uses it.
- `rebuild_reference.py --replace`'s help text now names `.rebuild-backup/`.
- failure-modes §10 records what changed.

Tests: test_31 and test_33 in `test_server.py`, test_32 in
`test_converters.py`, test_31b and test_33b in `test_checker.py`. Each fails
on the old code, except test_33b, which guards a check the old code already
made. All seven fixture conversions are byte-identical before and after.

**Why.**
- `rebuild_reference.py` builds in `docs/<slug>.new/sections` with
  `exist_ok=True` and never clears it. Section files left by an interrupted
  run, named for a different chunk count, survive the rename as orphans.
- `.new` sits inside `docs/`. `build_index.py` and `editions.is_document_dir`
  skip it. But `build_search_db.py`, `enrich_chunks.py`, `ocr_figures.py`
  (and `extract_figures.py` and `sample_sections.py`, through inline copies
  of the rule) glob `docs/*/manifest.json` with no filter, or with a copy of
  their own.
- `convert_manual.py` and `convert_docling.py` write straight into the final
  folder.
- `build_index.py` identifies a document by its folder name, and
  `build_search_db.py` by the manifest's `slug`.

**Decisions** (from IMPROVEMENT.md):
- Staging goes in `<collection>/.rebuild-backup/.staging/<slug>/`. A slug
  cannot start with `.`, so this never collides with a backup at
  `.rebuild-backup/<slug>/`. It is outside `docs/` and on the same filesystem
  as it, so the final rename is atomic.
- **The folder name identifies a document.** The manifest's `slug` is a copy,
  and the checker compares the two.

**Files.**
- `scripts/editions.py`
- the three converters
- `scripts/build_index.py`, `scripts/build_search_db.py`,
  `scripts/enrich_chunks.py`, `scripts/extract_figures.py`,
  `scripts/ocr_figures.py`, `scripts/sample_sections.py`
- `scripts/check_corpus.py`
- `SKILL.md` ("Platform notes", "The output contract")
- `references/failure-modes.md` §10
- tests

**Steps.**

1. **`editions.py`:**

   ```python
   STAGING_DIR = ".staging"

   def skips_name(name: str) -> bool:
       """A folder under docs/ that is not a document: a backup (.old), an
       interrupted build (.new), or anything hidden or private."""
       return name.endswith((".old", ".new")) or name.startswith((".", "_"))

   def is_document_dir(path: Path) -> bool:
       return (path / "manifest.json").is_file() and not skips_name(path.name)

   def staging_dir(out_root: Path, slug: str) -> Path:
       """An empty folder to build docs/<slug>/ in. Under .rebuild-backup/,
       so outside docs/ where every index builder looks, and in the same
       collection, so moving it into place is a rename on one filesystem.
       Cleared first: files an interrupted run left there would otherwise
       ship with this one."""
       path = out_root.parent / BACKUP_DIR / STAGING_DIR / slug
       if path.exists():
           shutil.rmtree(path)
       path.mkdir(parents=True)
       return path

   def publish(staging: Path, out_dir: Path) -> Path | None:
       """Move a finished build to docs/<slug>/. A document already there is
       kept at .rebuild-backup/<slug>/, replacing any earlier backup, and
       that path is returned."""
   ```

   `publish`:
   1. `out_dir.parent.mkdir(parents=True, exist_ok=True)`.
   2. If `out_dir` exists, move it to `out_dir.parent.parent / BACKUP_DIR / out_dir.name`,
      removing an older backup there first. This is the logic now at the end
      of `rebuild_reference.main`.
   3. `staging.rename(out_dir)`.
2. **Converters.** Each one writes `sections/`, `full.md` and `manifest.json`
   into `editions.staging_dir(out_root, slug)`, then calls
   `editions.publish(staging, out_dir)`, then `editions.finish(plan)`. The
   order matters: `finish` reads the published manifest.
   - In `rebuild_reference.py`, delete the `.new` lines and the inline
     backup block. Print the backup path that `publish` returns, as now.
   - `convert_manual.py` and `convert_docling.py` keep refusing an existing
     `out_dir`.
3. **Every walker uses `editions.is_document_dir`.** Replace each
   `glob("*/manifest.json")` and each inline copy of the name rule:
   - `build_index.load_manuals`
   - `build_search_db.find_collections` and `load_documents`
   - `enrich_chunks.main`
   - `extract_figures.main`
   - `ocr_figures.main`: it globs `*/figures.json`. Skip a parent folder that
     is not a document.
   - `sample_sections.pools`
4. **Folder identity in `build_search_db.load_documents`:**
   - Accept any manifest that is a JSON object. The error for one that isn't
     becomes "it is not a JSON object".
   - When `manifest.get("slug") != folder`, print
     `!! <path>: manifest slug 'x' differs from its folder; indexed as '<folder>'`.
   - Then set `manifest["slug"] = folder` in memory. That way every later
     read of `manifest["slug"]` uses the folder name, including
     `resolve_editions`, the build loop and `--stats-only`.
   - In `sample_sections.pools`, use `mp.parent.name` instead of
     `m["slug"]`.
5. **`check_corpus.py`.** It keeps its own copy of the rule (`index_skips`);
   it must not import editions.
   - `hidden-document`: change its severity to **WARN** and its message to
     "a folder under docs/ that every tool skips (.old, .new, dot,
     underscore) holds a manifest: a leftover backup or an interrupted
     build. Move it out of docs/".
   - `duplicate-slug`: count only `doc_dirs`, not `hidden`, and compare
     folder names instead of manifest slugs. Until T09 lands, a folder name
     shared by two collections still stops the build, so the check stays
     corpus-wide.
   - `slug-mismatch`: change the message to "the manifest's slug is not its
     folder's name; every tool uses the folder name, so the manifest's copy
     is wrong".
6. **Docs.**
   - SKILL.md's platform note "Keep backups outside `docs/`" says index
     builders list `docs/<slug>.old`. They no longer do; say they skip it
     and the checker warns.
   - Say in "The output contract" that the folder name identifies a
     document.
   - Update failure-modes §10 to match.

**Tests.**
- `test_31_hidden_folders_are_skipped_by_every_script`, using a fresh copy
  of `WS.corpus()`:
  - Copy `docs/prose` to `docs/prose.new` and `docs/_scratch`.
  - `build_search_db.py`: neither is in `documents`, and the build exits 0.
  - `enrich_chunks.py`: the bytes of `docs/prose.new` are unchanged.
  - `sample_sections.py --root`: neither is named.
  - Add a test that the three name rules agree on
    `["x.old", "x.new", ".x", "_x", "x", "a.b", "x.older"]`:
    `editions.skips_name`, `check_corpus.index_skips` and, from T22 on, the
    server's copy.
- `test_32_an_interrupted_build_leaves_nothing_behind`: create
  `<coll>/.rebuild-backup/.staging/ref/sections/9999-orphan.md`, then run
  `rebuild_reference.py ... --slug ref` into that collection. Afterwards
  `docs/ref/sections/9999-orphan.md` does not exist, and no `docs/*.new`
  exists. Do the same for `convert_manual.py`.
- `test_33_the_folder_names_the_document`: edit a copied document's manifest
  to `"slug": "other"`. `build_search_db.py` exits 0, prints `!!`, and
  `documents.slug` is the folder name.
- **test_18:** change the plant `("backup left in docs/", backup, "hidden-document", True)`
  to `False`, since it is a warning now. Delete the
  `("backup left in docs/", backup, "duplicate-slug", True)` plant. Add a
  `duplicate-slug` test that copies the hand document into two collections
  of one root (`Root/A/docs/hand`, `Root/B/docs/hand`). T09 deletes it again.

**Done when**
- [x] `grep -rn 'glob("\*/manifest.json")\|endswith((".old", ".new"))' scripts/`
      finds only `check_corpus.py` and `editions.py`.
- [x] No converter writes anywhere under `docs/` before its final rename.
- [x] Converting the fixtures before and after gives identical `docs/` trees.

---

### T08: Stop deleting short lines that begin the title [1.8]

**Status:** done, except the real-corpus diff (§4).

**Why.** `detect_furniture` treats a line as the running title if
`title_core.startswith(norm[:40])`, and that is true of *any* prefix of the
title. For "Design Compiler User Guide", a plain line reading "Design
Compiler" that appears in 3 or more sections is deleted as furniture. The
same function runs per page inside `rebuild_reference.py`.

**Files.** `scripts/enrich_chunks.py`, `SKILL.md` (the sentence from T04),
`tests/test_converters.py`.

**Steps.**

1. In `detect_furniture`:

   ```python
   # A line is the running title when it starts with the title (a footer
   # that adds the release: "Design Compiler User Guide V-2024.06") or is
   # most of it (one the layout cut short). A line that only begins the
   # title -- "Design Compiler" alone, the product name prose uses -- is
   # content.
   TITLE_SHARE = 0.6
   ...
   is_title_line = title_core and (
       norm.startswith(title_core[:40])
       or (title_core.startswith(norm[:40]) and len(norm) >= TITLE_SHARE * len(title_core)))
   ```

   Put `TITLE_SHARE` with the other module constants.
2. Add `--list-furniture` to `enrich_chunks.py`. It prints every distinct line
   the run deletes, or would delete with `--dry-run`, per document, with how
   many times each is deleted, most frequent first. That is the verification
   protocol's "enumerate every distinct line you would delete".
   - Give `strip_furniture` an optional `dropped: list | None = None`
     parameter, and append each dropped raw line to it.
   - Aggregate with a `Counter` in `process_manual`, and return it in the
     result dict.
3. Update the SKILL.md sentence from T04: lines that start the title are
   deleted only when they cover at least 60% of it. Mention
   `--list-furniture`.

**Tests.** `test_34_a_line_that_only_begins_the_title_is_kept`: call
`detect_furniture` directly with the title "Design Compiler User Guide" and
four section texts.
- Each text contains a "Design Compiler" line: that line is **not**
  furniture.
- Each text contains "Design Compiler User Guide": it is furniture.
- Each text contains "Design Compiler User Guide V-2024.06": it is
  furniture.
- Each text contains "Design Compiler User" (77% of the title): it is
  furniture.

Then run `enrich_chunks.py --dry-run --list-furniture` on a fresh copy of
`WS.corpus()`, and assert that the output lists `Feedback`.

**Real-corpus check (owed).** On the user's corpus, run
`enrich_chunks.py <collection> --dry-run --list-furniture` with the old and
the new code, and diff the outputs. Every line that is no longer deleted
should be content. A line that should still go is a reason to rethink the
threshold. Reference documents strip furniture at conversion, so they are
checked by reconverting one and running `check_corpus.py` (its `furniture`
check). Record both in §4.

**Done when**
- [x] The test fails on the old rule and passes now.
- [x] `--list-furniture` works with and without `--dry-run`.
- [x] The real-corpus diff is done, or its §4 row says why not.

---

### T09: Key documents by (collection, slug) [3.5]

**Status:** done, as specified below. Additions:
- `search()` refuses a `document` given without its `collection`, so no
  caller can narrow by slug alone again.
- `mcp_smoke_test.py` names each document with its collection, and its
  current-edition filter matches on both.
- `sample_sections.py` keys its pools by (collection, slug); it overwrote
  one collection's document with the other's. It names the collection when
  a slug is shared.
- ROADMAP's item for this bug is ticked.

Tests: test_35 and test_35b in `test_server.py`. test_35c in
`test_checker.py` replaces T07's test_33b and holds `collection_key` to
`build_search_db.slugify`. All three fail on the old code.

**Why.** `documents.slug` is the primary key (`build_search_db.py`, the
`CREATE TABLE documents` block). Two vendors that both have a `user-guide`
therefore crash the build, on the multi-collection layout the README shows.
Every table already has a `collection` column.

**Files.** `scripts/build_search_db.py`, `scripts/mcp_server.py`,
`scripts/eval_search.py`, `scripts/sample_sections.py`,
`scripts/check_corpus.py`, tests.

**Steps.**

1. **`build_search_db.py`:**
   - In the schema, give `documents` `PRIMARY KEY (collection, slug)` instead
     of the column constraint. Change
     `idx_entities_slug ON entities(slug)` to `(collection, slug)` and
     `idx_figures_section` to `(collection, slug, section_ord)`. Bump
     `schema_version` to `"4"`.
   - `resolve_editions` returns a dict keyed by `(key, slug)`, where `key` is
     the collection key from `find_collections`. Update every
     `edition_of[...]` lookup, `--stats-only` included.
   - Stop the build, like the other "Nothing was written" cases, when two
     collection folders slugify to the same key (for example `Tessent Manual`
     and `tessent-manual`).
2. **`mcp_server.py`:** every query that narrows by slug must also narrow by
   collection. Work through `grep -n "slug" scripts/mcp_server.py`. The list:
   - `Corpus.document(slug)` → `document(collection, slug)`. Key the cache
     by `(r["collection"], r["slug"])`.
   - `current_only()` →
     `" AND (collection, slug) IN (SELECT collection, slug FROM documents WHERE is_current = 1)"`.
   - `resolve_document`: `SELECT * FROM documents WHERE slug = ?` can now
     return several rows.
     - With `collection` given, keep the row in that collection, as now.
     - Without it, if rows come from more than one collection, raise
       ``ValueError(f"'{name}' is a document in more than one collection ({names}); pass `collection`.")``.
       This mirrors the existing doc_id rule.
   - `run_match(document)`: take the resolved row, or `(collection, slug)`,
     and add `AND collection = ?`.
   - `search`: key `per_doc` by `(collection, slug)`.
   - `document_label(slug, title)` → `document_label(collection, slug, title)`,
     and update all of its callers.
   - `section_figures(slug, ord)` → `section_figures(collection, slug, ord)`.
   - `tool_get_section`: add `AND collection = ?` to the neighbours query.
     The `is_current` ordering subquery uses the row-value form above.
   - `tool_lookup_entity`, `other_editions_with` and `suggest_entities`: join
     and filter on both columns, and carry `document` as a row, not a slug.
   - `tool_get_figure`: `JOIN documents d ON d.slug = f.slug AND d.collection = f.collection`,
     and look the section up by `(slug, collection, ord)`.
   - compare_versions (`entry_lines`, `compare_entry`, `compare_listing` and
     their `entity()`/`names()` helpers): pass `(collection, slug)`. Both
     editions are in one collection by construction.

   Indexes with schema 3 have every one of these columns, so the new queries
   work on them unchanged. Don't add a version branch.
3. **`eval_search.py`:**
   - An answer may carry `"collection"`. When it does, `is_answer` also
     compares `row["collection"]`, and `missing_answers` adds
     `AND collection = ?`.
   - Document the field in the module docstring.
4. **`check_corpus.py`:**
   - Delete `duplicate-slug`: its `CHECKS` entry, the loop in `main`, and
     T07's test. A folder name is unique within a collection, and
     collections no longer share a key space.
   - Add `"collection-clash": (FAIL, "two collection folders whose names give one key (letters and digits, lowercased); build_search_db.py stops")`.
     Write the slugify rule out again in the checker, and add it to the
     cross-check test with `build_search_db.slugify`.

**Tests.**
- `test_35_two_collections_may_share_a_slug`:
  - Build `Root/A/docs/user-guide` and `Root/B/docs/user-guide` with
    `handmade_document(..., slug="user-guide")`. In B, add a unique word,
    `zebraword`, to one section file, and update that section's `chars` in
    the manifest to match.
  - `build_search_db.py --root Root` exits 0, with 2 documents.
  - In process, using `serve()`-style setup:
    - `search("zebraword")` returns only collection `b`.
    - `tool_get_toc({"document": "user-guide"})` raises a `ValueError` that
      mentions both collections.
    - With `"collection": "b"` it succeeds.
    - `tool_list_documents({})` lists both.
  - `mcp_smoke_test.py --db` on that index passes.
- `test_35b_collection_folders_that_clash_stop_the_build`: two collections
  named `Tools` and `tools`. Skip this test where the filesystem is
  case-insensitive (check by creating both folders).
- The existing `test_26_a_name_means_what_it_does_in_the_collection_asked_for`
  must still pass unchanged.

**Done when**
- [x] Every `slug = ?` in `mcp_server.py` comes with `collection = ?`, or
      sits in a query already narrowed to one collection. Write the reasoning
      for any exception in the commit message.
- [x] The suite and the smoke test pass. An index built by `0e4dd59` (schema
      3) still serves. Checked with an index built by `1fbc596`, the last
      schema-3 commit, on gadget editions plus a reference: the new smoke
      test passed every check. Build one with the old code
      (`git worktree add ../old 0e4dd59`, then run
      `python ../old/scripts/build_search_db.py --root <fixture corpus> --out old.sqlite3`),
      and run the new `mcp_smoke_test.py --db old.sqlite3` on it.

---

### T10: Demote front matter only by its whole heading [1.1]

**Status:** done, except the eval comparison (§4).

**Why.** Verified: `is_noise` flags `Indexing Options`, `Index Types`,
`Contents of the Install Kit` and `Feedback Loops in PLLs` as front matter,
because they *start with* a front-matter word. A flagged chunk gets +8.0 in
`rank_adjust`, more than the largest single boost (−6.0), so in practice it
drops out of the top results.

**Files.** `scripts/build_search_db.py`, `tests/test_server.py`.
`sample_sections.py` imports `is_noise`, so it changes with it, which is
intended.

**Steps.**

1. Replace `FRONT_MATTER_HEADINGS` and the `startswith` test:

   ```python
   # The whole heading, not its first word: "Index Types", "Contents of the
   # Install Kit" and "Feedback Loops in PLLs" are real sections. The
   # chunker names the pieces of a split heading "<heading> (cont.)" and
   # "<heading> (intro)", so those count as the heading. "About this" stays
   # a prefix: it only ever starts "About This Manual" or "About This Guide".
   FRONT_MATTER_RE = re.compile(
       r"^(?:contents|table of contents|index|feedback|list of (?:figures|tables))"
       r"(?: \((?:cont\.|intro)\))*$")
   ```

   In `is_noise`, set `h = " ".join(heading.lower().split())`, then return
   `True` if `FRONT_MATTER_RE.match(h) or h.startswith("about this")`. Keep
   the dot-leader test exactly as it is.
2. `noise` is stored at build time, so an existing index changes only when it
   is rebuilt. Say so in the commit message.

**Tests.** `test_36_front_matter_is_a_whole_heading`:
- `is_noise(h, "Real text.\n")` is `False` for the four headings above.
- It is `True` for `Contents`, `TABLE OF CONTENTS`, `Index`,
  `Index (cont.)`, `Contents (intro) (cont.)`, `Feedback`, `List of Figures`,
  `List of Tables` and `About This Manual`.
- A heading "Overview" whose body is six dot-leader lines is `True`.

**Real-corpus check (owed).** Rebuild the index and run
`eval_search.py --json after.json`. Compare it with a run from before the
change (T12's `--compare`). Record the result in §4.

**Done when**
- [x] The test fails before the change and passes after it.
- [x] The eval comparison is done, or its §4 row says why not.

---

### T11: Boost an entity named inside a question [1.2]

**Status:** done, except the eval comparison (§4).

**Why.** Verified: `rank_adjust` joins *every* query word with `_`, so the
−6.0 boost fires only when the query is the bare identifier:
`set_scan_configuration` gets −6.0, `set_scan_configuration -chain_count`
gets −2.0, and `what does set_scan_configuration do` gets 0.0. Agents ask
the last kind. Also verified:
- The prefix branch gives an entity named `set` −2.0 for `set the clock`.
- `all(w in heading for w in words)` tests substrings and counts stopwords:
  the query `i a` gets −1.5 on the heading "Timing Analysis".

Identifier questions reach 100% hit@5 but only 85% hit@1 on the user's
corpus; this targets that gap.

**Files.** `scripts/mcp_server.py`, `tests/test_server.py`.

**Steps.**

1. Add these next to `WORD_RE`. The server imports nothing, so
   `MESSAGE_CODE_RE` is written out here. Add it to the cross-check test,
   comparing behaviour rather than pattern text: for samples such as
   `ADES-002`, `CMD-082`, `ades-002`, `X-1` and `ADES-002x`, check that
   `bool(_common.MESSAGE_CODE_RE.match(s)) == bool(server.MESSAGE_CODE_RE.fullmatch(s))`.
   `_common`'s pattern is anchored with `^…$` and the server's is not.

   ```python
   MESSAGE_CODE_RE = re.compile(r"[A-Z][A-Z0-9]{1,9}-\d{2,5}")

   def query_tokens(query: str) -> list[tuple[str, bool]]:
       """The query's words as typed, lowercased, without the punctuation
       around them, each with whether it names an identifier: it holds `_`,
       starts with `-`, is a message code (ADES-002), or is in backticks.
       A plain word is not one: `set` in "how do I set the clock" is English,
       even where a command is called set."""
       out = []
       for raw in query.split():
           ticked = raw.startswith("`") and raw.rstrip(".,;:?!").endswith("`")
           tok = raw.strip("`'\"()[]{}<>,;:?!").rstrip(".")
           if tok:
               ident = (ticked or "_" in tok or (tok.startswith("-") and len(tok) > 1)
                        or bool(MESSAGE_CODE_RE.fullmatch(tok)))
               out.append((tok.lower(), ident))
       return out
   ```
2. Rewrite the entity and heading parts of `rank_adjust`:

   ```python
   entity = " ".join((row["entity"] or "").lower().split())
   if entity:
       tokens = query_tokens(query)
       named = {t for t, ident in tokens if ident}
       runs = {" ".join(t for t, _ in tokens[i:i + n])
               for n in (2, 3, 4) for i in range(len(tokens) - n + 1)}
       if (entity == joined or entity.replace("_", " ") == spaced   # the query is the entity
               or entity in named                                   # the query names it
               or (" " in entity and entity in runs)):               # "tessent -shell" mid-question
           score -= 6.0

   heading = (row["heading"] or "").lower()
   if heading:
       if heading == spaced or heading.replace("_", " ") == spaced:
           score -= 4.0
       else:
           content = {w for w in words if w not in STOPWORDS}
           if content and content <= set(WORD_RE.findall(heading)):
               score -= 1.5
   ```

   The prefix branch (`joined.startswith(entity) or entity.startswith(joined)`)
   is deleted. Keep the docstring's first line, and add a sentence on what
   "names it" means.

**Tests.** `test_37_an_entity_named_in_a_question_is_boosted`: call
`rank_adjust` with dict rows
`{"score": 0.0, "entity": ..., "heading": "", "chars": 1000, "noise": 0}`.

| entity | query | expected |
|---|---|---|
| `set_scan_configuration` | `set_scan_configuration` | −6.0 |
| same | `set_scan_configuration -chain_count` | −6.0 |
| same | `what does set_scan_configuration do?` | −6.0 |
| same | `-chain_count option of set_scan_configuration` | −6.0 |
| same | `set scan configuration` | −6.0 |
| same | `scan configuration` | 0.0 |
| `set` | `set` | −6.0 |
| `set` | `how do I set the clock` | 0.0 |
| `set` | `` options of `set` `` | −6.0 |
| `tessent -shell` | `how to start tessent -shell in batch` | −6.0 |
| `ADES-002` | `what does ADES-002 mean` | −6.0 |

With heading rows:
- heading "Timing Analysis": `i a` → 0.0, `analysis of timing` → −1.5,
  `timing analysis options` → 0.0.
- heading "Reporting": `port` → 0.0.

End to end, on `WS.index()`, `search("what does set_widget_option_05 do")[0]["entity"] == "set_widget_option_05"`.

**Real-corpus check (owed).** Run `eval_search.py` before and after, and
compare with `--compare` (T12). Look at identifier hit@1 first, and read
every question whose rank moved. Record in §4.

**Done when**
- [x] The table test fails on the old `rank_adjust` and passes now.
- [x] The smoke test passes.
- [x] The eval comparison is done, or its §4 row says why not.

---

### T12: `eval_search.py --compare` [2.6]

**Status:** done

**Why.** The references keep saying to read the per-question changes, not
the totals, but nothing prints them. A saved run also can't be traced to the
code that produced it.

**Files.** `scripts/eval_search.py`, `SKILL.md` ("Measuring retrieval",
step 5), `tests/test_server.py`.

**Steps.**

1. In the `--json` output:
   - Add `"question": q["question"]` to each result.
   - Add a top-level `"git_commit"`: the output of
     `git -C <repo root> rev-parse --short HEAD`, run with `subprocess`, or
     `None` if that fails. The repo root is `Path(__file__).resolve().parent.parent`.
   - `meta` (which holds `built_at`) is already written.
2. Add `--compare A.json B.json`. With it, `--db` and `--questions` are not
   needed. Make them `required=False`, and check them by hand when not
   comparing. The output:

   ```
   A: run-a.json  built 2026-10-01T10:00:00  code 0e4dd59  figure text on
   B: run-b.json  built 2026-10-07T12:00:00  code 1a2b3c4  figure text on

   kind          n   hit@1 A  hit@1 B   hit@5 A  hit@5 B   MRR A  MRR B
   identifier   26      85%      92%      100%     100%    0.90   0.95
   ...
   Rank changes (rank 11+ shown as -):
     [q012] identifier   3 -> 1   How do I set the chain count?
     [q040] concept      - -> 7   ...
   Only in A: q077
   ```

   List only questions whose rank differs, ordered by kind and then id. Exit
   0.

**Tests.** `test_38_eval_compare_lists_each_rank_that_moved`: write two small
JSON files by hand, with 3 results each, one rank changed and one id only in
A. Run `eval_search.py --compare a.json b.json`. Assert the moved line, the
"Only in A" line, and that no unchanged question is listed. Then run a real
`--json` on `WS.index()` with a two-question file, and assert that
`git_commit` and each `question` are present.

**Done when**
- [x] `--compare` works without `--db`.
- [x] SKILL.md step 5 names it.

---

### T13: Chunk in spans, so every offset is exact [2.2]

**Status:** done. Additions:
- `convert_manual.trim_span(text, start, end)`: a chunk's page is where its
  first word is. A piece cut at a newline starts with that newline, which can
  be the last character of the page before, so `rebuild_reference.py` measures
  pages on the trimmed span (test_39b). T14 uses it too.
- The fixture diff was empty except `page_end` of the last piece of
  `edition_reference_fixture`'s long entry: 26 before, 25 now. The old code
  searched for the body's stripped text, then added the body's full length,
  so it ran past the end by the leading whitespace and into the next entry's
  page. Entry 5 ends on page 25.

**Why.** `rebuild_reference.locate` finds each chunk again by searching for
its first 200 characters, and falls back to the cursor. The chunker cut the
chunk out of the text, so it could just say where it cut. §1 item 10 shows
that bodies today are not always slices, so the chunker has to work in spans
for that to hold. Page numbers depend on this path.

**Files.** `scripts/convert_manual.py`, `scripts/rebuild_reference.py`,
`tests/test_converters.py`.

**Steps.**

1. Add `class Span(NamedTuple): heading: str; level: int; start: int; end: int`
   to `convert_manual.py`. Offsets are absolute in the text being chunked.
2. Make every chunking function take `(text, ..., start, end)` and return
   spans. Inside each one, `body = text[start:end]`. Run the regexes on
   `body` exactly as now (keep the `^`/`$` behaviour identical), and add
   `start` to every position found:
   - `split_at`
   - `next_heading_matches`
   - `split_by_paragraph`
   - `hard_wrap`
   - `pack_adjacent`
   - `split_oversized`

   Two of them need more than offset bookkeeping:
   - `split_by_paragraph`: walk `body.split("\n\n")` with a running offset
     (`+ len(p) + 2`). A piece spans from its first paragraph's start to its
     last paragraph's end, and the separators between pieces belong to
     neither.
   - `pack_adjacent`: a merged group spans `first.start .. last.end`, so it
     **includes** whatever lay between its parts. Check the size as
     `candidate.end - group.start > MAX_CHUNK`, not as a sum of lengths, so a
     merged chunk never exceeds `MAX_CHUNK`.
3. `chunk_spans(md_text, dictionary) -> list[Span]` is the new entry point.
   Keep `chunk_markdown(md_text, dictionary)` as a wrapper returning
   `[(s.heading, s.level, md_text[s.start:s.end]) for s in chunk_spans(...)]`,
   so the existing callers and tests keep working.
4. In `rebuild_reference.main`:
   - Iterate `cm.chunk_spans(region, dictionary=True)`, and take
     `off = r_start + span.start` and `body = region[span.start:span.end]`.
   - Delete `locate` and the `cursor` bookkeeping.
   - Move the page-range arithmetic (the two `bisect` lines and the lookups)
     into `convert_manual.page_range(page_starts, numbers, start, end)`. T14
     uses it.

**Tests.** `test_39_chunks_are_exact_spans_of_the_text`:
- For each text below, the spans are in order, don't overlap, and lie inside
  the text.
- `chunk_markdown`'s bodies equal `text[s.start:s.end]`.
- Every chunk is at most `MAX_CHUNK` characters.
- `re.sub(r"\s", "", "".join(bodies)) == re.sub(r"\s", "", text)`: nothing
  is lost or duplicated.

The texts: each fixture's `full.md`, after converting it, and three
synthetic ones:
1. A `## A` block with an intro, then `### B` holding a ~9.5 KB paragraph of
   short lines and a short tail, then `### C`. This is the case that glued
   paragraphs today: assert the chunk holding the tail contains
   `"\n\nShort tail of B."`.
2. One 30 KB line with no newline (the hard wrap).
3. A `--dictionary` text: 12 `**cmd_NN**` entries, one longer than
   `MAX_CHUNK`.

**Before/after check.** Convert every fixture PDF with the old and the new
code, and `diff -r` the `docs/` trees. Expect no difference. A difference is
acceptable only if it is the separator case from §1 item 10, and it must be
explained in the commit message. `edition_reference_fixture`'s long entry is
the one fixture block over `MAX_CHUNK`, so look at it first.

**Done when**
- [x] `locate` is gone, and `rebuild_reference.py` gets offsets only from
      spans.
- [x] The property test passes on every text listed above.
- [x] The fixture diff is empty or explained.

---

### T14: Page numbers on the light prose path [2.1]

**Status:** done, except the real-corpus check (§4). `convert_manual.join_pages` is the shared helper; `rebuild_reference.build_pages` uses it. The trim test is test_39b, so T14's tests keep their numbers.

**Why.** This is the largest gain on the list. `convert_manual.py` calls
`to_markdown()` without `page_chunks=True`, so prose chunks carry no page
numbers. That is the "46% page coverage" in failure-modes §12, and the
reason the docs send prose to Docling: a multi-gigabyte install at about
1.2 s a page (IMPROVEMENT.md puts 25,000 pages at about 8.6 hours).
Verified on both fixtures: joining the pages from
`page_chunks=True` gives byte-identical markdown to the whole-document call.

**Files.**
- `scripts/convert_manual.py`
- `scripts/extract_figures.py`
- `SKILL.md`: "Choosing an extractor", "Page range", "Figures", "Bundled
  scripts" table
- `references/failure-modes.md` §12
- `scripts/convert_docling.py` docstring ("The pymupdf4llm prose path carries
  none")
- README.md, if it says the same
- tests

**Steps.**

1. **`convert_manual.convert`:**
   1. `texts, numbers = page_markdown(pdf_path)` (T03).
   2. `furniture = ec.detect_furniture(texts, title)`. Import
      `enrich_chunks as ec`; it is standard library only.
   3. Strip each page with `ec.strip_furniture`, building `full_md` and
      `page_starts` exactly as `rebuild_reference.build_pages` does. Move that
      loop into a shared helper,
      `convert_manual.join_pages(texts, furniture) -> (full_md, page_starts, removed)`,
      and use it from both converters.
   4. `chunk_spans(full_md, dictionary)` (T13). Each section gets
      `page_start`/`page_end` from `page_range` (T13).
   5. `full.md` is `full_md`, now with furniture stripped. Print
      `N furniture lines stripped pre-chunking`, as the reference converter
      does.
   6. Keep the manifest's other fields and their order. Add `page_start` and
      `page_end` after `chars` in each section, as `rebuild_reference.py`
      orders them.
2. **`extract_figures.extract`:**
   - Today a document with pages never uses the paragraph-above rule. On
     prose that rule measured 96–99%, but on a rebuilt reference it measured
     5.8%. Keep it as the fallback after the page rule, except for
     `rebuild_reference.py` output.
   - Identify that output by the `"command"` key on its sections (§1,
     item 11), or by T20's provenance once that exists:

     ```python
     # One chunk per entry strands the paragraph above a figure in the
     # previous chunk: 5.8% right on a rebuilt command reference, against
     # 96-99% on documents chunked by heading.
     paragraph_rule = not any("command" in s for s in sections)
     ...
     if section is None and linker.has_pages:
         section = linker.by_page(f["page"]); how = "page" if section is not None else None
     if section is None and f["context"] and (paragraph_rule or not linker.has_pages):
         section = linker.by_context(f["context"]); how = "context" if section is not None else None
     ```

   - Docling output has pages and no `command` key, so it gains the fallback
     too. That is a change for Docling documents: say so in the commit
     message. The run summary's "context agrees with caption" line measures
     it.
   - Factor this into a small function so it can be tested without the
     layout model. Update the module docstring's "context" paragraph, and
     SKILL.md's "Figures" bullets: "never used where pages exist" becomes
     "never used on a reference rebuilt one chunk per entry".
3. **Docs.**
   - SKILL.md "Choosing an extractor": the prose row's reason for Docling
     can no longer be "100% pages". What Docling still adds is TOC-anchored
     breadcrumbs with a confidence on every chunk. Don't change the routing
     advice itself until the real-corpus comparison below is done; say it is
     under review.
   - failure-modes §12: replace "Page coverage is partial" with what is true
     now. Prose documents converted before this change still have no pages
     until they are reconverted.

**Tests.**
- test_01: every section has integer `page_start` ≤ `page_end`, both inside
  `1..page_count`, and `page_start` never decreases.
- test_16: change `assertIn("no-pages", raised)` to `assertNotIn`, and fix
  its docstring.
- `test_40_light_prose_conversion_passes_the_strict_check`: convert and
  enrich the prose fixture, then run `check_corpus.py --strict --only prose`
  on it. If a warning remains, find out why before deciding what to do:
  - A real defect is fixed in this task.
  - A fixture artifact is explained in the test's docstring, and that one
    check is excluded from the assertion.
- `test_41_an_uncaptioned_figure_in_prose_falls_back_to_its_paragraph`: unit
  test of the factored function.
  - Use a `Linker` whose two sections share page 1, and a figure on page 1
    whose context words appear in section 2 only. With prose sections, it
    links to section 2 by `context`.
  - With sections carrying `"command": None`, it links to nothing.
- test_13 must still link both figures by `caption`.

**Joined pages equal the whole document: settled, and pinned.** §1 item 8
shows that pymupdf4llm 1.28.2 builds them identically. Add
`test_41b_joined_pages_are_the_whole_document`: for the prose and ref
fixtures, `"".join(page_markdown(pdf)[0])` equals
`pymupdf4llm.to_markdown(str(pdf))`. A future pymupdf4llm release that
breaks this then fails CI, instead of quietly changing prose chunks.

**Real-corpus check (owed, not blocking).** Reconvert two or three prose
manuals. Run `check_corpus.py --strict` and the content check, and compare
`extract_figures.py`'s summary (links by caption, page and context, and how
often each fallback agrees with the caption) with the old conversion. Record
it in §4.

**Done when**
- [x] Prose sections carry pages. test_16 and the new tests pass.
- [x] Figures in prose fall back to the paragraph rule; figures in a rebuilt
      reference don't.
- [x] Docs updated. The real-corpus check is done, or its §4 row says why
      not.

---

### T15: One pymupdf4llm converter [2.1, "next step"]

**Status:** done. `rebuild_reference.py` is 64 lines. `detect_shape`,
`looks_like_entry` and `pick_command_level` live in `_common.py`; tests are
test_41c and test_41d. Fixture output is byte-identical to T14's.

**Why.** After T14 the two pymupdf4llm converters share:
- page extraction
- per-page furniture stripping
- the chunker
- page mapping

The real remaining difference is splitting the text into entity regions
before chunking. Two scripts to keep in step is how features drift (SKILL.md
"Adding one document later" tells that story about a wrapper script).

**Design.**
- Move `rebuild_reference.py`'s region logic into `convert_manual.py`:
  `boundaries`, `entry_offset`, the regions, and per-region chunking with
  `command` and `breadcrumb`. Make it a function,
  `convert_reference(plan, title, command_level)`, next to `convert(...)`.
- `convert_manual.py` gets `--shape {auto,prose,reference}` (default `auto`)
  and `--command-level N`.
  - `auto` uses pick_extractor's `detect_shape`, moved to `_common.py` with
    its density threshold. Never use `pick_command_level` alone for this: a
    1,648-page prose manual with 22 option names at L6 would pass it.
  - `reference` goes to `convert_reference`. `prose` goes to `convert`
    (`--dictionary` still applies).
  - `mixed` exits non-zero and asks the user to pass `--shape`.
- `rebuild_reference.py` keeps its command line: `--title` defaulting from
  the existing manifest, `--replace`, and `--slug` (optional after T25). It
  becomes a thin wrapper that calls
  `convert_manual.convert_reference(...)`, so existing commands and docs
  keep working.
- `--replace` works for both shapes through `editions.publish` (T07).
- SKILL.md: say that `convert_manual.py` routes by shape. Change the
  "Reference documents get this during their own conversion, so don't run
  both" warning to say why it is now safe:
  `converter_owns_breadcrumb` leaves converter breadcrumbs alone, and
  furniture is already gone.

**Tests.**
- `convert_manual.py --shape reference` on `ref` produces the same `docs/`
  tree as `rebuild_reference.py` (`diff -r`). If T20 has landed, the only
  allowed difference is the manifest's `converter.script` and
  `converter.converted_at`. Compare the manifests with those two keys
  removed.
- `--shape auto` picks reference for `ref`, prose for `prose`, and refuses a
  mixed fixture. Build one with 0.10–0.15 entries per page.
- Every existing `rebuild_reference.py` test passes unchanged.

**Done when**
- [x] One implementation of the reference path. `rebuild_reference.py` is
      under about 80 lines.
- [x] The output is byte-identical to before for the reference fixtures.

---

### T16: Reference breadcrumbs from the TOC chain [2.3]

**Status:** done, except the eval comparison (§4). Tests are test_41e and test_41f.

**Why.** Reference breadcrumbs are `Title › command`. Each region starts at a
known TOC entry, so its enclosing chain is known exactly and costs nothing to
add. The checker's nesting and page rules already cover it.

**Files.** `scripts/rebuild_reference.py`, or `convert_manual.py` after T15.
Also `SKILL.md` and tests.

**Steps.**

1. Each boundary already carries its TOC index `i`. Build the chain of each
   TOC entry once: a level stack, as `convert_docling.chain_for_entry` does.
   Drop entries whose title, normalized, equals the document title (the same
   rule as `enrich_chunks.toc_chains`). Then:
   - A command region at entry `i` gets the breadcrumb
     `Title › <ancestors of i> › command`. The command stays last, so
     `entity-breadcrumb-mismatch` still holds.
   - A non-command region that starts at a shallower entry `j` (a chapter, an
     appendix) gets `Title › <chain of j, j included>`.
   - The region before the first boundary keeps `Title` alone.
2. **Opt-in, for `mixed` documents:** add `--prose-outside-entries`. When it
   is set:
   - At the command level, only titles that pass the entry test become
     commands. Others become non-command boundaries.
   - Non-command regions are chunked with `dictionary=False`.

   Off by default: it changes what is attributed. pick_extractor's `mixed`
   advice can mention the flag, with "measure before relying on it".

**Tests.**
- With `nested_reference_fixture`: commands get
  `Widget Commands › Command Reference › set_widget_option_NN`. The appendix
  chunks get `Widget Commands › Appendix A Troubleshooting`.
- `check_corpus.py --strict --only nested` raises no ancestor or entity
  check.
- test_11 and test_12 pass unchanged.
- A mixed fixture with `--prose-outside-entries`: prose titles at the command
  level are not attributed.

**Real-corpus check (owed).** Breadcrumbs are indexed with weight 3, so
reconvert the two references and compare `eval_search.py` runs before and
after (T12). Record in §4.

**Done when**
- [x] Chain breadcrumbs pass the checker on all reference fixtures.
- [x] The opt-in flag is tested and documented.

---

### T17: Section identity across reconversions [2.4]

**Status:** done, all three parts. Tests: test_45 (part A), test_46 (part C); part B is in test_01.

**Why.** Section files are named by position, so a reconversion renames every
file. After the reference rebuild, the eval answers and figure links were
remapped by hand (`references/retrieval-measurement.md`).

**Files.** `scripts/remap_answers.py` (new), `scripts/convert_manual.py`,
`scripts/build_index.py`, `scripts/eval_search.py` (part C),
`SKILL.md` ("Measuring retrieval", "Bundled scripts"), tests.

**Part A: `remap_answers.py`.**

```
python scripts/remap_answers.py --root <corpus> --questions eval/questions.jsonl [--out FILE]
```

For each answer `{slug, file}` whose file is gone from the current
`docs/<slug>/` but present in `<collection>/.rebuild-backup/<slug>/<file>`:
1. Read the old text. Shingle it into word 5-grams over
   `[0-9a-z]+` tokens, lowercased.
2. For each current section of that slug, score
   `|old ∩ new| / |old|`: the share of the old section's text found in it.
3. If the best score is ≥ 0.6 and beats the second by ≥ 0.2, replace the
   answer (1:1).
4. Otherwise, if the sections that each score ≥ 0.3 together cover ≥ 0.8 of
   the old shingles, replace the answer with all of them (1:n). Add a note,
   `"remapped from <old file> to n sections"`.
5. Otherwise leave it unresolved, and print the old file with the top three
   candidates and their scores.

Write `--out` (default `<questions stem>.remapped.jsonl`), **never in place**,
keeping every other field and each line's order. Print counts: unchanged,
1:1, 1:n, unresolved. Exit 1 if any are unresolved.

**Part B: four-digit section numbers.** `convert_manual.py` writes
`f"{i:03d}"` and the other converters `f"{i:04d}"`. Past 999 chunks,
`1000-…` sorts before `101-…`. Change it to `{i:04d}`. Update:
- test_21's `"sections/002-unpacking.md"` (now `0002-unpacking.md`)
- `build_index.write_readme`'s `001-*.md` layout line
- SKILL.md's layout block (`sections/NNN-slug.md`)

Existing corpora are unaffected until reconverted.

**Part C (optional): answers that survive a rebuild.** Let an answer be
`{"doc_id": ..., "version": ... (optional), "quote": "a short exact phrase"}`.
At scoring time, `eval_search.py` resolves it to the sections of that
edition whose body contains the quote, with whitespace normalized. Use the
current edition when `version` is absent, resolved the way the server's
`resolve_document` does. Zero matches, or more than three, is reported like a
stale answer.

**Tests.**
- Part A: simulate a reconversion, so the result is known exactly. In a
  fresh copy of `WS.corpus()`, copy `docs/prose` to
  `.rebuild-backup/prose`. Then, in `docs/prose`:
  - Rename every section file (`001-x.md` → `0001-x.md`) and update the
    manifest. These should remap 1:1, with a score of 1.0.
  - Split one section's text into two new files. That one should remap 1:n.
  - Replace one section's text with unrelated words. That one is
    unresolved.

  A question file points at those old files. Expect exactly those three
  outcomes, the output file to be valid JSONL with every other field kept,
  exit code 1 (one unresolved), and the input file unchanged.
- Part B: test_01 sees `sections/0001-…`.
- Part C: a quote answer resolves on `WS.index()`.

**Done when**
- [x] `remap_answers.py` is in the scripts table and in "Measuring
      retrieval".
- [x] Section numbers are four digits everywhere.

---

### T18: Retrieval experiments behind flags [2.5]

**Status:** done: both flags are built and off by default; the weights sentence is in retrieval-measurement.md; the comparison is owed (§4). Test: test_47. Each change ships as a flag, **off by default**.
Build and test the flags; don't turn any on. Whether one becomes the default
is decided later, from the user's eval (§4), and is not part of this work.

**Files.** `scripts/build_search_db.py`, `scripts/mcp_server.py`,
`references/retrieval-measurement.md`, tests.

1. **The breadcrumb counted twice.** Add
   `build_search_db.py --body-without-breadcrumb`. It indexes `body` without
   its first line when that line is the breadcrumb (the rule in
   `mcp_server.without_label`), and records meta
   `body_without_breadcrumb = 1`. The server needs no change: `cite()`
   already shows the breadcrumb. Test: with the flag, no chunk body in an
   index of `WS.corpus()` starts with that chunk's own breadcrumb line
   (`*<breadcrumb>*`), and the smoke test passes.
2. **The BM25 weights `(10, 8, 3, 1, 1)`:** change nothing. Add a sentence to
   `retrieval-measurement.md` saying why: tuning and scoring on the same 76
   questions measures nothing. Wait for held-out `real` questions.
3. **An exact-identifier column.** Add `build_search_db.py --ident-index`:
   - An extra FTS5 table,
     `CREATE VIRTUAL TABLE idents USING fts5(words, tokenize = "unicode61 tokenchars '_-'")`,
     with `rowid` = the chunk's `rowid`. Its content is the identifier-shaped
     tokens (T11's rule) of the heading, entity and body, joined by spaces.
   - In the server: if the table exists and `query_tokens` finds
     identifiers, fetch
     `SELECT rowid FROM idents WHERE idents MATCH ?` with the identifiers as
     quoted phrases joined by `OR`. `rank_adjust` gives those rows −3.0.
     Pass the set of row ids in; don't query per row.
   - Test: a fixture query naming `set_widget_option_05` ranks that entry's
     chunk first, with and without the table.

**Real-corpus check (owed).** Run each flag against the default with
`--compare`. Adopt a flag only if identifier hit@1 rises and no kind falls.
Record in §4.

---

### T19: Say which copy of a field is canonical; stop writing `chars` [3.1]

**Status:** not started

**Why.** Four fields are stored twice, and each pair needed its own checker
rule to catch the copies drifting apart:

| field | stored in | checker rule |
|---|---|---|
| breadcrumb | the manifest, and the chunk's first line | `breadcrumb-file-mismatch` |
| `chars` | the manifest, and the file itself | `chars-mismatch` |
| slug | the manifest, and the folder name | `slug-mismatch` |
| which edition is current | `index.json`, and the search index | `index-stale` |

**Steps.**

1. **SKILL.md, "The output contract":** add a short "Which copy wins" list.
   - breadcrumb: the manifest. The file's first line renders it for a reader
     of the chunk alone, and converters and `enrich_chunks.py` write both.
   - slug: the folder name (T07).
   - current edition: decided from the manifests and
     `current_versions.json` at build time. `index.json` and the search index
     are both derived from them.
   - `chars`: not stored. Compute it from the file.
2. **Stop writing `chars`:**
   - Remove it from `convert_manual.py`, `rebuild_reference.py` and
     `convert_docling.py`.
   - `enrich_chunks.process_manual`: replace `s["chars"] = len(new)` with
     `s.pop("chars", None)`, so a manifest written before this loses its
     stale copy the first time enrich rewrites a section.
   - Make the readers compute it: `build_search_db.py` (`len(body)`; the
     `chunks.chars` column stays) and `sample_sections.py` (read the file's
     length).
   - `check_corpus.py`: keep `chars-mismatch` for manifests that still carry
     the field, and say so in its message.
3. **Tests.**
   - test_01: assert `"chars" not in s` instead of comparing it.
   - test_22: compute `long_chars` from the files.
   - The handmade document keeps `chars`, so the checker rule stays tested.

**Done when**
- [ ] `grep -n '"chars"' scripts/convert_*.py scripts/rebuild_reference.py`
      finds nothing.
- [ ] The contract says which copy wins.

---

### T20: Manifest schema and provenance [3.2]

**Status:** not started

**Files.** `scripts/manifest.schema.json` (new), the three converters,
`scripts/enrich_chunks.py`, `SKILL.md`, tests.

**Steps.**

1. Write `scripts/manifest.schema.json` (JSON Schema 2020-12).
   - `required` is exactly `check_corpus.REQUIRED_FIELDS`, and
     `additionalProperties: true`.
   - Describe every field the converters write: `source_pdf`, `title`,
     `slug`, `doc_id`, `version`, `version_and_later` (`const: true`),
     `page_count` (integer ≥ 1), `toc` (items `{level, title, page}`),
     `sections`, `full_md_chars`, `extractor`, `attribution` (T06),
     `schema_version` and `converter`.
   - Section items: `file` (pattern `^sections/[^/]+\.md$`), `heading`,
     `level`, `page_start`/`page_end` (integer or null), `command` (string or
     null), `entity`, `breadcrumb`, `confidence` (enum
     `anchored|page|none`), and `chars` (optional, for old manifests).
   - Use only `type`, `required`, `properties`, `items`, `enum`, `const`,
     `minimum`, `pattern` and `additionalProperties`, so a small validator
     can check it.
2. Every converter writes `"schema_version": 1` and:

   ```json
   "converter": {"script": "convert_manual.py", "extractor": "pymupdf4llm",
                 "extractor_version": "1.28.2", "converted_at": "2026-10-07T12:00:00",
                 "owns_breadcrumbs": false}
   ```

   - Take the version from `importlib.metadata.version("pymupdf4llm")`, or
     `"docling"` for Docling.
   - `owns_breadcrumbs` is `true` for `rebuild_reference.py` and
     `convert_docling.py`.
   - Keep Docling's top-level `"extractor": "docling"` for older readers.
3. **`enrich_chunks.converter_owns_breadcrumb`:** take the section and the
   manifest. If the manifest has `converter.owns_breadcrumbs`, use it.
   Otherwise fall back to today's section-field rule (`command` or
   `confidence` present), so manifests written before this keep working.
4. **`requirements.txt`:** leave it as it is, with no upper bound. An upper
   bound would also block fixes. Recording the version is the fix
   IMPROVEMENT.md proposes, and T14's test catches the one behaviour change
   that would matter.
5. **Tests.**
   - Put a ~40-line validator for that subset of keywords in
     `tests/_support.py`.
   - Validate every manifest the suite produces: corpus, figures, gadgets,
     hand.
   - Assert the schema's `required` equals `check_corpus.REQUIRED_FIELDS`.
   - Assert enrich still leaves `ref`'s breadcrumbs alone (test_12) and
     still rewrites `prose`'s.

**Done when**
- [ ] Every converter's manifest validates and carries `converter`.
- [ ] SKILL.md's contract section points at the schema.

---

### T21: Export chunks as JSONL [3.3]

**Status:** not started

**Files.** `scripts/export_chunks.py` (new), SKILL.md ("Bundled scripts",
"Where this skill stops"), README.md, tests.

**Steps.**

1. `python scripts/export_chunks.py --root <corpus> --out chunks.jsonl [--current-only]`.
2. Reuse `build_search_db.load_documents`, `resolve_editions` and
   `read_chunk` by importing `build_search_db`. Since T09, `resolve_editions`
   is keyed by `(collection key, slug)`, and `load_documents` has already set
   each manifest's `slug` to its folder name (T07). Stop on the same failures:
   unreadable manifests, unorderable editions. Also stop on unreadable
   section files: an export with holes is worse than none.
3. Write one JSON object per line, with `ensure_ascii=True`:

   ```json
   {"id": "tessent/tshell-ref-2026-2/sections/0042-set-x.md", "collection": "tessent",
    "slug": "tshell-ref-2026-2", "doc_id": "tshell-ref", "version": "2026.2",
    "version_and_later": false, "is_current": true, "title": "Tessent Shell Reference Manual",
    "file": "sections/0042-set-x.md", "ord": 41, "heading": "set_x",
    "breadcrumb": "Tessent Shell Reference Manual › set_x", "page_start": 310, "page_end": 311,
    "entity": "set_x", "confidence": null, "text": "*Tessent Shell Reference Manual › set_x*\n\n..."}
   ```

   `entity`, `confidence`, `version`, `page_start` and `page_end` are `null`
   when the manifest has none. `text` is the chunk file exactly as on disk.
4. **Test:** export `WS.corpus()`. The line count equals the number of
   sections. Every line parses. Every `id` is unique. `text` equals the file.
   With `--current-only` on a fresh copy of `WS.gadgets()`, only
   `gadget-2026-1` appears.

---

### T22: Warn when the index is stale [3.4]

**Status:** not started

**Why.** The server already warns when its index is partial, on the principle
that a missing result must not pass for a complete one. A stale index fails
the same way, and SKILL.md notes that people skip the rebuild.

**Files.** `scripts/build_search_db.py`, `scripts/mcp_server.py`, `SKILL.md`
("Serving the corpus over MCP"), tests.

**Steps.**

1. **Build:** add a table,
   `sources (path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, sha256 TEXT NOT NULL)`.
   Add one row, with paths relative to the corpus root (POSIX), for:
   - every indexed `manifest.json`
   - every `figures.json` that exists
   - every collection's `current_versions.json` that exists
2. **Server:** add `Corpus.staleness_warning()`, computed once per process
   and cached.
   - Return `""` if there is no `sources` table, or if none of the recorded
     files exist (the index was copied without its corpus; nothing can be
     told).
   - Otherwise a file has **changed** when it is missing, its size differs,
     or its mtime differs *and* its SHA-256 differs. Hashing only on an mtime
     change keeps a synced copy whose mtimes moved from warning when nothing
     changed.
   - **Added** documents: list each collection's `docs/*/` with the server's
     own copy of the skip rule (T07). Count any document whose manifest is
     not in `sources`. Do the same for a `current_versions.json` that
     appeared.
   - The warning text:
     `\n\n> **Stale index:** {n} document(s) changed on disk since this index was built ({first three}). Answers may not match the corpus. Rebuild with build_search_db.py.`
   - Append it wherever `coverage_warning()` is appended.
3. Add the server's skip-rule copy to T07's cross-check test.

**Tests.** `test_42_a_stale_index_says_so`. Build an index over a fresh copy
of `WS.corpus()`, then use a new `Corpus` instance for each step:
- No changes: no warning.
- Change a manifest's title on disk: the warning names that slug.
- Restore the exact bytes but keep the new mtime: no warning.
- Add a document folder: a warning.
- An index without the table (run `DROP TABLE sources` on a copy, which is
  what an index built before this task looks like): no warning, no error.

**Done when**
- [ ] The warning appears in `search_docs` and `list_documents` output when
      stale, and nowhere otherwise.
- [ ] SKILL.md says so.

---

### T23: One `collection.json` (not now) [3.6]

**Status: not now. Skip this task.** IMPROVEMENT.md §3.6 records the
decision. The bug in the item, `superseded.json` going unvalidated, is fixed
by T04. Consolidation pays only if the old files stop being read, and that
means migrating every existing corpus to save three small files. The design
below is kept for when a fourth per-collection setting makes it worth it.

**Design (for later).**
- `<collection>/collection.json` is
  `{"label": "...", "pins": {"<doc_id>": "<version>"}, "superseded": [{"file": ..., "superseded_by": ...}]}`,
  with every key optional.
- One loader, `editions.load_collection(collection) -> dict`:
  - It reads `collection.json` if present.
  - Otherwise it reads the three old files (`vendor.json`,
    `current_versions.json`, `superseded.json`).
  - If both are present, it raises `ValueError`: two sources for one setting
    is the drift this removes.
  - It validates everything `check_corpus.py` validates today.
- `load_pins`, `build_index.load_superseded` and `load_vendor_label` become
  thin readers of it.
- `editions.py migrate-config <collection> [--dry-run]` writes
  `collection.json` from the three files and moves them to
  `.rebuild-backup/`.
- The checker gets its own reader (independence), and a
  `collection-config-invalid` FAIL.
- T22's `sources` records `collection.json` too.

**Tests.** Old files only; new file only; both present (refused); migrate
round-trip (index.json and the search index unchanged after migrating).

---

### T24: Remove accidental duplication [4.3]

**Status:** not started

**Rule for this task: behaviour must not change.** Every merge comes with an
equivalence test: the old and the new function agree on a list of inputs.
The list includes non-ASCII text, emphasis, numbering and empty strings. If
they disagree on any input, don't merge that one: name the two functions
apart and note why in the code.

Deliberate duplication stays: `check_corpus.py`'s and `mcp_server.py`'s own
copies, each held by a cross-check test.

1. **The console block** that sets UTF-8 on stdout and stderr: add
   `_common.utf8_console()`. Call it where each script does this today,
   keeping module level as module level and `main()` as `main()`. This
   applies to every script except `mcp_server.py` (which also sets
   `newline="\n"`) and `check_corpus.py`.
2. **`BREADCRUMB_SEP`:** define it once in `_common.py`. `enrich_chunks.py`
   imports it under the same name: `rebuild_reference.py` reads
   `ec.BREADCRUMB_SEP`. `convert_docling.py` imports it. `check_corpus.py`
   keeps its own, and the cross-check test compares the two.
3. **`slugify`** is defined four times, with different rules:
   - `convert_manual` (drops `*`, `_` and backticks first, max 60, `"section"`)
   - `convert_docling` (after its `normalize`, max 60, `"section"`)
   - `editions` (no limit, no default)
   - `build_search_db` (collection keys, `"corpus"`)

   Write one `_common.slugify(text, maxlen=None, default="")` core. Each
   caller keeps its own preprocessing and default. Prove each caller's old
   and new outputs equal on the input list. Expect `build_search_db`'s to
   differ on characters whose lowercase is not ASCII (`"İ"`), because it
   lowercases after substituting. If so, keep its order.
4. **`find_collections`** is defined twice:
   - `editions.find_collections` returns paths; root `docs/` wins.
   - `build_search_db.find_collections` returns `(key, display, docs)` and
     needs a document present.

   Make the rule one rule in `editions.find_collections`: a collection is a
   folder whose `docs/` holds at least one document dir, and the root is one
   collection when its own `docs/` does. Have `build_search_db` derive key
   and display from it. Say in the commit message that `editions.py status`
   on a root whose `docs/` is empty now reports no collections.
5. **`strip_emphasis` / `normalize`:** move `enrich_chunks.strip_emphasis`
   to `_common.py`. `convert_docling.normalize` uses it for its first step
   (the heading marks and emphasis loop are the same code). Keep each
   function's own prefix and numbering rules: they differ, and breadcrumb
   matching was measured with them.
6. **Import style:** `rebuild_reference.py` loads its siblings with
   `importlib`. Change it to the `sys.path.insert` + `import` that every
   other script uses, keeping the `cm`, `ec` and `editions` names.

**Done when**
- [ ] Converting the fixtures before and after gives identical `docs/`
      trees.
- [ ] Every merge has its equivalence test.

---

### T25: Consistent command lines [4.4]

**Status:** not started

**Steps.**

1. **Accept a corpus root everywhere.** Use one rule: when `<path>/docs` is a
   folder, the path is one collection; otherwise loop over
   `editions.find_collections(path)`.
   - Apply it to `build_index.py`, `enrich_chunks.py`, `extract_figures.py`
     and `ocr_figures.py`. `--only`/`--skip` apply across collections.
   - `build_index.py` writes each collection that has no problems, reports
     the ones that do (writing nothing for them), and exits 1 if any did.
   - `build_search_db.py` also accepts the root as a positional argument.
     Keep `--root`, and reject both together.
2. **`rebuild_reference.py --slug` optional.**
   - When it is omitted, derive the slug as the other converters do:
     `editions.default_slug(editions.slugify(pdf.stem) or "document", version)`.
     `version` is `editions.split_version(args.version)[0]`, or
     `editions.read_cover(pdf).version`.
   - Print the derived slug. The rest (prior manifest, `--replace`) works
     from that slug.
   - This fixes the trap where a hand-picked slug for a second edition does
     not end in the version suffix `default_doc_id` strips, so the editions
     get different `doc_id`s and both answer every search.
3. **Docs.** Update SKILL.md's "Adding one document later" and every usage
   line. `rebuild_reference.py` usage no longer needs `--slug`.

**Tests.**
- `build_index.py <root>` on a two-collection root writes both `index.json`
  files.
- `enrich_chunks.py <root>` processes both collections.
- `rebuild_reference.py new_docs/widget_ref.pdf --title ...`, with "Software
  Version 2026.1" on the cover (adapt `edition_reference_fixture` with a
  cover page), gives `docs/widget-ref-2026-1` with `doc_id` `widget-ref`.

---

### T26: Server robustness and tool metadata [4.5]

**Status:** not started

**Files.** `scripts/mcp_server.py`, `scripts/mcp_smoke_test.py`,
`scripts/eval_search.py`, tests.

**Steps.**

1. **A message that is not a JSON object kills the server.** Verified: the
   input `[1,2]` raises `AttributeError` at
   `req_id, method = msg.get("id"), msg.get("method")`, outside any `try`.
   The process exits, so the next request is never answered.
   - In `main()`, after `json.loads`: if `msg` is not a dict, write
     `{"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request: expected one JSON-RPC object (batches are not supported)"}}`
     and `continue`.
   - If `params` is present and not a dict, answer `-32602` "Invalid params".
2. **Declare the tools read-only.** Give each of the eight tools in
   `build_tools()` a `title`, plus
   `"annotations": {"readOnlyHint": True, "openWorldHint": False}`. Both are
   in MCP revision 2025-06-18, which the server negotiates; older clients
   ignore unknown fields. Titles:
   - search_docs: "Search the documentation"
   - get_section: "Read a section"
   - lookup_entity: "Look up an entry"
   - list_documents: "List documents"
   - get_toc: "Table of contents"
   - compare_versions: "Compare editions"
   - get_figure: "Show a figure"
   - get_page_image: "Show a PDF page"
3. **`lookup_entity` can return several times its budget.** Move
   `budget = MAX_SECTION_CHARS` before the `for hit in hits` loop. When it
   runs out between hits, append
   ``…[{n} more document(s) have this entry: {slugs}. Pass `document` to read one.]``
   and stop.
4. **`get_section` can match the wrong file.** `file LIKE ?` with
   `f"%{needle}"`: `_` is a wildcard, and nearly every filename has one, so
   `sections/0001-set_x.md` matches a file `sections/0001-setax.md` when no
   exact file exists.
   - Add `like_escape(s)`, which escapes `\`, `%` and `_` with `\`.
   - Query `file = ? OR file LIKE ? ESCAPE '\'` with
     `"%/" + like_escape(needle)`. The `/` also stops `02-x.md` matching
     `002-x.md`.
   - Do the same in `eval_search.missing_answers`.
5. **The smoke test.** Give `Client` a `send_raw(line)` method. After the
   handshake, send `[1,2]`: expect an error object with `id: null` and code
   `-32600`. Then send `ping`: expect it answered. Check that
   `tools/list`'s tools all carry `annotations.readOnlyHint`.
6. Bump `SERVER_VERSION` to `"1.1.0"`.

**Tests.**
- The smoke-test additions run in test_10 and test_13.
- `test_43_a_file_name_is_not_a_pattern`: a hand-built document whose only
  section is `sections/0001-setax.md`. `tool_get_section({"file": "sections/0001-set_x.md"})`
  raises "No section matching".
- `test_44_lookup_budget_covers_the_whole_response`: convert
  `edition_reference_fixture` twice into one collection with
  `rebuild_reference.py`, under different `--doc-id`s, so both are current.
  Its `LONG_ENTRY` runs past 40,000 characters. `lookup_entity` on
  `LONG_ENTRY` returns at most `MAX_SECTION_CHARS` plus a few hundred
  characters of headers, and names the second document as not shown.

**Done when**
- [ ] The server survives `[1,2]`, a bare string and a number, and answers
      the next request.
- [ ] All four sub-items are tested.

---

## 4. Measurements owed

The rows below are pre-filled for every measurement this plan already
knows needs the user's corpus or an optional tool. Fill in the result and
date, or write "not measured:" and the reason. Add a row for any other step
you could not run. Never write a number you did not measure.

| task | what to measure | how | result | date |
|---|---|---|---|---|
| T05 | Do Docling's later chunks list the title first? | the snippet in T05, on a real manual | not measured: Docling is not installed here and needs several GB; the claim rests on docling-core's source, as T05 says | 2026-10-07 |
| T05 | Docling anchored share, after the fix | rerun `references/extractor-benchmark.md`'s method | not measured: needs Docling and the benchmark's corpus slices, neither here; the benchmark file carries a dated note that its numbers predate the fix | 2026-10-07 |
| T08 | Lines no longer deleted as furniture | `enrich_chunks.py --dry-run --list-furniture`, old vs new, diffed | not measured: no real corpus here. Run `enrich_chunks.py <collection> --dry-run --list-furniture` with `TITLE_SHARE = 0` (the old rule) and with 0.6, and diff; for references, reconvert one and run `check_corpus.py` (its `furniture` check) | 2026-10-07 |
| T10 | Search change from whole-heading front matter | `eval_search.py --json`, then `--compare` | not measured: needs the user's corpus and its eval questions. Rebuild the index with this commit (`noise` is stored at build time, so an existing index does not change) and compare with a run from before using T12's `--compare` | 2026-10-07 |
| T11 | Identifier hit@1 and every rank moved | `eval_search.py --json`, then `--compare` | not measured: needs the user's corpus and its eval questions. Run `eval_search.py --json` on an index built before this commit and on one built after, compare with T12's `--compare`, and read every question whose rank moved | 2026-10-07 |
| T14 | Strict check and figure links after reconverting prose | the T14 real-corpus check | not measured: needs two or three of the user's prose manuals. Reconvert them, run `check_corpus.py --strict` and the content check, and compare `extract_figures.py`'s summary (links by caption, page and context, and how often each fallback agrees with the caption) with the old conversion | 2026-10-07 |
| T16 | Search change from chain breadcrumbs | `--compare` after reconverting the references | not measured: needs the user's two references and eval questions. Breadcrumbs are indexed with weight 3: `eval_search.py --json` before reconverting and after, then `--compare` | 2026-10-07 |
| T18 | Each experiment flag against the default | `--compare` | not measured: needs the user's corpus and eval questions. Build an index with each flag and one without, run `eval_search.py --json` on each and `--compare`; adopt a flag only if identifier hit@1 rises and no kind falls | 2026-10-07 |

## 5. Found along the way

Problems noticed while executing that no task covers. Add a line, don't fix
it.

- CI warns that `actions/checkout@v4` and `actions/setup-python@v5` target
  Node.js 20, which GitHub has deprecated; for now it runs them on Node.js
  24. Harmless today. Bump them to the releases that target Node.js 24 when
  convenient, and check the next run stays green.
