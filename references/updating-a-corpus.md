# Adding documents to a corpus

Read before running `update_corpus.py` on a real corpus, or when it stops and
you need to know why.

Put the PDF in the collection's `new_docs/`, run `update_corpus.py --dry-run` and
read the plan before running it for real (the commands are in SKILL.md).

`update_corpus.py` runs the by-hand sequence in SKILL.md for every PDF waiting in any
collection, printing each step as the command it is, and stops where a step
would have to guess:

- **Routing.** It takes `pick_extractor.py`'s verdict (the same function the
  pre-flight prints from) and converts only what the verdict settles: a
  reference whose command level the converter and the pre-flight agree on, and
  prose, with `convert_docling.py` where Docling is installed or
  `convert_manual.py` under `--prose-converter convert_manual`. A new edition
  takes the converter its manual's other editions were converted with, as the
  last paragraphs here ask. These wait in
  `new_docs/`, each with the decision it needs:
  - a mixed document
  - one with no outline or no text layer
  - a level the two disagree on
  - anything `editions.py` refuses
  - a PDF whose title the PDF does not confirm. The metadata title counts if
    the first pages print it; failing that, the cover's largest text counts if
    later pages print it as a line of its own, as a running title. A product
    name set largest on the cover is only part of the footer line that names
    the manual, so it does not count.

  The decisions go in a `--titles` JSON file, keyed by filename.
- **Nothing is replaced.** It never passes `--replace` and never deletes. A
  PDF whose `docs/<slug>` exists waits.
- **Gates.** `check_corpus.py --strict` runs on the new documents. A FAIL stops
  the run before any index is built; warnings are counted, and `--strict`
  makes them stop it too. The index is built as a candidate beside the live
  one and smoke-tested. With `--questions`, it is also scored against the
  current index. It replaces the current index only if neither hit@5 nor MRR
  drops (`--accept-regression` overrides).
- **Resuming.** The new documents are those the index does not hold as they
  are on disk, so a rerun after a stop carries on. A document finished earlier
  is checked and indexed, not enriched or re-extracted again.

Report the table it ends with (chunks, % with pages, % with an entity, warnings
per document), what waits and why, and whether the index was replaced.

By hand, the sequence is the one in SKILL.md. Its last two steps (build the
search index, smoke-test it) are the ones people skip: without them the server
keeps answering from the corpus as it used to be.

None of these needs `--slug`: the converters name a document after its PDF with
the release on the end (`widget-ref-2026-1`), which is what makes a second
edition share the first's `doc_id`. A slug chosen by hand for one often lacked
that ending, and the two became different manuals that both answered every
search. Every command in that sequence also takes the corpus root in place of `<collection>`
and runs each collection under it (`--only` and `--skip` apply across them).

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

An earlier wrapper, `update.ps1`, was deleted because it drifted four features
behind without anyone noticing. It still sent every document to
`convert_manual.py`, including the command references that need
`rebuild_reference.py`, and it never rebuilt the search index. The first step is
a judgement call, and the rest changes as this skill grows; both were better
read than buried. `update_corpus.py` answers each of those:

- It runs every step as the script itself, never a copy of what the script does.
- It stops at every judgement call instead of making it.
- It rebuilds the index, behind the smoke test and the eval.
- It prints every command before running it, and `--dry-run` shows the whole
  plan.

A test (test_53i) fails when the by-hand sequence in SKILL.md or README names
a script `update_corpus.py` does not run, so a step added to one has to be
added to the other.
