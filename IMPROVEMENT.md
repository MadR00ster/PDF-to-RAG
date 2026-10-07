# Improvements

A review of the implementation, the methodology and the data layout, written
2026-10-07 against commit `0e4dd59`. Nothing here is built yet.

Each item says how it is known:

- **Verified**: reproduced here by running the code (Python 3.13,
  pymupdf4llm 1.28.2), or read from a dependency's source.
- **From reading**: found by reading the code and not reproduced. Check it
  before acting on it.

The test suite passes as it stands: 36 tests, 2 skipped (no Tesseract, no
Docling), 51 s.

## What to keep

These are why the repo works. None of the changes below should weaken them.

- **One rule for metadata: right or absent, never guessed.** Every
  converter, the checker and the server keep to it.
- **Every decision has a number behind it.** Stemming, the OR fallback,
  OCR, and the decision not to add embeddings were each measured, and the
  references record the measurements.
- **`check_corpus.py` shares no code with the converters.** A converter bug
  cannot pass its own check.
- **Editions refuse rather than approximate.** A release no edition covers
  gets an error, not the nearest edition.
- **The server is a single standard-library file.** It travels with the
  index.

---

## 1. Bugs and incorrect behaviour

### 1.1 Front-matter demotion catches real chapters — Verified

`build_search_db.py:167-192` (`is_noise`) flags a chunk as front matter when
its heading *starts with* `contents`, `index`, `feedback` or `about this`. A
flagged chunk gets +8.0 in `rank_adjust` (`mcp_server.py:491`), which
outweighs the largest single boost there (−6.0 for an exact entity match), so in
practice the chunk drops out of the top results.

```
'Indexing Options'             noise=True
'Index Types'                  noise=True
'Contents of the Install Kit'  noise=True
'Feedback Loops in PLLs'       noise=True
```

**Fix:** match the whole heading:
`^(contents|table of contents|index|feedback|list of (figures|tables))$`. Keep
`about this` as a prefix, since it only ever starts "About This Manual/Guide".
Keep the dot-leader test as it is. Rebuild the index, because `noise` is
stored at build time, then add the four headings above to a test.

### 1.2 The entity boost only fires when the whole query is the name — Verified

`mcp_server.py:462-476` joins *every* query word with `_` and compares the
result to the chunk's entity. The boost therefore needs the query to be the
bare identifier and nothing else:

```
'set_scan_configuration'                          -6.0
'set_scan_configuration -chain_count'             -2.0
'what does set_scan_configuration do'             +0.0
'-chain_count option of set_scan_configuration'   +0.0
```

Agents ask the last two kinds of question. Two smaller problems in the same
function:

- The prefix branch gives −2.0 to an entity called `set` for any query that
  begins with "set".
- `all(w in heading for w in words)` tests substrings and counts stopwords,
  so "i" and "a" match nearly every heading.

**Fix:**

- Pull identifier-shaped tokens out of the raw query (anything containing
  `_`, or starting with `-`) and boost a chunk whose entity equals one of
  them.
- For multi-word entities such as `tessent -shell`, compare against runs of
  adjacent query words.
- Drop the prefix branch.
- Match headings on whole words, using content words only.

**Measure it.** Identifier questions reach 100% hit@5 but only 85% hit@1
(`references/retrieval-measurement.md`). This fix targets exactly that gap,
and costs less than the reranker the roadmap plans for it. Run
`eval_search.py` before and after.

### 1.3 Docling chunks use the outermost heading, not their own — Verified in docling-core's source; not run end to end

`convert_docling.py:194` and `:258` both read `(c["headings"] or [""])[0]`.

In docling-core (2.100.0,
`transforms/chunker/hierarchical_chunker.py`), `headings` is sorted by level,
**outermost first**. A `TitleItem` takes level 0, and only headings at its own
level or deeper are removed when a new one arrives. So once Docling labels
the cover text as the title, `headings[0]` is the document title for every
chunk after it. That has three effects:

- No chunk can be anchored by its own heading.
- Every file is named after the title.
- The `### sub-heading` lines that `merge_sections` writes into a merged chunk
  never appear.

`test_09b` passes one-element heading lists, so it cannot catch this.

