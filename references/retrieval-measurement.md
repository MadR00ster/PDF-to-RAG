# Retrieval measurement on a real corpus

Measured, not assumed: the test set, the method, the numbers and the decisions
they drove, on one corpus — 24 Synopsys and Siemens Tessent manuals, 23,856
pages, 14,860 sections. The method transfers; the numbers are this corpus's.
Reproduce them with `scripts/eval_search.py` against the corpus's
`eval/questions.jsonl`.

## The test set

57 text questions, one per section drawn by `sample_sections.py --n 60 --seed 7`
(two of the 59 skipped: a copyright page and a contents stub), each written
while reading its answer:

| kind | n | what it tests |
|---|---|---|
| identifier | 26 | names a command, option, rule or message |
| concept | 20 | natural wording, sharing some of the section's terms |
| paraphrase | 11 | deliberately avoids the section's own terms |

19 figure questions — 10 from Tessent manuals, 9 from Synopsys — come from
`sample_sections.py --figures` (seeds 11 and 13), each written after looking at
the figure. Five are answerable only from words inside a raster image: step
names in a flowchart, signal names in a waveform, column names in a table.

Three misses turned out to be right answers the test did not list. One
question's answer was a command's EXAMPLES chunk, and the DESCRIPTION chunk
search returned explains the same option; another named no tool, and search
returned the other vendor's section on the same behaviour; a figure question's
flow is also spelled out, step by step, in the companion reference manual. Each
was added as an answer, with a note saying so. Review misses before believing
them.

## Lexical search: two changes, measured together

Right section in the top five (hit@5) and mean reciprocal rank, through
`search_docs`' defaults — ten results, at most five per document:

| index | falls back to OR | identifier | concept | paraphrase | hit@1 | hit@5 | MRR |
|---|---|---|---|---|---|---|---|
| unstemmed | when AND finds nothing | 92% | 85% | 55% | 54% | 82% | 0.673 |
| unstemmed | when AND returns < 10 | 100% | 95% | 55% | 54% | 89% | 0.699 |
| stemmed | when AND finds nothing | 85% | 70% | 73% | 53% | 77% | 0.632 |
| **stemmed** | **when AND returns < 10** | **100%** | **90%** | **73%** | **60%** | **91%** | **0.724** |

The old fallback failed four questions the same way: one chunk happened to
contain every word, a generic one like "option" included, so AND returned a
single wrong result and the fallback never ran. Stemming alone made that worse
— broader AND matches pre-empt the fallback more often — which is why the two
changes ship together. A third variant, AND results first and OR results after
them, matched these totals unstemmed but ranked answers lower when stemmed.

On 57 questions one question is 1.75 points. Read the per-question changes
(`eval_search.py --json` keeps them), not just the totals.

## Experiments left off

Two changes to the index are built, tested and off by default. Neither has been
measured here, and choosing a default is a separate decision from building them.

- `build_search_db.py --body-without-breadcrumb` indexes a chunk's text without
  its first line when that line is its breadcrumb. The breadcrumb has a column of
  its own (weight 3), so with the line in the body as well it counts twice.
- `build_search_db.py --ident-index` keeps each chunk's identifier-shaped words
  whole in a second index. A query naming one gives the chunks that hold it a
  further boost of 3.0 in the server, on top of the entity and heading rules.

Measure each against the default on the same questions with
`eval_search.py --compare`. Adopt one only if identifier hit@1 rises and no kind
falls.

The BM25 weights, `(10, 8, 3, 1, 1)`, stay as they are. Tuning them on the 76
questions they were scored on measures nothing: any weights can be fitted to
the questions that chose them. They are worth revisiting once there are
held-out `real` questions, asked by someone who had not read the answer.

## Orphaned sub-chunks

Eleven questions have as their answer a chunk headed only "Arguments", "Note",
"Example 1", "Syntax" or "What Next" — failure mode 1. All eleven were found in
the top five: in this corpus each such chunk still carried its command's name
as a running header, which furniture stripping keeps because single-token lines
are never furniture. Finding them was not the problem; attributing them was,
and a running header is luck, not a guarantee. The two manuals holding eight of
the eleven have since been rebuilt with `rebuild_reference.py`, which names the
command on every such chunk — see "What rebuilding the two reference manuals
changed" below. The other three sit in `etassemblereference` and `tmax-rules`,
which were not rebuilt.

