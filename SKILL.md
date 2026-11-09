---
name: pdf-to-rag
description: Convert a folder of PDF manuals, handbooks, or reference documentation into a retrieval-ready markdown corpus — chunked, indexed, and carrying the chunk-level metadata (breadcrumbs, page numbers, entity attribution) that decides whether retrieval is actually safe. Also use this to diagnose or repair an existing chunked corpus. Trigger whenever the user wants to make PDFs searchable or queryable, build a RAG dataset / knowledge base / vector-store corpus from documents, chunk documents for embedding, add page citations to retrieved text, wire a document corpus into an editor as an MCP server they can query from VS Code or Claude Code, or asks why their RAG answers are wrong or vague — including casual framings like "I have a pile of vendor manuals I want to ask questions about", "turn these datasheets into something I can search", or "my retrieval keeps returning useless fragments".
---

# PDF → RAG corpus

Extracting text from PDFs is the easy part; a library does it in one call. What
determines whether the resulting corpus is *safe to retrieve from* is the
metadata on each chunk. This skill exists mostly to stop you re-learning that
the expensive way.

## The governing idea

**A mislabeled chunk is worse than an unlabeled one.**

A reference manual splits one entry into `Description` / `Arguments` /
`Usage` / `Examples` chunks. If an `Arguments` chunk doesn't name what it
documents, retrieval surfaces a list of flags with no owner — and the model
will confidently attach them to whatever the user asked about. That is a
silent wrong answer, which is strictly worse than a miss the user can see.

So every metadata decision follows one rule: **either right or absent, never
guessed.** Prefer coverage gaps over confident errors. Prefer precision over
recall for anything destructive.

## Target layout

```
<corpus>/
  new_docs/                        PDFs waiting to be converted; new ones go here
  source/                          PDFs that have been converted
  current_versions.json            optional {"<doc_id>": "<version>"} pins
  superseded.json                  [{"file": old.pdf, "superseded_by": slug}]
  vendor.json                      optional {"label": "Siemens Tessent"}
  docs/
    index.json                     machine-readable manifest of all documents
    README.md                      human-readable version of the same
    <slug>/                        one edition of one manual
      manifest.json                title, doc_id, version, page_count, PDF TOC, section list
      full.md                      whole document, un-chunked fallback
      sections/NNNN-slug.md       retrieval chunks, ~2-9 KB
      figures.json                 figures: page, box, caption, owning section
      figures/pNNNNN-K.png         one crop per figure
```

A corpus holding several collections (one per vendor, say) repeats this under
each collection folder. Two collections may each have a document of the same
slug: the index keys documents by collection and slug, and a tool call naming
such a document passes `collection` as well. A converter moves a PDF from `new_docs/` to `source/`
as its last step, so what is left in `new_docs/` is what has not been
converted. A collection that still has its PDFs beside `docs/` works unchanged;
`scripts/editions.py migrate` moves them.

`full.md` matters more than it looks: it's the escape hatch whenever a chunk
boundary lands badly, so never drop it.

## When the input does not look like this

The scripts were written for one input shape: PDFs dropped into a collection's
`new_docs/`, with `docs/` beside it and nothing else writing there. Real inputs
are often something else, and nothing here detects it yet. **Before running any
script that writes, check the input against the layout above.** If it differs,
tell the user what you found and choose an approach with them. Don't force the
input into this layout, and don't run the pipeline anyway to see what breaks.

`references/unusual-inputs.md` has the three shapes met so far (scattered PDFs,
a vendor HTML help folder with the PDFs alongside, and a corpus built by
something else) and what each script damages when the layout differs. Read it
before touching one. The short version:

- On a corpus that already exists, run `scripts/check_corpus.py` first. It is
  read-only and says which documents meet the contract.
- Source PDFs are looked for only in `source/` and beside `docs/`, and the
  converters write to `docs/` in the PDF's own collection unless given
  `--out-root`.
- `enrich_chunks.py` rewrites section files and `manifest.json` in place, with
  no backup. On anything this skill did not convert, run `--dry-run
  --list-furniture` first and back the folder up.
- No script reads HTML.

## Workflow

