# Editions

Read when converting a second release of a manual, pinning one, or comparing
two.

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

Two things it does not do yet. It compares exact lines (a heading level and a
list marker are ignored, as emphasis is), so an edition whose page footers
survived conversion shows them as differences: the 2025.2 and 2022.4 editions of
the Tessent Shell Reference Manual keep a "Command Dictionary (...) name" footer
that 2026.2 does not. And for prose it
compares headings only; to compare a topic, search each edition.

`scripts/editions.py status <corpus>` shows every manual, its editions, which
is current and what is waiting. For a corpus converted before any of this,
`editions.py stamp <collection>` adds `doc_id` and `version` to the manifests
(originals kept under `.rebuild-backup/`), and `editions.py migrate` moves the
PDFs into `source/`.