## Semantic search: measured, and not built

After the two lexical changes, 5 of 57 text questions missed the top five —
three reworded, two naturally worded — so an embedding index had under nine
points to win, nearly all on questions that avoid the manual's own words.

It won none of them. On the rebuilt corpus, every chunk embedded as overlapping
1,200-character passages prefixed with the section's breadcrumb (44,935
passages), with BAAI/bge-small-en-v1.5 on CPU, scored against all 76 questions:

| ranker | identifier | concept | paraphrase | figure | hit@1 | hit@5 | MRR |
|---|---|---|---|---|---|---|---|
| BM25, as shipped | 100% | 90% | 73% | 95% | **71%** | **92%** | **0.803** |
| embeddings alone | 85% | 80% | 45% | 74% | 57% | 75% | 0.654 |
| reciprocal rank fusion of both | 96% | 90% | 73% | 84% | 64% | 88% | 0.753 |

Fusion rescued a handful — one reworded question from rank 21 to 2 — and
demoted more: three identifier questions from rank 1 to 5 or worse, two figure
questions from the top three to 12 and 27. The kind embeddings exist for,
reworded questions, did not move at all. Manual text is identifier-dense
jargon, and a general-purpose small model blurs exactly what BM25 matches
exactly.

What was not tried, so this does not rule out: a larger or domain-tuned model;
fusion weighted towards BM25, which would be tuned on the same 76 questions it
was then scored on; and passages carrying figure OCR text, which the index
searches and the embeddings here did not — part of the figure gap is that.
None is worth the cost on this evidence: 74 minutes to embed the corpus on this
machine, a model download and a dependency, and a re-embed after every
conversion. Real users' questions (kind `real`) missing in a way this test set
does not would be the reason to revisit.

## What a client reads to reach the answer

`eval_search.py --cost` counts the characters a client takes in on the way to
the answer, through the tools as they ship. Production's corpus (34 documents,
26,491 chunks), read through its live index, all 76 questions, 70 of which reach
their answer in the top ten (ranking as before: 68% hit@1, 92% hit@5, MRR
0.789):

| characters | median | mean | 90th percentile |
|---|---|---|---|
| the page of results `search_docs` returns | 5,384 | 5,392 | 5,877 |
| the answer's section, `get_section` whole | 4,924 | 5,160 | 8,547 |
| the answer's section, `get_section` with `query` | 4,243 | 4,186 | 5,865 |
| page + the answer's section, whole | 10,222 | 10,552 | 13,929 |
| page + the answer's section, passages | 9,618 | 9,579 | 11,217 |
| page + every section read down to it, whole | 11,334 | 11,947 | 16,958 |
| page + every section read down to it, passages | 10,138 | 10,862 | 15,965 |

(3.5 characters is about a token. The "whole" rows are what `get_section`
returned before it took a `query`: the same code path, unchanged.) Of the 70, 23
answer sections were narrowed; the other 47 came whole, because they fit in the
4,000 character budget or because the passages would have been over three
quarters of the section. Passages save 6% of the median, 9% of the mean and 20%
at the 90th percentile of reaching the answer. The results page, not the
section, is the larger half of the cost, and passages do not touch it.

### Holes inside lists

The first version of the passages split a section at blank lines. The converter
puts a blank line between every bullet and the paragraph that explains it, and
keeps no indentation, so a list is many small blocks, and the scorer kept some
and dropped the ones between: the answer to "what kinds of black boxes does
Design Compiler topographical mode support?" came back with the explanation of
its first kind missing, in the middle of the list that is the answer. Measured
on the 30 answer sections that version narrowed, counting each gap that has text
on both sides and a list item, a lead-in (a paragraph ending in a colon) or a
heading right beside it:

| | narrowed | gaps | beside a list, lead-in or heading | sections with one |
|---|---|---|---|---|
| first version | 30 of 70 | 71 | 32 (24 list, 8 lead-in or heading) | 17 |
| now | 23 of 70 | 18 | 0 | 0 |

Two rules did it, and neither did it alone:

