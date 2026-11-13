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

## Whole-corpus baseline (2026-11-12)

The 76 questions above were drawn when the corpus had 24 manuals. Production
now has 34 documents (26,491 chunks), and ten of them, 12,364 chunks, had no
question: the eight PrimeTime-family manuals (`pt-cmds`, `pt-errors`,
`pt-vars-attrs`, `pt-invoke`, `primelib-cmds`, `primelib-ug`, `psug`, `ptug`)
and two older IJTAG editions. The set now has 146 questions, over a copy of
production's live index (`mcp-index.sqlite3`, built 2026-11-10, code `1fa341f`).

What changed in the set:

- Every answer of the 76 also carries a `quote` form (`doc_id`, `version`, a
  short phrase that is in that section and no other of its document). Scored
  with the file form, the quote form, or both, the 76 give the same rank on
  every question (`eval_search.py --compare`: 0 rank changes).
- 70 new questions, written with the answer section open and carrying `quote`
  answers only: 43 for the eight PrimeTime-family manuals (5 or 6 each, 3 for
  `pt-invoke`, whose whole text is 4 chunks), 4 figure questions
  (2 each for `ptug` and `primelib-ug`), 15 top-ups so that every current
  document has at least 3, and 8 `version` questions for the IJTAG manual.
  Of the large chunks (`psug`, `ptug`, `primelib-ug`: 8,000 to 9,000
  characters each) the question author read the opening of each section, not
  all of it, and wrote each question from what was read.
- A `version` question carries `document` and `version`, and `eval_search.py`
  searches that edition only, as `search_docs` does. Before this change every
  question was a plain search, which answers from the edition in use, so a
  question about an older edition could only score as a miss. The 8 `version`
  questions (2022.4, 2025.2 and 2026.2 asked the same two questions, and the
  last two asked of 2025.2 and 2026.2 alone) all rank first: a scoped search
  of one edition tests the scoping, not ranking across editions, so that 100%
  says little about retrieval.

Scores, through the shipped defaults:

| set | kind | n | hit@1 | hit@3 | hit@5 | MRR |
|---|---|---|---|---|---|---|
| old 76 | identifier | 26 | 81% | 100% | 100% | 0.897 |
| | concept | 20 | 70% | 90% | 90% | 0.800 |
| | paraphrase | 11 | 45% | 64% | 73% | 0.538 |
| | figure | 19 | 63% | 89% | 95% | 0.774 |
| | all | 76 | 68% | 89% | 92% | 0.789 |
| new 70 | identifier | 22 | 91% | 100% | 100% | 0.947 |
| | concept | 25 | 76% | 88% | 100% | 0.835 |
| | paraphrase | 11 | 64% | 73% | 73% | 0.692 |
| | figure | 4 | 50% | 75% | 75% | 0.625 |
| | version | 8 | 100% | 100% | 100% | 1.000 |
| | all | 70 | 80% | 90% | 94% | 0.854 |
| all 146 | identifier | 48 | 85% | 100% | 100% | 0.920 |
| | concept | 45 | 73% | 89% | 96% | 0.819 |
| | paraphrase | 22 | 55% | 68% | 73% | 0.615 |
| | figure | 23 | 61% | 87% | 91% | 0.748 |
| | version | 8 | 100% | 100% | 100% | 1.000 |
| | all | 146 | 74% | 90% | 93% | 0.820 |

The new questions score above the old ones, and the cause is not known. Their
authors read the answer first, as the old ones did, and the PrimeTime-family
manuals are made of small entries (a variable, a command or a message each)
whose first line names the thing a question names, which a lexical index
matches well. The old set's paraphrase row (45% at rank 1) is still the
weakest, and nothing new measured here changes it.

Every new question that did not rank first was read against what ranked above
it before it was recorded. Six answers, in five questions, were added where
another section answers as well, each with a note in the question file:

