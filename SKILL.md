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
      figures.json                 figures: page, box, caption, owning section
      figures/pNNNNN-K.png         one crop per figure
```

`full.md` matters more than it looks: it's the escape hatch whenever a chunk
boundary lands badly, so never drop it.

## When the input does not look like this

The scripts were written for one input shape: a flat folder of PDFs, converted
in place, with `docs/` beside them and nothing else writing there. Real inputs
are often something else, and nothing here detects it yet. **Before running any
script that writes, check the input against the layout above.** If it differs,
tell the user what you found and choose an approach with them. Don't force the
input into this layout, and don't run the pipeline anyway to see what breaks.

Three shapes seen so far:

- **Scattered PDFs**, in nested folders or with copies of the same file. That is
  the case the scripts handle, once the PDFs are gathered into one collection
  folder.
- **A vendor HTML help folder with the PDFs alongside.** One example is an
  Oxygen WebHelp output built from DITA:
  - one folder per manual, split into topic subfolders
  - every PDF gathered in one `pdf/` folder, named after its manual's folder
    (`avalon_maskview_ug/` ↔ `pdf/avalon_maskview_ug.pdf`)
  - `index.html` redirecting to a landing page
  - a table of contents that exists only as per-node JavaScript fragments
    (`oxygen-webhelp/app/nav-links/json/tocId-*.js`), though a top-level
    `sitemap.html` may also hold it

  Asked whether the HTML and PDFs hold the same text, an agent reported that
  they do: the same manual rendered twice, with the PDF adding only page
  furniture. That answer came without numbers. The PDFs on hand may also cover
  only some of the manuals, so some manuals may exist only as HTML. Other
  vendors ship custom help trees that look like neither.
- **An existing corpus built by something else**: an earlier version of this
  skill, the deleted `update.ps1` (which moved PDFs and deleted `docs/<slug>`
  folders), an HTML-based RAG skill, or hand edits. An agent asked to update one
  such corpus reported that the layout matched this skill's for none of its
  documents.

What breaks, and what it damages:

- **Source PDFs are looked for only beside `docs/`.** Each manifest records
  `source_pdf` as a bare filename, and `build_index.py`, `extract_figures.py`,
  `ocr_figures.py` and `get_page_image` all resolve it against the collection
  folder. When the PDFs live elsewhere, every document looks unaccounted for,
  and figures and page images fail.
- **Converters write to `<pdf's folder>/docs` by default.** In a help folder
  that means inside the vendor's install tree. Pass `--out-root`.
- **`enrich_chunks.py` rewrites section files and `manifest.json` in place, with
  no backup.** Its furniture pass deletes any line repeated across 5% of
  sections (at least 10), which is right for PDF page headers and can delete real content in
  text another tool produced. Run `--dry-run` first on anything this skill did
  not convert, and back the folder up.
- **`build_index.py` replaces `docs/index.json` and `docs/README.md` outright**,
  and stops with an error on a manifest missing `title`, `source_pdf`,
  `page_count` or `sections`.
- **No script reads HTML.** Treating the HTML as a duplicate is safe only for
  manuals whose PDF is actually there.

For DITA-built help, the HTML is probably the better source even when the PDF
exists: exact heading hierarchy, no page furniture, figures already as image
files, real tables. What it lacks is page numbers, which could be recovered by
matching each section's text against the PDF's pages. This is unmeasured.
Adaptive input handling is planned, and waits on a real HTML help folder to
build and test against.

## Workflow

1. **Inventory.** If the input is not a flat folder of PDFs, or a corpus
   already exists, read "When the input does not look like this" first.
   List the PDFs. Identify superseded versions (same document,
   older release) — convert only the newest, record the rest in
   `superseded.json`, keep the old PDFs on disk.
2. **Classify each document.** Prose manual or reference/dictionary (one entry
   per command, function, part number, error code)? This single call drives
   everything downstream — see "Two document shapes".
   `scripts/pick_extractor.py <pdfs>` decides it from the PDF's own bookmark
   outline in a second or two, and also reports the text layer, bookmark
   density and the breadcrumb confidence to expect. It classified 38 real
   manuals with no false positives, and flags genuinely mixed documents as
   `mixed` rather than guessing. Read its output; don't just take the verdict.