1. **Inventory.** If the input is not a flat folder of PDFs, or a corpus
   already exists, read "When the input does not look like this" first.
   List the PDFs. Identify releases of the same manual: each one converted
   becomes an edition (see "Editions"), and search answers from one of them.
   A release nobody will ask about can stay a PDF: record it in
   `superseded.json` and leave it in `source/`.
2. **Classify each document.** Prose manual or reference/dictionary (one entry
   per command, function, part number, error code, design rule)? This single call drives
   everything downstream — see "Two document shapes".
   `scripts/pick_extractor.py <pdfs>` decides it from the PDF's own bookmark
   outline in a second or two, and also reports the text layer, bookmark
   density and the breadcrumb confidence to expect. It classified 38 real
   manuals with no false positives, and flags genuinely mixed documents as
   `mixed` rather than guessing. Read its output; don't just take the verdict.
3. **Convert.** Pick an extractor first (next section), then
   `scripts/convert_manual.py`. It decides prose or reference from the
   outline the way `pick_extractor.py` does (`--shape prose` or `--shape
   reference` says it outright, and a `mixed` document is refused until it
   does); `scripts/rebuild_reference.py` is the same reference conversion with
   the command line it always had.
4. **Index.** `scripts/build_index.py <corpus>` regenerates `index.json` and
   `README.md` from what's actually on disk, and reports any PDF that is
   neither converted nor marked superseded, and any still waiting in
   `new_docs/`.
5. **Enrich** prose documents with `scripts/enrich_chunks.py` (strips page
   furniture, adds breadcrumbs). A reference document gets both during its own
   conversion, so running it again is safe but pointless:
   `enrich_chunks.py` leaves a breadcrumb a converter wrote alone, and the
   furniture is already gone.
6. **Extract figures** with `scripts/extract_figures.py <collection>`, then
   `scripts/ocr_figures.py <collection>` where Tesseract is installed — see
   "Figures". Both only add files, so they run on a corpus converted long ago.
7. **Verify.** `scripts/check_corpus.py <corpus>` checks every document
   against the output contract, then use the protocol below for what it
   cannot see. See "The output contract".
8. **Serve it.** A corpus nobody can query is a folder of markdown. Build the
   search index and wire it into the user's editor — see "Serving the corpus
   over MCP". Do this as part of delivering, not as a follow-up they have to
   ask for.
9. **Measure it** with a test set before changing anything about retrieval —
   see "Measuring retrieval".

## Choosing an extractor

Run `scripts/pick_extractor.py <pdfs>` first. It reads the PDF's bookmark
outline in a second or two and reports text layer, document shape, bookmark
density and the breadcrumb confidence to expect, then names the converter. Read
its reasoning, not only its verdict.

The decision matters because of the root cause behind most of
`references/failure-modes.md`: **`pymupdf4llm` infers heading levels from font
size, so the "hierarchy" is a guess.** Docling parses a real document model and
attaches a page number to every chunk — measured across seven slices and five
PDF producers, 100% page coverage against 0% for the light prose path as it was
then. `convert_manual.py` now tracks pages too, so what Docling still adds is
the TOC-anchored breadcrumb with a confidence on every chunk. Pages are what
make every other piece of metadata auditable.

**Route by document shape. Do not pick one extractor for a whole corpus.**
Prose and reference documents scored oppositely, by wide margins:

| shape | converter | why |
|---|---|---|
| prose | `convert_docling.py` | multi-level TOC-anchored breadcrumbs with a confidence on every chunk. Under review: `convert_manual.py` carries pages as well now, and the routing waits for a comparison on a real corpus |
| reference / dictionary | `convert_manual.py --shape reference` (or `rebuild_reference.py`) | 99–100% entity attribution; Docling-derived headings managed 16–62% |
| mixed | inspect first | convert the entry chapters as reference, the rest as prose |
| no bookmark TOC | either, warily | nothing can verify a breadcrumb; treat every ancestor as unverified |
| no text layer | neither | OCR first — the `pdf` skill bundled with Claude covers it |