- `report_variation_bottleneck` (q59): the command's own man page in
  `pt-cmds` ranked first and answers the question.
- `si_multi_input_switching_tied_input_filter` (q63): the variable's man page
  in `pt-vars-attrs` ranked first and lists the same values; the user guide's
  continuation chunk is a second answer.
- `gui_create_pref_key` (q79): the question names no tool and Design
  Compiler's reference documents the same option in the same words.
- Ring against hierarchical SMS architecture (q104): the two sections say in
  the same words that only a complete ring can be bypassed.
- Defect test summary table (q110): the sibling Silicon.da guide's table says
  the same as the Yield Explorer guide's; the question names no product.

Four new questions still miss, recorded as they are: q66 (extracted timing
models and level shifters, a reworded question: nothing in the top ten was
found to answer it), q72 (names no option or tool; PrimeTime's
minimum period section ranks first, and the question was first filed as
`identifier` although it names no identifier, so its kind was changed to
`paraphrase` after the first score, the rank unchanged), q78 (rank 9: the user
guide's ECO flow sections outrank the command that writes the session) and f22
(a figure whose words are raster and OCR read badly). The six old misses stand
as before (q01, q04, q25, q30, q34, f09).

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

Two changes to the index are built, tested and off by default. Both were
measured on 2026-11-12 (next section); choosing a default is a separate
decision from building them, and is the user's.

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

## Index-only measurements (2026-11-12)

Four changes that need no reconversion, each scored on the 146 questions of the
whole-corpus baseline above against a control: an index rebuilt from the same
converted corpus with the code on `main` (`1fa341f`), which reproduces
production's live index exactly (74% hit@1, 90% hit@3, 93% hit@5, MRR 0.820,
26,491 chunks). One scratch index per measurement, built with `--out` into a
work copy of the corpus; `eval_search.py --compare` read question by question.

| change | what was built | hit@1 | MRR | questions that moved |
|---|---|---|---|---|
| control | `main` | 74% | 0.820 | |
| `--body-without-breadcrumb` | flag on | 73% | 0.813 | 3: q23 1 to 2, f15 1 to 2, q78 9 to 10 |
| `--ident-index` | flag on | 75% | 0.824 | 1: q74 2 to 1 |
| front matter demoted by its whole heading | index built with the old rule (any heading starting with a front-matter word) restored | 74% | 0.820 | none |
| entity named inside a question is boosted | server with the old rule (only a query that is the entity, or a prefix of it) restored, control index | 74% | 0.822 | 1: q103 2 under the old rule, 5 now |

Per kind (hit@1, control to flag): `--ident-index` raises identifier from 85% to
88% (one question, q74) and no kind falls; `--body-without-breadcrumb` leaves
identifier at 85% and lowers concept from 73% to 71% and figure from 61% to
57%. The rule on record is that a flag is adopted only if identifier hit@1
rises and no kind falls: `--ident-index` meets it on one question, which is a
small basis, and `--body-without-breadcrumb` does not. Whether to turn
`--ident-index` on is the user's decision.

The last two rows are the changes already on `main`, measured by putting the
old rule back, so "moved" reads old rule to current. Demoting front matter by
its whole heading changed no rank on these questions (none of them is answered
by a section whose heading merely begins with "Index" or "Contents"). The
entity rule moved one question the wrong way: q103 ("what clock timing does
dft_drc assume ...", answered in the Boundary Scan guide) ranked 2 under the old
rule and 5 now; nothing moved the other way. The rule exists for questions such
as "what does set_scan_configuration do", of which this set has few that the
old rule missed; one question is the whole difference.

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

## Prose chunk size: cause, fix, and the pilot redone (2026-11-12)

The first pilot cut `bsdxg` into 152 chunks with a median of 323 characters (51
under 200) and `dftug` into 691 with a median of 473, against 5,231 and 5,650 in
the work copy's existing conversions (production's).