3. **Convert.** Pick an extractor first (next section), then
   `scripts/convert_manual.py` for prose, `scripts/rebuild_reference.py` for
   reference documents.
4. **Index.** `scripts/build_index.py <corpus>` regenerates `index.json` and
   `README.md` from what's actually on disk, and reports any PDF that is
   neither converted nor marked superseded.
5. **Enrich** prose documents with `scripts/enrich_chunks.py` (strips page
   furniture, adds breadcrumbs). Reference documents get this during their
   own conversion, so don't run both over the same document.
6. **Extract figures** with `scripts/extract_figures.py <collection>`, then
   `scripts/ocr_figures.py <collection>` where Tesseract is installed — see
   "Figures". Both only add files, so they run on a corpus converted long ago.
7. **Verify** with the protocol below before declaring done.
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
PDF producers, 100% page coverage against 0%. Pages are what make every other
piece of metadata auditable.

**Route by document shape. Do not pick one extractor for a whole corpus.**
Prose and reference documents scored oppositely, by wide margins:

| shape | converter | why |
|---|---|---|
| prose | `convert_docling.py` | 100% pages, multi-level TOC-anchored breadcrumbs |
| reference / dictionary | `rebuild_reference.py` | 99–100% entity attribution; Docling-derived headings managed 16–62% |
| mixed | inspect first | convert the entry chapters as reference, the rest as prose |
| no bookmark TOC | either, warily | nothing can verify a breadcrumb; treat every ancestor as unverified |
| no text layer | neither | OCR first — the `pdf` skill bundled with Claude covers it |

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

## Figures

A manual's diagrams carry what its prose leaves out, and a text extractor turns
them into label soup — `<!-- Start of picture text -->SoC<br>CPU<br>…` — or into
nothing at all when the figure is a raster image. Extract them as images and
let the model look.

`scripts/extract_figures.py <collection>` reads each source PDF with the layout
model pymupdf4llm itself uses, crops every figure into `docs/<slug>/figures/`,
and records page, box, caption and drawn labels in `figures.json`. It touches
nothing the converter wrote.

**A figure belongs to a section only on evidence**, and which evidence there is
depends on what the converter left behind. Its own caption appearing as a line
of the section is exact: 331 of 331 captioned figures in one Tessent guide, 454
of 454 in a Synopsys one. Failing that:

- **Its page, where the chunks carry page numbers** (`rebuild_reference.py`,
  `convert_docling.py`) and the page belongs to one section. Checked against the
  caption: 100% agreement on an unshared page, 76.7% where one entry ends and
  the next begins on it — so a shared page ties nothing.
- **The paragraph above it, where they carry no pages**: found just after the
  previous figure's section, or else exactly once in the document. Checked
  against the caption, that is 96–99% right on documents chunked by heading —
  and 5.8% on a command reference rebuilt into one chunk per entry, where a bold
  caption starts its own chunk and strands the paragraph above it in the
  previous one. So it is never used where pages exist.

Otherwise the figure stays unattached and is served by page — a guessed section
would present a diagram as illustrating text it does not.

Traps, each met on a real corpus:

- A sentence citing a figure ("Figure 59 illustrates …") starts like a caption.
  A caption follows its number with punctuation or a capitalised title.
- Synopsys sets "Figure 1" and its title apart with a tab, so a caption box's
  first line is the bare label; join the next line.
- The layout model's raw box clips vector drawings at their edges. Grow it the
  way pymupdf4llm does before cropping.
- Note icons (~15 pt) and horizontal rules (~3 pt tall) come back as pictures.
- The model costs ~0.2 s a page, so skip pages with no sizeable image and no
  drawing in the body — ignoring the panel Synopsys paints behind every page's
  text area, which otherwise sends every page through.
