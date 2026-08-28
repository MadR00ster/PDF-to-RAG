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
  <source>.pdf                     originals stay put
  superseded.json                  [{"file": old.pdf, "superseded_by": slug}]
  vendor.json                      optional {"label": "Siemens Tessent"}
  docs/
    index.json                     machine-readable manifest of all documents
    README.md                      human-readable version of the same
    <slug>/
      manifest.json                title, page_count, PDF TOC, section list
      full.md                      whole document, un-chunked fallback
      sections/NNN-slug.md         retrieval chunks, ~2-9 KB
```

`full.md` matters more than it looks: it's the escape hatch whenever a chunk
boundary lands badly, so never drop it.

## Workflow

1. **Inventory.** List the PDFs. Identify superseded versions (same document,
   older release) — convert only the newest, record the rest in
   `superseded.json`, keep the old PDFs on disk.
2. **Classify each document.** Prose manual or reference/dictionary (one entry
   per command, function, part number, error code)? This single call drives
   everything downstream — see "Two document shapes".
3. **Convert.** Pick an extractor first (next section), then
   `scripts/convert_manual.py` for prose, `scripts/rebuild_reference.py` for
   reference documents.
4. **Index.** `scripts/build_index.py <corpus>` regenerates `index.json` and
   `README.md` from what's actually on disk, and reports any PDF that is
   neither converted nor marked superseded.
5. **Enrich** prose documents with `scripts/enrich_chunks.py` (strips page
   furniture, adds breadcrumbs). Reference documents get this during their
   own conversion, so don't run both over the same document.
6. **Verify** with the protocol below before declaring done.
7. **Serve it.** A corpus nobody can query is a folder of markdown. Build the
   search index and wire it into the user's editor — see "Serving the corpus
   over MCP". Do this as part of delivering, not as a follow-up they have to
   ask for.

## Choosing an extractor

The highest-leverage decision here, and worth making deliberately rather than
inheriting. Most failure modes in `references/failure-modes.md` trace to one
root cause: **`pymupdf4llm` infers heading levels from font size, so the
"hierarchy" is a guess.** Breadcrumbs and entity attribution then become
reconstruction work to compensate for that guess.

**Docling** (IBM Research) parses into a structured document model and chunks
against that structure instead of font size. Its `HybridChunker` attaches
heading metadata and a **page number** to every chunk. Prefer it for prose.

Measured against seven slices spanning five PDF producers
(`references/extractor-benchmark.md`), it is decisive on page provenance —
100% of chunks versus 0% from the prose path here — and page numbers are what
make every other piece of metadata auditable.

It does **not** remove the need for TOC verification. Its heading precision
measured no better (41% vs 43% TOC-confirmed on the same slice), and it offers
its own artifacts as headings, including shell transcript lines like
`ANALYSIS> analyze_scan_chains`. Take `meta.headings` at face value and roughly
half your chunks claim an unverifiable ancestor.

**pymupdf4llm** is fast and light on native-text PDFs with no ML models to
load. Prefer it for large text-heavy corpora where throughput matters, or when
Docling's runtime is prohibitive — a 6,000-page manual makes that a real
constraint, not a theoretical one.

Verify two things on a sample before committing, because they decide how much
of this skill you still need. Both were measured across five PDF producers in
`references/extractor-benchmark.md` — read that before re-deriving it, but still
check them on *your* corpus, since the second one swung from 16% to 88% between
documents and by 20 points between two regions of the same manual:

- **Does chunk metadata carry page numbers, not just headings?** Page
  provenance is painful to bolt on afterwards (failure mode 3).
- **Does the detected hierarchy match the PDF's bookmark TOC?** If it
  diverges, you are back to reconstruction regardless of the library's claims.

**Route by document shape rather than picking one extractor.** This is the
benchmark's main finding, and it reversed an earlier conclusion drawn from prose
alone:

- **Prose** → Docling for extraction, then anchor ancestors to the TOC. Buys
  100% page coverage and multi-level breadcrumbs where the prose path today
  gives 0% pages and one ancestor at best.
- **Reference / dictionary** → `rebuild_reference.py`, unchanged. It attributes
  99–100% of chunks to the right entity; anything derived from Docling's
  headings managed 16–62% on the same documents. Retiring it would be a severe
  regression exactly where wrong attribution does the most damage.

When anchoring prose ancestors, a chunk whose own heading the TOC confirms is
positionally precise. Everything else falls back to "the stack in force at this
page", which is **60.5% correct at page granularity and 82.9% once the chunk and
the headings are located by y-coordinate on the page**. Mark those chunks lower
confidence rather than presenting them like anchored ones.

**Pre-flight on bookmark density.** TOC titles per chunk predicts the anchoring
rate (0.55 → ~88% confirmed; 0.09 → ~16%), and the share of pages where two or
more TOC entries start predicts how far the fallback can be trusted. Both are
computable before converting anything.

Keep the corpus layout, index building, furniture stripping and verification
protocol whichever extractor you choose — they are extractor-independent.

**Scanned PDFs** have no text layer, so neither path produces anything until
OCR runs. The `pdf` skill bundled with Claude covers that, along with tables,
forms and page manipulation.

## Two document shapes

**Prose manuals** have a real heading hierarchy. Chunk on headings; breadcrumbs
come from the heading stack.

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

Build ancestors from the heading stack, **but verify each against the PDF's
bookmark TOC**. Heading levels from font-size heuristics routinely promote a
procedure step or a stray running footer into a fake chapter — one corpus
produced `Design Compiler® User Guide › Specify the libraries`, where that is a
step in a numbered list, not a chapter. Roughly half of headings survive TOC
verification; the rest fall back to document-title-only. That coverage loss is
the correct trade.

### Page range — so answers can cite

Extract with page tracking (`page_chunks=True` in pymupdf4llm), record each
page's character span, and map chunk offsets back to pages. Engineers using
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

Install: `pip install -r scripts/requirements.txt` (pymupdf4llm).

| Script | Use |
|---|---|
| `convert_manual.py` | One prose PDF → `docs/<slug>/`. `--dictionary` for bold-delimited entries. |
| `rebuild_reference.py` | One reference PDF → `docs/<slug>/` with page ranges + entity attribution. |
| `build_index.py` | Regenerate `index.json` + `README.md`; reports unaccounted-for PDFs. |
| `enrich_chunks.py` | Post-process existing chunks: strip furniture, add breadcrumbs. `--dry-run` supported. |
| `build_search_db.py` | Corpus → one SQLite FTS5 index. `--emit-vscode-config` also wires up VS Code. |
| `mcp_server.py` | Serves that index to any MCP client over stdio. Standard library only. |
| `mcp_smoke_test.py` | Drives a real MCP handshake and every tool against a built index. |
| `update.ps1` | Windows drop-and-run: converts PDFs staged in `<corpus>/new pdf/`, enriches each new slug, then reindexes. `-Root <path>` drives a corpus kept outside this repo. |

They are parameterized by corpus directory and slug, and assume the layout
above. Read the module docstrings — each records why it works the way it does.

`references/failure-modes.md` has the full catalog with measurements. Read it
when debugging a corpus that already exists, or before changing chunking or
furniture logic.

`references/extractor-benchmark.md` measures pymupdf4llm, Docling and a
TOC-anchored hybrid against seven slices spanning five PDF producers and both
document shapes. Read it before switching extractors — it reverses the
conclusion a prose-only comparison suggests.

## Serving the corpus over MCP

Chunking and metadata only pay off when something can retrieve them. Two
stdlib-only scripts turn a converted corpus into a server any MCP client
(VS Code/Copilot, Claude Code, Cursor) can query:

```bash
python scripts/build_search_db.py --root <corpus> --emit-vscode-config
python scripts/mcp_smoke_test.py --db <corpus>/mcp-index.sqlite3
```

The first writes one SQLite FTS5 index and a `.vscode/mcp.json` pointing at
`mcp_server.py`; the second drives a real handshake and every tool. Collections
are discovered from disk — `<corpus>/docs/` is one collection, or one per
subfolder that has a `docs/` — so nothing is hardcoded per corpus. The index is
a **snapshot**: rebuild after any conversion or enrichment, or the server keeps
answering from the old corpus.

Tools: `search_docs`, `get_section`, `lookup_entity`, `list_documents`,
`get_toc`.

**Lexical, not embeddings.** Real queries are identifiers and error strings —
`set_scan_configuration`, `-chain_count`, `K23 DRC`. Exact matching serves those;
a vector index adds a dependency, a rebuild cost and an API key to blur them.
Heading and entity columns are weighted above body text, and a chunk whose
entity *is* the query is boosted further.

**Rewrite queries before they reach FTS5.** `_` and `-` are token separators and
stray quotes are a syntax error, so every term becomes an explicitly quoted
phrase: `set_scan_configuration` → `"set scan configuration"`, matching the
literal identifier and prose that spells it out. Drop stopwords (FTS5 ANDs
everything, and agents ask questions: "how do I define a clock" must not require
"how"), and if the ANDed terms find nothing, retry with OR rather than reporting
a dead end.

**Demote front matter.** A contents page lists every heading in the document, so
it matches almost any query and outranks the real page — "DRC Rule K23 . . . 151"
beating the text of rule K23. Flag those at build time and push them down; don't
delete them.

**Cap hits per document.** One large reference will otherwise fill every result
page. Default to at most 5.

**A partial index must say so.** This is the failure mode that looks like a
correct answer: if some chunks did not make it in, search returns "no matches"
for something the corpus does cover and nothing on screen says the shelf was
half empty. The build refuses to exit 0 and names the unreadable files; the
server reports per-document coverage and appends a "Partial index" warning to
every result until it is whole.

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
- Keep backups **outside** `docs/`. Index builders glob `docs/*/manifest.json`,
  so a `docs/<slug>.old` gets listed as a real document.

## Where this skill stops

It produces a corpus plus **lexical** retrieval over it: chunked markdown, a
BM25 full-text index, and an MCP server an editor can query. That answers
"where is this identifier documented" extremely well.

What it does not do is semantic search. There is no embedding index, no vector
similarity, no reranking, so a question phrased in words the documents never
use will miss — paraphrase, synonym and concept queries are exactly where a
lexical index is weakest. Say so plainly rather than letting "RAG-ready" imply
more than was built.

For the layer above, the community `rag-architect` skills cover vector store
selection, embedding models, hybrid BM25 + vector search, reranking, and
RAGAS-style evaluation — and explicitly do *not* cover PDF extraction or chunk
metadata, so the two compose cleanly. This skill decides what a chunk *is*;
those decide how it gets found.

One idea worth borrowing from them: **choose chunk size empirically against the
real corpus** rather than by hand. The 9 KB default here was inherited by
matching an existing corpus, which is a defensible starting point and not a
measured optimum.

## Reporting

Say what was measured, not what was hoped. Report chunk counts, metadata
coverage (breadcrumb / page / entity percentages), lines removed, and content
lines lost — that last one should be zero and worth stating explicitly. Name
what is still unfixed; a corpus with known gaps is far more useful than one
with unknown ones.
