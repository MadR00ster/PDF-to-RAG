# The output contract

Read when a document comes from anywhere but these converters, or when
`check_corpus.py` reports something you need to understand.

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
