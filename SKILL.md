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
  documents. `scripts/check_corpus.py` is read-only, so run it first: it says
  which documents meet the contract and where the rest fall short.

What breaks, and what it damages:

- **Source PDFs are looked for only in `source/` and beside `docs/`.** Each
  manifest records `source_pdf` as a bare filename, and `build_index.py`,
  `extract_figures.py`, `ocr_figures.py`, `check_corpus.py` and
  `get_page_image` all resolve it against those two places. When the PDFs live
  elsewhere, every document looks unaccounted for, and figures and page images
  fail.
- **Converters write to `docs/` in the PDF's collection by default**: the
  folder the PDF is in, or the one above when that folder is `new_docs/` or
  `source/`. In a help folder that means inside the vendor's install tree.
  Pass `--out-root`.
- **`enrich_chunks.py` rewrites section files and `manifest.json` in place, with
  no backup.** Its furniture pass deletes `Feedback` lines, lines matching the
  document's own title that recur in at least 3 sections, and the bare page
  numbers and `Chapter N:` headers within 3 lines of either. A line that only
  begins the title is kept: it has to start with the title, or cover at least
  60% of it. That is right for PDF page headers and footers; on text another
  tool produced it can still delete a short line that happens to be most of
  the title. Run `--dry-run --list-furniture` first on anything this skill did
  not convert, to see every distinct line it would delete, and back the folder
  up.
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
   List the PDFs. Identify releases of the same manual: each one converted
   becomes an edition (see "Editions"), and search answers from one of them.
   A release nobody will ask about can stay a PDF: record it in
   `superseded.json` and leave it in `source/`.
2. **Classify each document.** Prose manual or reference/dictionary (one entry
   per command, function, part number, error code)? This single call drives
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

A manual released every year is several PDFs saying nearly the same thing.
Index two of them side by side and every question is answered twice, with
nothing in the ranking to say which answer is the release the user runs.
Convert only the newest and nobody can ask about the one they are still on.

So each converted release is its own document, an **edition**, and two manifest
fields tie them together:

- `doc_id` names the manual every edition shares (`tshell-ref`). It defaults
  to the slug without its release (`tshell-ref-2026-2`); `--doc-id` sets it.
- `version` is the tool release the document applies to, as the PDF's cover
  prints it. `version_and_later` is set when the cover says "and later".

**Read the version from the cover, not the filename.** On one corpus of 44
PDFs, twelve filenames carried no version, and two named a later release than
their cover. Those two covers read "Software Version 2023.1 and later": the
vendor ships the manual unchanged with every release and renames the file. So
a version is not "which release of the documentation is this" but "which tool
release does this document apply to", and "and later" is recorded with it
rather than thrown away.

The converters read the first three pages and record a version only when it is
unambiguous. When the cover names two, when the filename names an earlier
release than the cover, or a different one where the cover makes no "and
later" claim, `version` is left out and the converter says why. Set it with
`--version` (`2023.1+` for "2023.1 and later"). A manual with one edition does
not need one; a second edition cannot be ordered without it, and the index
build stops.

**One edition per manual is current**: the newest, unless
`<collection>/current_versions.json` pins another by `doc_id`. Search and
`lookup_entity` read current editions only, so the default result is what it
was with one release converted. Any other edition is read when a call names
it: `document` (a slug, or a doc_id) with `version`. Every result names its
version, and one from a non-current edition says so.

**`version` on a call is the tool release the user is on.** It selects the
edition for exactly that release. Failing that, it selects the nearest earlier
edition if that one says "and later": that is the document's own claim to
cover what follows it, until the next edition. An edition for 2023.1 "and
later" answers for 2025.2; one for 2026.1 that makes no such claim does not
answer for 2026.2.

**A release no edition covers is refused, never approximated.** The server
answers with the editions it does have. An earlier edition that does not claim
to cover later releases is a different document, and serving it would be a
guess the caller cannot see. A pin works the same way: it names the release in
use, and one that no edition covers stops the index build and leaves the
previous index in place.

**Vendors reuse filenames** (`ptug.pdf` every release). A PDF from `new_docs/`
whose name is already in `source/` is filed with its version on the end
(`ptug_Y-2027.03.pdf`), and the manifest records that name. The same file
dropped in twice is refused: it is not an edition.