**Cause.** Not a change in the code. `convert_manual.py` was run on `bsdxg` at
eight commits from the first (`6c2e7d0`, 2026-08-09) to `dbe40aa`: every one
gives 152 to 156 chunks with a median of 281 to 362, so the chunker has cut
there since it was committed. The first commit's output has the same
`full.md` as production's (401,597 characters, the same 88 level-1 and 34
level-2 headings), so the difference is in the chunking, not the extraction:
the manual sets its captions and callouts in a larger font, pymupdf4llm writes
them as `#` and `##` lines, and the chunker cuts at each. Production's
`bsdxg` has 81 chunks and its sections folder is dated 2026-08-09 21:09,
before the first commit; no committed version of the chunker reproduces its
boundaries (variants of the top-level split matched 68 of its 79 chunk starts at
best), so it was written by an earlier version that is not in git. Joining a
chunk smaller than 500 characters to the next gives 82 chunks for `bsdxg` and
397 for `dftug`, against production's 81 and 402.

**Fix.** `pack_small` in `convert_manual.py` joins a chunk under `MIN_CHUNK`
(500 characters) to the one after it, never past `MAX_CHUNK` and never across a
chunk that opens a bookmarked section (its heading, without numbering and
emphasis, is a title in the PDF's outline). A joined chunk starts at its first
piece's heading and ends at its last, so it is a slice of the text and its pages
come from `trim_span` and `page_range` as before. Test 11e fails without it. The
fixture method (convert every fixture PDF with `main` and with the change,
`diff -r`): the only difference is the manifest's timestamp, except in `tiny`
(a reference below the command threshold, converted as prose), whose
SYNTAX and ARGUMENTS pieces under 500 characters now join their entry's heading.

### Three arms on the same three manuals

Current is the work copy's existing conversion. Fixed is `convert_manual.py` with
`pack_small`, then `enrich_chunks.py` for the breadcrumbs. Backfill is a
prototype (not repo code) that leaves the existing chunks as they are and adds
`page_start` and `page_end` by matching each chunk's opening and closing words
against the PDF's page text. Content is `check_corpus.py`'s share of 30 sampled
pages' words found in the chunks claiming the page.

| | current | fixed `convert_manual.py` | in-place backfill |
|---|---|---|---|
| **`bsdxg`** chunks / median / under 200 | 81 / 5,231 / 5 | 82 / 4,007 / 0 | 81 / 5,231 / 5 |
| pages on chunks | 0% | 100% | 100% (97.5% matched, 2 chunks from the next one's page) |
| figures tied | 97.7% | 100% | 100% |
| content | 98.1% | 94.5% | 96.4% |
| `--strict` | WARN (no pages) | ok | WARN: 1 figure on the page after its section |
| **`dftug`** chunks / median / under 200 | 402 / 5,650 / 26 | 397 / 5,433 / 1 | 402 / 5,650 / 26 |
| pages on chunks | 0% | 100% | 100% (94.0% matched; 24 from the next, 3 ends widened) |
| figures tied | 92.9% | 98.7% | 98.4% |
| content | 99.0% | 95.7% | 96.4% |
| `--strict` | WARN (3 furniture, no pages) | ok | WARN: 2 figures, the 3 furniture |
| **`atpg-gd`** chunks / median / under 200 | 765 / 1,285 / 163 | 269 / 6,716 / 0 | 765 / 1,285 / 163 |
| pages on chunks | 0% | 100% | 100% (98.7% matched, 10 from the next) |
| figures tied | 99.1% | 100% | 99.4% |
| content | 100% | 99.9% | 99.9% |
| `--strict` | WARN (2 empty, no pages) | ok | WARN: 2 empty (not touched), 1 figure |

The fixed `atpg-gd` is coarser than the existing one (269 chunks, median 6,716,
against 765 and 1,285): the existing conversion cut it finer than any version of
this chunker does, and the fix does not bring that back.

Search, 138 text and figure questions, whole index each time, `--compare`
against the current conversion:

| | hit@1 | hit@3 | hit@5 | MRR | median characters to the answer, whole / passages |
|---|---|---|---|---|---|
| current | 71% | 88% | 92% | 0.801 | 9,921 / 9,528 |
| fixed, first pilot (no `pack_small`) | 70% | 88% | 91% | 0.795 | 10,117 / 9,618 |
| fixed | 70% | 88% | 91% | 0.795 | 10,117 / 9,620 |
| backfill | 70% | 88% | 92% | 0.797 | 9,942 / 9,528 |

`pack_small` changes the sizes (above) and moves one of the first pilot's five
questions (q09, 4 to 3); the others move as before, so they were not about size.
q09 (hybrid flow, scan cells outside any subpartition) goes from 1 to 3 because
its answer phrase sits at the start of an 8,398-character chunk of the current
conversion and, in the fixed one, in two chunks of 603 and 3,640 characters; the
sentence that answers it ("If some scan cells exist in a top-level partition but
...") is in one chunk that ranks 2, so by that sentence the move is 1 to 2. q03
(`atpg-gd`: when random patterns stop) goes from 1 to 2 because the answer's
2,315-character chunk of the current conversion is part of an 8,947-character
one in the fixed. f12 rose (2 to 1) and q05 and q103 fell by one in manuals
that were not converted. An answer written as a phrase moves with the chunk
boundaries, so a comparison between two chunkings is a comparison of where the
phrase landed.

Backfill, built with the new `figures.json`: one move among the 138, f15 1 to 2.
The bodies are the current ones, so the cause is not the text: with the page
numbers backfilled and the old `figures.json` the index moves no question; the
new one ties 30 more figures of `dftug` (and 1 each in the others) to sections
by their pages, which puts their text into those chunks. So "0 rank changes"
holds for the backfilled pages and not for the figures they let
`extract_figures.py` tie.

Where the fixed arm and the backfill have a chunk with the same heading (48
pairs in `bsdxg`, 241 in `dftug`, 175 in `atpg-gd`, matching the n-th chunk of
each heading), the start pages are equal in 87.5%, 85.1% and 92.0%, and within
a page in 91.7%, 88.0% and 94.3%. Most of the rest are not disagreements about
one chunk: repeated headings ("Contents", "Table of Contents", "List of
Figures") pair different pieces. Of the 43 pairs more than a page apart, each
chunk's start words are on the page named for it in 39 of 43 (fixed) and 38 of
43 (backfill), so both are mostly right about their own chunk.

Backfill, of 1,248 chunks: 1,212 (97.1%) were matched by their opening words (the
first 8 words, then 6, 5, 4, searched from where the previous chunk began, trying
up to 40 words into the chunk). 36 more are chapter-title stubs and front matter
too short to match (33, under 3 words) or had no match (3), and took the page
their next neighbour starts on. 3 chunks' closing words were not found: they got
their start page and an end page no later than the next chunk's. It cannot place
a chunk whose words the PDF's text layer writes differently, and it never looks
backwards, so one wrong early match would take the rest with it (the tool
counted chunks whose start page ran backwards from an earlier one's: none). Not vouched for: the page of a chunk with no match, which is a guess,
and the end page where the closing words matched an earlier repeat (4 figures
sit on the page after their section's backfilled end).

### What the content shortfall is

On the sampled pages of the fixed arm only two are below 90%: p219 of `bsdxg`
(87%) and p202 of `dftug` (89%). The words missing from the chunks that claim a
page are of two kinds, and neither is text the converter dropped. (1) Running
headers and footers the converter's furniture pass removes on purpose:
"feedback", "chapter", and the words of the chapter title on every page
("components", "inserting", "registers"). (2) Words that exist only in PyMuPDF's
reading of the page, not in the chunks: two or three words run together
("delaytest", "periodtest", "variablesetingtest") and words with a doubled
letter missing ("comand", "atribute", "mesage", "controler", "asembly"), which
are the PDF's text layer for table cells and double-letter ligatures and are in
no chunk of the document. Over all 218 pages of `bsdxg` the mean is 93.3% and 49
pages are under 90%; `dftug` 96.2% and 58 of 1,152; `atpg-gd` 100%. The current
conversion scored higher (98 to 100%) because it kept the header lines. Whether
that is a loss depends on whether a header is content: for citing a chapter it
is not. The pages under 90% on all pages were not read one by one; the eight
lowest of each sample were.

## Page numbers in place: `backfill_pages.py` over 23 documents (2026-11-12)

The user decided that existing manuals get pages in place (the pilot above:
reconverting moved five questions, in place moved none). The prototype became
`scripts/backfill_pages.py`. It writes only `page_start` and `page_end` to the
manifest (bodies, `full.md`, file names and headings are not opened for
writing), copies the manifest to `.rebuild-backup/manifests-<date>-pre-pages/`
first, and has `--dry-run` and `--redo`.

**What differs from the prototype.** Matching is on characters, not words: both
texts are cut to lower-case letters and digits with every run of one repeated
character collapsed, so a text layer that runs words together or drops a doubled
letter ("comand") still matches. A chunk's start is estimated from every probe
slid along its first 600 characters, and the estimate the most probes agree on
is taken (ties: nearest where the chunks before it leave off). That fixed two
real mistakes found while building it: a table header that is the same in a
dozen tables (`ye-data-intg`, the one `--strict` FAIL of the first run), and a
cross-reference to the next chapter inside the previous chunk's "Related
Topics". The end is the last words of the chunk found between its start and the
next chunk's, no nearer than 0.6 of the chunk's length from its start (a 7,914
character chunk had ended on its own first page, on an earlier repeat of its
closing words: `tshell-ijtag-user-2025-2` p131). A start more than 150,000
characters beyond where the search stands must be confirmed by a second probe,
so one repeated phrase cannot carry every later chunk with it. A chunk is still
never searched for behind the previous one's start, so a wrong early match is
bounded by that confirmation, not undone; the run found no chunk whose start
page ran backwards (`pages-backwards`: none in 23 documents).

**Chunks that cannot be matched** (under 3 words or 16 letters: chapter titles,
front matter; 189 of 7,647) sit between two placed chunks. If the one before
ends on the page the one after starts on, the page is known and not flagged
("bounded", 57). Otherwise the chunk takes the page the next one starts on, and
the manifest says so: `"pages_from": "neighbour"` on the section (132). A chunk
whose start was found and whose closing words were not gets `"page_end_from":
"neighbour"` where its end page differs from its start (35; the end is the page
the next chunk starts on, an upper bound). `manifest.schema.json` allows both
and a `pages_backfill` record of the counts. Nothing was left unplaced (0).

How far a guess can be off: a flagged chunk lies between the previous chunk's
end page and the next chunk's start page. That gap is 0 or 1 page for 89 of 118
(75%), 2 for 21, 3 to 5 for 8 (14 more sit at the edge of a document), so the
page given is within two pages of the truth for 110 of 118 (93%).

**`check_corpus.py` knows the flag** (`pages_from_neighbour` in the metrics; test
18b fails without it): a guess is not held to the outline, because
`ancestor-contradicts-pages` and `entity-contradicts-pages` are FAILs and a
chapter stub can sit on a different page from the outline's entry. It does not
warn on the flag: 132 chunks in 23 documents are stubs. **The server does not
know it**: a stub's citation reads "p. N" with N the page its chapter starts on,
which is where a reader would look. If a flagged page proves misleading in use,
the server could print "p. ~N"; there is no evidence yet.

### Accuracy

Two measurements, neither a gold standard.

*Against the PDF's outline.* A chunk whose first heading equals exactly one
bookmark title has that bookmark's page as its true start page: 4,060 of the
7,647 chunks. The start page equals the bookmark's in 96.6% (3,920) and is
within one page in 97.0% (3,937); lowest `ye-data-intg` 86% and `tmax-ug` 87%.
Most misses are false golds: the table of contents is cut into many chunks all
headed "Contents" (23 of `tmax-ug`'s 30 misses; without them 96.6%). The
prototype on the three pilot manuals scored 176 of 196, 29 of 33 and 417 of 429
against 180, 30 and 422 here.

*Chunk words on the claimed pages*, the spot check from the pilot (a stride
sample of the chunks; the first 12 distinct words of 4 letters or more must be
on the start page, 75% of them, and the last 12 on the end page or the one
before): `bsdxg` 78 of 79, `dftug` 182 of 188, `atpg-gd` 191 of 193 (prototype:
78, 183, 193). The pilot's "47 of 54 on `bsdxg`" was taken with a rule that was
not recorded and is not comparable. Every sampled chunk has its words
somewhere in its page range (`bsdxg` 79 of 79).

The end page is the weaker half. It can be a page early or late: a chunk that
closes on the lines naming the previous and next topic (these manuals print
them at a page's foot) ends where those are, and the nearest repeat of ordinary
closing words can be on an earlier page. `silicon-da-loc-data-intg` p439 is an
example: the content check reads p439 as 62% in the chunk that claims it,
because the chunk before it is given an end on p437 and the page's first lines
are its tail. Not vouched for: any single end page.

### Per document

All 23 documents with no pages in the work copy: `backfill_pages.py` (23 s in
all), then `extract_figures.py` and `ocr_figures.py`. The two figure scripts
were run over the whole of each collection, because the helper that built the
`--only` list failed; documents that already had pages were extracted again.
"Tied" is `check_corpus.py`'s `figures_attached`. Content is its sampled-pages
share; with pages it compares each sampled page with the chunks that claim it,
without pages with the whole document, so the before and after columns measure
slightly different things.

| document | chunks | matched | end from next | bounded | neighbour | figures | tied before | tied after | content before | content after | --strict after |
|---|---|---|---|---|---|---|---|---|---|---|---|
| bsdrm-q-2019-12 | 64 | 63 | 0 | 0 | 1 | 87 | 94% | 99% | 93% | 90% | WARN: figure-outside-section |
| bsdxg-q-2019-12 | 81 | 79 | 0 | 0 | 2 | 44 | 98% | 100% | 98% | 96% | WARN: figure-outside-section |
| dcug-v-2023-12 | 317 | 298 | 0 | 1 | 18 | 225 | 92% | 100% | 100% | 98% | WARN: figure-outside-section |
| dftug-v-2023-12-sp1 | 402 | 378 | 1 | 8 | 15 | 548 | 93% | 99% | 99% | 96% | WARN: figure-outside-section,furniture |
| silicon-da-loc-data-intg | 129 | 119 | 2 | 0 | 8 | 113 | 56% | 94% | 98% | 95% | WARN: content-gap,figure-outside-section |
| smsug-y-2026-03-sp2 | 76 | 75 | 1 | 0 | 0 | 51 | 67% | 84% | 100% | 97% | WARN: figure-outside-section |
| tmax-ug-v-2023-12 | 456 | 413 | 3 | 11 | 29 | 517 | 68% | 97% | 100% | 97% | WARN: figure-outside-section |
| tmsmsvmug-y-2026-03-sp2 | 47 | 45 | 2 | 0 | 0 | 37 | 86% | 97% | 97% | 97% | WARN: figure-outside-section |
| vcs | 446 | 408 | 3 | 1 | 34 | 263 | 75% | 87% | 100% | 98% | WARN: furniture |
| ye-data-intg | 96 | 89 | 3 | 0 | 4 | 120 | 70% | 98% | 100% | 100% | ok: ok |
| 9-tessent-deterministic-on-chip-compare-diagnosis-tessent-streaming-scan-network | 5 | 3 | 0 | 0 | 2 | 34 | 71% | 100% | 99% | 99% | ok: ok |
| atpg-gd-2025-2 | 765 | 754 | 2 | 8 | 1 | 348 | 99% | 99% | 100% | 100% | WARN: empty-chunk,figure-outside-section |
| edt-gd-2025-2 | 362 | 358 | 0 | 3 | 1 | 101 | 97% | 100% | 100% | 99% | WARN: figure-outside-section |
| etassemblereference-2021-2 | 518 | 514 | 2 | 0 | 2 | 24 | 100% | 100% | 100% | 97% | ok: ok |
| tessent-diagnosis-user-2025-2 | 396 | 395 | 0 | 0 | 1 | 165 | 95% | 100% | 100% | 100% | ok: ok |
| tessentmbist-useref-2025-2 | 549 | 537 | 7 | 0 | 5 | 69 | 97% | 100% | 100% | 100% | ok: ok |
| tshell-ijtag-user-2022-4 | 75 | 73 | 1 | 0 | 1 | 41 | 100% | 100% | 99% | 99% | ok: ok |
| tshell-ijtag-user-2025-2 | 79 | 78 | 0 | 0 | 1 | 47 | 100% | 100% | 100% | 100% | ok: ok |
| tshell-ijtag-user-2026-2 | 292 | 290 | 1 | 0 | 1 | 85 | 93% | 99% | 100% | 94% | ok: ok |
| tshell-mbist-user-2026-2 | 765 | 757 | 6 | 1 | 1 | 151 | 97% | 97% | 100% | 93% | WARN: figure-outside-section |
| tshell-siliconinsight-2025-2 | 335 | 332 | 1 | 1 | 1 | 73 | 90% | 100% | 100% | 99% | WARN: figure-outside-section |
| tshell-user-2025-2 | 1161 | 1143 | 0 | 17 | 1 | 349 | 94% | 98% | 100% | 100% | WARN: empty-chunk,figure-outside-section |
| tsi-usage-ref-2025-2 | 231 | 222 | 0 | 6 | 3 | 159 | 100% | 100% | 100% | 100% | WARN: empty-chunk |

Across the 23: figures tied 3,178 of 3,651 (87.0%) before, 3,562 (97.6%) after;
mean content 99.2% before, 97.6% after. `--strict`: 0 FAIL, 23 warnings: 32
`figure-outside-section` in 13 documents (a figure on a page outside its
section's pages; the check cannot run without pages), 3 `furniture` in `dftug`,
3 documents with `empty-chunk` (known), 1 `content-gap`. Two documents are still
under the plan's 92% tied: `smsug` (84%) and `vcs` (87%). `bsdrm-q-2019-12`
content went 93% to 90%.

### Search

One index (`bf-pages.sqlite3`) from the work copy, 146 quote-only questions, the
control `p4-furn.sqlite3` reproduced first.

| | hit@1 | hit@3 | hit@5 | MRR |
|---|---|---|---|---|
| control | 73% | 89% | 92% | 0.812 |
| pages in place | 72% | 89% | 92% | 0.808 |

One question moved: f15 (PRPG driving the scan chain inputs, a figure question)
1 to 2. All 123 text questions kept their rank. The cause is the figures, not
the bodies: pages let `extract_figures.py` tie more figures to sections, and a
figure's text is searched with its section (the same move as in the pilot).

## Caveats

- Small: 76 questions, 57 text and 19 figure, so one question moves a text
  score 1.75 points and a figure score 5.
- Every question was written by someone who had just read its answer, which
  flatters lexical search. `paraphrase` measures that bias; `real` questions are
  the only ones free of it.
- First search only. An agent that reads results and searches again does better
  than hit@5 says.