- A list is one passage: its lead-in, the items, the paragraphs between items
  (at most two in a row, never a heading) and one paragraph after the last
  item, when that is no longer than 2,000 characters. A longer list is cut
  where the scorer says, because narrowing has to cut somewhere. A passage also
  never ends on a heading or a lead-in, so what it introduces goes with it.
- A gap of at most 900 characters between two chosen passages is returned, not
  marked: the marker says something is missing, and a short gap costs less to
  return than to explain.

Swept on the same 76 questions, with the list limit at 0, 1,500, 2,000 and
3,000 characters and the gap at 0, 300, 600, 900 and 1,200: the list rule alone
(2,000, no gap) left 9 gaps beside a list in 6 sections; the gap rule alone (900,
no list rule) left 9 in 7; together at 600 they left 5 in 4; together at 900
none (every row of the sweep also has the rule that a passage never ends on a
heading or a lead-in). The values were picked from that sweep on those
questions, so they are fitted to them: 900 is the smallest gap tried that left
no hole, and a different manual could still show one. The cost of choosing them
is the saving above, which was 7%, 12% and 24% before.

What this does not say: that the passages hold the answer. The questions carry
no answer text, only which section answers, so the check is a proxy. Of the
question's words that the whole section holds, the narrowed ones kept 99.4% on
average and at worst 86% (one of the 23 lost a word). That is a floor on
recall, not recall; the queries here are the full questions, and a client that
passes two keywords narrows differently. Reading seven of the narrowed sections
(black boxes, automatic mode, `-applies_to`, `-Cell_constraints`,
`+ntb_solver_bias_mode_auto`, `report_scan_path` and boundary-scan pin mapping)
against their questions, each still held what the question asks. Real use
(`real` questions) is what would show passages leaving the answer out.

## Figures

`extract_figures.py` then `ocr_figures.py` over all 24 manuals:

| | figures | |
|---|---|---|
| found | 4,150 | |
| captioned | 2,945 | 71% |
| tied to a section by caption | 2,942 | 99.9% of captioned |
| tied by its page | 87 | where the page belongs to one section |
| tied by the paragraph above | 580 | |
| not tied, served by page | 541 | 13% |
| with drawn labels (vector) | 764 | 18% |
| words read by OCR | 3,284 | 97% of the 3,386 with no text layer |

Which fallback applies depends on the converter. Where chunks carry page
numbers, the figure's page decides — but only where that page belongs to one
section: checked against the caption, that is 100% right on an unshared page
(26 figures) and 76.7% where one entry ends and the next begins on the same one
(344), so a shared page ties nothing and the figure is served by page instead.

Checked against the caption on the 1,656 figures that have both, the context
method picked the caption's section 97.5% of the time. Its disagreements are
near-misses: the paragraph above a figure closes one section while the caption
opens the next, often because pymupdf4llm made the caption a heading of its
own. Four documents have no captions at all, and there context tied 233 of 341
figures — once it was allowed past the window after the previous figure. Held
to that window, one of them had 5 of 113 tied.

The layout model is the cost, ~0.2 s a page, and the pre-filter keeps it off
pages that cannot hold a figure: the 3,202-page command dictionary needed it on
5. Its ONNX sessions are created when `pymupdf.layout` is imported, each with a
thread per core, so parallel jobs ran every document about six times slower
than one job alone (283 s against 1,830 s for the same guide). A thread cap did
nothing while it was installed after that import; installed before it, a
586-page guide went from 1,552 s to 82 s.

### Does figure text help search?

All 76 questions against three indexes built from the same `figures.json`
files, stemmed, with the OR fallback:

| figure text in the index | identifier | concept | paraphrase | figure | hit@1 | hit@5 | MRR |
|---|---|---|---|---|---|---|---|
| none | 100% | 90% | 73% | 74% | 61% | 87% | 0.719 |
| captions + drawn labels | 100% | 90% | 73% | 74% | 61% | 87% | 0.719 |
| + OCR of raster figures | 100% | 90% | 73% | **95%** | 63% | **92%** | 0.749 |

