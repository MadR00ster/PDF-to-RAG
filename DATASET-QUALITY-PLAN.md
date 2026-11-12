# Dataset quality plan

Written 2026-11-12 from `main` at `1fa341f` and a read-only pass over the
production corpus (`C:\Users\image\OneDrive\Playground\EDA Tool Manual`).
`HANDOVER.md` says how to work here; this file is the order of work. Delete a
line when it is done, and the file when it is empty. It carries forward the open
parts of `IMPROVEMENT-PLAN.md` (D2, D3, D5 and phase E); delete that file in the
same pull request that adds this one.

## Where the dataset stands (measured 2026-11-12)

34 documents, 34,458 pages, 26,491 chunks, 13,814 entities, 5,017 figures.
`check_corpus.py`: 0 FAIL, 40 WARN. `eval_search.py` on the 76 questions:

| kind | n | hit@1 | hit@3 | hit@5 | MRR |
|---|---|---|---|---|---|
| identifier | 26 | 81% | 100% | 100% | 0.897 |
| concept | 20 | 70% | 90% | 90% | 0.800 |
| paraphrase | 11 | 45% | 64% | 73% | 0.538 |
| figure | 19 | 63% | 89% | 95% | 0.774 |
| all | 76 | 68% | 89% | 92% | 0.789 |

Six questions miss the top five (q01, q04, q25, q30, q34, f09); eighteen sit at
rank 2 to 5 (q05, q11, q15, q26, q27, q32, q35, q45, q46, q52, q53, q57, f01,
f02, f05, f06, f12, f19).

The gaps, largest first:

| gap | size today | target | how it is counted |
|---|---|---|---|
| Documents with no test question | 10 of 34, holding 12,364 chunks (47% of the index): the 8 PrimeTime-family manuals and the two older IJTAG editions | 0 current documents without questions | questions per slug |
| Real questions | 0; the call log file does not exist yet (switched on 2026-11-10) | first triage from 2026-11-24 | `triage_log.py --summary` |
| Answer-level score | never run | one live run recorded | `eval_answers.py` |
| Chunks with no page number | 7,647 chunks in 23 documents (14,199 pages) | 0 | checker `pages` |
| Page furniture left in chunks | 4,088 chunks (15%); 3,070 in `tshell-ref-2026-2`, 357 `etassemblereference`, 335 `tshell-mbist-user`, 142 + 73 IJTAG, 88 `ye-data-intg`, 17 `vcs`, 3 each `dftug`, `pt-cmds` | under 50 | checker `furniture` |
| `tshell-ref-2026-2` entries | 66% of owned chunks name their owner; 104 chunks headed by another command; 1,171 chunks under 200 characters; 23 empty | 90% or more named; 0 mismatches; 0 empty | checker `entities`, `empty-chunk` |
| Empty chunks elsewhere | 12 (`atpg-gd` 2, `tshell-user` 5, `tsi-usage-ref` 5) | 0 | checker `empty-chunk` |
| Figures tied to no section | 594 of 5,017 (12%); worst: `tmax-rules` 34% tied, `silicon-da-loc-data-intg` 56%, `smsug` 67%, `tmax-ug` 68%, `ye-data-intg` 70%, `vcs` 75% | under 8% | checker `figures_attached` |
| Older editions not converted | 10 PDFs, about 17,000 pages (the entries of the two `superseded.json` files) | converted, or left out on purpose | `update_corpus.py --dry-run` |
| PDF words found in chunks | 93% to 100%; lowest `bsdrm-q-2019-12` 93% | explain every document under 95% | checker `content` |

## What to expect

These are estimates, not measurements. Replace each with the number once taken.

- **hit@5 on the 76 will barely move: 92% to 92–95%.** Four of the six misses
  are vocabulary gaps (q04 and q30 ask about "toggle activity" where the manual
  says "low power"), one is a slide deck in five chunks, one is a waveform OCR
  reads badly. Cleaning chunks does not close a vocabulary gap, and embeddings
  were measured and rejected.
- **hit@1 is where cleanup can show: 68% to roughly 70–75%** (2 to 5 of the 18
  near-misses). Nine questions answer from `tshell-ref`, the document with the
  most furniture and mislabelled fragments; the `--ident-index` flag exists for
  identifier questions at rank 2 and 3.
