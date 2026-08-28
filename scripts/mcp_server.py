#!/usr/bin/env python3
"""
Serve a converted corpus to an editor over MCP.

Speaks JSON-RPC 2.0 over stdio (newline-delimited), which is what VS Code,
Claude Code, Cursor and other MCP clients use for local servers. Standard
library only: point a client at `python scripts/mcp_server.py --db <index>`
and it works -- no packages, no API key, no network.

Queries the SQLite index that `build_search_db.py` writes. That index is a
snapshot, so rebuild it after changing the corpus.

  python scripts/build_search_db.py --root <corpus> --emit-vscode-config
  python scripts/mcp_server.py --db <corpus>/mcp-index.sqlite3

Tools:
  search_docs      full-text search across the corpus, BM25-ranked, with citations
  get_section      full text of one chunk, optionally with its neighbours
  lookup_entity    assemble a whole entry from a reference document
  list_documents   the catalog: slugs, titles, sizes, coverage
  get_toc          table of contents for one document
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sqlite3
import sys
import traceback
from pathlib import Path

SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2025-06-18"
KNOWN_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")

# MCP is UTF-8, but a Windows console defaults to a legacy code page that
# cannot encode the "™" and "›" these documents are full of -- every response
# carrying one would die on encode. Windows text mode would also turn the
# framing newline into \r\n.
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", newline="\n", errors="replace")
    except (AttributeError, ValueError):
        pass

# bm25 column weights, in table column order: heading, entity, breadcrumb, body.
# A hit in the heading, or in the name of the entity a chunk documents, says far
# more about relevance than the same word buried in a page of prose.
BM25_WEIGHTS = (10.0, 8.0, 3.0, 1.0)

MAX_SECTION_CHARS = 40_000
WORD_RE = re.compile(r"[0-9A-Za-z]+")

# FTS5 ANDs every term, so "how do I define a clock" demands that a chunk
# contain "how" and "do" and "I" -- which ranks prose padding above the page
# that answers the question. Agents phrase things as questions, so these come
# up constantly. Dropped only when content words survive.
STOPWORDS = frozenset("""
a an the and or of to in on for with from by at as is are was were be been being
do does did how what when where which who why can could should would will shall
my our your it its this that these those there here if then than else i you we
me us them he she they him her his hers their please tell show explain use using
about into over under between within any all some no not
""".split())


def log(msg: str) -> None:
    """Diagnostics go to stderr; stdout carries the protocol and nothing else."""
    print(f"[mcp_server] {msg}", file=sys.stderr, flush=True)


class Corpus:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._db: sqlite3.Connection | None = None
        self._entity_names: list[str] | None = None
        self.name = db_path.stem

    @property
    def available(self) -> bool:
        return self.db_path.is_file()

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            if not self.available:
                raise CorpusMissing(self.db_path)
            self._db = sqlite3.connect(f"{self.db_path.as_uri()}?mode=ro", uri=True, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            row = self._db.execute("SELECT value FROM meta WHERE key = 'corpus_name'").fetchone()
            if row and row["value"]:
                self.name = row["value"]
        return self._db

    def coverage_warning(self) -> str:
        """A line to append when the index does not cover the whole corpus.

        A partially built index is the one failure mode that looks like a
        correct answer: search returns "no matches" for a topic the documents
        do cover, and nothing on screen says the shelf was half empty.
        """
        row = self.db.execute(
            "SELECT SUM(section_count) AS total, SUM(indexed_count) AS indexed FROM documents"
        ).fetchone()
        if not row or not row["total"] or row["indexed"] >= row["total"]:
            return ""
        gap = row["total"] - row["indexed"]
        worst = self.db.execute(
            "SELECT slug, indexed_count, section_count FROM documents"
            " WHERE indexed_count < section_count"
            " ORDER BY (section_count - indexed_count) DESC LIMIT 4"
        ).fetchall()
        detail = ", ".join(f"{r['slug']} {r['indexed_count']}/{r['section_count']}" for r in worst)
        return (
            f"\n\n> **Partial index:** {gap:,} of {row['total']:,} sections are missing "
            f"(worst: {detail}). Absence of a result here is not evidence the corpus "
            f"lacks it. Rebuild with `build_search_db.py` once every file is readable."
        )

    def entity_names(self) -> list[str]:
        if self._entity_names is None:
            self._entity_names = [r["name"] for r in self.db.execute("SELECT DISTINCT name FROM entities")]
        return self._entity_names


class CorpusMissing(Exception):
    def __init__(self, path: Path):
        self.path = path

    def __str__(self) -> str:
        return (
            f"Search index not found at {self.path}.\n\n"
            "Build it with:\n\n"
            "    python scripts/build_search_db.py --root <corpus> --emit-vscode-config\n\n"
            "Rerun that after adding or reconverting documents."
        )


CORPUS: Corpus


def to_match_expr(query: str) -> str:
    """Turn a natural query into a safe FTS5 MATCH expression.

    Queries are full of identifiers like `set_scan_configuration`, `-chain_count`
    and `tessent -shell`, none of which survive being handed to FTS5 raw: `_` and
    `-` are token separators, and stray quotes or parens are syntax errors. Each
    term is rewritten into an explicitly quoted phrase, so
    `set_scan_configuration` becomes "set scan configuration" -- matching both
    the literal identifier and prose that spells it out, with BM25 preferring
    the denser hit. Explicit "quoted phrases" and trailing `*` are honoured.
    """
    phrases: list[str] = []
    singles: list[str] = []

    def render(raw: str) -> str | None:
        prefix = raw.endswith("*")
        words = WORD_RE.findall(raw)
        if not words:
            return None
        phrase = '"' + " ".join(words) + '"'
        return phrase + "*" if prefix else phrase

    rest = []
    pos = 0
    for m in re.finditer(r'"([^"]*)"', query):
        rest.append(query[pos:m.start()])
        term = render(m.group(1))
        if term:
            phrases.append(term)
        pos = m.end()
    rest.append(query[pos:])

    loose = [w for w in " ".join(rest).split() if render(w)]
    content = [w for w in loose if not all(p.lower() in STOPWORDS for p in WORD_RE.findall(w))]
    for word in (content if (content or phrases) else loose):
        term = render(word)
        if term:
            singles.append(term)
    return " ".join(phrases + singles)


def run_match(match_expr: str, collection: str | None, document: str | None, limit: int) -> list[sqlite3.Row]:
    sql = [
        "SELECT rowid AS id, heading, entity, breadcrumb, slug, collection, title, file,",
        "       ord, page_start, page_end, chars, noise,",
        f"       bm25(chunks, {', '.join(str(w) for w in BM25_WEIGHTS)}) AS score,",
        "       snippet(chunks, 3, '**', '**', ' … ', 28) AS snip",
        "FROM chunks WHERE chunks MATCH ?",
    ]
    params: list = [match_expr]
    if collection:
        sql.append("AND collection = ?")
        params.append(collection)
    if document:
        sql.append("AND slug = ?")
        params.append(document)
    # Over-fetch generously: the per-document cap discards a lot when one big
    # reference dominates the head of the ranking, and re-ranking a few hundred
    # rows in Python costs nothing next to a second query.
    sql.append("ORDER BY score LIMIT ?")
    params.append(max(limit * 20, 200))
    try:
        return list(CORPUS.db.execute(" ".join(sql), params))
    except sqlite3.OperationalError as exc:
        raise ValueError(f"Could not run that query ({exc}). Try plain words.") from exc


def search(query: str, collection=None, document=None, limit=10, max_per_document=5) -> list[sqlite3.Row]:
    match_expr = to_match_expr(query)
    if not match_expr:
        raise ValueError("Query has no searchable words in it.")

    rows = run_match(match_expr, collection, document, limit)
    if not rows and " " in match_expr:
        # Every term ANDed found nothing. One unlucky word should not turn a
        # good question into a dead end, so fall back to OR and let BM25 float
        # the chunks that hit the most terms.
        rows = run_match(match_expr.replace(" ", " OR "), collection, document, limit)

    scored = sorted(((rank_adjust(r, query), r) for r in rows), key=lambda pair: pair[0])
    picked: list[sqlite3.Row] = []
    per_doc: dict[str, int] = {}
    for _score, row in scored:
        if max_per_document and not document:
            n = per_doc.get(row["slug"], 0)
            if n >= max_per_document:
                continue
            per_doc[row["slug"]] = n + 1
        picked.append(row)
        if len(picked) >= limit:
            break
    return picked


def rank_adjust(row: sqlite3.Row, query: str) -> float:
    """Nudge BM25 (lower is better) using signals BM25 cannot see."""
    score = row["score"]
    words = [w.lower() for w in WORD_RE.findall(query)]
    if not words:
        return score
    joined = "_".join(words)
    spaced = " ".join(words)

    entity = (row["entity"] or "").lower()
    if entity:
        if entity == joined or entity.replace("_", " ") == spaced:
            score -= 6.0          # the query *is* this entity
        elif joined.startswith(entity) or entity.startswith(joined):
            score -= 2.0

    heading = (row["heading"] or "").lower()
    if heading:
        if heading == spaced or heading.replace("_", " ") == spaced:
            score -= 4.0
        elif all(w in heading for w in words):
            score -= 1.5

    # Near-empty stubs ("See Also" lists, one-line cross references) match
    # cheaply and answer nothing.
    if (row["chars"] or 0) < 200:
        score += 1.5
    # Contents pages and figure lists name every heading in the document, so
    # they match nearly everything. Demoted, not excluded.
    if row["noise"]:
        score += 8.0
    return score


def cite(row: sqlite3.Row) -> str:
    """One-line provenance for a chunk: where it came from, and where to look."""
    bits = [f"{row['title']} ({row['slug']})"]
    if row["breadcrumb"]:
        bits.append(row["breadcrumb"].strip("*").strip())
    ps, pe = row["page_start"], row["page_end"]
    if ps is not None:
        bits.append(f"p. {ps}" if pe in (None, ps) else f"pp. {ps}–{pe}")
    if row["entity"]:
        bits.append(f"`{row['entity']}`")
    return " · ".join(bits)


def format_results(rows: list[sqlite3.Row], query: str) -> str:
    if not rows:
        return (
            f'No matches for "{query}".\n\n'
            "Try fewer words, an identifier on its own, or drop the collection/document "
            "filter. `list_documents` shows what is in the corpus." + CORPUS.coverage_warning()
        )
    out = [f'{len(rows)} result(s) for "{query}":\n']
    for i, r in enumerate(rows, 1):
        out.append(f"### {i}. {r['heading'] or '(untitled section)'}")
        out.append(cite(r))
        out.append(f"section_id: {r['id']} · file: {r['file']}")
        snip = " ".join((r["snip"] or "").split())
        if snip:
            out.append(f"> {snip}")
        out.append("")
    out.append("Use `get_section` with a section_id above to read the full text.")
    return "\n".join(out) + CORPUS.coverage_warning()


def format_section(row: sqlite3.Row, body: str) -> str:
    head = [f"# {row['heading'] or '(untitled section)'}", cite(row), f"file: {row['file']}", ""]
    if len(body) > MAX_SECTION_CHARS:
        body = body[:MAX_SECTION_CHARS] + f"\n\n…[truncated at {MAX_SECTION_CHARS:,} characters]"
    return "\n".join(head) + body


SECTION_SELECT = (
    "SELECT rowid AS id, heading, entity, breadcrumb, slug, collection, title, file,"
    " ord, page_start, page_end, chars, body FROM chunks"
)


def clamp_int(value, default: int, low: int, high: int) -> int:
    if value is None or value == "":
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def known_collections() -> list[str]:
    return [r["collection"] for r in CORPUS.db.execute("SELECT DISTINCT collection FROM documents ORDER BY 1")]


def normalize_collection(value) -> str | None:
    if not value:
        return None
    v = str(value).strip().lower()
    known = known_collections()
    if v in known:
        return v
    matches = [k for k in known if k.startswith(v)]
    if len(matches) == 1:
        return matches[0]
    raise ValueError(f"Unknown collection '{value}'. Known: {', '.join(known) or '(none)'}.")


def tool_search_docs(args: dict) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        raise ValueError("`query` is required.")
    collection = normalize_collection(args.get("collection"))
    document = args.get("document") or None
    if document and not CORPUS.db.execute("SELECT 1 FROM documents WHERE slug = ?", (document,)).fetchone():
        raise ValueError(f"Unknown document slug '{document}'. Call `list_documents` for valid slugs.")
    return format_results(
        search(
            query,
            collection=collection,
            document=document,
            limit=clamp_int(args.get("limit"), 10, 1, 40),
            max_per_document=clamp_int(args.get("max_per_document"), 5, 0, 40),
        ),
        query,
    )


def tool_get_section(args: dict) -> str:
    section_id, file_arg = args.get("section_id"), args.get("file")
    if section_id is not None:
        row = CORPUS.db.execute(SECTION_SELECT + " WHERE rowid = ?", (int(section_id),)).fetchone()
        if not row:
            raise ValueError(f"No section with section_id {section_id}.")
    elif file_arg:
        needle = str(file_arg).replace("\\", "/").lstrip("./")
        row = CORPUS.db.execute(
            SECTION_SELECT + " WHERE file = ? OR file LIKE ?", (needle, f"%{needle}")
        ).fetchone()
        if not row:
            raise ValueError(f"No section matching file '{file_arg}'.")
    else:
        raise ValueError("Pass either `section_id` (from search results) or `file`.")

    context = clamp_int(args.get("context"), 0, 0, 3)
    if not context:
        return format_section(row, row["body"])
    neighbours = CORPUS.db.execute(
        SECTION_SELECT + " WHERE slug = ? AND ord BETWEEN ? AND ? ORDER BY ord",
        (row["slug"], row["ord"] - context, row["ord"] + context),
    ).fetchall()
    return "\n\n".join(
        f"---{'  <-- requested section' if n['id'] == row['id'] else ''}\n" + format_section(n, n["body"])
        for n in neighbours
    )


def tool_lookup_entity(args: dict) -> str:
    name = (args.get("name") or "").strip().strip("`")
    if not name:
        raise ValueError("`name` is required, e.g. 'set_scan_configuration'.")
    collection = normalize_collection(args.get("collection"))

    where, params = "WHERE name_lower = ?", [name.lower()]
    if collection:
        where += " AND collection = ?"
        params.append(collection)
    hits = CORPUS.db.execute(f"SELECT * FROM entities {where} ORDER BY slug", params).fetchall()
    if not hits:
        return suggest_entities(name, collection)

    out = []
    for hit in hits:
        chunks = CORPUS.db.execute(
            SECTION_SELECT + " WHERE slug = ? AND entity = ? ORDER BY ord", (hit["slug"], hit["name"])
        ).fetchall()
        if not chunks:
            continue
        doc = CORPUS.db.execute("SELECT title FROM documents WHERE slug = ?", (hit["slug"],)).fetchone()
        pages = ""
        if hit["page_start"] is not None:
            pages = (
                f", p. {hit['page_start']}" if hit["page_end"] in (None, hit["page_start"])
                else f", pp. {hit['page_start']}–{hit['page_end']}"
            )
        out.append(f"# `{hit['name']}`\n{doc['title']} ({hit['slug']}{pages}) · {len(chunks)} chunk(s)\n")
        budget = MAX_SECTION_CHARS
        for c in chunks:
            body = c["body"]
            if budget <= 0:
                out.append(f"…[remaining chunks omitted; read {c['file']} for the rest]")
                break
            if len(body) > budget:
                body = body[:budget] + "\n…[truncated]"
            budget -= len(body)
            out.append(f"<!-- section_id: {c['id']} · {c['file']} -->\n{body}")
    return "\n\n".join(out) if out else suggest_entities(name, collection)


def suggest_entities(name: str, collection: str | None) -> str:
    """No exact hit: offer prefix matches, then fuzzy ones, then full-text."""
    where, params = "WHERE name_lower LIKE ?", [name.lower() + "%"]
    if collection:
        where += " AND collection = ?"
        params.append(collection)
    prefix = CORPUS.db.execute(
        f"SELECT name, slug FROM entities {where} ORDER BY name LIMIT 25", params
    ).fetchall()
    if prefix:
        listing = "\n".join(f"- `{r['name']}` ({r['slug']})" for r in prefix)
        return f"No entry named exactly `{name}`. Entries starting with it:\n\n{listing}"

    close = difflib.get_close_matches(name.lower(), [c.lower() for c in CORPUS.entity_names()], n=8, cutoff=0.7)
    if close:
        return f"No entry named `{name}`. Did you mean:\n\n" + "\n".join(f"- `{c}`" for c in close)
    return (
        f"`{name}` is not in any reference document's entry index. It may still be "
        "discussed in prose -- falling back to full-text search:\n\n"
        + format_results(search(name, collection=collection, limit=8), name)
    )


def tool_list_documents(args: dict) -> str:
    collection = normalize_collection(args.get("collection"))
    sql, params = "SELECT * FROM documents", []
    if collection:
        sql += " WHERE collection = ?"
        params.append(collection)
    rows = CORPUS.db.execute(sql + " ORDER BY collection, slug", params).fetchall()

    out = [f"{len(rows)} document(s) in the corpus:\n"]
    current = None
    for r in rows:
        if r["collection"] != current:
            current = r["collection"]
            out.append(f"\n## {current}")
        flags = []
        if r["has_pages"]:
            flags.append("page numbers")
        if r["has_entities"]:
            flags.append("per-chunk entity attribution")
        extra = f" — {', '.join(flags)}" if flags else ""
        gap = (r["section_count"] or 0) - (r["indexed_count"] or 0)
        coverage = (
            f"{r['indexed_count']} of {r['section_count']} sections indexed (**{gap} missing**)"
            if gap else f"{r['section_count']} sections"
        )
        pages = f", {r['page_count']} PDF pages" if r["page_count"] else ""
        out.append(f"- `{r['slug']}` — {r['title']}\n  {coverage}{pages}, {r['char_count']:,} chars{extra}")
    out.append("\nPass a slug as `document` to `search_docs` to search just that one.")
    return "\n".join(out) + CORPUS.coverage_warning()


def tool_get_toc(args: dict) -> str:
    slug = (args.get("document") or "").strip()
    if not slug:
        raise ValueError("`document` (a slug from `list_documents`) is required.")
    row = CORPUS.db.execute("SELECT * FROM documents WHERE slug = ?", (slug,)).fetchone()
    if not row:
        raise ValueError(f"Unknown document slug '{slug}'. Call `list_documents` for valid slugs.")

    max_level = clamp_int(args.get("max_level"), 2, 1, 6)
    contains = (args.get("contains") or "").strip().lower()

    lines = [f"# {row['title']} ({slug})", f"{row['page_count']} pages, {row['section_count']} sections"]
    gap = (row["section_count"] or 0) - (row["indexed_count"] or 0)
    if gap:
        # The TOC comes from the manifest and is always complete; the text
        # behind these entries may not be. Say so rather than let a listed
        # chapter turn into an unexplained empty search.
        lines.append(f"**{gap} of these sections are not in the search index** — see `list_documents`.")
    lines.append("")

    shown = 0
    for e in json.loads(row["toc_json"] or "[]"):
        level, title = e.get("level", 1), (e.get("title") or "").strip()
        if level > max_level and not contains:
            continue
        if contains and contains not in title.lower():
            continue
        lines.append("  " * (level - 1) + f"- {title}" + (f"  (p. {e['page']})" if e.get("page") else ""))
        shown += 1
    if not shown:
        lines.append("(nothing matched — try a larger `max_level` or a different `contains`)")
    return "\n".join(lines)


def build_tools() -> list[dict]:
    """Tool schemas, with the corpus's own collections named in the text.

    An agent choosing between tools reads these descriptions; naming the real
    collections beats a generic "filter by collection" it cannot act on.
    """
    try:
        collections = known_collections()
    except Exception:
        collections = []
    coll_desc = (
        "Restrict to one collection: " + ", ".join(f"`{c}`" for c in collections) + ". Omit to search all."
        if collections else "Restrict to one collection. Omit to search all."
    )
    coll_schema = {"type": "string", "description": coll_desc}
    if collections:
        coll_schema["enum"] = collections

    return [
        {
            "name": "search_docs",
            "description": (
                "Full-text search across the whole corpus. Use this first for any question "
                "about what these documents cover. Results are BM25-ranked and cite document, "
                "breadcrumb and page; each carries a section_id for get_section. Prefer this "
                "over answering from memory, and cite what you used."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Words, an identifier, or a \"quoted phrase\". Underscores and hyphens are handled."},
                    "collection": coll_schema,
                    "document": {"type": "string", "description": "Restrict to one document slug (see list_documents)."},
                    "limit": {"type": "integer", "description": "Max results, 1-40 (default 10)."},
                    "max_per_document": {"type": "integer", "description": "Cap results per document so one big reference cannot crowd out the rest (default 5, 0 = no cap)."},
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_section",
            "description": (
                "Return the full markdown of one section chunk, by section_id from a search "
                "result or by file path. Use it when the snippet is not enough. `context` also "
                "returns neighbouring sections, which is how you read a procedure that "
                "continues past one chunk."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "section_id": {"type": "integer", "description": "section_id from a search result."},
                    "file": {"type": "string", "description": "Path as shown in a search result."},
                    "context": {"type": "integer", "description": "Also return this many sections either side, 0-3 (default 0)."},
                },
            },
        },
        {
            "name": "lookup_entity",
            "description": (
                "Look up one entry by exact name in a reference document -- a command, API "
                "function, part number, error code -- and return its whole entry reassembled "
                "from every chunk that belongs to it, with its page. Prefer this over search "
                "when you know the name: it will not attach one entry's details to another."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exact entry name, e.g. 'set_scan_configuration'."},
                    "collection": coll_schema,
                },
                "required": ["name"],
            },
        },
        {
            "name": "list_documents",
            "description": (
                "List the documents in the corpus with slugs, titles, sizes and whether they "
                "carry page numbers and entity attribution. Call this to find the right slug "
                "for a filtered search, or to say what is actually covered."
            ),
            "inputSchema": {"type": "object", "properties": {"collection": coll_schema}},
        },
        {
            "name": "get_toc",
            "description": (
                "Table of contents for one document, with page numbers. Use it to orient in an "
                "unfamiliar document or find the chapter covering a topic before searching in it."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "document": {"type": "string", "description": "Document slug."},
                    "max_level": {"type": "integer", "description": "Deepest heading level to show, 1-6 (default 2)."},
                    "contains": {"type": "string", "description": "Only entries whose title contains this text (any level)."},
                },
                "required": ["document"],
            },
        },
    ]


HANDLERS = {
    "search_docs": tool_search_docs,
    "get_section": tool_get_section,
    "lookup_entity": tool_lookup_entity,
    "list_documents": tool_list_documents,
    "get_toc": tool_get_toc,
}


class MethodNotFound(Exception):
    pass


def handle_request(method: str, params: dict) -> dict:
    if method == "initialize":
        asked = params.get("protocolVersion")
        try:
            docs = CORPUS.db.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
            scope = f"{docs} document(s) converted from PDF"
        except Exception:
            scope = "a converted PDF corpus"
        return {
            "protocolVersion": asked if asked in KNOWN_PROTOCOLS else PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": f"{CORPUS.name}-docs", "version": SERVER_VERSION},
            "instructions": (
                f"Authoritative documentation for {CORPUS.name} ({scope}). Answer questions "
                "about it from these tools rather than from memory, and cite the document and "
                "page you used."
            ),
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": build_tools()}
    if method == "resources/list":
        return {"resources": []}
    if method == "prompts/list":
        return {"prompts": []}
    if method == "tools/call":
        return call_tool(params)
    raise MethodNotFound(method)


def call_tool(params: dict) -> dict:
    name = params.get("name")
    handler = HANDLERS.get(name)
    if handler is None:
        raise MethodNotFound(f"tool '{name}'")
    try:
        text, is_error = handler(params.get("arguments") or {}), False
    except (CorpusMissing, ValueError) as exc:
        text, is_error = str(exc), True
    except Exception:
        log(traceback.format_exc())
        text, is_error = f"{name} failed unexpectedly; see the server log for the traceback.", True
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def resolve_db(explicit: str | None) -> Path:
    """--db wins, then $PDF_TO_RAG_DB, then an index beside the working directory.

    The scripts are shared across corpora while an index belongs to exactly one,
    so the server has to be told which. The cwd fallback is a convenience for
    running it by hand from inside a corpus.
    """
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("PDF_TO_RAG_DB")
    if env:
        return Path(env).resolve()
    return Path.cwd().resolve() / "mcp-index.sqlite3"


def main() -> int:
    global CORPUS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="path to the index built by build_search_db.py")
    args = ap.parse_args()

    CORPUS = Corpus(resolve_db(args.db))
    if CORPUS.available:
        log(f"serving {CORPUS.db_path} ({CORPUS.db_path.stat().st_size / 1048576:.0f} MB)")
    else:
        log(f"warning: no index at {CORPUS.db_path} -- run build_search_db.py")

    stdout = sys.stdout
    while True:
        # readline(), not `for line in stdin`: iteration read-aheads can hold a
        # request in the buffer while the client waits for its response.
        raw = sys.stdin.readline()
        if not raw:
            break  # client closed stdin: shut down
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log(f"ignoring non-JSON input: {line[:120]!r}")
            continue

        req_id, method = msg.get("id"), msg.get("method")
        if method is None:
            continue  # a response to something we sent; we send no requests
        try:
            response = {"jsonrpc": "2.0", "id": req_id, "result": handle_request(method, msg.get("params") or {})}
        except MethodNotFound as exc:
            response = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {exc}"}}
        except Exception as exc:
            log(traceback.format_exc())
            response = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(exc)}}

        if req_id is None:
            continue  # notification: acknowledged by silence
        stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
