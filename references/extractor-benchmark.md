# Extractor benchmark: pymupdf4llm vs Docling vs a TOC-anchored hybrid

Measured, not assumed. Everything here comes from running all three paths over
slices of one 26-manual technical corpus and scoring them against each PDF's own
bookmark TOC. Numbers are reproducible from the method below; where a result is
an artifact of the harness rather than a property of a pipeline, it says so.

## Why this exists

The skill's own guidance said Docling "natively [does] much of what `scripts/`
rebuilds by hand", which invited the conclusion that a structure-aware extractor
makes the reconstruction code redundant. A first benchmark on one prose manual
appeared to confirm it. Widening to seven slices across five PDF producers and
both document shapes **reversed that conclusion for reference documents.** The
first round had overfit to prose.

## Method

Seven slices, each starting on a chapter boundary ~40% into its document, each
carrying its bookmark TOC rebased to the slice. All three pipelines see byte-
identical input and are scored against the same ground truth.

| slice | shape | PDF producer |
|---|---|---|
| atpg-gd-2025 | prose | Apache FOP |
| bsdrm-2019 | prose | Ghostscript (2019) |
| dcug-2023 | prose | Ghostscript |
| mbist-2025 | prose | Acrobat Distiller 11 |
| vcs | prose | Acrobat Distiller 22 |
| syn2 | dictionary | Qt 4.8.7 |
| tshell-ref-2026 | dictionary | Antenna House |

Ground truth is the bookmark TOC: a heading it does not contain is unverifiable
and must not become a breadcrumb ancestor. That measures *agreement with
bookmarks*, not truth — a real heading can be absent from a shallow bookmark
tree — so it is a precision proxy, and reported as one.

## Result 1: page provenance — Docling, unambiguously

100% of chunks carry page numbers on all seven slices, every producer. The
pymupdf4llm prose path carries **0%**. Pages are not just for citation: without
them a breadcrumb cannot be audited positionally at all.

## Result 2: heading precision — a tie, and unstable

Headings confirmed by the TOC: pymupdf4llm 43%, Docling 41% on the same slice.
Docling's structure model is **not** measurably better at identifying real
headings, and it promotes its own artifacts — observed being offered as section
headings:

```
'Note:'   'Related Topics'   'Procedure'   '1. Constant value'
'ANALYSIS> analyze_scan_chains'   '> set_case_analysis control_point_en 0'
```

Shell transcript lines. **TOC verification is extractor-independent and still
load-bearing.** 72 distinct headings had to be refused as ancestors on one slice.

The rate is also wildly unstable — 16% to 88% across slices, and **41% vs 61% on
two different regions of the same manual.** No single slice supports a
conclusion. It tracks bookmark density, not the producer:

```
TOC titles per chunk   0.55  0.55  0.48  0.26  0.15  0.09
headings verified       88%   77%   61%   48%   24%   16%
```

## Result 3: entity attribution — the skill wins decisively

For reference documents the decisive metric is whether a chunk can name the
command it documents. Attaching one command's flags to another is the failure
this skill exists to prevent.

| | `rebuild_reference.py` | derived from Docling headings |
|---|---|---|
| syn2 | **99–100%** | 62% |
| tshell-ref | **90–99%** | 16% |

Retiring `rebuild_reference.py` in favour of Docling would be a severe
regression on exactly the documents where wrong attribution is most dangerous.

## Result 4: the hybrid, and how far to trust it

The hybrid takes Docling's pages and the TOC's hierarchy: where the chunk's own
heading is TOC-confirmed, walk that entry's level stack (**anchored**, precise);
otherwise take the stack in force at the chunk's page (**fallback**). It reaches
90–99.8% ancestor coverage with chains averaging ~2.3 levels, against a single
ancestor at best from either pipeline alone.

The anchored share is only 16–87% depending on bookmark density, so the fallback
carries most chunks on sparsely bookmarked documents and needed measuring.
Held-out test — take anchorable chunks, hide the anchor, run the fallback,
compare:

| fallback variant | whole chain correct |
|---|---|
| page granularity | **60.5%** |
| position-refined (y-coordinate on page) | **82.9%** |

Position refinement locates both the TOC heading and the chunk by y-coordinate
via PyMuPDF text search (77.7% of chunks locatable; degrades to page behaviour
otherwise). No new dependency.

That estimate is optimistic — anchorable chunks sit at section starts. Bounding
it on the real fallback population: **47.6% of fallback chunks sit on a page
where no TOC entry starts**, so the stack cannot be wrong; 37.4% have one
possible transition; only 15% are genuinely hard.

Per document, position-refined: prose 82–93%, syn2 66.5%. syn2 is a dictionary
routed to `rebuild_reference.py` anyway — **the document where the fallback
collapses is the one it is never used on.**

## What this implies

1. **Route by document shape, don't pick one extractor.** Prose → Docling +
   TOC anchoring. Reference/dictionary → `rebuild_reference.py`, unchanged.
2. **Keep TOC verification everywhere.** It is what stops
   `ANALYSIS> analyze_scan_chains` becoming a breadcrumb ancestor.
3. **Label fallback ancestors as lower confidence.** ~17% are wrong even
   position-refined; presenting them identically to anchored ones overstates
   what is known.
4. **Pre-flight on bookmark density.** TOC titles per page, and the share of
   pages with 2+ TOC starts, predict both anchoring rate and fallback accuracy
   before converting anything.

Cost: Docling ran at ~1.2 s/page CPU — about 8.6 hours for a 25,000-page corpus,
against seconds for pymupdf4llm.

## Harness bugs found while doing this

Recorded because each first appeared as a pipeline result, and three of the four
would have produced a wrong conclusion:

- Flattening TOC levels to satisfy PyMuPDF's `set_toc` destroyed the level
  *grouping* that `rebuild_reference.py` uses to find commands (tshell-ref keeps
  them at L3, syn2 at L1). Fixed by inserting filler entries that preserve true
  levels.
- `pick_command_level` requires ≥20 command-like titles; a 60-page slice had 18,
  reporting **0%** attribution for a manual that achieves 90%+. Widened the slice.
- A document named a "reference manual" whose TOC is prose sections was routed
  down the dictionary path, making the skill look like it failed at attribution
  when it was correctly declining to invent it.
- The first fallback probe compared ancestors-only against a full stack and
  scored a flat 0.0% — a measurement bug, not a fallback failure.

Slice-based benchmarking also systematically penalises the skill's prose path:
its breadcrumb walk depends on document-wide heading statistics and collapses to
title-only on small slices. Skill figures here are from full documents.
