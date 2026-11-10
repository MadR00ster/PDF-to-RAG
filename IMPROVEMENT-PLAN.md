# Improvement plan

Written 2026-11-10 from a review of `main` at `0f3d6a8` (113 tests pass, 1
skipped). `HANDOVER.md` stays the guide to how to work here; this file is the
order of work. Delete a line when it is done, and the file when it is empty.

## The finding behind the order

The code is in good shape. What is missing is evidence from real use:

- The call log (`PDF_TO_RAG_LOG`) has never been switched on, so nothing has
  been triaged into `real` questions.
- `eval_answers.py` has never run against a live `claude`.
- Every retrieval number comes from 76 questions written by someone who had
  just read the answer, and the PrimeTime-family manuals have none.
- Seven shipped changes sit in HANDOVER's "Measurements owed" unmeasured.

So measuring comes before building. The code phases below are small and can
run first only because they need nothing from the user.

## Phase C: tell users apart in the HTTP call log (one pull request)

A team trial cannot be read if every line looks the same.

- C1. Each HTTP call-log line carries a token id: a short, stable label that
  is not the token and cannot be turned back into it (for example the first 8
  hex characters of its SHA-256, or the token's line number in the token
  file). Say which in the docs.
- C2. The client's name over HTTP needs a session the server does not keep.
  Leave it out and say so in ROADMAP, unless a way appears that keeps the
  server stateless.
- C3. Update ROADMAP step 1 and HANDOVER's known-issues line.

Not in this phase: trying the printed VS Code and Cursor settings in the
editors. Only the user can do that; see D4.

## Phase D: measure real use (needs the user)

Nothing here is code. Each step names what it needs.

- D1. **Switch the call log on in production.** Needs the user's go-ahead: it
  changes production's `.vscode/mcp.json` and writes what was searched to a
  file. Decide where that file lives first (it is private, and the corpus
  folder is synced). Check that an index rebuild with `--emit-vscode-config`
  does not drop the `env` block; if it does, that is a bug to fix with a test.
- D2. **First live answer eval.** `eval_answers.py --dry-run`, then
  `--limit 5`. Needs the user's go-ahead: it spends tokens. Record tokens,
  cost and the three grades in `references/retrieval-measurement.md`.
- D3. **Questions for the manuals that have none.** List how many questions
  each document has. For every document with zero, draft questions with
  `sample_sections.py --exclude <questions>` in the kinds the existing set
  uses, add them, and score them. Report the new documents separately from the
  old 76 so the baseline stays comparable.
- D4. **Try the HTTP settings in VS Code and Cursor.** The user's hands;
  ROADMAP step 1's last open box.
- D5. After a week or two of D1: `triage_log.py`, add the `real` questions,
  rescore. This is the number the user asked for.

## Phase E: the measurements owed (needs the production corpus, read-only)

HANDOVER's table lists seven. Split by what they cost:

- **Index-only, do first:** front matter demoted by its whole heading; entity
  named inside a question is boosted; the two search flags
  (`--body-without-breadcrumb`, `--ident-index`). Each is two or three index
  builds into a scratch folder and `eval_search.py --json` / `--compare`. For
  a change already on `main`, build the "before" index from a worktree at the
  commit before it.
- **Need a reconversion on a scratch copy:** short lines that begin the title;
  page numbers on the light prose path; reference breadcrumbs from the TOC
  chain.
- **Needs Docling (several GB):** Docling chunks use their own heading. Ask
  before installing.

Rules: build every index with `--out` into a scratch folder, never over the
live one. Read every question whose rank moved. Write each result into
`references/retrieval-measurement.md` and delete its row from HANDOVER.
Changing a default (a search flag, the prose converter) is the user's
decision: report the numbers and stop.

The prose-converter result also unblocks the ten unconverted older PDFs, which
wait on that choice.

## Phase F: passage-level `get_section` (ROADMAP step 2)

Build this before `ask_docs`. It needs no model and no host, and it is the
baseline `ask_docs` has to beat.

- F1. `get_section` takes an optional `query` and returns only the passages
  that match it, with enough around each to be read alone.
- F2. `eval_search.py` counts the characters a client receives to reach the
  answer, per question.
- F3. Record the baseline before and after.

`ask_docs` and the Gaudi deployment stay deferred until D5's log shows how
many questions are really asked.

## Decisions for the user, not work to start

- **Split `mcp_server.py`?** It is 2,815 lines because it ships as one file
  beside the index. The build already copies it into the corpus, so it could
  build a zipapp from several modules instead and stop duplicating
  `_common.py`'s rules. Worth deciding before `ask_docs` adds more to it.
- **Headline numbers.** README and ROADMAP quote 71% / 92% on 24 manuals;
  production has since grown. Refresh them once D3 gives a number for the
  whole corpus, not before.
- **Housekeeping.** The untracked `update/` folder (September notes and
  backups for a change since committed) and the merged branches can go. Both
  are deletions: the user's call.
