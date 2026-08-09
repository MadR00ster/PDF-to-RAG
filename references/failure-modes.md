# Failure modes, with measurements

Everything here was observed on a real corpus: 24 EDA vendor manuals (Synopsys
+ Siemens Tessent), 14,860 chunks, converted with `pymupdf4llm`. Numbers are
included because they change how seriously to take each item.

**Read §2 first.** Items 1, 2, 3 and 4 all descend from the same root cause —
the extractor inferred document hierarchy from font size, so every structural
decision downstream was built on a guess. A structure-aware extractor such as
Docling parses into a real document model and would likely have prevented most
of that cluster, at the cost of speed. See "Choosing an extractor" in SKILL.md.
The remaining items (furniture, recursion, idempotency, verification) are
extractor-independent and apply regardless.

## Contents

- [1. Orphaned sub-chunks](#1-orphaned-sub-chunks-the-dangerous-one)
- [2. Font-size headings are not structure](#2-font-size-headings-are-not-structure)
- [3. Text-scanning cannot recover entity ownership](#3-text-scanning-cannot-recover-entity-ownership)
- [4. Chunk-then-label merges entries](#4-chunk-then-label-merges-entries)
- [5. Frequency-based furniture detection deletes content](#5-frequency-based-furniture-detection-deletes-content)
- [6. Furniture becomes chunk boundaries](#6-furniture-becomes-chunk-boundaries)
- [7. Infinite recursion in the splitter](#7-infinite-recursion-in-the-splitter)
- [8. Unbounded chunk size](#8-unbounded-chunk-size)
- [9. Idempotency traps](#9-idempotency-traps)
- [10. Index builders and backup directories](#10-index-builders-and-backup-directories)
- [11. Verification artifacts](#11-verification-artifacts)
- [12. Things left unfixed](#12-things-left-unfixed)

---

## 1. Orphaned sub-chunks (the dangerous one)

Measured chunk headings across the corpus:

| Heading | Count |
|---|---|
| `Note` | 845 |
| `Description` | 725 |
| `Arguments` | 675 |
| `Usage` | 368 |
| `Examples` | 286 |

~2,000 chunks are sub-parts of an entry with no indication of which entry.
Opening three `Arguments` chunks confirmed none named its command. One began
`-i skeleton_design_input_file` with nothing identifying the owner.

Also: 16.7% of all chunk headings were non-descriptive — 9.2% were `(intro)`
(heading detection simply failed), plus bullet fragments and bare chapter
numbers like `6` or `A`.

Why this is worse than a miss: retrieval returns plausible-looking flags, and
the model attributes them to whatever the user asked about. The user has no
signal that anything went wrong.

## 2. Font-size headings are not structure

`pymupdf4llm` infers heading levels from font size. That produces:

- Procedure steps promoted to chapters — `Design Compiler® User Guide ›
  Specify the libraries` (a numbered step, not a section).
- The document's own running footer becoming a heading, so the document title
  appears as its own ancestor.
- Reference manuals rendered almost completely flat: one 6,414-page manual had
  **95%** of chunks at a single level.

The PDF's bookmark TOC is the authoritative structure and is usually sitting
unused in the manifest. Cross-check headings against it: 35–54% matched, and
those are the ones worth trusting as breadcrumb ancestors.

Flatness (share of chunks at the modal heading level) is a good automatic
classifier. Across 24 manuals it ranged from 29% to 95%; only 36% of chunks
lived in manuals below the 50% threshold.

## 3. Text-scanning cannot recover entity ownership

Three approaches tried, to attach `Arguments`-style chunks to their command
without re-extracting PDFs:

| Approach | Result |
|---|---|
| Walk the `level` hierarchy | Useless — manual is 95% flat |
| Match chunk headings to TOC entries | 12.9% coverage |
| Scan chunk bodies for entity names, track most recent | 99% of chunks got an owner, but only **82.5% of entities were ever anchored** |

The third is the trap. When an entity's name doesn't survive chunking, its
chunks inherit the *previous* entity silently. Verified: content documenting
`tessent -shell` (TOC page 80) was labeled `tessent -diagserver` (page 77),
because the `tessent -shell` anchor line never appeared as a standalone line.

A monotonicity check against TOC page order showed 10.9% of owner switches
going *backwards*, i.e. false anchors from cross-references. Monotonicity
constraints don't help with the missing-anchor case, which is the larger one.

Also checked and rejected as a cheap source of page numbers: page delimiters in
`full.md` (none present — 96 horizontal rules across 6,414 pages) and footer
page numbers (worked on a 158-page prose manual with a 117-long monotonic run;
on the 6,414-page reference, 87 numbers with values like `0, 0, 1, 1, 0, 1100`).

**Conclusion: go through pages, from a page-aware extraction.** The TOC is
complete (1,319 and 1,334 entries with pages in the two reference manuals), so
once chunks have page ranges the mapping is exact.

## 4. Chunk-then-label merges entries

Even with correct page→entity mapping, labeling chunks after chunking is
insufficient. The splitter accumulates text up to its size limit and merges
several entries into one chunk. Measured after a first rebuild:

- `syn2`: 1,853 chunks for 1,334 commands — **72% straddled a boundary**
- `tshell-ref`: **26.7%** straddled

Fix: split `full.md` into one region per entity *before* chunking, using each
entity's TOC page, refined to the exact line naming it (two entries can share a
page). Result: `tshell-ref` straddle fell to 0.6%, and `syn2` to zero actual
text leaks across all 1,334 boundary pairs.

Note the metric subtlety: `syn2` still *reported* 1,334 straddles afterwards,
because an entry ends mid-page and the next begins on that same page, so page
ranges touch. Checking the chunk text directly showed zero leaks. Measure text
overlap, not page overlap.

## 5. Frequency-based furniture detection deletes content

First attempt classified any line repeated ≥ N times as furniture. It flagged:

- `insert_dft`, `create_test_protocol` — **real command names**
- `"The following syntax specifies this property:"` (244×)
- `"where valid values are as follows:"` (175×)
- `"Note the following:"` (127×)

6,100 distinct lines were queued for deletion when real furniture is ~2 patterns
per document.

Fix — restrict by shape before frequency is considered:

- Only the document's own running title and the feedback link qualify.
- Single-token lines are identifiers, never furniture (this one distinction
  removes the whole command-name class).
- Skip markdown structure (headings, tables, bullets, code fences) and HTML
  comments.
- Take bare page numbers and `Chapter N:` headers only when adjacent to a
  confirmed furniture line.

After the fix: 27,569 lines removed across 22 manuals with **zero content lines
deleted**, verified by enumerating and categorizing every distinct deletion.

## 6. Furniture becomes chunk boundaries

A standalone `**Feedback**` line is exactly the shape a dictionary-mode splitter
looks for. Chunking before stripping produced 220 chunks where 55 were correct —
one junk chunk per page.

Strip per page, before concatenation, so page offsets stay valid for later
attribution.

## 7. Infinite recursion in the splitter

In dictionary mode, an entry larger than `MAX_CHUNK` with no internal headings
re-matched the bold entry name it already began with. `split_at` returned one
block identical to the input, and recursion never terminated → `RecursionError`.

This crashes on exactly the manuals `--dictionary` exists for. It survived an
initial synthetic test because the test's entries were ~1 KB — under the limit,
so the oversized path never executed. **A test that never reaches the code path
is not a test.**

Guard: bail to paragraph packing whenever a split fails to produce 2+ pieces.
Stated generally — every recursive step must strictly shrink its input.

## 8. Unbounded chunk size

Text with no blank line (a large table) produced a single 12.5 KB chunk against
a 9 KB target. Add a hard wrap at a line boundary as the final fallback.

For calibration, the original hand-built corpus had 8 chunks over 9 KB out of
14,860, max 9,922 — mild overage is normal, unbounded is not.

## 9. Idempotency traps

- Breadcrumb detection keyed on the `›` separator, so title-only breadcrumbs
  (no separator) weren't recognized and got re-prepended every run — 2,117
  chunks would have accumulated duplicates. Key off the document title instead.
- An index builder wrote `ensure_ascii=False` while existing files used escapes,
  and added a trailing newline where the originals had none — producing diff
  churn on every regeneration. Match existing conventions or accept one
  deliberate, documented one-time change.

## 10. Index builders and backup directories

`rebuild_reference.py` originally left the previous version at
`docs/<slug>.old`. `build_index.py` globs `docs/*/manifest.json`, so the backup
was listed as a real document — 13 manuals instead of 12.

Keep backups outside `docs/`, and defensively skip `.old` / `.new` / dot-prefixed
directories in any index builder.

## 11. Verification artifacts

A verification pass reported **5,347 deleted content lines**. Investigation
showed the checker compared raw lines while the transform applied a whole-text
`.strip()`, so first/last lines losing surrounding whitespace registered as
deletions. Normalizing per line brought it to 2, and those turned out to be
leading-whitespace normalization on a first line. Real count: **zero**.

Confirm the checker before acting on an alarming result. Overshooting on
verification is cheap; shipping a corpus with holes is not.

## 12. Things left unfixed

Recorded so a future pass doesn't rediscover them as surprises.

- **No retrieval layer.** The corpus is chunked markdown plus grep. Grep is
  genuinely strong for exact identifier lookup and useless for conceptual
  questions. Hybrid BM25 + embeddings is the natural next step.
- **Page coverage is partial** — 46% of chunks, i.e. only the documents rebuilt
  with page tracking. A full-corpus rebuild would fix it at the cost of hours.
- **Figure text is unusable soup** — `<!-- Start of picture text -->SoC<br>CPU<br>…`.
  Diagram labels with no structure, diluting embeddings. Left in place because
  deleting the markers would make it indistinguishable from prose.
- **Copyright/legal boilerplate** survives as chunk 001 of most documents.
- **Residual footers** where a document's footer is a version string rather than
  its title (`Version T-2022.03`), which title-matching doesn't catch.
- **TOC noise** — a `Contents` entry sitting at the same TOC level as real
  entries gets treated as one.