- Its ONNX sessions take a thread per core, so parallel jobs fight: one guide
  took 283 s alone and 1,830 s as one of four jobs. `--jobs` caps each job's
  share, installed before the model is imported — the sessions are created on
  import, and importing pymupdf4llm triggers it. Installed after, the cap
  silently did nothing; installed first, another guide went from 1,552 s to 82 s.

The server shows a figure with `get_figure` and any page with `get_page_image`,
and `get_section` lists a section's figures by id.

Captions and drawn labels are indexed with their section, and measured they add
nothing: the caption is already in the section's text, and so, as picture
soup, are most vector figures' labels. What a raster figure says is invisible to
search until `scripts/ocr_figures.py` reads it — that took questions answered by
a figure from 14 of 19 in the top five to 18 of 19, and moved no text question
out of it. Noisy OCR is enough; search needs only some of the words. With that
in place, written figure descriptions had one question left to win and were not
generated — the model looks at the figure itself through `get_figure`.

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
| `convert_docling.py` | One prose PDF → `docs/<slug>/` with page numbers and TOC-anchored breadcrumbs. Needs Docling. |
| `pick_extractor.py` | Pre-flight a PDF: text layer, shape, bookmark density, expected confidence, runtime. |
| `extract_figures.py` | Crops every figure from the source PDFs into `docs/<slug>/figures/` and ties each to its section. Additive; `--dry-run`, `--jobs N`. |
| `ocr_figures.py` | Reads the words in figures that have no text of their own (raster images) into `figures.json`, so search can find them. Needs Tesseract's language data. |
| `build_search_db.py` | Corpus → one stemmed SQLite FTS5 index, figures included. `--emit-vscode-config` also wires up VS Code. |
| `mcp_server.py` | Serves that index to any MCP client over stdio. Standard library only; `get_page_image` also needs PyMuPDF. |
| `mcp_smoke_test.py` | Drives a real MCP handshake and every tool against a built index. |
| `sample_sections.py` | Stratified sample of sections to write test questions from; `--figures` for sections with figures. |
| `eval_search.py` | Scores search against a test set: hit@k and MRR per question kind, and what came back for each miss. |

They are parameterized by corpus directory and slug, and assume the target
layout — see "When the input does not look like this" for what happens when it
isn't. Read the module docstrings — each records why it works the way it does.

## Adding one document later

Once `pick_extractor.py` has said what shape the document is, the rest is a
fixed sequence:

```bash
python scripts/convert_manual.py new.pdf --title "Widget User's Manual"  # or rebuild_reference.py / convert_docling.py
python scripts/enrich_chunks.py "<collection>" --only <slug>             # prose only
python scripts/extract_figures.py "<collection>" --only <slug>
python scripts/ocr_figures.py "<collection>" --only <slug>
python scripts/build_index.py "<collection>"
python scripts/build_search_db.py --root "<corpus>"
python scripts/mcp_smoke_test.py --db "<corpus>/mcp-index.sqlite3"
```

The last two are the ones people skip: without them the server keeps answering
from the corpus as it used to be.

To retire the edition this one replaces, add `{"file": "old.pdf",
"superseded_by": "<new slug>"}` to the collection's `superseded.json` and delete
`docs/<old slug>/` — `build_index.py` then lists it as superseded instead of
reporting its PDF as unaccounted for.

There is deliberately no wrapper script for this. One existed, and it drifted
four features behind without anyone noticing: it still sent every document to
`convert_manual.py`, including the command references that need
`rebuild_reference.py`, and it never rebuilt the search index. The first step is
a judgement call, and the rest changes as this skill grows — both are better
read than buried.

`references/failure-modes.md` has the full catalog with measurements. Read it
when debugging a corpus that already exists, or before changing chunking or
furniture logic.

`tests/test_pipeline.py` generates fixture PDFs with a known bookmark TOC and
runs the pipeline over them: `python tests/test_pipeline.py`. It covers the
things that have actually broken here -- idempotency, never downgrading
metadata a better-informed pass wrote, declining to attribute entities from too
little evidence, entity regions ending at chapters, figures tied to a section
only on evidence, and the output contract the index and search builders read.
Run it before changing a converter.

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

