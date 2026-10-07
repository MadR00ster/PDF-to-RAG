# PDF-to-RAG

Turn a folder of PDF manuals into a corpus a language model can retrieve from
safely, and serve it to an editor over MCP.

Extracting text from a PDF is one library call. What decides whether the result
is safe to retrieve from is the metadata on each chunk: where it came from, what
page it is on, and which command or entry it documents. This repo is built
around one rule for that metadata: **either right or absent, never guessed.** A
chunk of arguments that names the wrong command is worse than one that names
none.

It is packaged as a [Claude skill](SKILL.md), and the scripts also run on their
own.

## What it produces

```
<corpus>/
  mcp-index.sqlite3                one full-text search index for the corpus
  <collection>/                    e.g. one folder per vendor
    new_docs/                      put new PDFs here
    source/                        PDFs that have been converted
    docs/
      index.json, README.md        catalog of the collection
      <slug>/                      one edition of one manual
        manifest.json              title, version, page count, PDF table of contents
        full.md                    the whole document
        sections/NNNN-slug.md      retrieval chunks, about 2-9 KB each
        figures.json, figures/     every figure, cropped and tied to its section
```

Each chunk carries a breadcrumb taken from the PDF's own table of contents, a
page range where the converter can supply one, and, in reference documents,
the name of the entry it belongs to.

A manual can be converted in several releases. Each is its own document with
the tool release it applies to read from the PDF's cover, search answers from
one of them per manual (the newest unless you pin another), and the rest are
read when a question names the release in use. A manual whose cover says
"2023.1 and later" answers for the releases after it too.

## What the MCP server does

`scripts/mcp_server.py` serves the index to VS Code, Cursor, Claude Code or any
other MCP client over stdio. It uses the standard library only: no packages, no
API key, no network.

| tool | returns |
|---|---|
| `search_docs` | BM25-ranked sections with document, breadcrumb and page |
| `get_section` | the full text of one section, optionally with its neighbours |
| `lookup_entity` | one command, function or error code, reassembled from all its chunks |
| `list_documents` | the catalog, with versions and coverage |
| `get_toc` | one document's table of contents |
| `compare_versions` | what two editions of a manual add, remove or word differently |
| `get_figure` | a figure as an image, with its caption and section |
| `get_page_image` | one page of the source PDF as an image |

The server retrieves; the client's model reads and answers. A shared server
that writes the answer itself is planned, not built: see
[ROADMAP.md](ROADMAP.md).

## Quick start

Needs Python 3.10 or later: pymupdf4llm 1.28 requires it.

```bash
pip install -r scripts/requirements.txt
```

Put the PDFs in `new_docs/` inside a collection folder inside a corpus folder,
then for each PDF:

```bash
python scripts/pick_extractor.py my-corpus/vendor/new_docs/*.pdf
```

That reports whether a document is prose or a reference (one entry per command,
part or error code) and names the converter to use:

```bash
python scripts/convert_manual.py my-corpus/vendor/new_docs/guide.pdf --title "Widget User Guide"
python scripts/convert_manual.py my-corpus/vendor/new_docs/commands.pdf --slug widget-cmds --title "Widget Command Reference"
```

Each converter writes `my-corpus/vendor/docs/<slug>/` and then moves the PDF to
`my-corpus/vendor/source/`.

Then, for the collection:

```bash
python scripts/enrich_chunks.py my-corpus/vendor --only guide
python scripts/extract_figures.py my-corpus/vendor
python scripts/ocr_figures.py my-corpus/vendor
python scripts/build_index.py my-corpus/vendor
python scripts/check_corpus.py my-corpus/vendor
```

`enrich_chunks.py` is for prose documents only. `ocr_figures.py` needs
Tesseract's language data. `check_corpus.py` is read-only and should print no
`FAIL` lines.

Build the search index and connect an editor:

```bash
python scripts/build_search_db.py --root my-corpus --emit-vscode-config
python scripts/mcp_smoke_test.py --db my-corpus/mcp-index.sqlite3
```