Captions and drawn labels change nothing: the caption is already a line of the
section's text, and so, as picture soup, are most vector figures' labels. OCR
is what moves figure questions — 14 of 19 in the top five to 18 of 19 — by
making findable the words that exist only as pixels: block names in an EDT
diagram, the steps of a repair flowchart, the headings of a table screenshot.
It cost the text questions nothing; one moved from rank 4 to 5. The figure
question still missed asks about waveform signal names, which OCR reads worst.

### Figure descriptions: not generated

A vision model's description of each of these 4,150 figures would cost roughly
$35–40 (Claude Opus 5 through the Batch API, about 2,000 tokens in and 300 out
a figure). After OCR it has one figure question left to win on this set, and an
agent that finds the section looks at the figure itself through `get_figure`.
Worth revisiting only if real questions keep missing figures that OCR reads
badly — waveforms and dense schematics.

## What rebuilding the two reference manuals changed

`syn2` and `tshell-ref` had been converted with `convert_manual.py`, so no chunk
in this corpus carried a page number or the name of the command it documented —
failure mode 1, live, in the two manuals most exposed to it. Rebuilt with
`rebuild_reference.py`:

| | syn2 | tshell-ref |
|---|---|---|
| chunks | 856 → 1,645 | 6,393 → 4,511 |
| naming their command | 0 → 1,643 (99.9%) | 0 → 4,246 (94.1%) |
| carrying page numbers | 0 → all | 0 → all |

Every bare `Description` or `Arguments` chunk in syn2 now names its command and
carries a `Title › command` breadcrumb; the two chunks that name none are the
copyright pages. Across the corpus the index went from 14,860 chunks and no
entities at all to 13,767 chunks and 2,653 entities, so `lookup_entity` answers
here for the first time.

On the same 76 questions, the gain is in the first result rather than the page:

| | before | after |
|---|---|---|
| hit@1 | 63% | **71%** |
| hit@5 | 92% | 92% |
| MRR | 0.749 | **0.802** |
| identifier hit@1 | 69% | **85%** |

The rebuild renames every section in both manuals, which breaks any test
question pointing into them and every figure link in `tshell-ref`. The answers
were remapped by matching each old chunk's text, kept in `.rebuild-backup`,
against the new sections (`scripts/remap_answers.py` does that now); the
figures were re-extracted. Budget for both after any rebuild.

## Reconverting `tshell-ref-2026-2` with entries kept whole (2026-11-12)

The Tessent Shell reference had 3,070 chunks (of 4,511) carrying the page
footer "N-NNN Software Version 2026.2", 104 chunks headed by one command and
owned by another (examples cut one bold command line per chunk, 110 bytes
each), 23 chunks that were only a breadcrumb, and 66% of owned chunks naming
their owner. It was reconverted in a work copy with two changes to the
converter: a footer made of plain words and a changing number is page furniture
(a line holding an identifier, an option or a number glued to letters is not),
and the pieces of one entry are packed back up to 9,000 characters. Figures
were extracted again and read with Tesseract. Production was not touched.

| `check_corpus.py`, tshell-ref | before | after |
|---|---|---|
| chunks | 4,511 | 2,339 |
| median chunk, characters | 1,258 | 4,527 |
| chunks holding page furniture | 3,070 | 0 |
| heading mismatches | 104 | 1 |
| empty chunks | 23 | 0 |
| owned chunks that name their owner | 66.3% | 90.0% |
| chunks attributed to a command | 94.1% | 92.7% |
| figures tied to a section | 89.1% (513) | 95.9% (513) |
| PDF words found in chunks (30 sampled pages) | not measured | 93% |

The one mismatch left is a false positive of the checker: the entry
`report_edt_configurations` prints an example whose script has a bold line
`set_edt_pins...`, which the checker reads as another entry's heading.
Attribution fell by 1.4 points because the pieces that used to be orphans are
now inside the entries they belong to, but 7.3% of the chunks (171) are still owned
by none: the front matter, contents and lists of figures, and the chapters that
are not command entries.

Figures: 370 tied by caption, 122 by page, 21 by neither. On the 247 captioned
figures the page method picked the caption's section every time.