**Comparing editions is done by the server, on the whole text.**
`compare_versions` lists the entries (or, for prose, the headings) added and
removed between two editions, and with `name` compares one entry line by line.
Do not compare by reading two lookups: `lookup_entity` returns the first
40,000 characters of an entry, 29 entries in one reference are longer, and two
of those that differ near the end look identical that far in. What it reports
is what the manuals say. That is not a release note: a manual reworded is not
a tool changed, and a tool can change without its manual. Say which it is.

It reads prose and code differently. In prose, emphasis marks, quote style and
spacing change with the vendor's template and are ignored. Code spans and
fenced code are compared exactly as written: `*foo*` in a command is a
wildcard, not emphasis. A line of code that differs only in spacing or quote
style is listed as that, on its own, because one name added to an aligned
block moves every other line in it.

Two things it does not do yet. It compares exact lines, so an edition whose
page footers survived conversion shows them as differences. And for prose it
compares headings only; to compare a topic, search each edition.

`scripts/editions.py status <corpus>` shows every manual, its editions, which
is current and what is waiting. For a corpus converted before any of this,
`editions.py stamp <collection>` adds `doc_id` and `version` to the manifests
(originals kept under `.rebuild-backup/`), and `editions.py migrate` moves the
PDFs into `source/`.

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

- **Its page, where the chunks carry page numbers** (every converter here) and
  the page belongs to one section. Checked against the
  caption: 100% agreement on an unshared page, 76.7% where one entry ends and
  the next begins on it — so a shared page ties nothing.
- **The paragraph above it**, where the page ties nothing or the chunks carry
  none: found just after the previous figure's section, or else exactly once in
  the document. Checked
  against the caption, that is 96–99% right on documents chunked by heading —
  and 5.8% on a command reference rebuilt into one chunk per entry, where a bold
  caption starts its own chunk and strands the paragraph above it in the
  previous one. So it is never used on a reference rebuilt one chunk per entry.

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

## The output contract

`docs/<slug>/` is the interface, not any converter. A document may come from a
script here, a newer library, a converter written for one awkward manual, or
a hand repair. Any document is fit to index once `check_corpus.py` reports no
failures. A new way of converting is fit to replace an existing one when:

1. `python scripts/check_corpus.py <collection> --only <slug> --strict` exits 0.
2. `scripts/eval_search.py` scores no worse on the same questions.

That is how a converter gets replaced: on evidence, not because its output
looks right. `check_corpus.py` shares no code with the converters, so a bug in
one cannot pass itself.

A document is its folder: the folder's name is its slug in every tool. The
manifest's `slug` is a copy, and the checker warns when the two differ.

`scripts/manifest.schema.json` describes the manifest: the fields the builders
read are required, the rest are optional, and a manifest may carry others.
Every converter here writes `schema_version` and a `converter` record (the
script, the extractor and its version, when, and whether it wrote the
breadcrumbs itself), so a wrong page or label is traced to what produced it and
`enrich_chunks.py` knows which breadcrumbs to leave alone.

Where a value is stored twice, one copy wins:

- **breadcrumb:** the manifest. The chunk's first line renders it for a reader
  of the chunk alone, and the converters and `enrich_chunks.py` write both.
- **slug:** the folder name.
- **which edition is current:** decided from the manifests and
  `current_versions.json` when an index is built. `index.json` and the search
  index are both derived from them, and the checker warns when either is stale.
- **a section's length:** not stored. Compute it from the file. A manifest from
  before this still holding `chars` loses it when `enrich_chunks.py` rewrites
  the section, and `chars-mismatch` reports it until then.

Editions are part of it. A manual with two editions needs a version on each
that orders them, no two the same, and any pin has to name one of them; each
of those fails the check because each stops the index build. Two documents
with one title and different `doc_id`s are warned about: if they are releases
of one manual, both answer every search.

It checks what the index builders and server read, and whether the metadata
is right, not just present:

- **Breadcrumb ancestors must be TOC entries that contain one another.** Where
  a chunk has pages, each ancestor's TOC page span must also overlap them.
- **An owning entity must be a TOC entry** whose span overlaps the chunk's
  pages, and whose name appears in the chunk's text.
- **Sampled PDF pages must find their words in the chunks that claim them.**
  That tests the page numbers and catches dropped text together.
- **Page furniture must be gone**, including page-numbered footers.

On its first run the checker found real mislabels, all in chunks that passed
every earlier test:

- A Docling `anchored` breadcrumb matched a same-titled TOC entry 100 pages
  away.