The first command also copies `mcp_server.py` into `my-corpus/` and writes
`my-corpus/.vscode/mcp.json` with paths relative to that folder, so the corpus
can be synced to another machine and served there with nothing but Python. For
another client, register the command `python my-corpus/mcp_server.py` as a
stdio server; it reads the index beside it. The index is a snapshot: rebuild it
after any conversion, which also refreshes the server copy.

`convert_manual.py` puts a page range on every chunk of a prose manual.
`convert_docling.py` adds breadcrumbs anchored to the PDF's table of contents,
each with a confidence. It needs Docling, a multi-gigabyte install that is
deliberately left out of `requirements.txt`: `pip install docling`.

To add a newer release of a manual later, drop its PDF in `new_docs/` and
convert it the same way. It becomes the edition search answers from; the older
one stays answerable by version. To keep answering from the older release, pin
it in `my-corpus/vendor/current_versions.json`:

```json
{"guide": "2025.1"}
```

`python scripts/editions.py status my-corpus` shows every manual, its editions
and which one is current. Rebuild the index after changing a pin.

[SKILL.md](SKILL.md) has the full workflow, the reasons behind each step, and
what to do when the input is not a flat folder of PDFs.

## Using it as a Claude skill

Clone the repo into a skills folder, for example `~/.claude/skills/pdf-to-rag/`.
Claude then loads `SKILL.md` when asked to make PDFs searchable, build a
knowledge base from documents, or diagnose a corpus whose retrieval returns
useless fragments.

## Scripts

| script | use |
|---|---|
| `pick_extractor.py` | inspect a PDF and recommend a converter |
| `convert_manual.py` | PDF to chunks with page numbers, prose or reference by its outline (pymupdf4llm) |
| `convert_docling.py` | prose PDF to chunks with page numbers and TOC-anchored breadcrumbs (Docling) |
| `rebuild_reference.py` | reference PDF to chunks with pages and entry names (what `convert_manual.py` runs for a reference) |
| `enrich_chunks.py` | strip page headers and footers, add breadcrumbs |
| `extract_figures.py`, `ocr_figures.py` | crop figures; read the words in raster ones |
| `build_index.py` | regenerate a collection's catalog |
| `editions.py` | list editions and pins; add versions to an older corpus; move its PDFs into `source/` |
| `check_corpus.py` | check any converted document against the output contract |
| `build_search_db.py` | corpus to one SQLite FTS5 index |
| `mcp_server.py`, `mcp_smoke_test.py` | serve the index; test the server end to end |
| `sample_sections.py`, `eval_search.py` | build a question set; score search against it |

## Measured, on one real corpus

24 vendor manuals, 23,856 pages, scored on 76 questions written against
sampled sections:

- Right section first: 71%. In the top five: 92%.
- Questions naming an identifier: 100% in the top five. Questions deliberately
  reworded to avoid the manual's terms: 73%.
- A small embedding model with rank fusion lowered the top-five rate to 88%,
  so the index stays lexical.

Method and numbers are in
[references/retrieval-measurement.md](references/retrieval-measurement.md). The
extractor comparison is in
[references/extractor-benchmark.md](references/extractor-benchmark.md), and the
catalog of ways a corpus goes wrong is in
[references/failure-modes.md](references/failure-modes.md).

## Limits

- Search is lexical. A question phrased in words the documents never use can
  miss.
- The scripts expect a flat folder of PDFs per collection. They do not read
  HTML help folders.
- Comparing two releases reports what the manuals say, line for line. It is
  not a release note, and it has not been scored against a test set yet.
- PDFs with no text layer need OCR first.
- 76 questions is a small test set, and most were written by someone who had
  just read the answer.

## Tests

```bash
python -m unittest discover -s tests -v    # the whole suite
python tests/run_each.py                   # every test alone, in its own process
```

Plain `unittest`, one file per area: converters, server, figures, checker,
editions. The fixture PDFs are generated with a known table of contents, so
nothing is checked in. Run it before changing a converter.
GitHub Actions runs it on Linux and Windows, with Python 3.10 and 3.13,
on every push.

## Licence

GPL-3.0. See [LICENSE](LICENSE). The licence covers this code, not the
documents you convert with it: check that you may redistribute or serve those.
