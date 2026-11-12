# Hand-over: working on this repo

For whoever changes this code next. `SKILL.md` and `references/` say what the
pipeline does and why; this file says how the code is laid out, how to work on
it, what bit earlier sessions, and what is still open.

## Where things stand

Updated 2026-11-10.

- `main` has every merged pull request (#1–#15). 130 tests, 1 skipped where
  Tesseract's language data is missing; `python tests/run_each.py` passes too.
- The October 2026 review (`IMPROVEMENT.md`) and the plan that carried it out
  (`TODO.md`, tasks T01–T26) are finished and were deleted on 2026-11-09. Read
  them in git history at `935af75`. What they left open is under "Open work"
  below; T23 (one `collection.json` per collection) was decided against, and
  its design is in that history for when a fourth per-collection setting makes
  it worth it.
- `ROADMAP.md` is the shared answering server. Its HTTP transport is built;
  the rest waits on the user.

## How to work

1. One change, one commit (or a few small ones). The full suite passes before
   each commit, and `python tests/run_each.py` passes before a push that adds
   tests. The suite takes about 8 minutes serially; `python tests/run_parallel.py`
   runs the test files two at a time (about 7 minutes on a 32-core Windows
   machine, slower than serial on a 2-core CI runner; its docstring has the
   numbers).
2. Work on a branch and open a pull request against `main`. CI runs Linux and
   Windows, Python 3.10 and 3.13. A red cell, Windows included, is yours to fix.
3. A bug fix comes with a test that fails without it. Show that it fails:
   stash `scripts/` (`git stash push -- scripts`), run the new test, pop.
4. Never write a measurement you did not take. Steps that need the user's
   corpus go under "Measurements owed" below.
5. Put no model names in commits, code or docs.
6. The user's production corpus runs this repo's server and scripts, never
   bespoke copies. Changing it needs the user's go-ahead.

## Structure

- `scripts/_common.py` holds what several scripts share, standard library only:
  `pick_command_level`, `detect_shape`, `looks_like_entry`, `slugify`,
  `strip_emphasis`, `BREADCRUMB_SEP`, `utf8_console`.
- **Rules that exist twice on purpose.** `check_corpus.py` and `mcp_server.py`
  import nothing from `scripts/`: the checker so a converter bug cannot pass
  itself, the server so it is one standard-library file that ships beside its
  index. Each copy of a rule is held to the shared one by a test:
  - the skip rule: `editions.skips_name`, `check_corpus.index_skips` and the
    server's (test_31b)
  - the collection key: `build_search_db.slugify` and
    `check_corpus.collection_key` (test_35c)
  - the version rule (test_20b), and the rest in test_37, test_47 and test_50.
- **Documents are (collection, slug).** `Corpus.document(collection, slug)`,
  `document_label`, `section_figures` and `entry_lines` all take both, and
  `search()` raises if given a `document` without its `collection`. A query
  added to `mcp_server.py` that narrows by slug must narrow by collection too.
- **The folder name is the slug.** `build_search_db.load_documents` sets
  `manifest["slug"]` from it and warns when the manifest's copy differs. Walk
  `docs/` with `editions.document_dirs(docs)`, never
  `glob("*/manifest.json")`.
- **Converters write to staging.** All three build in
  `editions.staging_dir(out_root, slug)`, then call
  `editions.publish(staging, out_dir)`, then `editions.finish(plan)`. New
  converter code follows the same order and never writes into `out_dir`.
- `convert_manual.py` is both pymupdf4llm converters: `convert` (prose) and
  `convert_reference`, chosen by `--shape`. `rebuild_reference.py` is a thin
  wrapper that keeps its command line. Both read pages with `page_markdown`,
  strip furniture with `join_pages`, cut with `chunk_spans` and read a chunk's
  pages with `trim_span` and `page_range`: a chunk is on the page its first
  word is, not the one its leading newline is on.
- `backfill_pages.py` gives a document converted without pages its `page_start` and
  `page_end` from the PDF's text, writing only the manifest (backup first). A
  chunk too short to match takes its next neighbour's page and carries
  `pages_from: neighbour`; `check_corpus.py` does not hold that page to the
  outline (test_18b).
- Every manifest carries `schema_version` and `converter`
  (`scripts/manifest.schema.json`); no converter writes `chars`.
- `editions.collections_under(path)` is how every command line turns a path
  into collections, and `editions.find_collections` is the one rule for what a
  collection is.
- The search experiments (`--body-without-breadcrumb`, `--ident-index`) are
  flags, off. Changing a default is the user's decision, on measurements.

## Tests

- `tests/_support.py` has the fixture writers and `WS`, a per-process
  workspace. It builds `corpus()`, `index()`, `fig_corpus()`, `hand_corpus()`
  and `gadgets()` the first time a test asks for them.
- Read what `WS` gives you; never change it. To change something, copy it
  first with `WS.fresh(src, name)`. For a new collection holding fixture
  PDFs, use `WS.collection(name, "prose", "ref", ...)`.
- A test that checks a step runs that step itself, in its own folder.
- New tests go in the file for their area: `test_converters.py`,
  `test_server.py`, `test_figures.py`, `test_checker.py`, `test_editions.py`,
  `test_update.py`, `test_usage.py` (the call log's triage and answer
  grading), `test_backfill.py`, `test_check_script.py` or `test_http.py`.
- **Test numbers.** Each test's number is unique across files; pick the next
  free one in the area's range and keep its letter series together: 53–53m
  `update_corpus.py` (53h, on `editions.waiting_collections`, is in
  `test_editions.py`); 54–54b the server's call log; 55–55c `triage_log.py`;
  56–56c `eval_answers.py`; 57–58f `check_script`; 59–59j the HTTP transport;
  60–60l `get_section`'s passages and `eval_search.py --cost`; 61–61h
  `backfill_pages.py` (18b, the checker's side of its flag, is in `test_checker.py`).
  22d is the server copy only the index it serves may refresh; 22e is the
  editor entry keeping its `env` across a rebuild; 25g is
  `editions.publish` putting the old document back.
- test_53i reads the bash blocks under README's "The same, step by step" and
  SKILL.md's "Adding one document later": a step named there that
  `update_corpus.py` does not run fails it.

## Things learned the hard way

- **A build rewrites the server copy at the corpus root.** Scoring a candidate
  with `build_search_db.py --root <live corpus> --out <scratch>` used to
  replace the live `mcp_server.py` while the live index stayed old. "Inside
  the corpus" was not enough either: `update_corpus.py`'s candidate sits beside
  the live index, so the copy was new for the whole smoke test and eval, and
  stayed new if the run was interrupted. Now only a build of the index the copy
  serves refreshes it (`build_search_db.copy_serves`: the default index, or the
  one `.vscode/mcp.json` opens with the copy; test_22d), or
  `--emit-vscode-config` asks for it; `update_corpus.py` refreshes it after the
  swap. Without `--out`, the build still replaces the live index;
  `update_corpus.py` is the gated way to do that.
- **`editions.publish` is two renames.** Between them `docs/<slug>/` does not
  exist; if the second fails (Windows, a file held open), the old document is
  renamed back before the error is raised (test_25g).
- **Windows.** CI is the only Windows machine. Folder names with a trailing
  space are invalid there. Names differing only in case are one folder.
  `tempfile` paths come back in 8.3 short form (`RUNNER~1`), so compare paths
  with `os.path.realpath`, never as text.
- **Editing with a script.** When a scripted edit replaces text, assert that
  each `old` string matches exactly once. If you rerun such a script, guard
  against replacements whose new text contains the old: one edit went in twice
  that way, caught by reading the diff.
- **Prove "nothing else changed" with a diff.** Convert every fixture PDF
  with the code before and after (`git stash push -- scripts` to get the old
  code) and `diff -r` the `docs/` trees.
- **pymupdf4llm's whole-document markdown** is its page-chunked markdown
  joined. That holds by construction in 1.28.2; the prose converter relies on
  it and a test pins it.

## Open work

### Measurements owed

Each needs the user's corpus or an optional tool, so none was taken. Never
fill one in with a number you did not measure.

| change | what to measure | how |
|---|---|---|
| Docling chunks use their own heading | Do later chunks still list the title first? Docling's anchored share after the fix | Convert a real manual with `convert_docling.py`; rerun `references/extractor-benchmark.md`'s method. Needs Docling (several GB) |
| Short lines that begin the title are kept | Lines no longer deleted as furniture | `enrich_chunks.py <collection> --dry-run --list-furniture` with `TITLE_SHARE = 0` (the old rule) and with 0.6, diffed; for references, reconvert one and run `check_corpus.py` |
| Page numbers on the light prose path | Strict check and figure links after reconverting prose | Reconvert two or three prose manuals; `check_corpus.py --strict`, the content check, and `extract_figures.py`'s summary against the old conversion |
| Reference breadcrumbs from the TOC chain | Search change | `eval_search.py --json` before and after reconverting the references, `--compare` |
| README's "Connecting a corpus to Claude chat" | Whether claude.ai accepts `mcp_server.py --http` with an `Authorization` request header and completes the handshake; whether the desktop-app entry lists the tools. Both were written from Anthropic's docs, not tried | Needs a public HTTPS URL and an organization that has request headers (beta); for the desktop app, a Windows machine with Claude installed |

Two decisions wait: whether `--ident-index` becomes a default (measured
2026-11-12, see `references/retrieval-measurement.md`), and the prose routing
SKILL.md marks "under review" (Docling against `convert_manual.py`, now that
both carry pages).

### Known issues

Noticed, not fixed. Fix one with a test, and delete its line here.

- `convert_docling.py` has never run on a real document here: it is checked
  only by importing it and by unit tests of its pure functions. Convert one
  real document with it and read the output before relying on it.
- Run with `python -W default`, the suite prints about 18 `ResourceWarning: unclosed
  database` lines: a test (or the code it drives) opens a SQLite connection and
  leaves it to the garbage collector.
- `ocr_figures.py` has no test that runs where Tesseract's language data is
  missing, which includes CI.
- `eval_answers.py` runs `claude -p` with the user's own Claude Code
  configuration (CLAUDE.md, memory, plugins, hooks) unless `--bare` is passed,
  and asks its questions one at a time.
- Over HTTP, a call-log line has no client name (only `initialize` carries it
  and no session is kept); it has a `token_id` instead.
- Ten older PDFs in production are not converted (the entries of its two
  `superseded.json` files). Converted in the work copy 2026-11-13 with
  `--prose-converter convert_manual` (`references/retrieval-measurement.md`,
  "The ten older editions"); production still waits for the user's go-ahead.
  Three things stop or blur that update: `check_corpus.py --strict` FAILs
  `tshell-ref-2022-4` (3 chunks of the "Core Mapping ... (R Rules)" section are
  owned by the rule section before it, because the PDF's text writes that
  heading garbled); the index shifts BM25 scores of current editions by adding
  chunks of older ones (8 of 146 questions changed rank, none from an older
  edition); and the older reference editions keep page furniture the current
  one lost (`<mark>NNNN</mark>` page labels, "Command Dictionary (...) name"
  footers), which `compare_versions` shows as differences.