Search, on the 138 text and figure questions (the 8 `version` questions are
left out: this branch's `eval_search.py` does not read them), against the
index of the unchanged copy, `--compare` read question by question:

| | hit@1 | hit@3 | hit@5 | MRR |
|---|---|---|---|---|
| before | 72% | 89% | 93% | 0.810 |
| after | 72% | 88% | 92% | 0.804 |

Six questions moved, and two of them are the reconversion's: q42 (which
HsioIsolationMultiplexer property is the WLAST output pin) fell from 1 to 3,
because its continuation chunk and the examples chunk, which also hold the
word, now rank above the chunk with the property's description; q11 (a message
printed during EDT rules checking) rose from 2 to 1, because the reference
result that outranked the guide was a one-line fragment of the
`report_edt_configurations` example, a chunk of its own, and is now inside that
entry's chunk, which ranks lower. q05 fell from 3 to
5 as other reference chunks moved above it. Three questions about other
manuals (f20, q103, q78) moved by one rank or two with no change to their
documents: BM25 scores depend on the whole index, and 2,172 fewer chunks change
the average length and every term's frequency. The answer to "did cleaning the
reference help search" on this set is therefore: no measurable change, with
one question a little worse and one a little better. What it bought is in the
structural table above. Two things this does not show: that an agent finds an
entry's example sooner (the chunks hold whole entries now), and what packing
costs entries that used to be chunks of their own (a packed chunk keeps the
heading of its first piece).

### Why `tmax-rules` ties 34% of its figures

49 of its 74 figures are tied to no section. Every one of the 49 sits on a page
that holds two sections (the median page belongs to two sections), and none has a caption (DRC rule diagrams are
drawn without one). The page method ties a figure only where the page belongs
to one section, because on a shared page it was right 76.7% of the time, so it
declines, and with no caption nothing else applies. The 25 figures on a page of
one section are tied. Nothing was changed. The figures are still served by
page.

### `pt-cmds`

Not reconverted. The plan called for it only if the checker or the new
questions showed the Tcl-comment scraps hurting: the checker's two warnings for
`pt-cmds` are 3 chunks with furniture and its owner-named share (88.5%, under
the 90% line), and of the 6 new questions answered there, 5 are in the top five
and the sixth (q78) ranks 9 behind user-guide sections, not behind a scrap.

## Furniture pass and empty chunks, in place (2026-11-12)

`enrich_chunks.py --dry-run --list-furniture` over both collections of a work
copy, the lists read, then applied to the five documents whose lists were
running titles, page numbers and page labels and nothing else:
`etassemblereference-2021-2` (491 copies of its running title),
`tshell-ijtag-user-2022-4` and `-2026-2` (the title cut by highlight tags
around its apostrophe, and "N-N Software Version 2026.2"), `tshell-mbist-user-2026-2`
(462 page labels, chapter and appendix) and `ye-data-intg` (465 running titles
and 465 bare page numbers). The first dry run found nothing in the IJTAG
editions because of the apostrophe, and left the appendix labels ("A-12 ...")
of the MemoryBIST guide; both were fixed in `enrich_chunks.py` and the pass
repeated.

| `check_corpus.py`, 34 documents | before | after |
|---|---|---|
| chunks holding page furniture | 4,088 | 23 (`vcs` 17, `dftug` 3, `pt-cmds` 3) |
| warnings | 40 | 32 |
| empty chunks | 12 outside tshell-ref (`atpg-gd` 2, `tshell-user` 5, `tsi-usage-ref` 5) | 12 |

The 23 left are a standalone "Feedback" line in `vcs`'s contents and running
titles `enrich_chunks.py` does not recognise; they were not touched. Scored on
the 138 text and figure questions after this pass and the tshell-ref
reconversion above: 71% hit@1, 88% hit@3, 92% hit@5, MRR 0.801 (before: 72%,
89%, 93%, 0.810). Besides the reference's questions, q110 moved from 1 to 2 and
f05 from 5 to 4, both in documents whose chunks lost furniture.

Empty chunks: the cause is a line of a printed script or a table rule that the
text layer sets flush left ("#  ------  -----", a bare "#"), which the chunker
cut at as a heading and left as a chunk of one line. The fix (a heading must
hold a letter or digit) is in `convert_manual.py`, so it reaches a document only
when that document is converted again: reconverting `atpg-gd` in the pilot below
left no empty chunk (the other two were not converted). The 12 stay in the work
copy until they are.