**Fix:** read `headings[-1]`. Add a test that passes
`["Widget Guide", "Syntax"]`. Then rerun the benchmark: the reported anchored
share (16–87%) may change. Docling is not installed here, so first confirm the
problem on one real PDF by printing `meta.headings` for a few chunks.

### 1.4 The page-number key no longer exists — Verified

`rebuild_reference.py:109-111` reads `metadata["page"]`. pymupdf4llm 1.28 (the
minimum `requirements.txt` allows) writes `page_number`, so every page number
comes from the fallback, `i + 1`. That is correct only while pymupdf4llm
returns every page, in order, with none skipped. If it ever skips one (a
`pages=` argument, or a blank page dropped by a future release), every page
after the gap is misnumbered and nothing reports it.

**Fix:** read `page_number`, then `page`, then fall back to `i + 1`. If the
number of pages returned differs from `doc.page_count`, stop.

### 1.5 The router and the reference converter disagree on what an entry is — From reading

`pick_extractor.py:264-273` counts a lowercase single word (`after`, `append`,
`foreach`) as an entry. `rebuild_reference.py:72-94` needs an `_`, a ` -`, or
a message code. So `pick_extractor` can send a Tcl-style command reference to
`rebuild_reference.py`, which then finds no command level. It prints
`command level LNone: 0 commands` and exits 0.

Declining is deliberate, and `test_06` expects it. The problem is that the
decline is easy to miss. The benchmark already hit this once: a manual that
reaches 90% attribution was reported at 0%.

**Fix:**

- Use one entry test and one level picker in both scripts.
- When nothing is attributed, keep declining, but print a `!!` line that
  says why.
- Record the decline in the manifest, for example `"attribution": "declined"`.
- Add a `--command-level N` option to override the picker.

### 1.6 Staging and backup folders leak into the index — From reading

- `rebuild_reference.py:272-274` creates `docs/<slug>.new/sections` with
  `exist_ok=True` and does not clear it first. Section files left by an
  interrupted run (named for a different chunk count) survive the rename as
  orphans.
- `.new` sits inside `docs/`. `build_index.py:35` and `editions.is_document_dir`
  skip it, but `build_search_db.py:243-264` and `enrich_chunks.py:428` glob
  `docs/*/manifest.json` with no filter. `check_corpus.py` reports this
  disagreement as `hidden-document` (FAIL) but does not prevent it.
- `convert_manual.py` and `convert_docling.py` write straight into the final
  folder, with no staging step at all.

**Fix:**

- Build in a staging folder that is cleared first and sits outside `docs/`
  but on the same filesystem, for example `.rebuild-backup/staging/<slug>`,
  so the final rename stays atomic.
- Have all three converters stage this way.
- Make every script that walks `docs/` use `editions.is_document_dir`.
- Decide which identifies a document, the folder name or the manifest's
  `slug`. `build_index.py` uses the folder and `build_search_db.py` uses the
  manifest field, and `slug-mismatch` exists only because the two can differ.
  Use the folder name, and keep the manifest field as a copy the checker
  compares against it.

### 1.7 Documentation and code that no longer match — Verified

- `SKILL.md:104-106` says `enrich_chunks.py` "deletes any line repeated across
  5% of sections (at least 10)". It does not. `min_count` is computed
  (`enrich_chunks.py:356`) and passed in, but `detect_furniture` never uses it.
  The only furniture it removes is "Feedback" and lines matching the
  document's title. The warning describes a risk the code no longer has; the
  risk it does have is 1.8.
- Dead code in `enrich_chunks.py:365-387`: `chars_before`, `chars_after` and
  `samples` are computed and never used.
- `convert_manual.py:28` points to "each vendor folder's CLAUDE.md", which
  does not exist. Point it to SKILL.md's "Chunking" section.
- `build_index.py:106` writes "EDA tool PDFs" into every collection's README,
  and `:86` strips " Manual" from the folder name. Both are left over from the
  corpus the repo was built on.
- `build_index.py:154,164` raise `KeyError` on a `superseded.json` entry with
  no `file` or `superseded_by`. `check_corpus.py` already checks those
  entries, and `build_index.py` should check them the same way.

### 1.8 Short lines that match the start of the title are deleted — From reading; not observed