- The heading walk kept a preface as the parent of chapter 1.
- 3,070 chunks of one reference still carried Tessent's page footer.

It cannot test idempotency. That needs a converter run twice, which the
test suite in `tests/` does.

Without page numbers it is much weaker:

- The content check compares against the whole document, so it catches only
  text lost wholesale.
- Wrong breadcrumbs mostly pass. On the old heading walk, the nesting and
  order checks flagged 768 chunks. Locating each chunk's page in the PDF showed
  about 1,700 with a wrong parent: most wrong parents still nest correctly in
  the TOC.

`--strict` therefore rejects documents without pages, and it should: they
cannot be cited or audited.

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
| `mcp_server.py` | Serves that index to any MCP client over stdio. Standard library only; `get_page_image` also needs PyMuPDF. |
| `mcp_smoke_test.py` | Drives a real MCP handshake and every tool against a built index. |
| `sample_sections.py` | Stratified sample of sections to write test questions from; `--figures` for sections with figures. |
| `eval_search.py` | Scores search against a test set: hit@k and MRR per question kind, and what came back for each miss. `--compare` shows what moved between two saved runs. |
| `remap_answers.py` | Carries a question file's answers across a reconversion by matching the old sections' text to the new ones. |

They are parameterized by corpus directory and slug, and assume the target
layout — see "When the input does not look like this" for what happens when it
isn't. Read the module docstrings — each records why it works the way it does.

## Adding one document later

Put the PDF in the collection's `new_docs/`. Once `pick_extractor.py` has said
what shape the document is, the rest is a fixed sequence:

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
from the corpus as it used to be.

Read what the converter prints first: the version it took from the cover, the
`doc_id`, and whether that makes this a new manual or an edition of one already
here. A new release of a manual already converted needs nothing more — it
becomes the current edition when the index is rebuilt, and the older one stays
answerable by version. Convert it with the same converter as the edition it
joins, or a comparison between them measures the converters.

To keep an edition as a PDF only, delete `docs/<its slug>/` and add
`{"file": "old.pdf", "superseded_by": "<current slug>"}` to the collection's
`superseded.json` — `build_index.py` then lists it as superseded instead of
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

The suite in `tests/` generates fixture PDFs with a known bookmark TOC and
runs the pipeline over them: `python -m unittest discover -s tests -v`, or
`python tests/run_each.py` to run every test alone. It covers the
things that have actually broken here -- idempotency, never downgrading
metadata a better-informed pass wrote, declining to attribute entities from too
little evidence, entity regions ending at chapters, figures tied to a section
only on evidence, the output contract the index and search builders read, and
editions: one answering per manual, a missing version refused, a comparison
that reads past the lookup's cut.
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
so nothing is hardcoded per corpus.

**The corpus folder serves itself.** `--emit-vscode-config` copies
`mcp_server.py` beside the index and writes every path in `.vscode/mcp.json`
relative to the workspace folder. A config that names this checkout by path
works on one machine, and a corpus in a synced folder is opened on several;
the server is one standard-library file, so shipping it with the index costs
nothing and needs only Python on the other machine. Later builds refresh the
copy, which also keeps the server and the index it reads on the same schema.
Do not edit the copy. The server reads figures and PDFs from the folder its
index is in, not from the path the index was built at. Tools: `search_docs`, `get_section`,
`lookup_entity`, `list_documents`, `get_toc`, `compare_versions`, `get_figure`,
`get_page_image`.
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
- **Search one edition per manual.** The cap is per document and editions are
  separate documents, so it does nothing to stop three releases of one manual
  taking fifteen places with the same answer. See "Editions".

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
   Save each run with `--json`, then `eval_search.py --compare before.json
   after.json` prints the totals side by side and every question whose rank
   moved. Each run records the build time of its index and the commit of the
   code.
6. **After a reconversion, carry the answers across.** Section files are named
   by position, so converting a document again renames them and every answer
   by file reads as a miss. `scripts/remap_answers.py --root <corpus>
   --questions <file>` compares each lost section's text, kept in
   `.rebuild-backup/<slug>/`, with the sections now there, and writes a remapped
   copy (never over the input), reporting what it could not place. An answer
   can instead be written as `{"doc_id": …, "quote": "a short exact phrase"}`,
   which `eval_search.py` resolves to the sections holding the phrase and which
   needs no remap at all.

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
