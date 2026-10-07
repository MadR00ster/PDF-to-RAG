# Hand-over: carrying out TODO.md

Written 2026-10-07 for the next session, which picks up the plan in
`TODO.md` and carries out the rest of it. Read this file first, then TODO.md
§0 to §2, then start at T03.

## Where things stand

- **Branch:** `claude/wizardly-maxwell-6r2cni`, based on `main` at `0e4dd59`.
  Develop and push here. No pull request is open; don't open one unless the
  user asks.
- **Documents:**
  - `IMPROVEMENT.md` is the review: what is wrong and why. It is corrected
    where it was wrong; look for **Amended** and **Decided**.
  - `TODO.md` is the plan: one task per review item, with the code to
    change, the tests and a "Done when" list. The two documents agree.
  - This file is the state of play.
- **Done** (each marked done in TODO.md, with its checkboxes ticked):
  - **T02**: CI. `.github/workflows/tests.yml` runs the suite on Linux and
    Windows with Python 3.10 and 3.13 on every push. Its first run found a
    Windows-only bug, fixed in `b5c3172` (editor config paths compared as
    text).
  - **T01**: the tests are split by area. `tests/_support.py` holds the
    fixtures and `WS`, and every test passes alone (`python tests/run_each.py`).
  - **T07**: conversions are built in `.rebuild-backup/.staging/<slug>/` and
    moved into `docs/` whole. Every script skips `.old`/`.new`/dot/underscore
    folders, and the folder name is the document's slug.
  - **T09**: documents are keyed by (collection, slug) in the index, the
    server, the eval, the sampler and the smoke test.
- **Tests:** 44, all passing, with 2 skipped (no Tesseract language data, no
  Docling). About 85 s for `python -m unittest discover -s tests -v`.
- **CI:** green on every push since `b5c3172`.
- **Next:** T03, then on in TODO.md §2's order. **Skip T23**, as decided.
  Every other gate is settled: see the "Decisions already made" table in
  TODO.md §0. Nothing should need the user except the measurements in §4.

## How to work

1. One task, one commit (or a few small ones). The full suite passes before
   each commit, and `python tests/run_each.py` passes before a push that adds
   tests.
2. Tick the task's boxes and set its **Status** in TODO.md in the same commit.
3. Push to the branch, then read the CI run with the GitHub MCP tools
   (`actions_list` with `list_workflow_runs`, filtered to this branch). A red
   cell, Windows included, is yours to fix before the next task.
4. A bug fix comes with a test that fails without it. Show that it fails:
   stash `scripts/` (`git stash push -- scripts`), run the new test, pop.
5. Never write a measurement you did not take. Steps that need the user's
   corpus go in TODO.md §4.
6. Put no model names in commits, code or docs. End commit messages with the
   attribution lines your session's instructions give.

## What changed under the plan's feet

TODO.md was written before T01, T07 and T09 landed. Where it quotes code
those tasks changed, find the current code by meaning, not by the quoted
text:

- **Documents are (collection, slug) now.** `Corpus.document(collection,
  slug)`, `document_label(collection, slug, title)`, `section_figures(collection,
  slug, ord)` and `entry_lines(doc_row, name)` all changed signature.
  `search()` raises if given a `document` without its `collection`. Any query
  you add to `mcp_server.py` that narrows by slug must narrow by collection
  too: T22's staleness check and T26's `lookup_entity` budget both touch this
  code.
- **The folder name is the slug.** `build_search_db.load_documents` sets
  `manifest["slug"]` to the folder name in memory, and warns when the
  manifest's copy differs. Walk `docs/` with `editions.document_dirs(docs)`,
  never with `glob("*/manifest.json")`.
- **Converters write to staging.** All three build in
  `editions.staging_dir(out_root, slug)`, then call
  `editions.publish(staging, out_dir)`, then `editions.finish(plan)`. New
  converter code (T14, T15) must follow the same order and never write into
  `out_dir` directly.
- **Rules that exist twice on purpose**, each held together by a test:
  - The skip rule: `editions.skips_name` and `check_corpus.index_skips`
    (test_31b). T22 adds the server's copy; add it to test_31b.
  - The collection key: `build_search_db.slugify` and
    `check_corpus.collection_key` (test_35c).
  - The version rule (test_20b).
- **`duplicate-slug` is gone** from `check_corpus.py`. `collection-clash`
  replaces it, and `hidden-document` is now a warning.

## Tests: how the suite is laid out

- `tests/_support.py` has the fixture writers and `WS`, a per-process
  workspace. It builds `corpus()`, `index()`, `fig_corpus()`, `hand_corpus()`
  and `gadgets()` the first time a test asks for them.
- Read what `WS` gives you; never change it. To change something, copy it
  first with `WS.fresh(src, name)`. For a new collection holding fixture
  PDFs, use `WS.collection(name, "prose", "ref", ...)`.
- A test that checks a step runs that step itself, in its own folder.
- New tests go in the file for their area: `test_converters.py`,
  `test_server.py`, `test_figures.py`, `test_checker.py` or
  `test_editions.py`.
- **Test numbers already taken:** 27–30 are reserved for T03–T06 and 34 for
  T08. 31, 31b, 32, 33, 35, 35b and 35c are used by T07 and T09. TODO.md's
  later tasks name 36–44; keep those.

## Things learned the hard way

- **Windows.** CI is the only Windows machine. Folder names with a trailing
  space are invalid there. Names differing only in case are one folder.
  `tempfile` paths come back in 8.3 short form (`RUNNER~1`), so compare paths
  with `os.path.realpath`, never as text.
- **Editing with a script.** When a scripted edit replaces text, assert that
  each `old` string matches exactly once. If you rerun such a script, guard
  against replacements whose new text contains the old: one T07 edit went in
  twice that way, caught by reading the diff.
- **Prove "nothing else changed" with a diff.** Convert every fixture PDF
  with the code before and after (`git stash push -- scripts` to get the old
  code) and `diff -r` the `docs/` trees. T07 came out byte-identical that
  way, and T13, T15 and T24 all ask for the same proof.
- **pymupdf4llm's whole-document markdown** is its page-chunked markdown
  joined. That holds by construction in 1.28.2, so T14 relies on it and pins
  it with a test.

## Not settled, but not yours to wait on

- TODO.md §4 lists measurements on the user's corpus. Fill in what you can;
  for the rest, write "not measured:" and the reason.
- When asked, the earlier session suggested to the user that T18 (search
  experiments behind flags) and T24 (removing duplication) are the weakest
  value for their risk. The user has not changed the plan, so carry them out
  as TODO.md says: T18's flags stay off by default, and every T24 merge
  needs its equivalence test.
