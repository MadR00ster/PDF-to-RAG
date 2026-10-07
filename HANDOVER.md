# Hand-over: carrying out TODO.md

Written 2026-10-07 for the next session. The plan in `TODO.md` has been carried out;
see "Where things stand". The rest of this file is how the work was done and what it
learned, which a later change to this code will want.

## Where things stand

Updated 2026-10-07, after the session that carried out TODO.md.

- **Branch:** `claude/wizardly-maxwell-6r2cni`, based on `main` at `0e4dd59`.
  No pull request is open; don't open one unless the user asks.
- **Done:** every task in TODO.md except **T23**, which was decided against.
  Each is marked done in TODO.md with its checkboxes ticked, one commit apiece,
  CI green on every push (Linux and Windows, Python 3.10 and 3.13).
- **Tests:** 73, all passing, with 2 skipped (no Tesseract language data, no
  Docling). `python tests/run_each.py` passes too: every test alone.
- **Owed, and why:** TODO.md section 4 lists the measurements that need the
  user's corpus (search comparisons for T10, T11, T16 and T18, furniture for
  T08, a prose reconversion for T14, Docling for T05). Each row says "not
  measured:" with what to run. None was invented. Section 5 lists what turned
  up along the way, including that Docling itself was never run.
- **Next:** nothing in the plan. Whatever the user does with section 4's
  numbers is next: T18's flags and T14's routing advice ("under review" in
  SKILL.md) wait on them.

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

## Structure after the whole plan

- `scripts/_common.py` holds what several scripts share, standard library only:
  `pick_command_level`, `detect_shape`, `looks_like_entry`, `slugify`,
  `strip_emphasis`, `BREADCRUMB_SEP`, `utf8_console`. `check_corpus.py` and
  `mcp_server.py` still import nothing from `scripts/`; their copies of a rule
  are each held to the shared one by a test (test_31b, test_37, test_47, test_50).
- `convert_manual.py` is both pymupdf4llm converters: `convert` (prose) and
  `convert_reference`, chosen by `--shape`. `rebuild_reference.py` is a 64-line
  wrapper that keeps its command line. Both read pages with `page_markdown`,
  strip furniture with `join_pages`, cut with `chunk_spans` and read a chunk's
  pages with `trim_span` and `page_range`: a chunk is on the page its first word
  is, not the one its leading newline is on.
- Every manifest carries `schema_version` and `converter`
  (`scripts/manifest.schema.json`); no converter writes `chars`.
- `editions.collections_under(path)` is how every command line turns a path
  into collections, and `editions.find_collections` is the one rule for what a
  collection is.
- The search experiments (`--body-without-breadcrumb`, `--ident-index`) are
  flags and off. Changing a default is the user's decision, from section 4.

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
