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

## Caveats

- Small: 76 questions, 57 text and 19 figure, so one question moves a text
  score 1.75 points and a figure score 5.
- Every question was written by someone who had just read its answer, which
  flatters lexical search. `paraphrase` measures that bias; `real` questions are
  the only ones free of it.
- First search only. An agent that reads results and searches again does better
  than hit@5 says.