- **The honest headline will probably drop first.** Half the index has never
  been scored. The three Docling guides have chunks of about 8,700 characters
  spanning a median of six pages; expect them to find the section but rank and
  cost worse. `pt-errors` (8,656 small message chunks) should score near 100%
  on identifiers. A corpus-wide number comes from phase 1, not from a guess.
- **The structural numbers are the sure gains:** pages on 71% of chunks to
  100%, furniture 15% to under 0.2%, warnings 40 to under 10.

## Rules for every phase

- Production is read-only. Work on a copy (`H:\Playground\dq-work\`, outside
  the repo), build indexes with `--out` into scratch, and never write under
  the OneDrive folder. Bringing production up to date is a separate step that
  needs the user's go-ahead each time, done as on 2026-11-09: backup, candidate,
  smoke test, `eval_search.py --compare`, swap.
- A reconversion is a retrieval change: `check_corpus.py --strict` on the
  document and `eval_search.py --compare` before and after, and read every
  question whose rank moved.
- Changing a default (a search flag, the prose converter) is the user's
  decision. Report the numbers and stop.
- Write each result into `references/retrieval-measurement.md`. Never write a
  number that was not measured.
- Repo changes go on a branch with a pull request, a test with each fix.

## Phase 3: repair the reference manuals

1. Port the unmerged branch `reference-chunks` (`28385d9`) onto `main`: footer
   lines like "5-138 Software Version 2026.2" stripped as furniture, titles
   compared without ®, ™ and `<mark>`. Its tests are in the old
   `tests/test_pipeline.py`; move them to `tests/test_converters.py`. Entry
   packing (`pack_adjacent`) is already on `main`; check before porting it again.
2. Reconvert `tshell-ref-2026-2` in the work copy, re-extract and OCR its 513
   figures. Targets: furniture 3,070 to about 0, heading mismatches 104 to 0,
   owner named 66% to 90% or more, no empty chunk.
3. Reconvert `pt-cmds` only if the checker or the phase 1 questions show the
   Tcl-comment scraps hurting (169 chunks under 200 characters).
4. `tmax-rules`: 49 of 74 figures are tied to no section. Find out why (its
   pages are shared between rules, so the page tie declines) before changing
   anything.

## Phase 4: repair the prose manuals

1. **Furniture, in place.** After phase 3's port, `enrich_chunks.py
   --dry-run --list-furniture` on the six documents listed above; read the list;
   apply. Bodies otherwise unchanged, so file answers still hold.
2. **Empty chunks.** Find which step writes a chunk that is only a breadcrumb
   (`sections/NNN-section.md`); fix it with a test, or drop them at conversion.
3. **Pages: a pilot, then the user decides.** On three prose manuals (one
   small, one large, one Tessent: `bsdxg`, `dftug`, `atpg-gd`) compare
   (a) `convert_manual.py` as it is on `main`, which now writes pages, and
   (b) `convert_docling.py`. For each: `check_corpus.py --strict`, the content
   percentage, figures tied, chunk size, questions' ranks, and
   `eval_search.py --cost`. Report and stop. This is the "prose routing under
   review" decision in HANDOVER, and it also settles which converter the ten
   older editions get.
4. Roll the chosen converter over the remaining no-page documents, one at a
   time, each gated as in the rules. Then re-extract figures: with pages, a
   figure on an unshared page ties by page, which should lift the six worst
   documents.
5. `bsdrm-q-2019-12` at 93% content: list the sampled pages whose words are
   missing and say whether it is the PDF's text layer or dropped text.

## Phase 5: completeness of editions

Convert the ten older PDFs through `update_corpus.py --questions` in the work
copy with the converter phase 4 chose. Then add entry-level `compare_versions`
questions for `tshell-ref` (three editions), which has only been tested on
fixtures.

## Phase 6: real use (needs the user)

- From 2026-11-24: `triage_log.py --log ~/.pdf-to-rag/calls.jsonl`. The file
  did not exist on 2026-11-12; if it still does not after the corpus has been
  used from VS Code, check that the server is the one writing it before
  assuming no use. Add the `real` questions and rescore.
- `eval_answers.py --dry-run`, then `--limit 5 --bare`. It spends tokens: ask
  first. Record tokens, cost and the three grades.

## Order and stops

Phases 1 and 2 need nothing from the user and come first: every later phase is
judged on phase 1's questions. Phase 3 follows. Phase 4 stops at step 3 for the
converter decision; phase 5 waits on it. Every swap into production stops for
a go-ahead.