## Prose pilot: `convert_manual.py` against Docling (2026-11-12)

Three prose manuals converted two ways in copies of the work corpus and each
copy indexed whole: (a) `convert_manual.py --shape prose` as it is on `main`,
then `enrich_chunks.py` (which writes the breadcrumbs; without it every chunk
fails the checker's breadcrumb check), figures extracted again and read with
Tesseract; (b) `convert_docling.py` (Docling was already installed), same
figure steps. `bsdxg` is the small one (227 pages), `dftug` the large (1,168),
`atpg-gd` the Tessent one (780). Docling stopped on `atpg-gd` twice, once inside
a batch and once alone, with a segmentation fault in the layout model's
convolution ("not enough memory: you tried to allocate 13,107,200 bytes"); the
unchanged conversion stands in for it below. Content is `check_corpus.py`'s
share of 30 sampled PDF pages' words found in the chunks claiming the page.

| | current (no pages) | (a) `convert_manual.py` | (b) Docling |
|---|---|---|---|
| **`bsdxg`** chunks / median characters | 81 / 5,076 | 152 / 314 | 46 / 8,647 |
| pages on chunks | 0% | 100% | 100% |
| figures tied to a section | 97.7% | 100% | 97.7% |
| content | 98.1% | 94.5% | 96.0% |
| `--strict` | WARN (no pages) | ok | ok |
| conversion time | | 61 s (0.27 s a page) | 148 s (0.65 s a page) |
| **`dftug`** chunks / median characters | 402 / 5,520 | 691 / 459 | 226 / 8,660 |
| pages on chunks | 0% | 100% | 100% |
| figures tied | 92.9% | 98.7% | 99.1% |
| content | 99.0% | 95.7% | 95.3% |
| `--strict` | WARN | ok | ok |
| conversion time | | 271 s (0.23 s a page) | 668 s (0.57 s a page) |
| **`atpg-gd`** chunks / median characters | 765 / 1,256 | 442 / 1,799 | crashed |
| pages on chunks | 0% | 100% | |
| figures tied | 99.1% | 100% | |
| content | 100% | 99.9% | |
| `--strict` | WARN (empty chunks, no pages) | ok | |

Search, 138 text and figure questions, whole index each time (the questions in
these three manuals are 12 of them):

| | hit@1 | hit@3 | hit@5 | MRR | median characters to reach the answer, page + section whole / with passages |
|---|---|---|---|---|---|
| current | 71% | 88% | 92% | 0.801 | 9,921 / 9,528 |
| (a) | 70% | 88% | 91% | 0.795 | 10,117 / 9,618 |
| (b) | 72% | 88% | 91% | 0.804 | 10,117 / 9,616 |

Moves against the current conversion: (a) q09 1 to 4 (`dftug`), q03 1 to 2
(`atpg-gd`), f12 2 to 1, q05 5 to 6 and q103 6 to 7 (documents not converted);
(b) f12 2 to 1, q05 5 to 6, q103 6 to 7. Nothing in (b) moved a question
of the converted manuals, which is no evidence for it: only two manuals were
converted and each has few questions. The cost of reaching the answer is
within 2% of the current conversion either way.

What the table shows and does not. Both converters give every chunk a page, pass
the strict check once the breadcrumbs are there, and tie the same or more
figures. Chunk shape differs a great deal: `convert_manual.py` cut `bsdxg` into
chunks with a median of 314 characters (the current ones: 5,076), where Docling
merges to about 8,650; the retrieval numbers above do not tell which suits an
agent better. Content found in the chunks fell by 3 to 5 points for both on
`bsdxg` and `dftug`; the sample is 30 pages, so a difference of that size is one
page's words, and which words were lost was not read. Docling is 2.4 times
slower, needs several gigabytes, and failed on one of three. Which converter
the older editions get is the user's decision.

## Caveats

- Small: 76 questions, 57 text and 19 figure, so one question moves a text
  score 1.75 points and a figure score 5.
- Every question was written by someone who had just read its answer, which
  flatters lexical search. `paraphrase` measures that bias; `real` questions are
  the only ones free of it.
- First search only. An agent that reads results and searches again does better
  than hit@5 says.