`enrich_chunks.py:144-148` treats a line as the running title if
`title_core.startswith(norm[:40])`, which is true of any prefix of the title.
For "Design Compiler User Guide", a plain-text line reading "Design Compiler"
that appears in three or more sections is deleted as furniture.

**Fix:** require the line to cover most of the title, for example at least
60% of its length. Before shipping that, list every distinct line the change
would remove on an existing corpus (the verification protocol's own step) to
confirm it changes nothing that should have been kept.

---

## 2. Methodology

### 2.1 Give the light prose path page numbers — Verified on fixtures

This is the largest gain on the list. `convert_manual.py:248` calls
`to_markdown()` without `page_chunks=True`, so prose chunks carry no page
numbers. That is the "page coverage is partial, 46%" gap in
`failure-modes.md` §12, and the reason the docs send prose documents to
Docling (a multi-gigabyte install, about 8.6 hours for 25,000 pages).

On both test fixtures, joining the per-page output of `page_chunks=True` gave
**byte-identical** markdown to the whole-document call, in the same time.
`rebuild_reference.py` already does the page-span bookkeeping (`build_pages`
and the offset-to-page mapping). Doing the same in `convert_manual.py` would:

- give every prose chunk a citable page range;
- make `check_corpus.py --strict` passable, and its content check per page
  instead of per document;
- let furniture be stripped per page, before chunking, as `rebuild_reference.py`
  does. Furniture stripping would then no longer need to happen inside
  `enrich_chunks.py` for new conversions.

One side effect needs handling. `extract_figures.py` never uses the
paragraph-above rule where chunks have pages, and its page rule ties nothing
on a page two sections share. On prose chunked by heading, the paragraph rule
measured 96–99% right, so uncaptioned figures in prose would lose their links.
Keep the paragraph rule as the fallback after the page rule for prose, and
measure the result against the caption rule.

**Next step:** merge the two pymupdf4llm converters. They already share the
chunker and the furniture code; the only real difference is splitting the
document into entity regions before chunking. One converter that attributes
entities when it finds a command level (or takes `--shape prose|reference`)
would remove the "don't run `enrich_chunks.py` over a reference document"
trap, and the inconsistent `--slug` handling (4.4).

**Before relying on it:** check on real manuals that the two outputs still
match. The fixtures are small. Diff `full.md` from both modes, then run the
checker's content check.

### 2.2 Carry exact offsets through the chunker — From reading

`rebuild_reference.py:125-138` (`locate`) finds where each chunk sits by
searching for its first 200 characters. If that fails, it searches from the
start of the region, and if that fails too, it uses the cursor. But
`chunk_markdown` cuts its chunks as slices of the text, so it could return
each chunk's `(start, end)` offsets directly. Page mapping would then be exact
by construction. No failure has been seen; this removes a heuristic from the
path that page numbers depend on.

### 2.3 Use the TOC chain the reference converter already knows — From reading

Reference breadcrumbs are `Title › command` (`rebuild_reference.py:282`). Each
region starts at a known TOC entry, so its enclosing chain is known exactly
and costs nothing to add:

- Commands could carry `Title › Scan Commands › set_scan_configuration`. The
  checker's existing nesting and page checks would cover it.
- Regions that are not commands could carry `Title › Chapter` instead of the
  title alone.
- Those non-command regions could be chunked as prose (`dictionary=False`).
  That gives a conversion path for documents `pick_extractor.py` calls
  `mixed`; at the moment it says "inspect first" and offers nothing.

### 2.4 Keep section identity across reconversions — From reading

Section files are named by position (`NNN-slug.md`), so a reconversion renames
every file. `retrieval-measurement.md` records that the eval answers and
figure links were remapped by hand after the reference rebuild. Two ways to
avoid repeating that:

- A `remap_answers.py` that pairs old sections with new ones by how much text
  they share, using the copy kept in `.rebuild-backup/`. This automates the
  manual remapping.
- Answers in `questions.jsonl` that also record what stays fixed through a
  rebuild: the `doc_id`, version, page, heading, and a short quote.
  `eval_search.py` would resolve them to section files at scoring time.

Separately, `convert_manual.py` numbers files with 3 digits and the other two
converters with 4. Past 999 chunks, `1000-…` sorts before `101-…`. The
manifest's order is still correct, but a directory listing is not.

### 2.5 Retrieval changes worth measuring — From reading

Measure each of these with `eval_search.py` before adopting it.

- **Breadcrumb and heading are counted twice.** The chunk body begins with
  the breadcrumb line, and usually the heading too, while both are also
  indexed as their own columns (weights 3 and 10). The document title is
  therefore in the body of every chunk. Try removing the breadcrumb line from
  `body` at index time, and keep it for display.
- **The BM25 weights `(10, 8, 3, 1, 1)` were set by hand.** Do not tune them
  on the 76 questions: they would be tuned and scored on the same set. Wait
  until there are `real` questions to hold out.
- **Add an exact-identifier column.** An FTS5 column or table tokenised with
  `unicode61 tokenchars '_-'` would match `set_scan_configuration` as one
  token, where now it is a phrase that also matches prose saying "set scan
  configuration". The aim is hit@1; hit@5 for identifiers is already 100%.
- **Fix 1.1 and 1.2 first.** They cost nothing to run, and they move the same
  number the roadmap's reranker is meant to move.

### 2.6 Let the eval tool compare runs — From reading

The references keep saying to read the per-question changes, not the totals,
but nothing prints them. Add `eval_search.py --compare a.json b.json`, which
lists each question whose rank moved, with both ranks. Also record the git
commit and the index's `built_at` in `--json` output, so a saved run can be
traced back to the index and code that produced it.

---

## 3. Data and file structure

### 3.1 Say which copy of each duplicated field is canonical — From reading

Several fields are stored twice, and each pair needed its own checker rule to
catch the two copies drifting apart:

| field | stored in | checker rule |
|---|---|---|
| breadcrumb | the manifest, and the chunk's first line | `breadcrumb-file-mismatch` |
| `chars` | the manifest, and the file itself | `chars-mismatch` |
| slug | the manifest, and the folder name | `slug-mismatch` |
| which edition is current | `index.json`, and the search index | `index-stale` |

Keep the breadcrumb in the file: it serves people and programs reading a
chunk on its own. But state in the contract which copy wins, and drop the
copies nothing needs. `chars` can be computed when it is read, and the slug
can be the folder name.

### 3.2 Make the manifest a written schema, with its provenance recorded — From reading

The contract exists as prose in SKILL.md and as code in `check_corpus.py`. A
`manifest.schema.json` file, plus a `schema_version` field in each manifest,
would give the planned HTML importer, a third-party converter, or an editor
something exact to target.

Also record which converter wrote each document, in every manifest:
`converter`, its version, the pymupdf4llm or Docling version, and
`converted_at`. Today only Docling writes an `extractor` field. Meanwhile
`enrich_chunks.converter_owns_breadcrumb` works out who wrote a breadcrumb
from which fields are present (`command`, `confidence`); it could read a
recorded provenance field instead. `requirements.txt` sets no upper bound on
pymupdf4llm, which has already renamed a metadata key once (see 1.4), so
recording the version also explains later differences between conversions.

### 3.3 Export the chunks for vector stores — From reading

SKILL.md says this repo composes with vector-store tooling, but a consumer
has to join the manifest and the section files itself. Add an
`export_chunks.py` (or `build_search_db.py --jsonl`) that writes one record
per chunk: text, breadcrumb, pages, entity, confidence, `doc_id`, version, and
whether the edition is current. That is a few dozen lines, and it is the
format vector-store loaders read.

### 3.4 Have the server warn when its index is stale — From reading

SKILL.md notes that people skip the rebuild. The server already warns when
its index is partial, on the principle that a missing result must not pass
for a complete one. A stale index fails the same way.

**Fix:** at build time, store a fingerprint of every manifest and
`figures.json`: their newest modification time, or a hash. At startup, the
server compares it against the files on disk (a few hundred `stat` calls) and,
if they differ, adds a warning line just as the coverage warning does.

### 3.5 Fix the slug collision across collections — From reading

`documents.slug` is the primary key (`build_search_db.py:76`). Two vendors
that both have a `user-guide` therefore crash the build, on the
multi-collection layout the README itself shows. The roadmap lists this under
the HTTP work, but it is a crash on a supported setup: fix it now. Key
documents by `(collection, slug)`.

### 3.6 Merge the three collection files — From reading

`current_versions.json`, `superseded.json` and `vendor.json` each have their
own shape and their own loader, and `superseded.json` is not validated
(1.7). One `collection.json` holding `label`, `pins` and `superseded` would
need one loader and one checker rule. Low priority: it means migrating
existing corpora.

---

## 4. Engineering

### 4.1 Add CI — Verified that none exists

There is no `.github/`. The suite needs only pymupdf4llm and takes about a
minute. Run it on Linux and Windows: half the platform notes in SKILL.md come
from Windows. Also state the minimum Python version. The code needs at least
3.9 (`str.removeprefix`, `Path.is_relative_to`), and nothing says so; CI on
the oldest version claimed is what would confirm it.

### 4.2 Make tests runnable on their own — Verified

`python -m unittest tests.test_pipeline.PipelineTest.test_10_mcp_server_passes_its_smoke_test`
fails when run alone, because test_08 builds the index it uses. Build shared
fixtures on demand: a helper that converts and indexes if that has not
happened yet. Then split the 1,710-line test file by area: converters,
checker, server, editions.

### 4.3 Remove duplication that is not deliberate — From reading

Some duplication is deliberate and should stay: `check_corpus.py`'s
independence, and `mcp_server.py` being a standalone file. The version rule
copied into those files is already held in agreement by a test. The rest is
accidental:

- `slugify` has four definitions with different rules.
- `BREADCRUMB_SEP` is defined three times.
- `find_collections` has two definitions with different meanings.
- `strip_emphasis` and `normalize` do overlapping jobs.
- Scripts load each other in two ways: `sys.path` plus `import`, and
  `importlib` loading by file path in `rebuild_reference.py`.
- Every script repeats the same block that sets the console to UTF-8.

Move these into `editions.py`, which the converters already import, or into a
small `_common.py`. Anything that has to stay duplicated should get a
cross-check test like the version rule's.

### 4.4 Make the command lines consistent — From reading

- `build_index.py` takes one collection as a positional argument;
  `build_search_db.py` takes `--root`; `check_corpus.py` and `editions.py
  status` accept either a corpus or a collection. Accept a corpus root
  everywhere, and loop over its collections as `check_corpus.py` does.
- `rebuild_reference.py` requires `--slug`, while the other converters derive
  it. A slug chosen by hand for a reference's second edition easily fails to
  end in the version suffix `default_doc_id` strips. The two editions then get
  different `doc_id`s and both answer every search. Make `--slug` optional,
  with the same default as the other converters.

### 4.5 Server robustness and MCP metadata — From reading

- **A message that is not a JSON object kills the server.** A JSON array, for
  example, raises `AttributeError` at `mcp_server.py:1533`, outside any `try`.
  Answer it with JSON-RPC error -32600 instead.
- **Declare the tools read-only.** Give all eight tools a `title` and
  `annotations: {"readOnlyHint": true, "openWorldHint": false}`. Both are in
  MCP revision 2025-06-18, which the server already negotiates. Clients may
  use them to ask for fewer confirmations, and they cost nothing.
- **`lookup_entity` can return several times its budget.** The 40,000-character
  limit applies per hit (`mcp_server.py:669`). An entry present in several
  current manuals returns up to 40,000 characters for each. Make the budget
  cover the whole response.
- **`get_section` can match the wrong file.** Its `file` lookup uses `LIKE`
  without escaping (`mcp_server.py:612`), and `_` is a wildcard in `LIKE`.
  Nearly every section filename contains `_`, so other files can match too.
  The exact match is sorted first, so this is mostly harmless; escape it
  anyway.

---

## Suggested order

| when | items | why |
|---|---|---|
| now, hours each | 1.1, 1.2, 1.4, 1.7, 4.1, 4.5 | free retrieval gains, a silent failure removed, CI to keep it that way |
| next | 1.3 (and rerun the benchmark), 1.5, 1.6, 3.5, 2.6 | wrong labels and crashes on supported inputs; a way to see each ranking change |
| then | 2.1, then the merged converter | page numbers for all prose without Docling; one fewer path to keep in step |
| later | 2.3, 2.4, 3.2, 3.3, 3.4, 4.2–4.4 | structure that pays off as corpora and contributors grow |

Fixes 1.1 and 1.2 should come before the roadmap's `ask_docs` work. A model
writing answers inherits whatever retrieval puts first, and these two change
what comes first at no cost.