The first writes one SQLite FTS5 index plus a `.vscode/mcp.json`; the second
drives a real handshake and every tool. Collections are discovered from disk,
so nothing is hardcoded per corpus. Tools: `search_docs`, `get_section`,
`lookup_entity`, `list_documents`, `get_toc`, `get_figure`, `get_page_image`.
The index is a **snapshot** —
rebuild after any conversion or enrichment, or the server keeps answering from
the corpus as it used to be.

**Lexical, not embeddings**, because real queries are identifiers and error
strings: `set_scan_configuration`, `-chain_count`, `K23 DRC`. A vector index
would add a dependency, a rebuild cost and an API key to blur them.

Retrieval decisions worth carrying to any implementation, each of which came
from watching bad results or measuring them rather than from theory:

- **Rewrite queries before FTS5 sees them.** `_` and `-` are token separators
  and stray quotes are a syntax error, so each term becomes a quoted phrase.
  Combine terms, never words: `"set scan configuration"` must stay one phrase.
- **Drop stopwords, then rank by OR whenever AND comes up short.** FTS5 ANDs
  everything and agents ask questions: "how do I define a clock" must not
  require "how". Falling back only when AND found *nothing* let one weak chunk
  that happened to hold every word stand in for the answer; falling back
  whenever AND returns less than a page took hit@5 from 82% to 89%.
- **Stem.** Questions say "options" and "toggling" where manuals say "option"
  and "toggle". Porter stemming lifted reworded questions from 55% to 73% hit@5
  and cost no identifier lookup.
- **Demote front matter.** A contents page lists every heading in the document,
  so it outranks the real one — "DRC Rule K23 . . . 151" beating rule K23.
- **Cap hits per document**, or one large reference fills every result page.

**A partial index must say so.** This is the failure that looks like a correct
answer: search returns "no matches" for something the corpus does cover, and
nothing on screen says the shelf was half empty. The build refuses to exit 0
and names the unreadable files; the server reports per-document coverage and
warns on every result until the index is whole.

## Measuring retrieval

Every retrieval choice — chunk size, stemming, a fallback, embeddings, figure
descriptions — is a guess until it moves a number. Build the number first.

1. `scripts/sample_sections.py --root <corpus> --n 60` draws a stratified
   sample: every document gets a share by the square root of its size, and
   front matter, boilerplate and stubs are left out.
2. Write one question per sampled section while reading it, into
   `<corpus>/eval/questions.jsonl` with that section as the answer. Give each a
   kind: `identifier` (names a command, option or message), `concept` (natural
   wording), `paraphrase` (deliberately avoids the section's own terms — the
   gap an embedding index would close), `figure` (answered by a figure; sample
   with `--figures` and look at the image), and `real` for questions users
   actually asked.
3. `scripts/eval_search.py --db <index> --questions … --misses` scores hit@k and
   MRR per kind through the server's own `search()`.
4. **Review every miss before believing it.** When what came back answers the
   question just as well, add it as an answer and say so in the question's
   note. Two of the first twelve misses on a real corpus were right answers the
   test did not know about.
5. Compare configurations on the same questions and read the per-question
   changes, not just the totals — on 57 questions, one question is 1.75 points.

Questions written by someone who has just read the answer share its words, so
they flatter lexical search. `paraphrase` measures that bias; `real` questions
are the only ones free of it, so ask the user for some.

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

Measure that gap before building for it. On a 24-manual EDA corpus, stemmed
BM25 put the answer in its top five for 100% of questions naming an identifier,
90% of naturally worded ones and 73% of questions deliberately reworded to avoid
the manual's terms. Adding a small general-purpose embedding model with rank
fusion did not move the reworded questions at all and lowered the overall top-5
rate from 92% to 88%: identifier-dense manuals are where lexical search is
strongest and a small model blurs what it matches exactly
(`references/retrieval-measurement.md`). Real users' questions missing in a way
a test set does not are the reason to revisit, not the promise of the technique.

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
