# Serving the corpus over MCP

Read before changing the server, the index, or what a tool returns.

Chunking and metadata only pay off when something can retrieve them. Two
stdlib-only scripts turn a converted corpus into a server any MCP client
(VS Code/Copilot, Claude Code, Cursor) can query:

```bash
python scripts/build_search_db.py --root <corpus> --emit-vscode-config
python scripts/mcp_smoke_test.py --db <corpus>/mcp-index.sqlite3
```

The first writes one SQLite FTS5 index plus a `.vscode/mcp.json`; the second
drives a real handshake and every tool. Collections are discovered from disk,
so nothing is hardcoded per corpus.
Every tool is declared read-only (`readOnlyHint`, with a title), so a client that
honours MCP's tool annotations can skip its confirmation prompt. The smoke test
also sends the server what is not a JSON-RPC object (a batch, a bare string, a
number) and checks that it answers with an error and carries on: the server is
one process, and a message that killed it left every later request unanswered.

**The corpus folder serves itself.** `--emit-vscode-config` copies
`mcp_server.py` beside the index and writes every path in `.vscode/mcp.json`
relative to the workspace folder. A config that names this checkout by path
works on one machine, and a corpus in a synced folder is opened on several;
the server is one standard-library file, so shipping it with the index costs
nothing and needs only Python on the other machine. Later builds of the index
the copy serves (the default `mcp-index.sqlite3`, or the one `.vscode/mcp.json`
opens with the copy) refresh it, which also keeps the server and the index it
reads on the same schema. Any other index (`--out` a scratch or candidate path,
to try a build or to score it first, beside the live index or not) leaves the
copy alone; `update_corpus.py` refreshes it only once its candidate has
replaced the live index.
Do not edit the copy. The server reads figures and PDFs from the folder its
index is in, not from the path the index was built at. Tools: `search_docs`, `get_section`,
`lookup_entity`, `explain_log`, `check_script`, `list_documents`, `get_toc`, `compare_versions`,
`get_figure`, `get_page_image`.

**A script in, its names checked.** `check_script` reads a Tcl script (comments,
continuations, quotes, braces, `[...]` and the bodies of `if`, `foreach`,
`proc`, `catch`, `switch` and the like; not variable values), lists every
command and option it uses, and checks each against the reference entries of the
edition in use: a command no entry has is reported with the nearest names, or
with the edition that does have it; an option its command's entry does not show
is reported with the nearest ones; an abbreviation is accepted and listed. Each
finding carries its line, and each documented command its document and pages.
Three limits are stated in its output: an option is taken from the `-words` in
the entry's text, so one that appears there only in another command's example
passes; a command several manuals share is checked against all of them together
(and an option only some show is listed), so pass `document` when the script is
for one tool; and a name used as a command that is an entry only in a variables,
messages or rules reference is not counted as documented.

**A log in, the entries out.** `explain_log` takes pasted tool output, finds
every message code (`CMD-082`) and every design rule named after the word
"Rule" (`Warning: Rule S29 ... was violated 32 times`), and returns each one's
entry, errors first, with what no manual here documents listed rather than
guessed at. A bare id with no "Rule" before it is not taken: in a log, `D5` is
as likely a net. A rules manual titles its entries `DRC Rule C1`; that title
stays the entry's name, and `lookup_entity` and `explain_log` also answer for
`C1` and `Rule C1`, the way the tool and the engineer say it.
The index is a **snapshot** —
rebuild after any conversion or enrichment, or the server keeps answering from
the corpus as it used to be.

**Lexical, not embeddings**, because real queries are identifiers and error
strings: `set_scan_configuration`, `-chain_count`, `K23 DRC`. A vector index
would add a dependency, a rebuild cost and an API key to blur them.

Retrieval decisions worth carrying to any implementation, each of which came
from watching bad results or measuring them rather than from theory:

- **Rewrite queries before FTS5 sees them.** `_` and `-` are token separators
  and stray quotes are a syntax error, so each term becomes a quoted phrase.
  Combine terms, never words: `"set scan configuration"` must stay one phrase.
- **Drop stopwords, then rank by OR whenever AND comes up short.** FTS5 ANDs
  everything and agents ask questions: "how do I define a clock" must not
  require "how". Falling back only when AND found *nothing* let one weak chunk
  that happened to hold every word stand in for the answer; falling back
  whenever AND returns less than a page took hit@5 from 82% to 89%.
- **Stem.** Questions say "options" and "toggling" where manuals say "option"
  and "toggle". Porter stemming lifted reworded questions from 55% to 73% hit@5
  and cost no identifier lookup.
- **Demote front matter.** A contents page lists every heading in the document,
  so it outranks the real one — "DRC Rule K23 . . . 151" beating rule K23.
- **Cap hits per document**, or one large reference fills every result page.
- **Search one edition per manual.** The cap is per document and editions are
  separate documents, so it does nothing to stop three releases of one manual
  taking fifteen places with the same answer. See `editions.md`.

**A partial index must say so.** This is the failure that looks like a correct
answer: search returns "no matches" for something the corpus does cover, and
nothing on screen says the shelf was half empty. The build refuses to exit 0
and names the unreadable files; the server reports per-document coverage and
warns on every result until the index is whole.

**So must a stale one.** The same failure arrives a rebuild later: the index
answers confidently from manifests that have since changed. The build records
each manifest, `figures.json` and `current_versions.json` by size, time and
SHA-256, and the server compares them with the disk, again every 30 seconds
while they still match. A changed file, a document folder or `figures.json`
the index does not know, or a pins file that appeared adds "Stale index" to every search and `list_documents` answer until
the index is rebuilt. An index built before this, or copied without its corpus,
says nothing, since there is nothing to compare.