A reference chunk's breadcrumb is the TOC chain down to its entry (`Title › Chapter
› command`), and a chapter's own text takes the chapter's chain. For a document
that is part reference, `--prose-outside-entries` attributes only the titles at
the command level that look like commands and chunks the rest as prose; it
changes what is attributed, so measure before relying on it.

`rebuild_reference.py` takes the TOC level whose titles look like commands (an
underscore, ` -`, or a message code; 20 or more of them). Where none has that
many, it converts without attributing a chunk, says so on a line beginning
`!!`, and records `"attribution": {"status": "declined", ...}` in the manifest,
which `check_corpus.py` reports as `attribution-declined`. A reference whose
entries are plain words does this; pass `--command-level N` with the level they
are at. `pick_extractor.py` prints the same warning, and another when its own
level and the converter's differ.

**A structure-aware extractor does not retire TOC verification.** Docling's
heading precision measured no better (41% vs 43% TOC-confirmed), and it offers
its own artifacts as headings — including shell transcript lines like
`ANALYSIS> analyze_scan_chains`. Take `meta.headings` at face value and roughly
half your chunks claim an unverifiable ancestor.

Prose ancestors therefore come from the TOC rather than from detected headings:
the chunk's own heading where the TOC confirms it, otherwise the heading stack
in force at its position on the page. The second is 82.9% correct, so
`convert_docling.py` marks it `page` instead of `anchored`. Weigh them
differently downstream.

**What is bundled.** `convert_manual.py` and `rebuild_reference.py` are the
`pymupdf4llm` path and need only `requirements.txt`. `convert_docling.py` needs
Docling — a multi-gigabyte install pulling in PyTorch, running at ~1.2s/page —
deliberately **not** in `requirements.txt`. Install it when the pre-flight says
a corpus earns it:

```bash
pip install docling
```

Corpus layout, index building, furniture stripping and the verification
protocol are extractor-independent; keep them either way. Method, per-slice
numbers and the harness bugs found along the way are in
`references/extractor-benchmark.md`. Check the bands against your own sample:
the anchoring rate swung 16–88% between documents, and 20 points between two
regions of the same manual.

## Editions

Each converted release of a manual is its own document, an **edition**, so
search answers from one release instead of every release at once. Two manifest
fields tie the editions together: `doc_id` names the manual they share (by
default the slug without its release), and `version` is the tool release the
document applies to.

- **Read the version from the cover, not the filename.** Vendors ship a manual
  unchanged with later releases and rename the file. "2023.1 and later" is
  recorded as `version_and_later`. When the cover is ambiguous the converter
  leaves `version` out and says why; set it with `--version` (`2023.1+` for
  "and later"). A second edition without a version stops the index build.
- **One edition per manual is current**: the newest, unless
  `<collection>/current_versions.json` pins another by `doc_id`. Search and
  `lookup_entity` read current editions only. Any other edition is read when a
  call names it with `document` and `version`.
- **`version` on a call is the tool release the user is on.** It selects the
  edition for exactly that release, or else the nearest earlier one that says
  "and later". **A release no edition covers is refused, never approximated.**
- **Compare editions with `compare_versions`**, which reads the whole text,
  never by reading two lookups (`lookup_entity` cuts long entries). What it
  reports is what the manuals say, not a release note.

`scripts/editions.py status <corpus>` shows every manual, its editions, which
is current and what is waiting; `stamp` and `migrate` bring a corpus converted
before editions up to date. The reasons, the edge cases (reused filenames,
pins, how prose and code are compared) and the limits are in
`references/editions.md`.

## Two document shapes

**Prose manuals** have a real heading hierarchy. Chunk on headings; breadcrumbs
come from the TOC entries the headings match.

**Reference/dictionary documents** are a flat list of entries. Their heading
levels are often meaningless (one corpus measured 95% of chunks at a single
level), so there is no hierarchy to walk. What they *do* have is a complete
TOC mapping every entry to a page. Use pages, not headings.

Detect the shape by measuring flatness — the share of chunks sitting at the
most common heading level. Above ~50%, treat it as a reference document.

## Chunking

Implemented in `scripts/convert_manual.py`; the rules matter more than the code.

1. Split on H1/H2. In dictionary mode also split on standalone `**bold**` lines.
2. Any block over `MAX_CHUNK` (9 KB) splits again on the *shallowest deeper
   heading level that actually appears*, recursing only into pieces still
   oversized. Splitting on "any heading found anywhere" over-fragments badly.
3. No deeper heading → pack blank-line paragraphs → hard-wrap at a line
   boundary. Without that final step one unbroken table becomes a single
   enormous chunk.
4. Greedily merge sibling fragments from the same split, or a chapter that is
   merely choppy at one level explodes into dozens of tiny files.

**Invariant: every recursive step must strictly shrink its input.** Violating
this is not theoretical — in dictionary mode a >9 KB entry with no internal
headings re-matched the bold entry name it already started with, produced a
"split" identical to its input, and recursed until `RecursionError`. Guard by
bailing to paragraph packing whenever a split fails to yield 2+ pieces.

## The three metadata fields that decide quality

### Breadcrumb — every chunk says where it came from

Prepend `*Document › Chapter › Section*`. It helps the embedding and the reader
equally.

**Take ancestors from the PDF's bookmark TOC, never from heading levels.**
Heading levels from font-size heuristics routinely promote a procedure step or
a stray running footer into a fake chapter. One corpus produced
`Design Compiler® User Guide › Specify the libraries`, where that is a step in
a numbered list, not a chapter.

Checking each heading against the TOC is not enough on its own. A walk that
kept a stack of TOC-confirmed headings, nested by font-size level, still got
parents wrong. One chapter heading set smaller than the preface, or one never
matched, left the previous section as the parent of everything after it.
Against each chunk's page located in the PDF, 14–23% of the parents it gave
across 13 manuals were right.

`enrich_chunks.py` instead matches heading lines to TOC entries and keeps the
longest set of matches that runs forward through both. A chunk that opens with
a kept match gets that entry's TOC parents. Every other chunk gets the title
alone. That is 99.3–99.9% right across 22 manuals, and gives a parent to about
half of chunks. Letting the other chunks inherit the section they follow would
reach 93–96% of chunks at 89–95% right: a wrong parent in one chunk of ten.

### Page range — so answers can cite

Extract with page tracking (`page_chunks=True` in pymupdf4llm), record each
page's character span, and map chunk offsets back to pages. The chunker returns
where it cut each chunk, so the offsets are read, not searched for. Both
`convert_manual.py` and `rebuild_reference.py` do this; a prose document
converted before that has no pages until it is reconverted. Engineers using
vendor manuals need to verify claims; a chunk that can't cite a page can't be
checked.

Do not try to recover pages afterwards from footer numbers in the text. It
works on tidy prose documents and fails exactly where you need it — one
6,414-page reference yielded 87 usable numbers, values garbage.

### Entity attribution — which command/part/code this chunk documents

Only for reference documents, and **only via pages**:

```
chunk → page range → TOC (entry → page) → owning entity
```

Text-scanning for the entity name looks tempting and does not work. Measured:
99% of chunks got *an* owner but only 82.5% of entities were ever anchored,
and every miss silently inherits the previous entity. Verified failure: a
`tessent -shell` block labeled `tessent -diagserver`. That is the exact
wrong-answer class this whole skill is trying to prevent.

**Split the document into one region per entity *before* chunking.** Labeling
after chunking is not enough — the splitter packs text up to its size limit and
merges several entries into one chunk, which left 72% of chunks in one corpus
straddling a boundary: right at the start, wrong by the end.

## Page furniture

Running title, page number, "Feedback" link and running chapter header get
injected at every page break, frequently mid-sentence. Removing them is worth
~3-4% of tokens and repairs prose continuity.

**Strip furniture before chunking, per page.** Two reasons: a standalone
`**Feedback**` line is exactly the shape a dictionary-mode splitter treats as a
boundary (this produced one junk chunk per page — 220 chunks where 55 were
correct), and stripping after concatenation invalidates the page offsets that
entity attribution depends on.

**Detect furniture by shape, not frequency.** Frequency alone is far too blunt:
it flagged the real command names `insert_dft` and `create_test_protocol`, plus
ordinary prose that manuals simply reuse — "Note the following:" (127×), "where
valid values are as follows:" (175×). Deleting those is unrecoverable without
reconverting. Restrict to the document's own running title and the feedback
link, then take bare page numbers and `Chapter N:` headers only when adjacent
to a confirmed furniture line. Single-token lines are identifiers, never
furniture.

## Figures

A manual's diagrams carry what its prose leaves out, and a text extractor turns
them into label soup or into nothing. Extract them as images and let the model
look: `scripts/extract_figures.py <collection>` crops every figure into
`docs/<slug>/figures/` and records page, box, caption and drawn labels in
`figures.json`, touching nothing the converter wrote. `scripts/ocr_figures.py`
then reads the words in raster figures so search can find them.

**A figure belongs to a section only on evidence**: its caption appearing in the
section, else its page where only one section is on it, else the paragraph
above it (never on a reference rebuilt one chunk per entry). Otherwise it stays
unattached and is served by page. The server shows a figure with `get_figure`
and any page with `get_page_image`.

`references/figures.md` has the measurements behind each rule and the traps met
on real corpora (captions that are citations, clipped vector drawings, icons
returned as pictures, the layout model's threads).

## The output contract

`docs/<slug>/` is the interface, not any converter. A document may come from a
script here, a newer library, a converter written for one awkward manual, or
a hand repair. Any document is fit to index once `check_corpus.py` reports no
failures. A new way of converting is fit to replace an existing one when:

1. `python scripts/check_corpus.py <collection> --only <slug> --strict` exits 0.
2. `scripts/eval_search.py` scores no worse on the same questions.

That is how a converter gets replaced: on evidence, not because its output
looks right. `check_corpus.py` shares no code with the converters, so a bug in
one cannot pass itself. It checks that the metadata is right, not just present:
breadcrumb ancestors against the TOC, owning entities against the TOC and the
chunk's text, and sampled PDF pages against the chunks that claim them.
`--strict` rejects documents without pages: they cannot be cited or audited.

A document is its folder: the folder's name is its slug in every tool.
`scripts/manifest.schema.json` describes the manifest. Which copy wins when a
value is stored twice, what the checker found on its first run, and how much
weaker it is without page numbers are in `references/output-contract.md`.

## Verification protocol

Text you delete is gone unless someone reconverts the PDF, which can take
hours. Earn confidence before writing.

- **Dry-run first**, and work on a copy for anything structural.
- **Enumerate every distinct line you would delete and categorize each one.**
  Require zero unexplained. This is the single highest-value check here — it
  caught the command-name deletion above before it happened.
- **Check idempotency.** Re-running must change nothing. A breadcrumb detector
  that keyed on the `›` separator failed to recognize title-only breadcrumbs
  and re-prepended one on every run — 2,117 chunks would have accumulated
  duplicates.
- **Verify your verifier.** A first verification pass reported 5,347 deleted
  content lines; all were artifacts of its own whitespace handling and the real
  number was zero. When a check reports something alarming, confirm the check
  before acting on it.
- **Coverage is not correctness.** "99% of chunks got an owner" hid a 17.5%
  misattribution rate. Find an independent signal — TOC page ordering,
  monotonicity, a known-correct example — and test against that.

## Bundled scripts

Install: `pip install -r scripts/requirements.txt` (pymupdf4llm). Needs Python 3.10 or later,
which pymupdf4llm 1.28 requires.

| Script | Use |
|---|---|
| `update_corpus.py` | Everything below for the PDFs waiting in `new_docs/`, stopping at each decision; gated on the checker, the smoke test and, with `--questions`, the eval. `--dry-run`, `--titles`. See "Adding one document later". |
| `convert_manual.py` | One PDF → `docs/<slug>/` with page ranges; prose or reference by `--shape` (default: from the outline). `--dictionary` for bold-delimited entries. |
| `rebuild_reference.py` | One reference PDF → `docs/<slug>/` with page ranges + entity attribution; the same as `convert_manual.py --shape reference`. `--command-level N` names the TOC level of the entries when it cannot be told. |
| `build_index.py` | Regenerate `index.json` + `README.md`; reports unaccounted-for PDFs. |
| `editions.py` | `status`: manuals, editions, pins, waiting PDFs. `stamp`: add `doc_id`/`version` to older manifests. `migrate`: move root PDFs into `source/`. The converters import it. |
| `enrich_chunks.py` | Post-process existing chunks: strip furniture, add breadcrumbs. `--dry-run` supported. |
| `convert_docling.py` | One prose PDF → `docs/<slug>/` with page numbers and TOC-anchored breadcrumbs. Needs Docling. |
| `pick_extractor.py` | Pre-flight a PDF: text layer, shape, bookmark density, expected confidence, runtime. |
| `check_corpus.py` | Read-only check of any `docs/<slug>/` against the output contract: the fields consumers read, and breadcrumbs, owners and pages checked against the TOC and the PDF text. `--strict`, `--json`, `--only`, `--no-pdf`. |
| `extract_figures.py` | Crops every figure from the source PDFs into `docs/<slug>/figures/` and ties each to its section. Additive; `--dry-run`, `--jobs N`. |
| `ocr_figures.py` | Reads the words in figures that have no text of their own (raster images) into `figures.json`, so search can find them. Needs Tesseract's language data. |
| `build_search_db.py` | Corpus → one stemmed SQLite FTS5 index, figures and every edition included; marks one edition per manual current. `--emit-vscode-config` also wires up VS Code. `--body-without-breadcrumb` and `--ident-index` are experiments, off by default (retrieval-measurement.md). |
| `mcp_server.py` | Serves that index to any MCP client over stdio, or over HTTP with `--http` and a bearer token. Standard library only; `get_page_image` also needs PyMuPDF. With `PDF_TO_RAG_LOG=<file>` set it appends one JSON line per tool call (off otherwise). |
| `mcp_smoke_test.py` | Drives a real MCP handshake and every tool against a built index. `--url` drives a server already running over HTTP. |
| `sample_sections.py` | Stratified sample of sections to write test questions from; `--figures` for sections with figures. |
| `eval_search.py` | Scores search against a test set: hit@k and MRR per question kind, and what came back for each miss. `--compare` shows what moved between two saved runs. |
| `triage_log.py` | Reads the server's call log, shows each search with its results, and turns the ones you mark hit or miss into `real` questions. `--summary`, `--list`. |
| `eval_answers.py` | Runs questions that have a `gold` answer through `claude -p` with the corpus's server alone, grades each answer (correct, unsupported, declined) and reports tokens. `--dry-run`, `--limit`. |
| `export_chunks.py` | Every chunk as one line of JSONL, with its document, edition, breadcrumb, pages and entry, for a vector store or another pipeline. `--current-only`. |
| `remap_answers.py` | Carries a question file's answers across a reconversion by matching the old sections' text to the new ones. |

They are parameterized by corpus directory and slug, and assume the target
layout — see "When the input does not look like this" for what happens when it
isn't. Read the module docstrings — each records why it works the way it does.

## Adding one document later

Put the PDF in the collection's `new_docs/`, then read the plan before running
it:

```bash
python scripts/update_corpus.py "<corpus>" --dry-run
python scripts/update_corpus.py "<corpus>" --questions "<eval>/questions.jsonl"
```

`update_corpus.py` runs the sequence below for every PDF waiting in any
collection, printing each step as the command it is. It stops where a step
would have to guess (a mixed document, an unconfirmed title, a command level
in doubt) and leaves that PDF in `new_docs/` with the decision it needs, which
goes in a `--titles` JSON file. It never replaces or deletes a converted
document. A failing check stops it before any index is built, and the new
index replaces the live one only after the smoke test and, with
`--questions`, only if neither hit@5 nor MRR drops. Report the table it ends
with, what waits and why, and whether the index was replaced.

By hand, once `pick_extractor.py` has said what shape the document is, the
sequence is:

```bash
python scripts/convert_manual.py "<collection>/new_docs/new.pdf" --title "Widget User's Manual"  # or rebuild_reference.py / convert_docling.py
python scripts/enrich_chunks.py "<collection>" --only <slug>             # prose only
python scripts/extract_figures.py "<collection>" --only <slug>
python scripts/ocr_figures.py "<collection>" --only <slug>
python scripts/build_index.py "<collection>"
python scripts/check_corpus.py "<collection>" --only <slug>             # no FAIL lines
python scripts/build_search_db.py --root "<corpus>"
python scripts/mcp_smoke_test.py --db "<corpus>/mcp-index.sqlite3"
```

The last two are the ones people skip: without them the server keeps answering
from the corpus as it used to be. None of these needs `--slug`; a slug chosen
by hand makes a second edition a different manual. A test fails when this
sequence names a script `update_corpus.py` does not run.

`references/updating-a-corpus.md` has the routing rules, the gates, resuming
after a stop, keeping an old edition as a PDF only, and why the earlier
`update.ps1` wrapper was deleted.

## Serving the corpus over MCP

Chunking and metadata only pay off when something can retrieve them. Two
stdlib-only scripts turn a converted corpus into a server any MCP client
(VS Code/Copilot, Claude Code, Cursor) can query:

```bash
python scripts/build_search_db.py --root <corpus> --emit-vscode-config
python scripts/mcp_smoke_test.py --db <corpus>/mcp-index.sqlite3
```

The first writes one SQLite FTS5 index plus a `.vscode/mcp.json`, and copies
`mcp_server.py` beside the index so the corpus folder serves itself on any
machine with Python. Do not edit that copy; a later build of the index it
serves refreshes it. The second drives a real handshake and every tool. Tools:
`search_docs`, `get_section`, `lookup_entity`, `explain_log`, `check_script`,
`list_documents`, `get_toc`, `compare_versions`, `get_figure`,
`get_page_image`. With `--http` and a bearer token the server listens over
MCP's Streamable HTTP for several users (README.md has the commands).

The index is a **snapshot**: rebuild after any conversion or enrichment, or
the server keeps answering from the corpus as it used to be. It is **lexical,
not embeddings**, because real queries are identifiers and error strings
(`set_scan_configuration`, `-chain_count`, `K23 DRC`). A partial index or a
stale one says so on every answer.

`references/serving.md` has what each tool does (`check_script`, `explain_log`),
the retrieval decisions worth carrying to any implementation (query rewriting,
OR fallback, stemming, front-matter demotion, per-document caps), and how the
server detects a partial or stale index.

## Measuring retrieval

Every retrieval choice — chunk size, stemming, a fallback, embeddings, figure
descriptions — is a guess until it moves a number. Build the number first:

1. `scripts/sample_sections.py --root <corpus> --n 60` draws a stratified
   sample of sections.
2. Write one question per sampled section into
   `<corpus>/eval/questions.jsonl`, with that section as the answer and a kind:
   `identifier`, `concept`, `paraphrase`, `figure`, or `real` for questions
   users actually asked.
3. `scripts/eval_search.py --db <index> --questions … --misses` scores hit@k
   and MRR per kind. **Review every miss before believing it**: what came back
   may answer the question just as well.
4. Compare configurations with `--json` and `--compare`, and read the
   per-question changes, not just the totals.

Questions written by someone who has just read the answer flatter lexical
search; `real` questions are the only ones free of that, so ask the user for
some. To collect them, start the server with `PDF_TO_RAG_LOG=<file>`;
`triage_log.py` turns the searches you mark hit or miss into `real` questions,
and `eval_answers.py` runs questions with a `gold` answer through `claude -p`
and grades the answers (it spends tokens: start with `--dry-run --limit 1`).

`references/evaluation-workflow.md` has the full procedure, including carrying
answers across a reconversion with `remap_answers.py` and what the call log
holds. The results on one real corpus are in
`references/retrieval-measurement.md`.

## References and tests

Read these when the task reaches them; they hold the detail and the
measurements behind the rules above.

| file | read it when |
|---|---|
| `references/unusual-inputs.md` | the input is not a flat folder of PDFs, or a corpus already exists |
| `references/editions.md` | converting a second release, pinning one, or comparing two |
| `references/figures.md` | extracting figures or changing how they attach to sections |
| `references/output-contract.md` | a document came from elsewhere, or the checker's report needs explaining |
| `references/updating-a-corpus.md` | running `update_corpus.py` on a real corpus, or it stopped |
| `references/serving.md` | changing the server, the index or a tool |
| `references/evaluation-workflow.md` | building a question set, or measuring real use |
| `references/failure-modes.md` | debugging a corpus that already exists, or before changing chunking or furniture logic |
| `references/extractor-benchmark.md` | before switching extractors: it reverses the conclusion a prose-only comparison suggests |
| `references/retrieval-measurement.md` | before changing retrieval, or proposing embeddings |

The suite in `tests/` generates fixture PDFs with a known bookmark TOC and
runs the pipeline over them: `python -m unittest discover -s tests -v`, or
`python tests/run_each.py` to run every test alone. It covers the things that
have actually broken here: idempotency, never downgrading metadata a
better-informed pass wrote, declining to attribute entities from too little
evidence, entity regions ending at chapters, figures tied to a section only on
evidence, the output contract, and editions. Run it before changing a
converter.

## Platform notes

Encountered on Windows; harmless to apply anywhere.

- Write JSON with `ensure_ascii=True`. A machine whose Python defaults to a
  non-UTF-8 locale (cp950) chokes on a literal `™` in any reader that omits
  `encoding="utf-8"`. Read with `encoding="utf-8-sig"` to tolerate BOMs.
- Windows PowerShell 5.1's `-Encoding utf8` always writes a BOM, which breaks
  `json.loads`. Use `[System.IO.File]::WriteAllText` with
  `UTF8Encoding($false)`.
- In PowerShell, don't assign a `Tee-Object` pipeline to a variable — it
  suppresses live output, so a long conversion looks like a hang.
- Keep backups **outside** `docs/`, in the collection's `.rebuild-backup/`,
  where the converters keep theirs and build each conversion before moving it
  into place. Every script skips a folder under `docs/` named `*.old`,
  `*.new`, `.*` or `_*`, and `check_corpus.py` warns about one. A backup named
  anything else is read as a document.

## Where this skill stops

It produces a corpus plus **lexical** retrieval over it: chunked markdown, a
BM25 full-text index, and an MCP server an editor can query. That answers
"where is this identifier documented" extremely well.

What it does not do is semantic search. There is no embedding index, no vector
similarity, no reranking, so a question phrased in words the documents never
use will miss — paraphrase, synonym and concept queries are exactly where a
lexical index is weakest. Say so plainly rather than letting "RAG-ready" imply
more than was built.

Measure that gap before building for it. On a 24-manual EDA corpus, stemmed
BM25 put the answer in its top five for 100% of questions naming an identifier,
90% of naturally worded ones and 73% of questions deliberately reworded to avoid
the manual's terms. Adding a small general-purpose embedding model with rank
fusion did not move the reworded questions at all and lowered the overall top-5
rate from 92% to 88%: identifier-dense manuals are where lexical search is
strongest and a small model blurs what it matches exactly
(`references/retrieval-measurement.md`). Real users' questions missing in a way
a test set does not are the reason to revisit, not the promise of the technique.

To feed that layer, `scripts/export_chunks.py --root <corpus> --out chunks.jsonl` writes
every chunk as one JSON line (its text exactly as on disk, its document, edition,
breadcrumb, pages and entry), `--current-only` for the edition search answers
from. It stops on a section it cannot read rather than export a corpus with holes.

For the layer above, the community `rag-architect` skills cover vector store
selection, embedding models, hybrid BM25 + vector search, reranking, and
RAGAS-style evaluation — and explicitly do *not* cover PDF extraction or chunk
metadata, so the two compose cleanly. This skill decides what a chunk *is*;
those decide how it gets found.

One idea worth borrowing from them: **choose chunk size empirically against the
real corpus** rather than by hand. The 9 KB default here was inherited by
matching an existing corpus, which is a defensible starting point and not a
measured optimum; `eval_search.py` turns it into one — rebuild at another size
and compare on the same questions.

## Reporting

Say what was measured, not what was hoped. Report chunk counts, metadata
coverage (breadcrumb / page / entity percentages), lines removed, and content
lines lost — that last one should be zero and worth stating explicitly. Name
what is still unfixed; a corpus with known gaps is far more useful than one
with unknown ones.
