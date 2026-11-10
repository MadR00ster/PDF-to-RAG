# When the input does not look like the target layout

Read before running any script that writes on an input that is not a flat
folder of PDFs, or on a corpus something else built.

The scripts were written for one input shape: PDFs dropped into a collection's
`new_docs/`, with `docs/` beside it and nothing else writing there. Real inputs
are often something else, and nothing here detects it yet. **Before running any
script that writes, check the input against the target layout in SKILL.md.** If it differs,
tell the user what you found and choose an approach with them. Don't force the
input into this layout, and don't run the pipeline anyway to see what breaks.

Three shapes seen so far:

- **Scattered PDFs**, in nested folders or with copies of the same file. That is
  the case the scripts handle, once the PDFs are gathered into one collection
  folder.
- **A vendor HTML help folder with the PDFs alongside.** One example is an
  Oxygen WebHelp output built from DITA:
  - one folder per manual, split into topic subfolders
  - every PDF gathered in one `pdf/` folder, named after its manual's folder
    (`avalon_maskview_ug/` ↔ `pdf/avalon_maskview_ug.pdf`)
  - `index.html` redirecting to a landing page
  - a table of contents that exists only as per-node JavaScript fragments
    (`oxygen-webhelp/app/nav-links/json/tocId-*.js`), though a top-level
    `sitemap.html` may also hold it

  Asked whether the HTML and PDFs hold the same text, an agent reported that
  they do: the same manual rendered twice, with the PDF adding only page
  furniture. That answer came without numbers. The PDFs on hand may also cover
  only some of the manuals, so some manuals may exist only as HTML. Other
  vendors ship custom help trees that look like neither.
- **An existing corpus built by something else**: an earlier version of this
  skill, the deleted `update.ps1` (which moved PDFs and deleted `docs/<slug>`
  folders), an HTML-based RAG skill, or hand edits. An agent asked to update one
  such corpus reported that the layout matched this skill's for none of its
  documents. `scripts/check_corpus.py` is read-only, so run it first: it says
  which documents meet the contract and where the rest fall short.

What breaks, and what it damages:

- **Source PDFs are looked for only in `source/` and beside `docs/`.** Each
  manifest records `source_pdf` as a bare filename, and `build_index.py`,
  `extract_figures.py`, `ocr_figures.py`, `check_corpus.py` and
  `get_page_image` all resolve it against those two places. When the PDFs live
  elsewhere, every document looks unaccounted for, and figures and page images
  fail.
- **Converters write to `docs/` in the PDF's collection by default**: the
  folder the PDF is in, or the one above when that folder is `new_docs/` or
  `source/`. In a help folder that means inside the vendor's install tree.
  Pass `--out-root`.
- **`enrich_chunks.py` rewrites section files and `manifest.json` in place, with
  no backup.** Its furniture pass deletes `Feedback` lines, lines matching the
  document's own title that recur in at least 3 sections, and the bare page
  numbers and `Chapter N:` headers within 3 lines of either. A line that only
  begins the title is kept: it has to start with the title, or cover at least
  60% of it. That is right for PDF page headers and footers; on text another
  tool produced it can still delete a short line that happens to be most of
  the title. Run `--dry-run --list-furniture` first on anything this skill did
  not convert, to see every distinct line it would delete, and back the folder
  up.
- **`build_index.py` replaces `docs/index.json` and `docs/README.md` outright**,
  and stops with an error on a manifest missing `title`, `source_pdf`,
  `page_count` or `sections`.
- **No script reads HTML.** Treating the HTML as a duplicate is safe only for
  manuals whose PDF is actually there.

For DITA-built help, the HTML is probably the better source even when the PDF
exists: exact heading hierarchy, no page furniture, figures already as image
files, real tables. What it lacks is page numbers, which could be recovered by
matching each section's text against the PDF's pages. This is unmeasured.
Adaptive input handling is planned, and waits on a real HTML help folder to
build and test against.
