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

To serve several people from one machine, listen over HTTP instead (MCP
Streamable HTTP: one endpoint, POST /mcp, a JSON-RPC message in and out):

  python scripts/mcp_server.py --db <index> --http 127.0.0.1:8765
  PDF_TO_RAG_TOKEN=<token> python scripts/mcp_server.py --db <index> --http 0.0.0.0:8765
  python scripts/mcp_server.py --print-client-config https://docs.example.com/mcp

Every request must carry `Authorization: Bearer <token>` when a token is set,
from $PDF_TO_RAG_TOKEN or the lines of --token-file; GET /health reports the
index. Listening on anything but a loopback address without a token is
refused. There is no TLS here: put a reverse proxy in front of anything that
leaves the machine. Each request thread reads the index through a connection
of its own. After build_search_db.py or update_corpus.py replaces the index,
restart the server, which holds the old file open until then.

Tools:
  search_docs      full-text search across the corpus, BM25-ranked, with citations
  get_section      full text of one chunk, optionally with its neighbours
  lookup_entity    assemble a whole entry from a reference document
  explain_log      every message code and DRC rule a tool log names, each with its entry
  check_script     every command and option a Tcl script uses, checked against the reference entries
  list_documents   the catalog: slugs, titles, versions, sizes, coverage
  get_toc          table of contents for one document
  compare_versions what differs between two editions of one manual
  get_figure       a figure's image, with its caption and the section it illustrates
  get_page_image   one page of a source PDF as an image (needs PyMuPDF)

A manual can be in the index in several editions. Search and lookup answer
from one edition of each -- the one the index marks current -- so two releases
never compete for a question. Any other is read only when a call names it:
`document` with `version`, the tool release in use. That selects the edition
for exactly that release, or an earlier one whose cover says "and later". A
release no edition covers is refused with the list of those that are here;
nothing is substituted for it.

Set PDF_TO_RAG_LOG=<file> to append one JSON line per tool call to that file:
when, which tool, the arguments, the sections returned, how many characters
and how long. Over HTTP each line also carries `token_id`, the first eight hex
characters of the SHA-256 of the bearer token the call came with: it tells one
token's calls from another's and cannot be turned back into the token. It is
off unless set. `triage_log.py` turns what people asked
into test questions.
"""
from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import traceback
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple

SERVER_VERSION = "1.1.0"
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

# bm25 column weights, in table column order: heading, entity, breadcrumb, body,
# figures. A hit in the heading, or in the name of the entity a chunk documents,
# says far more about relevance than the same word buried in a page of prose.
# An index built before figures has the UNINDEXED `slug` fifth, which never
# matches, so the extra weight is inert there.
BM25_WEIGHTS = (10.0, 8.0, 3.0, 1.0, 1.0)

MAX_SECTION_CHARS = 40_000
# Names listed per side by compare_versions before it says how many more.
MAX_LISTED = 300

# get_page_image renders: body text stays readable at about 1,800 image tokens
# a page, and the long edge stays under every current model's native limit.
PAGE_DPI = 120
IMAGE_MAX_PX = 1568
WORD_RE = re.compile(r"[0-9A-Za-z]+")
# Written out here, not imported: this file runs on its own, beside an index.
# tests hold it to scripts/_common.py's MESSAGE_CODE_RE.
MESSAGE_CODE_RE = re.compile(r"[A-Z][A-Z0-9]{1,9}-\d{2,5}")
# A DRC rule's entry title ("DRC Rule C1"); tests hold it to _common.py's
# RULE_TITLE_RE. RULE_ID_RE is how a lookup names one: "C1", "Rule C1" or
# "DRC Rule C1", lower-cased.
RULE_TITLE_RE = re.compile(r"(?:DRC\s+)?[Rr]ule\s+[A-Z]{1,3}\d{1,4}")
RULE_ID_RE = re.compile(r"^(?:(?:drc\s+)?rule\s+)?([a-z]{1,3}\d{1,4})$")

# explain_log: the most of one entry it quotes, and the most codes it quotes
# at all. A log with 30 distinct codes should cost one read, not 30 entries.
LOG_ENTRY_CHARS = 2_500
LOG_MAX_ENTRIES = 25
# How a log names what went wrong. A message code is printed as is
# ("Error: ... (CMD-082)"); a design rule by its id after "Rule"
# ("Warning: Rule S29 (...) was violated 32 times", "Clock Rule C2: ...").
LOG_CODE_RE = re.compile(r"(?<![\w-])(" + MESSAGE_CODE_RE.pattern + r")(?![\w-])")
LOG_RULE_RE = re.compile(r"\b(?:DRC\s+)?[Rr]ule\s+([A-Z]{1,3}\d{1,4})\b")
LOG_SEVERITY_RE = re.compile(r"\b(fatal|error|warning|info(?:rmation)?)\b", re.I)
SEVERITY_ORDER = {"fatal": 0, "error": 1, "warning": 2, "information": 3, None: 4}


def query_tokens(query: str) -> list[tuple[str, bool]]:
    """The query's words as typed, lowercased, without the punctuation
    around them, each with whether it names an identifier: it holds `_`,
    starts with `-`, is a message code (ADES-002), or is in backticks.
    A plain word is not one: `set` in "how do I set the clock" is English,
    even where a command is called set."""
    out = []
    for raw in query.split():
        ticked = raw.startswith("`") and raw.rstrip(".,;:?!").endswith("`")
        tok = raw.strip("`'\"()[]{}<>,;:?!").rstrip(".")
        if tok:
            ident = (ticked or "_" in tok or (tok.startswith("-") and len(tok) > 1)
                     or bool(MESSAGE_CODE_RE.fullmatch(tok)))
            out.append((tok.lower(), ident))
    return out


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


# An opt-in record of real use: one JSON object per tool call, appended to the
# file $PDF_TO_RAG_LOG names. Off unless that is set. It is how to learn what
# people actually ask and what the server answered, which no test set written
# by someone who had read the answers can say; triage_log.py reads it.
#
# Each line: t (UTC), tool, args (strings cut at CALL_LOG_ARG_CHARS), sections
# (the section ids the call returned, in order), refs (the same sections as
# [collection, slug, file], which survive an index rebuild when ids do not),
# chars (text returned), ms (latency), error, and client when the client said
# who it was, and token_id over HTTP when a token is required (see token_id()).
# Nothing is ever written to stdout; a log that cannot be written
# is dropped with one note on stderr and the server goes on answering.
CALL_LOG_ENV = "PDF_TO_RAG_LOG"
CALL_LOG_ARG_CHARS = 2_000
CALL_LOG: Path | None = None
# What the call in flight has done, kept for the thread it is on. Over HTTP each
# request is a thread of its own, so one call must not see another's sections,
# clear them, or be given the name of a client that never made it (a request
# that is not the `initialize` has no name to give). On stdio there is one thread.
CALL_STATE = threading.local()


def token_id(token: bytes) -> str:
    """A label for a bearer token that is safe to write down: the first eight
    hex characters of its SHA-256. Stable, short, and no way back to the token."""
    return hashlib.sha256(token).hexdigest()[:8]


def returned() -> list:
    """The sections the call in flight on this thread has returned so far."""
    rows = getattr(CALL_STATE, "rows", None)
    if rows is None:
        rows = CALL_STATE.rows = []
    return rows


def configure_call_log() -> None:
    global CALL_LOG
    where = os.environ.get(CALL_LOG_ENV, "").strip()
    CALL_LOG = Path(where).expanduser().resolve() if where else None
    if CALL_LOG:
        log(f"logging tool calls to {CALL_LOG}")


def note_returned(row: sqlite3.Row) -> None:
    """Remember a section the call in flight is returning, for the call log."""
    if CALL_LOG is not None:
        returned().append(row)


def clip_arg(value):
    if isinstance(value, str) and len(value) > CALL_LOG_ARG_CHARS:
        return value[:CALL_LOG_ARG_CHARS] + f"…[{len(value)} chars]"
    return value


def record_call(name: str, args, content: list[dict], is_error: bool, started: float) -> None:
    """Append one line to the call log. Never raises, never touches stdout."""
    global CALL_LOG
    if CALL_LOG is None:
        return
    try:
        ids, refs = [], []
        for row in returned():
            if row["id"] not in ids:
                ids.append(row["id"])
                refs.append([row["collection"], row["slug"], "sections/" + row["file"].rsplit("/", 1)[-1]])
        entry = {
            "t": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "tool": name,
            "args": {k: clip_arg(v) for k, v in args.items()} if isinstance(args, dict) else {"_": repr(args)[:200]},
            "sections": ids,
            "refs": refs,
            "chars": sum(len(b.get("text", "")) for b in content if b.get("type") == "text"),
            "ms": round((time.perf_counter() - started) * 1000, 1),
            "error": is_error,
        }
        images = sum(1 for b in content if b.get("type") == "image")
        if images:
            entry["images"] = images
        client = getattr(CALL_STATE, "client", None)
        if client:
            entry["client"] = client
        who = getattr(CALL_STATE, "token_id", None)
        if who:
            entry["token_id"] = who
        CALL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(CALL_LOG, "a", encoding="utf-8", errors="replace", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        log(f"call log dropped, and no more will be written: {exc}")
        CALL_LOG = None
    finally:
        returned().clear()


# How long a check that found the index up to date is trusted before the
# files are looked at again.
STALE_RECHECK = 30.0


class Corpus:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        # One read-only connection per thread: stdio is one thread, an HTTP
        # server is one per request. A connection is never shared, so no two
        # requests wait on each other's statements. What is worked out once
        # below (the root, the flags, the document table) is the same for
        # every thread, so two threads finding it out together is harmless.
        self._local = threading.local()
        self._stale_lock = threading.Lock()
        self._root: Path | None = None
        self._has_figures: bool | None = None
        self._has_editions: bool | None = None
        self._has_idents: bool | None = None
        self._stale: str | None = None
        self._stale_checked = 0.0
        self._documents: dict[tuple[str, str], sqlite3.Row] | None = None
        self.name = db_path.stem

    @property
    def available(self) -> bool:
        return self.db_path.is_file()

    @property
    def _db(self) -> sqlite3.Connection | None:
        """This thread's connection, if it has opened one."""
        return getattr(self._local, "db", None)

    @_db.setter
    def _db(self, conn: sqlite3.Connection | None) -> None:
        """Setting it to None makes this thread open a fresh connection, which
        is how a test (or a caller that replaced the index) picks up the new file."""
        self._local.db = conn

    @property
    def db(self) -> sqlite3.Connection:
        conn = self._db
        if conn is None:
            if not self.available:
                raise CorpusMissing(self.db_path)
            conn = sqlite3.connect(f"{self.db_path.as_uri()}?mode=ro", uri=True, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.db = conn
            row = conn.execute("SELECT value FROM meta WHERE key = 'corpus_name'").fetchone()
            if row and row["value"]:
                self.name = row["value"]
        return conn

    def coverage_warning(self) -> str:
        """What to append to an answer when the index cannot be trusted to
        cover the corpus: it is partial, or the corpus has changed since."""
        return self._partial_warning() + self.staleness_warning()

    def staleness_warning(self) -> str:
        """A line to append when the corpus has changed since the index was built.

        The same failure as a partial index, one rebuild later: search answers
        confidently from manifests that are gone. Worked out from the files the
        index recorded (manifests, figures.json and current_versions.json,
        each by size, then mtime, then content) and from any document folder
        it did not know. Empty where there is no record,
        or where none of the recorded files can be found: an index copied
        without its corpus has nothing to be compared with.

        Once found stale, the index stays stale for the life of the process.
        Until then the answer is kept for STALE_RECHECK seconds, not for good:
        the server lives as long as the editor, and a manual reconverted
        after the first search is the case this warning exists for.
        """
        # One thread checks at a time: the check reads and hashes files, and
        # four requests arriving together should not each do it.
        with self._stale_lock:
            now = time.monotonic()
            if self._stale is None or (not self._stale and now - self._stale_checked >= STALE_RECHECK):
                self._stale = self._compute_staleness()
                self._stale_checked = now
            return self._stale

    def _compute_staleness(self) -> str:
        try:
            recorded = self.db.execute("SELECT path, size, mtime_ns, sha256 FROM sources").fetchall()
        except sqlite3.OperationalError:
            return ""
        root = self.root
        if not recorded or not any((root / r["path"]).exists() for r in recorded):
            return ""

        def name(path: str) -> str:
            parts = path.split("/")
            return parts[-2] if parts[-1] in ("manifest.json", "figures.json") and len(parts) > 1 else path
        changed: dict[str, None] = {}
        for r in recorded:
            file = root / r["path"]
            try:
                st = file.stat()
                same = st.st_size == r["size"] and (
                    st.st_mtime_ns == r["mtime_ns"] or hashlib.sha256(file.read_bytes()).hexdigest() == r["sha256"])
            except OSError:
                same = False
            if not same:
                changed[name(r["path"])] = None
        known = {r["path"] for r in recorded}
        for coll in sorted({r["collection_dir"] for r in self.db.execute("SELECT DISTINCT collection_dir FROM documents")}):
            base = root / coll
            docs = base / "docs"
            for folder in sorted(docs.iterdir()) if docs.is_dir() else []:
                if not (folder / "manifest.json").is_file() or skips_folder(folder.name):
                    continue
                # A document the index does not know, or a figures.json made
                # after the build (extract_figures.py run later).
                if any((folder / f).is_file() and (folder / f).relative_to(root).as_posix() not in known
                       for f in ("manifest.json", "figures.json")):
                    changed[folder.name] = None
            pins = (base / "current_versions.json")
            if pins.is_file() and pins.relative_to(root).as_posix() not in known:
                changed[pins.relative_to(root).as_posix()] = None
        if not changed:
            return ""
        names = list(changed)
        shown = ", ".join(names[:3]) + (f", and {len(names) - 3} more" if len(names) > 3 else "")
        return (f"\n\n> **Stale index:** {len(names)} document(s) changed on disk since this index was "
                f"built ({shown}). Answers may not match the corpus. Rebuild with build_search_db.py.")

    def _partial_warning(self) -> str:
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

    @property
    def root(self) -> Path:
        """The corpus root: the index's own folder when the corpus is there,
        as it is by default, otherwise the root recorded at build time.

        The index's folder first, so a corpus synced or copied to another
        machine reads its own figures and PDFs. The recorded path names the
        machine that built the index, and where it still exists it may be an
        older copy.
        """
        if self._root is None:
            here = self.db_path.parent
            doc = self.db.execute("SELECT collection_dir FROM documents LIMIT 1").fetchone()
            meta = dict(self.db.execute("SELECT key, value FROM meta WHERE key IN ('root', 'root_from_index')"))
            recorded = Path(meta["root"]) if meta.get("root") else None
            # An index kept in a subfolder of its corpus (--out
            # <root>/indexes/x.sqlite3) records the way back up. Without it
            # `here` is indexes/, and a moved corpus fell through to the path
            # it was built at.
            nearby = [Path(os.path.normpath(here / meta["root_from_index"]))] if meta.get("root_from_index") else []
            for candidate in nearby + [here]:
                if doc and (candidate / doc["collection_dir"] / "docs").is_dir():
                    self._root = candidate
                    break
            else:
                self._root = recorded if recorded and recorded.is_dir() else here
        return self._root

    @property
    def has_figures(self) -> bool:
        """False for an index built before figures existed."""
        if self._has_figures is None:
            self._has_figures = bool(self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'figures'"
            ).fetchone())
        return self._has_figures

    @property
    def has_idents(self) -> bool:
        """True for an index built with --ident-index."""
        if self._has_idents is None:
            self._has_idents = bool(self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'idents'"
            ).fetchone())
        return self._has_idents

    @property
    def has_editions(self) -> bool:
        """False for an index built before documents carried doc_id and
        version. There every document is its own manual and all are searched,
        as they always were."""
        if self._has_editions is None:
            columns = {r["name"] for r in self.db.execute("PRAGMA table_info(documents)")}
            self._has_editions = "doc_id" in columns
        return self._has_editions

    def document(self, collection: str, slug: str) -> sqlite3.Row | None:
        """A document is its collection and its slug: two vendors may each
        have a `user-guide`, and every lookup here names both."""
        if self._documents is None:
            self._documents = {(r["collection"], r["slug"]): r for r in self.db.execute("SELECT * FROM documents")}
        return self._documents.get((collection, slug))


def skips_folder(name: str) -> bool:
    """A folder under docs/ that is not a document: a backup (.old), an
    interrupted build (.new), or anything hidden or private. editions.skips_name
    is the rule; this file imports nothing from the scripts, so it has its own
    copy, and a test holds the two together."""
    return name.endswith((".old", ".new")) or name.startswith((".", "_"))


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


# The open index, set before any tool runs. It is only annotated here, with no
# value, so a tool that runs before it is set fails loudly. pyflakes takes a
# name bound this way for undefined inside a nested function (a false report:
# the name is looked up when the function runs), so the three uses in
# compare_entry and compare_listing carry `# noqa: F821`.
CORPUS: Corpus


def to_match_terms(query: str) -> list[str]:
    """Turn a natural query into safe FTS5 MATCH terms, one per word or phrase.

    Queries are full of identifiers like `set_scan_configuration`, `-chain_count`
    and `tessent -shell`, none of which survive being handed to FTS5 raw: `_` and
    `-` are token separators, and stray quotes or parens are syntax errors. Each
    term is rewritten into an explicitly quoted phrase, so
    `set_scan_configuration` becomes "set scan configuration" -- matching both
    the literal identifier and prose that spells it out, with BM25 preferring
    the denser hit. Explicit "quoted phrases" and trailing `*` are honoured.

    Returned as a list, not a joined expression, so the caller can combine
    terms with AND or OR without reaching inside a phrase.
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
    return phrases + singles


def norm_version(version) -> str:
    """A version as people type it, comparable: "v2025_2" is "2025.2"."""
    v = re.sub(r"\s+", "", str(version or "")).upper().replace("_", ".")
    return v[1:] if re.match(r"V\d", v) else v


def version_sort(version) -> str | None:
    """A version as text that orders releases of one manual, the way the
    index's `version_sort` column was written by editions.version_sort: where
    there is a year, the year, the release, any further dotted components,
    then -1 and the service pack ("2026.1.1" is not "2026.1.2"); else its
    numbers. This file is copied beside indexes and imports nothing from the
    repo, so the rule is written out here too; tests hold the two together."""
    v = norm_version(version)
    m = re.search(r"(20\d\d)\.(\d{1,2})((?:\.\d+)*)", v)
    if m:
        sp = re.search(r"SP(\d+)(?:-(\d+))?", v)
        further = tuple(int(n) for n in m.group(3).split(".") if n)
        key = (int(m.group(1)), int(m.group(2)), *further, -1, int(sp.group(1)) if sp else 0,
               int(sp.group(2)) if sp and sp.group(2) else 0)
    else:
        key = tuple(int(n) for n in re.findall(r"\d+", v))
    return ".".join(f"{n:06d}" for n in key) if key else None


def current_only() -> str:
    """SQL limiting chunks or entities to the current edition of each manual."""
    return (" AND (collection, slug) IN (SELECT collection, slug FROM documents WHERE is_current = 1)"
            if CORPUS.has_editions else "")


def editions_of(doc_id: str, collection: str | None = None) -> list[sqlite3.Row]:
    sql, params = "SELECT * FROM documents WHERE doc_id = ?", [doc_id]
    if collection:
        sql += " AND collection = ?"
        params.append(collection)
    return CORPUS.db.execute(sql + " ORDER BY collection, version_sort, slug", params).fetchall()


def says_later(row: sqlite3.Row) -> bool:
    return bool(row["version"]) and "version_later" in row.keys() and bool(row["version_later"])


def edition_name(row: sqlite3.Row) -> str:
    """An edition as it is named to a reader: the release its cover gives,
    with "and later" where the cover says so."""
    if not row["version"]:
        return row["slug"]
    return f"{row['version']} and later" if says_later(row) else row["version"]


def describe_editions(rows: list[sqlite3.Row]) -> str:
    return ", ".join(f"{edition_name(r)} (`{r['slug']}`{', current' if r['is_current'] else ''})" for r in rows)


def resolve_document(name, version=None, collection: str | None = None) -> sqlite3.Row:
    """The one edition a call means by `document` and `version`.

    `document` is an edition's slug or a manual's doc_id. A slug alone is that
    edition; a doc_id alone is the manual's current edition; either with a
    version is the edition that applies to that tool release: the one for
    exactly it, or failing that the nearest earlier one if its cover says
    "and later" -- the document's own claim to cover what follows it. A
    release no edition covers is an error naming the editions there are. An
    earlier edition without that claim is a different document, and answering
    from it would be a guess the caller cannot see.
    """
    name = str(name or "").strip()
    if not name:
        raise ValueError("`document` (a slug or doc_id from `list_documents`) is required.")
    version = str(version).strip() if version not in (None, "") else None
    unknown = (f"Unknown document slug '{name}'" + (f" in collection '{collection}'" if collection else "")
               + ". Call `list_documents` for valid slugs.")
    rows = CORPUS.db.execute("SELECT * FROM documents WHERE slug = ?", (name,)).fetchall()
    if collection:
        # Another collection's slug is not this one's document. In the one
        # asked for, the name can only be a manual's doc_id -- and one
        # collection's slug may well be another's doc_id (`guide` here,
        # `guide-4-1` and `guide-4-2` there).
        rows = [r for r in rows if r["collection"] == collection]
    if len(rows) > 1:
        raise ValueError(f"'{name}' is a document in more than one collection "
                         f"({', '.join(sorted(r['collection'] for r in rows))}); pass `collection`.")
    row = rows[0] if rows else None
    if not CORPUS.has_editions:
        if not row:
            raise ValueError(unknown)
        if version:
            raise ValueError("This index was built before editions were recorded, so it cannot "
                             "select a version. Rebuild it with build_search_db.py.")
        return row
    if row and not version:
        return row
    doc_id = row["doc_id"] if row else name
    editions = editions_of(doc_id, row["collection"] if row else collection)
    if not editions:
        raise ValueError(unknown)
    if len({e["collection"] for e in editions}) > 1:
        raise ValueError(f"'{doc_id}' is a manual in more than one collection "
                         f"({', '.join(sorted({e['collection'] for e in editions}))}); pass `collection`.")
    if version is None:
        return next(e for e in editions if e["is_current"])
    # The same release however it is written: "2026.3" is "Y-2026.03". By the
    # numbers editions are ordered by, as the index build does for a pin.
    wanted = version_sort(version)
    for e in editions:
        if e["version"] and (norm_version(e["version"]) == norm_version(version)
                             or (wanted and e["version_sort"] == wanted)):
            return e
    earlier = [e for e in editions if wanted and e["version_sort"] and e["version_sort"] < wanted]
    if earlier and says_later(earlier[-1]):
        return earlier[-1]
    raise ValueError(
        f"No edition of `{doc_id}` in this corpus covers {version}. Editions here: "
        f"{describe_editions(editions)}. Nothing was substituted: use one of these, or say that "
        f"{version} is not available."
    )


def version_needs_document(args: dict) -> None:
    if args.get("version") not in (None, "") and not args.get("document"):
        raise ValueError("`version` needs `document`: versions belong to one manual, and two "
                         "manuals' releases are not comparable. `list_documents` shows each "
                         "manual's editions.")


def document_label(collection: str, slug: str, title: str | None = None) -> str:
    """A document as it is cited: its title, its release, its slug -- and a
    flag when it is not the edition search answers from."""
    doc = CORPUS.document(collection, slug)
    title = title or (doc["title"] if doc else slug)
    if doc is None or not CORPUS.has_editions:
        return f"{title} ({slug})"
    version = f", {edition_name(doc)}" if doc["version"] and doc["version"] not in title else ""
    if doc["is_current"]:
        return f"{title}{version} ({slug})"
    current = next((e for e in editions_of(doc["doc_id"], doc["collection"]) if e["is_current"]), None)
    return (f"{title}{version} ({slug}) — not the current edition"
            + (f" (current: {edition_name(current)})" if current else ""))


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
        # `collection` is the document's own: tool callers resolve it first.
        sql.append("AND slug = ?")
        params.append(document)
    else:
        # One edition per manual. Without this a manual held in three releases
        # answers every question three times, and nothing in the ranking says
        # which hit is the release in use.
        sql.append(current_only().removeprefix(" "))
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
    """`document` is a slug, and needs its `collection`: the same slug can
    name a document in each of two collections."""
    if document and not collection:
        raise ValueError("search() was given a document without its collection.")
    terms = to_match_terms(query)
    if not terms:
        raise ValueError("Query has no searchable words in it.")

    rows = run_match(" ".join(terms), collection, document, limit)
    if len(rows) < limit and len(terms) > 1:
        # Too few chunks hold every term. One unlucky word should not turn a
        # good question into a dead end -- nor should the one weak chunk that
        # happens to contain every word, "option" included, stand in for the
        # section that says "options". Rank by OR instead and let BM25 float
        # the chunks with the most and rarest terms. Falling back only on zero
        # AND results left 4 of 57 test questions with one wrong answer each;
        # this took hit@5 from 82% to 89%. Join the terms, not the words: an
        # identifier is a multi-word phrase, and "set scan configuration" has
        # to stay one phrase rather than become "set OR scan OR ...".
        rows = run_match(" OR ".join(terms), collection, document, limit)

    holding = ident_rows(query, [r["id"] for r in rows]) if rows else frozenset()
    scored = sorted(((rank_adjust(r, query, holding), r) for r in rows), key=lambda pair: pair[0])
    picked: list[sqlite3.Row] = []
    per_doc: dict[tuple[str, str], int] = {}
    for _score, row in scored:
        if max_per_document and not document:
            doc = (row["collection"], row["slug"])
            n = per_doc.get(doc, 0)
            if n >= max_per_document:
                continue
            per_doc[doc] = n + 1
        picked.append(row)
        if len(picked) >= limit:
            break
    return picked


def ident_rows(query: str, candidates: list[int] | None = None) -> frozenset[int]:
    """Rowids, among `candidates` where given, of the chunks that hold an
    identifier the query names, in an index built with --ident-index; empty
    where there is none or the query names none. Only the candidates are asked about: a flag
    such as -help is in tens of thousands of chunks, and only the few being
    ranked matter."""
    if not CORPUS.has_idents:
        return frozenset()
    named = dict.fromkeys(t for t, ident in query_tokens(query) if ident)
    if not named:
        return frozenset()
    expr = " OR ".join('"' + t.replace('"', "") + '"' for t in named)
    try:
        sql, params = "SELECT rowid FROM idents WHERE idents MATCH ?", [expr]
        if candidates is not None:
            sql += f" AND rowid IN ({', '.join('?' * len(candidates))})"
            params += candidates
        return frozenset(r[0] for r in CORPUS.db.execute(sql, params))
    except sqlite3.OperationalError:
        return frozenset()


def rank_adjust(row: sqlite3.Row, query: str, holding: frozenset[int] = frozenset()) -> float:
    """Nudge BM25 (lower is better) using signals BM25 cannot see.

    An entity is boosted when the query is the entity, or names it: an
    identifier-shaped word of the query equals it, or, for an entity of
    several words ("tessent -shell"), they appear in a row in the query."""
    score = row["score"]
    words = [w.lower() for w in WORD_RE.findall(query)]
    if not words:
        return score
    joined = "_".join(words)
    spaced = " ".join(words)

    entity = " ".join((row["entity"] or "").lower().split())
    if entity:
        tokens = query_tokens(query)
        named = {t for t, ident in tokens if ident}
        runs = {" ".join(t for t, _ in tokens[i:i + n])
                for n in (2, 3, 4) for i in range(len(tokens) - n + 1)}
        if (entity == joined or entity.replace("_", " ") == spaced   # the query is the entity
                or entity in named                                   # the query names it
                or (" " in entity and entity in runs)):               # "tessent -shell" mid-question
            score -= 6.0

    heading = (row["heading"] or "").lower()
    if heading:
        if heading == spaced or heading.replace("_", " ") == spaced:
            score -= 4.0
        else:
            content = {w for w in words if w not in STOPWORDS}
            if content and content <= set(WORD_RE.findall(heading)):
                score -= 1.5

    if holding and row["id"] in holding:
        score -= 3.0          # holds an identifier the query names, whole

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
    bits = [document_label(row["collection"], row["slug"], row["title"])]
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
        note_returned(r)
        out.append(f"### {i}. {r['heading'] or '(untitled section)'}")
        out.append(cite(r))
        figures = section_figures(r["collection"], r["slug"], r["ord"])
        out.append(f"section_id: {r['id']} · file: {r['file']}"
                   + (f" · {len(figures)} figure(s)" if figures else ""))
        snip = " ".join((r["snip"] or "").split())
        if snip:
            out.append(f"> {snip}")
        out.append("")
    out.append("Use `get_section` with a section_id above to read the full text.")
    return "\n".join(out) + CORPUS.coverage_warning()


def format_section(row: sqlite3.Row, body: str) -> str:
    note_returned(row)
    head =[f"# {row['heading'] or '(untitled section)'}", cite(row), f"file: {row['file']}", ""]
    if len(body) > MAX_SECTION_CHARS:
        body = body[:MAX_SECTION_CHARS] + f"\n\n…[truncated at {MAX_SECTION_CHARS:,} characters]"
    figures = section_figures(row["collection"], row["slug"], row["ord"])
    if figures:
        body = body.rstrip() + "\n\nFigures in this section -- view one with get_figure:\n" + "\n".join(
            f"- figure_id {f['id']}: {f['caption'] or '(no caption)'} (p. {f['page']})" for f in figures
        )
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
    version_needs_document(args)
    document = args.get("document") or None
    if document:
        row = resolve_document(document, args.get("version"), collection)
        document, collection = row["slug"], row["collection"]
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


def like_escape(text: str) -> str:
    """`text` as the literal pattern of a LIKE ... ESCAPE '\\': `_` and `%` are
    wildcards, and nearly every section file has an underscore."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# get_section with a `query` returns the passages of a section that match it,
# not all of it. A block is what lies between blank lines (a fenced code block
# is one block), and a list with its lead-in is one block too (group_lists).
# Blocks shorter than PASSAGE_MIN_CHARS are joined to the one after them, and a
# passage never ends on a heading or a lead-in, so what introduces something
# travels with it; a block is never cut inside. The best passages come back up
# to PASSAGE_BUDGET, and a gap between two of them of at most PASSAGE_GAP is
# returned too, so a list or a procedure has no hole in the middle. A section
# that already fits in PASSAGE_BUDGET is returned whole.
PASSAGE_MIN_CHARS = 300
PASSAGE_BUDGET = 4_000
PASSAGE_WORTH = 0.75     # narrow only if the passages are at most this share of the section
PASSAGE_LIST_MAX = 2_000    # a list with its lead-in is one passage if it is no longer than this
PASSAGE_GAP = 900           # a gap between two chosen passages this short is returned, not marked

LIST_ITEM_RE = re.compile(r"\s*(?:[-*•]|\d+[.)])\s")


def split_blocks(body: str) -> list[str]:
    blocks, current, fenced = [], [], False
    for line in body.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not line.strip() and not fenced:
            if current:
                blocks.append("\n".join(current))
                current = []
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def block_kind(block: str) -> str:
    """item (a bullet or numbered step), heading, code, lead (a paragraph that
    ends in a colon, so introduces what follows) or text."""
    first = block.lstrip().split("\n", 1)[0]
    if first.startswith("```"):
        return "code"
    if first.startswith("#"):
        return "heading"
    if LIST_ITEM_RE.match(first):
        return "item"
    return "lead" if block.rstrip().endswith(":") else "text"


def group_lists(blocks: list[str]) -> list[list[str]]:
    """`blocks` as units, each list one unit together with the line that
    introduces it. The converter puts a blank line between every bullet and
    keeps no indentation, so a list is many small blocks with the paragraphs
    that explain its items between them: a run of items, the blocks (up to two
    at a time, never a heading) between items, and one explanation after the
    last. A list longer than PASSAGE_LIST_MAX stays as it was; narrowing has to
    cut somewhere inside it."""
    kinds = [block_kind(b) for b in blocks]
    out, i, n = [], 0, len(blocks)
    while i < n:
        first = i + 1 if kinds[i] == "lead" and i + 1 < n and kinds[i + 1] == "item" else i
        if kinds[first] != "item":
            out.append([blocks[i]])
            i += 1
            continue
        last, t = first, first + 1
        while t < n:
            if kinds[t] == "item":
                last, t = t, t + 1
                continue
            between = 0
            while t + between < n and kinds[t + between] not in ("item", "heading") and between < 3:
                between += 1
            if between < 3 and t + between < n and kinds[t + between] == "item":
                t += between
                continue
            break
        end = last + 1 if last + 1 < n and kinds[last + 1] == "text" else last
        region = blocks[i:end + 1]
        if sum(map(len, region)) + 2 * (len(region) - 1) <= PASSAGE_LIST_MAX:
            out.append(region)
        else:
            out.extend([b] for b in region)
        i = end + 1
    return out


def split_passages(body: str) -> list[str]:
    blocks = split_blocks(body)
    units = group_lists(blocks) if PASSAGE_LIST_MAX else [[b] for b in blocks]
    passages, pending = [], ""
    for unit in units:
        pending = "\n\n".join([pending, *unit] if pending else unit)
        # A passage never ends on a heading or a lead-in: what it introduces goes with it.
        if len(pending) >= PASSAGE_MIN_CHARS and block_kind(unit[-1]) not in ("heading", "lead"):
            passages.append(pending)
            pending = ""
    if pending:
        if passages:
            passages[-1] += "\n\n" + pending
        else:
            passages.append(pending)
    return passages


def light_stem(word: str) -> str:
    """Enough of a stemmer for `clock` to meet `clocks` and `clocked`. The index
    stems with Porter; a passage is matched against the query with this."""
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[:-len(suffix)]
    return word


def query_terms(query: str) -> list[tuple[str, bool]]:
    """What a passage is matched on: the query's words without the stopwords
    (unless nothing else is left), each with whether it names an identifier."""
    tokens = query_tokens(query)
    content = [(tok, ident) for tok, ident in tokens if ident or tok not in STOPWORDS]
    return content or tokens


def passage_scores(passages: list[str], query: str) -> list[float]:
    """How well each passage answers `query`: the query's terms it holds, a term
    counting for less the more passages hold it (a word every passage has says
    nothing about which one answers), an identifier for three times as much."""
    terms = query_terms(query)
    lowered = [p.lower() for p in passages]
    words = [{light_stem(w) for w in re.findall(r"[a-z0-9_-]+", text)} for text in lowered]
    scores = [0.0] * len(passages)
    for tok, ident in terms:
        if ident or not tok.isalnum():
            who = {i for i, text in enumerate(lowered) if tok in text}
        else:
            stem = light_stem(tok)
            who = {i for i, w in enumerate(words) if stem in w}
        if len(who) == len(passages) > 1:
            continue                    # every passage holds it, so it picks none
        for i in who:
            scores[i] += (3.0 if ident else 1.0) * math.log(1 + len(passages) / len(who))
    return scores


def matching_passages(body: str, query: str) -> str | None:
    """The part of `body` that answers `query`, or None when there is nothing to
    narrow: the body fits already, or no passage holds a word of the query.

    The best passages first, up to PASSAGE_BUDGET characters, then the block
    either side of each while there is room, then any gap of at most PASSAGE_GAP
    characters between two of them; in the order they have in the section, with
    the other gaps said. A list or a short gap can take the answer a little past
    PASSAGE_BUDGET."""
    if len(body) <= PASSAGE_BUDGET:
        return None
    passages = split_passages(body)
    scores = passage_scores(passages, query)
    ranked = [i for i in sorted(range(len(passages)), key=lambda i: (-scores[i], i)) if scores[i] > 0]
    if not ranked:
        return None
    chosen, used = set(), 0
    for i in ranked:                    # the passages that match
        if chosen and used + len(passages[i]) > PASSAGE_BUDGET:
            continue
        chosen.add(i)
        used += len(passages[i])
    for i in sorted(chosen, key=lambda i: (-scores[i], i)):     # what is around them
        for near in (i - 1, i + 1):
            if 0 <= near < len(passages) and near not in chosen and used + len(passages[near]) <= PASSAGE_BUDGET:
                chosen.add(near)
                used += len(passages[near])
    order = sorted(chosen)              # a short gap is cheaper to return than to explain
    for before, after in zip(order, order[1:]):
        gap = sum(len(passages[j]) for j in range(before + 1, after))
        if 0 < gap <= PASSAGE_GAP:
            chosen.update(range(before + 1, after))
            used += gap
    if used > PASSAGE_WORTH * sum(map(len, passages)):
        return None                     # a small saving is not worth the risk of leaving the answer out
    out, last = [], -1
    for i in sorted(chosen):
        if i != last + 1:
            out.append(f"[… {sum(len(passages[j]) for j in range(last + 1, i)):,} characters omitted …]")
        out.append(passages[i])
        last = i
    if last + 1 < len(passages):
        out.append(f"[… {sum(len(passages[j]) for j in range(last + 1, len(passages))):,} characters omitted …]")
    out.append(f"Passages matching `{query}`: {used:,} of {sum(map(len, passages)):,} characters. "
               "Call get_section without `query` for the whole section.")
    return "\n\n".join(out)


def tool_get_section(args: dict) -> str:
    section_id, file_arg = args.get("section_id"), args.get("file")
    if section_id is not None:
        row = CORPUS.db.execute(SECTION_SELECT + " WHERE rowid = ?", (int(section_id),)).fetchone()
        if not row:
            raise ValueError(f"No section with section_id {section_id}.")
    elif file_arg:
        needle = str(file_arg).replace("\\", "/").lstrip("/").removeprefix("./")
        # Editions of a manual share their section filenames, so a path
        # without the edition's folder matches one in each. The exact path
        # first, then the current edition -- not whichever was indexed first,
        # which is the oldest.
        order = " ORDER BY (file = ?) DESC" + (
            ", ((collection, slug) IN (SELECT collection, slug FROM documents WHERE is_current = 1)) DESC"
            if CORPUS.has_editions else "")
        row = CORPUS.db.execute(
            SECTION_SELECT + " WHERE file = ? OR file LIKE ? ESCAPE '\\'" + order,
            (needle, "%/" + like_escape(needle), needle)
        ).fetchone()
        if not row:
            raise ValueError(f"No section matching file '{file_arg}'.")
    else:
        raise ValueError("Pass either `section_id` (from search results) or `file`.")

    query = str(args.get("query") or "").strip()

    def shown(r: sqlite3.Row) -> str:
        part = matching_passages(r["body"], query) if query else None
        return format_section(r, r["body"] if part is None else part)

    context = clamp_int(args.get("context"), 0, 0, 3)
    if not context:
        return shown(row)
    neighbours = CORPUS.db.execute(
        SECTION_SELECT + " WHERE slug = ? AND collection = ? AND ord BETWEEN ? AND ? ORDER BY ord",
        (row["slug"], row["collection"], row["ord"] - context, row["ord"] + context),
    ).fetchall()
    return "\n\n".join(
        f"---{'  <-- requested section' if n['id'] == row['id'] else ''}\n" + shown(n)
        for n in neighbours
    )


def entity_keys(name: str) -> list[str]:
    """The `name_lower` values an entry asked for as `name` can be filed under.

    An entry is filed under its TOC title, and a DRC rules manual titles its
    entries "DRC Rule C1" -- while the tool's log, and the engineer reading
    it, call that rule "C1" or "Rule C1". The name as given comes first, so
    an entry really named like a rule id is still found.
    """
    low = " ".join(name.lower().split())
    m = RULE_ID_RE.match(low)
    if not m:
        return [low]
    rule = m.group(1)
    return [low] + [k for k in (rule, f"rule {rule}", f"drc rule {rule}") if k != low]


def find_entities(keys: list[str], collection: str | None, document: str | None) -> list[sqlite3.Row]:
    """Entries filed under any of `keys`, from where a lookup looks: this
    collection, this edition, or else the current edition of each manual."""
    where, params = f"WHERE name_lower IN ({','.join('?' * len(keys))})", list(keys)
    if collection:
        where += " AND collection = ?"
        params.append(collection)
    if document:
        where += " AND slug = ?"
        params.append(document)
    else:
        where += current_only()
    return CORPUS.db.execute(f"SELECT * FROM entities {where} ORDER BY slug", params).fetchall()


def entry_chunks(hit: sqlite3.Row) -> list[sqlite3.Row]:
    return CORPUS.db.execute(
        SECTION_SELECT + " WHERE slug = ? AND collection = ? AND entity = ? ORDER BY ord",
        (hit["slug"], hit["collection"], hit["name"])
    ).fetchall()


def entity_pages(hit: sqlite3.Row) -> str:
    if hit["page_start"] is None:
        return ""
    if hit["page_end"] in (None, hit["page_start"]):
        return f" · p. {hit['page_start']}"
    return f" · pp. {hit['page_start']}–{hit['page_end']}"


def entry_text(chunks: list[sqlite3.Row], budget: int, rest: str | None = None) -> str:
    """An entry's chunks in order, cut at `budget` characters. `rest` says
    where to read what was cut; without it, the file the cut chunk is in."""
    out = []
    for c in chunks:
        body = c["body"]
        if budget <= 0:
            out.append(f"…[remaining chunks omitted; {rest or 'read ' + c['file'] + ' for the rest'}]")
            break
        if len(body) > budget:
            body = body[:budget] + (f"\n…[truncated; {rest}]" if rest else "\n…[truncated]")
        budget -= len(body)
        note_returned(c)
        out.append(f"<!-- section_id: {c['id']} · {c['file']} -->\n{body}")
    return "\n\n".join(out)


def tool_lookup_entity(args: dict) -> str:
    name = (args.get("name") or "").strip().strip("`")
    if not name:
        raise ValueError("`name` is required, e.g. 'set_scan_configuration'.")
    collection = normalize_collection(args.get("collection"))
    version_needs_document(args)
    document = None
    if args.get("document"):
        row = resolve_document(args["document"], args.get("version"), collection)
        document, collection = row["slug"], row["collection"]

    hits = find_entities(entity_keys(name), collection, document)
    if not hits:
        return suggest_entities(name, collection, document)

    out = []
    budget = MAX_SECTION_CHARS      # for the whole answer, not for each document that has the entry
    for n, hit in enumerate(hits):
        if budget <= 0:
            rest = [h["slug"] for h in hits[n:]]
            out.append(f"…[{len(rest)} more document(s) have this entry: {', '.join(rest)}. "
                       "Pass `document` to read one.]")
            break
        chunks = entry_chunks(hit)
        if not chunks:
            continue
        out.append(f"# `{hit['name']}`\n{document_label(hit['collection'], hit['slug'])}{entity_pages(hit)}"
                   f" · {len(chunks)} chunk(s)\n")
        text = entry_text(chunks, budget)
        budget -= len(text)
        out.append(text)
    return "\n\n".join(out) if out else suggest_entities(name, collection, document)


class LogItem:
    """One code or rule a log names: how often, how bad, and where first."""

    def __init__(self, name: str, kind: str):
        self.name, self.kind = name, kind
        self.lines = 0
        self.first_line = 0
        self.sample = ""
        self.severity: str | None = None

    def see(self, lineno: int, line: str) -> None:
        self.lines += 1
        if not self.first_line:
            self.first_line, self.sample = lineno, line.strip()[:200]
        m = LOG_SEVERITY_RE.search(line)
        if m:
            sev = m.group(1).lower()
            sev = "information" if sev.startswith("info") else sev
            if SEVERITY_ORDER[sev] < SEVERITY_ORDER[self.severity]:
                self.severity = sev

    def keys(self) -> list[str]:
        return entity_keys(self.name) if self.kind == "rule" else [self.name.lower()]


def scan_log(text: str) -> list[LogItem]:
    """Every message code and DRC rule a log names, worst and commonest first.

    A rule is only taken after the word "Rule": an id like "D5" on its own is
    as likely a pin, a net or a column of numbers.
    """
    items: dict[tuple[str, str], LogItem] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        seen = set()
        for kind, regex in (("code", LOG_CODE_RE), ("rule", LOG_RULE_RE)):
            for m in regex.finditer(line):
                k = (kind, m.group(1))
                if k in seen:
                    continue
                seen.add(k)
                items.setdefault(k, LogItem(m.group(1), kind)).see(lineno, line)
    return sorted(items.values(), key=lambda i: (SEVERITY_ORDER[i.severity], -i.lines, i.first_line))


def tool_explain_log(args: dict) -> str:
    text = args.get("text") or ""
    if not text.strip():
        raise ValueError("`text` is required: paste the log lines, or the whole log.")
    collection = normalize_collection(args.get("collection"))
    version_needs_document(args)
    document = None
    if args.get("document"):
        row = resolve_document(args["document"], args.get("version"), collection)
        document, collection = row["slug"], row["collection"]
    scope_name = document_label(collection, document) if document else "the current edition of any manual"

    items = scan_log(text)
    if not items:
        return ("No message codes (like `CMD-082`) or DRC rules (like `Rule C1`) in this text. "
                "To look up a message by its words, use search_docs.")

    out, missing, unquoted = [], [], []
    budget = MAX_SECTION_CHARS
    for item in items:
        found = [(h, c) for h in find_entities(item.keys(), collection, document) for c in [entry_chunks(h)] if c]
        if not found:
            missing.append(item)
            continue
        if len(out) >= LOG_MAX_ENTRIES or budget <= 0:
            unquoted.append(item)
            continue
        sev = f" · {item.severity}" if item.severity else ""
        block = [f"## `{item.name}` · {item.lines} line(s){sev} · first at line {item.first_line}\n"
                 f"> {item.sample}"]
        for hit, chunks in found:
            body = entry_text(chunks, max(0, min(LOG_ENTRY_CHARS, budget)),
                              f"lookup_entity name='{hit['name']}' document='{hit['slug']}' for the whole entry")
            budget -= len(body)
            block.append(f"### {document_label(hit['collection'], hit['slug'])}{entity_pages(hit)}\n\n{body}")
        out.append("\n\n".join(block))

    head = (f"{len(items)} distinct code(s) and rule(s) in {len(text.splitlines())} line(s); "
            f"{len(items) - len(missing)} documented in {scope_name}.")
    tail = []
    if unquoted:
        tail.append("Also documented, not quoted to keep this short (use lookup_entity): "
                    + ", ".join(f"`{i.name}` ({i.lines})" for i in unquoted))
    if missing:
        # Say where an entry the scope missed does exist; never substitute it.
        coll_sql, coll_params = (" AND collection = ?", [collection]) if collection else ("", [])
        where = []
        for item in missing:
            keys = item.keys()
            others = CORPUS.db.execute(
                f"SELECT DISTINCT slug FROM entities WHERE name_lower IN ({','.join('?' * len(keys))}){coll_sql}"
                " ORDER BY slug", keys + coll_params).fetchall()
            note = f" -- an entry in {', '.join(r['slug'] for r in others)}" if others else ""
            where.append(f"- `{item.name}` ({item.lines} line(s), first at line {item.first_line}){note}")
        more = f"\n- …and {len(where) - 50} more" if len(where) > 50 else ""
        tail.append(f"Not an entry in {scope_name}. A code no reference here lists may still be "
                    "explained in prose; try search_docs.\n" + "\n".join(where[:50]) + more)
    return "\n\n".join([head] + out + tail)


def editions_with_entry(name: str, collection: str | None, document: str | None) -> list[sqlite3.Row]:
    """The documents that have an entry called `name` in an edition other than
    the one a lookup read: other editions of that manual, or any edition of
    any manual when no document was named."""
    if not CORPUS.has_editions:
        return []
    keys = entity_keys(name)
    sql = ("SELECT DISTINCT d.* FROM entities e JOIN documents d ON d.slug = e.slug AND d.collection = e.collection"
           f" WHERE e.name_lower IN ({','.join('?' * len(keys))})")
    params: list = list(keys)
    if document:
        doc = CORPUS.document(collection, document)
        sql += " AND d.doc_id = ? AND d.collection = ? AND d.slug != ?"
        params += [doc["doc_id"], doc["collection"], document]
    elif collection:
        sql += " AND d.collection = ?"
        params.append(collection)
    return CORPUS.db.execute(sql + " ORDER BY d.collection, d.doc_id, d.version_sort", params).fetchall()


def other_editions_with(name: str, collection: str | None, document: str | None) -> str:
    """Where an entry missing from the edition asked about does exist.

    An entry dropped in a later release, or not yet added in an earlier one,
    is in the index under another edition. Saying which is the answer to
    "where did this command go"; a list of similar names is not.
    """
    rows = editions_with_entry(name, collection, document)
    if not rows:
        return ""
    scope = document_label(collection, document) if document else "the current edition of any manual"
    listing = "\n".join(f"- {document_label(r['collection'], r['slug'])}" for r in rows)
    return (f"`{name}` is not an entry in {scope}. It is an entry in:\n\n{listing}\n\n"
            "Pass one of these slugs as `document` to read it there.")


def suggest_entities(name: str, collection: str | None, document: str | None = None) -> str:
    """No exact hit: say which other edition has it, else offer prefix
    matches, then fuzzy ones, then full-text."""
    elsewhere = other_editions_with(name, collection, document)
    if elsewhere:
        return elsewhere
    # Every suggestion comes from where the lookup looked: this collection,
    # this edition, or the current edition of each manual. Close matches used
    # to be drawn from every entry in the corpus, so a typo asked of 2.0
    # could be answered with a command only 1.0 has, and nothing said so.
    scope, scope_params = "", []
    if collection:
        scope += " AND collection = ?"
        scope_params.append(collection)
    if document:
        scope += " AND slug = ?"
        scope_params.append(document)
    else:
        scope += current_only()
    prefix = CORPUS.db.execute(
        f"SELECT name, slug FROM entities WHERE name_lower LIKE ?{scope} ORDER BY name LIMIT 25",
        [name.lower() + "%"] + scope_params,
    ).fetchall()
    if prefix:
        listing = "\n".join(f"- `{r['name']}` ({r['slug']})" for r in prefix)
        return f"No entry named exactly `{name}`. Entries starting with it:\n\n{listing}"

    candidates = [r["name_lower"] for r in CORPUS.db.execute(
        f"SELECT DISTINCT name_lower FROM entities WHERE 1 = 1{scope}", scope_params)]
    close = difflib.get_close_matches(name.lower(), candidates, n=8, cutoff=0.7)
    if close:
        return f"No entry named `{name}`. Did you mean:\n\n" + "\n".join(f"- `{c}`" for c in close)
    return (
        f"`{name}` is not in any reference document's entry index. It may still be "
        "discussed in prose -- falling back to full-text search:\n\n"
        + format_results(search(name, collection=collection, document=document, limit=8), name)
    )


# ----------------------------------------------------------------------
# check_script: the commands and options of a Tcl script, against the entries
# the reference documents here have.
#
# A Tcl command is a line of words; a word is bare, "quoted" or {braced}, and
# `[...]` in a bare or quoted word runs another command. A braced word is data
# unless the command makes it a script (if, foreach, proc, ...) or an
# expression (which may hold `[...]` too). That is all this reads: it does not
# run the script, expand variables or know a tool's own syntax, so it checks
# what the script says, not what it will do.

MAX_SCRIPT_CHARS = 200_000
CHECK_LISTED = 40                      # findings listed per section before "and N more"

TCL_BUILTINS = frozenset("""
after append apply array binary break catch cd clock close concat continue dict encoding eof error eval exec exit
expr fblocked fconfigure fcopy file fileevent flush for foreach format gets glob global if incr info interp join
lappend lassign lindex linsert list llength lmap load lrange lrepeat lreplace lreverse lsearch lset lsort namespace
open package pid proc puts pwd read regexp regsub rename return scan seek set socket source split string subst switch
tell time trace try unset update uplevel upvar variable vwait while
""".split())
# Commands whose last word is a script, though they are not Tcl's own.
TCL_BODY_LAST = frozenset({"foreach", "lmap", "foreach_in_collection", "redirect", "uplevel"})
TCL_OPTION_RE = re.compile(r"-[A-Za-z][A-Za-z0-9_]*(?:-[A-Za-z0-9_]+)*")
# Documents that document variables, attributes, messages or rules, not commands.
NOT_COMMAND_DOC_RE = re.compile(r"variable|attribute|message|error|\brules?\b", re.I)


class TclWord(NamedTuple):
    text: str          # without the quotes or braces around it
    kind: str          # "bare", "quoted" or "braced"
    line: int


def skip_braced(text: str, i: int) -> int:
    """The index after the `}` matching the `{` at text[i] (the end, if none)."""
    depth, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def skip_bracket(text: str, i: int) -> int:
    """The index after the `]` matching the `[` at text[i] (the end, if none)."""
    depth, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            i = skip_braced(text, i)
            continue
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def tcl_word(text: str, i: int, line: int) -> tuple[TclWord, int]:
    """The word starting at text[i], and the index after it."""
    n, c = len(text), text[i]
    if c == "{":
        end = skip_braced(text, i)
        closed = end <= n and text[end - 1] == "}" and end > i + 1
        return TclWord(text[i + 1:end - 1 if closed else end], "braced", line), end
    if c == '"':
        j = i + 1
        while j < n and text[j] != '"':
            j = skip_bracket(text, j) if text[j] == "[" else j + (2 if text[j] == "\\" else 1)
        return TclWord(text[i + 1:j], "quoted", line), min(j + 1, n)
    j = i
    while j < n and text[j] not in " \t\r\n;":
        if text[j] == "\\":
            if text[j + 1:j + 2] == "\n":
                break                      # a backslash-newline ends the word, like a space
            j += 2
        elif text[j] == "[":
            j = skip_bracket(text, j)
        else:
            j += 1
    return TclWord(text[i:j], "bare", line), j


def tcl_parse(text: str, line: int = 1) -> list[list[TclWord]]:
    """The commands of a script, each as its words. `line` numbers the first line."""
    commands, words = [], []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\" and text[i + 1:i + 2] == "\n":
            i, line = i + 2, line + 1
        elif c in " \t\r":
            i += 1
        elif c in "\n;":
            if words:
                commands.append(words)
                words = []
            line += c == "\n"
            i += 1
        elif c == "#" and not words:       # a comment runs to the end of its line, continuations included
            while i < n and text[i] != "\n":
                if text[i] == "\\" and text[i + 1:i + 2] == "\n":
                    line += 1
                i += 2 if text[i] == "\\" else 1
        else:
            word, end = tcl_word(text, i, line)
            words.append(word)
            line += text.count("\n", i, end)
            i = max(end, i + 1)
    if words:
        commands.append(words)
    return commands


def tcl_substitutions(text: str, line: int) -> list[tuple[str, int]]:
    """The scripts inside `[...]` in a word's text, with the line each starts on."""
    out, i, n = [], 0, len(text)
    while i < n:
        if text[i] == "\\":
            i += 2
        elif text[i] == "[":
            end = skip_bracket(text, i)
            out.append((text[i + 1:end - 1 if text[end - 1:end] == "]" else end],
                        line + text.count("\n", 0, i)))
            i = end
        else:
            i += 1
    return out


class TclScan:
    """Every command a script runs, however deep: in if/while/for/foreach/proc/
    catch/switch bodies and in `[...]`. A command is recorded with the options
    after its name and the line it is on."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, list[tuple[str, int]]]] = []
        self.dynamic: list[int] = []          # lines whose command name is computed
        self.procs: set[str] = set()

    def script(self, text: str, line: int = 1) -> None:
        for words in tcl_parse(text, line):
            self.command(words)

    def subst(self, word: TclWord) -> None:
        if word.kind != "braced":
            for inner, ln in tcl_substitutions(word.text, word.line):
                self.script(inner, ln)

    def expr(self, word: TclWord) -> None:
        for inner, ln in tcl_substitutions(word.text, word.line):
            self.script(inner, ln)

    def body(self, word: TclWord) -> None:
        if word.kind == "braced":
            self.script(word.text, word.line)
        else:
            self.subst(word)

    def command(self, words: list[TclWord]) -> None:
        head, args = words[0], words[1:]
        name = head.text.removeprefix("::")
        if head.kind != "bare" or not name or name[0] in "$[" or "[" in name or "$" in name:
            self.dynamic.append(head.line)
            for w in args:
                self.subst(w)
            return
        n = len(args)
        if name == "if":
            if args:
                self.expr(args[0])
            i = 1
            while i < n:
                w = args[i]
                if w.kind == "bare" and w.text == "elseif":
                    if i + 1 < n:
                        self.expr(args[i + 1])
                    i += 2
                elif w.kind == "bare" and w.text in ("then", "else"):
                    i += 1
                else:
                    self.body(w)
                    i += 1
            return
        if name == "while":
            for i, w in enumerate(args):
                (self.expr if i == 0 else self.body)(w)
            return
        if name == "for":
            for i, w in enumerate(args):
                (self.expr if i == 1 else self.body)(w)
            return
        if name == "proc":
            if args:
                self.procs.add(args[0].text)
            if n >= 3:
                self.body(args[-1])
            return
        if name in ("catch", "time") or (name == "eval" and n == 1):
            for i, w in enumerate(args):
                (self.body if i == 0 else self.subst)(w)
            return
        if name == "namespace" and args and args[0].text == "eval":
            for w in args[:-1]:
                self.subst(w)
            if n > 1:
                self.body(args[-1])
            return
        if name == "expr":
            for w in args:
                self.expr(w)
            return
        if name == "try":
            if args:
                self.body(args[0])
            i = 1
            while i < n:
                if args[i].text in ("on", "trap") and i + 3 < n:
                    self.body(args[i + 3])
                    i += 4
                elif args[i].text == "finally" and i + 1 < n:
                    self.body(args[i + 1])
                    i += 2
                else:
                    i += 1
            return
        if name == "switch":
            i = 0
            while i < n and args[i].kind == "bare" and args[i].text.startswith("-"):
                last = args[i].text == "--"
                i += 2 if args[i].text in ("-matchvar", "-indexvar") else 1
                if last:
                    break
            rest = args[i + 1:]
            for w in args[:i + 1]:
                self.subst(w)
            if len(rest) == 1 and rest[0].kind == "braced":
                for pair in tcl_parse(rest[0].text, rest[0].line):
                    for k in range(1, len(pair), 2):
                        self.body(pair[k])
            else:
                for k, w in enumerate(rest):
                    (self.body if k % 2 else self.subst)(w)
            return
        if name in TCL_BODY_LAST and args:
            for w in args[:-1]:
                self.subst(w)
            self.body(args[-1])
            self.record(name, head.line, args[:-1])
            return
        self.record(name, head.line, args)
        for w in args:
            self.subst(w)

    def record(self, name: str, line: int, args: list[TclWord]) -> None:
        """A call to a command Tcl does not provide, with the options it was given."""
        if name in TCL_BUILTINS:
            return
        options = []
        for w in args:
            if w.kind == "braced":
                continue
            if w.text == "--":
                break
            if TCL_OPTION_RE.fullmatch(w.text):
                options.append((w.text, w.line))
        self.calls.append((name, line, options))


def entry_options(hit: sqlite3.Row) -> frozenset[str]:
    """The options an entry's text shows: every `-word` in it."""
    text = "\n".join(MD_ESCAPE_RE.sub(r"\1", c["body"]) for c in entry_chunks(hit))
    # A trailing `_` is where a PDF broke `-through_list` across lines.
    return frozenset(o.rstrip("_") for o in TCL_OPTION_RE.findall(text))


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def line_list(lines: list[int], cap: int = 6) -> str:
    seen = sorted(set(lines))
    shown = ", ".join(map(str, seen[:cap])) + (", …" if len(seen) > cap else "")
    return ("line " if len(seen) == 1 else "lines ") + shown


def check_script(script: str, collection: str | None, document: str | None) -> str:
    scan = TclScan()
    scan.script(script)
    calls = [(n, ln, opts) for n, ln, opts in scan.calls if n not in scan.procs]
    scope = document_label(collection, document) if document else "the current edition of each manual"
    if not calls:
        skipped = f" ({plural(len(scan.dynamic), 'command')} named by a variable)" if scan.dynamic else ""
        return ("No tool commands in this script" + skipped + ": only Tcl's own commands and the script's own procs, "
                "which have no reference entry.")

    by_name: dict[str, list] = {}
    for name, ln, opts in calls:
        by_name.setdefault(name, []).append((ln, opts))
    candidates = None
    unknown, bad_options, abbreviated, documented, no_options, partial = [], [], [], [], [], []
    for name, uses in by_name.items():
        hits = find_entities([name.lower()], collection, document)
        label = f"`{name}`"
        titles = [(CORPUS.document(h["collection"], h["slug"]) or {"title": h["slug"]})["title"] for h in hits]
        if not hits or all(NOT_COMMAND_DOC_RE.search(t) for t in titles):
            where = ""
            if hits:
                where = (" It is an entry only in "
                         + ", ".join(sorted({document_label(h['collection'], h['slug']) for h in hits}))
                         + ", which documents variables, messages or rules, not commands.")
            else:
                other = editions_with_entry(name, collection, document)
                if other:
                    where = " It is an entry in " + ", ".join(document_label(r["collection"], r["slug"]) for r in other) + "."
                elif len(unknown) < 25:
                    if candidates is None:
                        scope_sql = (" AND collection = ?" if collection else "") + (
                            " AND slug = ?" if document else current_only())
                        candidates = [r[0] for r in CORPUS.db.execute(
                            f"SELECT DISTINCT name_lower FROM entities WHERE 1 = 1{scope_sql}",
                            ([collection] if collection else []) + ([document] if document else []))]
                    close = difflib.get_close_matches(name.lower(), candidates, n=3, cutoff=0.75)
                    if close:
                        where = " Did you mean " + ", ".join(f"`{c}`" for c in close) + "?"
            unknown.append(f"- {label} ({line_list([ln for ln, _ in uses])}): not an entry in {scope}.{where}")
            continue

        places = "; ".join(f"{document_label(h['collection'], h['slug'])}{entity_pages(h)}" for h in hits)
        documented.append(f"- {label} -- {places} ({line_list([ln for ln, _ in uses])})")
        shown = {(h["collection"], h["slug"]): entry_options(h) for h in hits}
        known: set[str] = set().union(*shown.values())
        used = [(o, ln) for _, opts in uses for o, ln in opts]
        if used and not known:
            no_options.append(label)
            continue
        for opt, ln in sorted(set(used), key=lambda x: (x[1], x[0])):
            if opt in known:
                # A command several manuals have is checked against all of them
                # together; say where it is not, since a script is for one tool.
                have = [key for key, o in shown.items() if opt in o]
                if len(have) < len(shown):
                    partial.append(f"- {label} {opt} (line {ln}): in {', '.join(k[1] for k in have)}, not in "
                                   + ", ".join(k[1] for k in shown if k not in have))
                continue
            longer = sorted(k for k in known if k.startswith(opt))
            if len(longer) == 1:
                abbreviated.append(f"- {label} {opt} (line {ln}) is the start of `{longer[0]}`")
                continue
            if longer:
                bad_options.append(f"- {label} {opt} (line {ln}): ambiguous, the start of "
                                   + ", ".join(f"`{k}`" for k in longer[:6]))
                continue
            close = difflib.get_close_matches(opt, known, n=3, cutoff=0.6)
            hint = " Did you mean " + ", ".join(f"`{c}`" for c in close) + "?" if close else ""
            bad_options.append(f"- {label} {opt} (line {ln}): not an option its entry shows ({places}).{hint}")

    def section(title: str, rows: list[str]) -> list[str]:
        if not rows:
            return []
        more = [f"- …and {len(rows) - CHECK_LISTED} more"] if len(rows) > CHECK_LISTED else []
        return ["", f"## {title} ({len(rows)})", *rows[:CHECK_LISTED], *more]

    head = (f"{plural(len(by_name), 'command')} checked, used {plural(len(calls), 'time')}, against {scope}: "
            f"{len(documented)} documented, {len(unknown)} not.")
    out = [head, *section("Unknown commands", unknown), *section("Unknown options", bad_options),
           *section("Options only some manuals show (pass `document` to check against one)", partial),
           *section("Abbreviated options", abbreviated), *section("Documented", documented)]
    notes = []
    if no_options:
        notes.append("Options not checked, because the entry shows none: " + ", ".join(no_options[:20]))
    if scan.dynamic:
        notes.append(f"Not checked: {plural(len(scan.dynamic), 'command')} named by a variable or a substitution "
                     f"({line_list(scan.dynamic)}).")
    if scan.procs:
        notes.append("Not checked: procs the script defines (" + ", ".join(sorted(scan.procs)[:20]) + ").")
    notes.append("An option is checked against the `-words` in its command's entry, so one named only in another "
                 "command's example there passes; an abbreviation is accepted by many tools, not all. Variable "
                 "values and what a tool does with the script are not checked.")
    return "\n".join(out + [""] + notes)


def tool_check_script(args: dict) -> str:
    script = args.get("script")
    if not isinstance(script, str) or not script.strip():
        raise ValueError("`script` is required: the Tcl script, as text.")
    if len(script) > MAX_SCRIPT_CHARS:
        raise ValueError(f"The script is {len(script):,} characters; the most checked at once is {MAX_SCRIPT_CHARS:,}. "
                         "Send it in parts.")
    collection = normalize_collection(args.get("collection"))
    version_needs_document(args)
    document = None
    if args.get("document"):
        row = resolve_document(args["document"], args.get("version"), collection)
        document, collection = row["slug"], row["collection"]
    return check_script(script, collection, document)


def tool_list_documents(args: dict) -> str:
    collection = normalize_collection(args.get("collection"))
    sql, params = "SELECT * FROM documents", []
    if collection:
        sql += " WHERE collection = ?"
        params.append(collection)
    versioned = CORPUS.has_editions
    order = "collection, doc_id, version_sort, slug" if versioned else "collection, slug"
    rows = CORPUS.db.execute(f"{sql} ORDER BY {order}", params).fetchall()
    per_manual = Counter((r["collection"], r["doc_id"]) for r in rows) if versioned else Counter()

    out = [f"{len(rows)} document(s) in the corpus:\n"]
    current = None
    for r in rows:
        if r["collection"] != current:
            current = r["collection"]
            out.append(f"\n## {current}")
        flags = []
        if versioned and per_manual[(r["collection"], r["doc_id"])] > 1:
            flags.append(f"manual `{r['doc_id']}`, "
                         + ("the current edition" if r["is_current"] else "not current: read only when named"))
        if r["has_pages"]:
            flags.append("page numbers")
        if r["has_entities"]:
            flags.append("per-chunk entity attribution")
        if "figure_count" in r.keys() and r["figure_count"]:
            flags.append(f"{r['figure_count']} figures")
        extra = f" — {', '.join(flags)}" if flags else ""
        gap = (r["section_count"] or 0) - (r["indexed_count"] or 0)
        coverage = (
            f"{r['indexed_count']} of {r['section_count']} sections indexed (**{gap} missing**)"
            if gap else f"{r['section_count']} sections"
        )
        pages = f", {r['page_count']} PDF pages" if r["page_count"] else ""
        version = f", {edition_name(r)}" if versioned and r["version"] and r["version"] not in r["title"] else ""
        out.append(f"- `{r['slug']}` — {r['title']}{version}\n"
                   f"  {coverage}{pages}, {r['char_count']:,} chars{extra}")
    out.append("\nPass a slug as `document` to `search_docs` to search just that one.")
    if any(n > 1 for n in per_manual.values()):
        out.append("Where a manual has several editions, search and lookup_entity answer from the "
                   "current one. To read another, pass its slug as `document`, or the manual's "
                   "name with `version`; `compare_versions` says what differs between two.")
    return "\n".join(out) + CORPUS.coverage_warning()


def tool_get_toc(args: dict) -> str:
    row = resolve_document(args.get("document"), args.get("version"), normalize_collection(args.get("collection")))
    slug = row["slug"]

    max_level = clamp_int(args.get("max_level"), 2, 1, 6)
    contains = (args.get("contains") or "").strip().lower()

    lines = [f"# {document_label(row['collection'], slug)}", f"{row['page_count']} pages, {row['section_count']} sections"]
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


def section_figures(collection: str, slug: str, ordinal) -> list[sqlite3.Row]:
    """Figures extract_figures.py tied to one section, in page order."""
    if ordinal is None or not CORPUS.has_figures:
        return []
    return CORPUS.db.execute(
        "SELECT id, caption, page FROM figures WHERE slug = ? AND collection = ? AND section_ord = ?"
        " ORDER BY page, id",
        (slug, collection, ordinal),
    ).fetchall()


def image_block(png: bytes) -> dict:
    return {"type": "image", "data": base64.b64encode(png).decode("ascii"), "mimeType": "image/png"}


def tool_get_figure(args: dict) -> list[dict]:
    if not CORPUS.has_figures:
        raise ValueError("This index has no figures. Run extract_figures.py on the corpus, "
                         "then rebuild the index with build_search_db.py.")
    figure_id = args.get("figure_id")
    if figure_id is None:
        raise ValueError("`figure_id` is required; get_section lists a section's figures.")
    row = CORPUS.db.execute(
        "SELECT f.*, d.title FROM figures f JOIN documents d ON d.slug = f.slug AND d.collection = f.collection"
        " WHERE f.id = ?",
        (int(figure_id),),
    ).fetchone()
    if not row:
        raise ValueError(f"No figure with figure_id {figure_id}.")
    try:
        png = (CORPUS.root / row["file"]).read_bytes()
    except OSError as exc:
        raise ValueError(f"The image for figure {figure_id} could not be read ({exc}). "
                         "Rerun extract_figures.py, then build_search_db.py.") from exc

    lines = [row["caption"] or "(figure without a caption)",
             f"{document_label(row['collection'], row['slug'])} · p. {row['page']}"]
    if row["section_ord"] is not None:
        sec = CORPUS.db.execute(
            "SELECT rowid AS id, heading FROM chunks WHERE slug = ? AND collection = ? AND ord = ?",
            (row["slug"], row["collection"], row["section_ord"]),
        ).fetchone()
        if sec:
            lines.append(f"Illustrates section_id {sec['id']}: {sec['heading']}")
    else:
        lines.append("Not tied to a section; get_page_image shows its page in context.")
    if row["description"]:
        lines.append(f"Generated description (check it against the image): {row['description']}")
    return [{"type": "text", "text": "\n".join(lines)}, image_block(png)]


def tool_get_page_image(args: dict) -> list[dict]:
    row = resolve_document(args.get("document"), args.get("version"), normalize_collection(args.get("collection")))
    slug = row["slug"]
    page_no = clamp_int(args.get("page"), 0, 0, 1_000_000)
    if not 1 <= page_no <= (row["page_count"] or 0):
        raise ValueError(f"`page` must be between 1 and {row['page_count']} for {slug}.")
    try:
        import pymupdf  # optional: everything else here is standard library
    except ImportError:
        raise ValueError("Rendering pages needs PyMuPDF in the server's Python: pip install pymupdf. "
                         "get_figure works without it.") from None
    pdf = source_pdf_path(row)
    if pdf is None:
        raise ValueError(f"The source PDF for {slug} ({row['source_pdf']}) is not in its collection's "
                         "source/ folder, nor beside docs/.")
    with pymupdf.open(pdf) as doc:
        page = doc[page_no - 1]
        zoom = min(PAGE_DPI / 72, IMAGE_MAX_PX / max(page.rect.width, page.rect.height))
        png = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).tobytes("png")
    return [{"type": "text", "text": f"{document_label(row['collection'], slug)} · p. {page_no} of {row['page_count']}"},
            image_block(png)]


def source_pdf_path(row: sqlite3.Row) -> Path | None:
    """A document's PDF: where the index build found it, else the places a
    collection keeps PDFs -- they may have been moved into source/ since."""
    name = row["source_pdf"] or ""
    collection = CORPUS.root / row["collection_dir"]
    recorded = row["source_path"] if "source_path" in row.keys() else None
    candidates = ([CORPUS.root / recorded] if recorded else []) + [collection / "source" / name, collection / name]
    return next((p for p in candidates if name and p.is_file()), None)


# -------------------------------------------------------- compare_versions

HEADING_KEY_RE = re.compile(r"[0-9a-z]+")
# "Chapter 3 ", "Appendix A ", "3. ", "A. ": how a heading is numbered changes
# with the vendor's template. One manual went from "Chapter 3 A Typical PDL
# Retargeting Flow" to "3. A Typical PDL Retargeting Flow" between releases,
# which listed every chapter as both removed and added.
HEADING_NUMBER_RE = re.compile(
    r"^\s*(?:(?:chapter|appendix|part|section)\s+[0-9A-Za-z]+[.:]?|\d+(?:\.\d+)*[.:)]?|[A-Za-z][.:)])\s+", re.I)
DIFF_QUOTES = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'",
                             "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-"})
# Markdown emphasis, matched only where it is emphasis: a pair of markers
# around text, the opening one not following a word character and the closing
# one not preceding one. `_cell_em_` loses its outer pair and keeps the
# underscore inside; `set_mode -pattern data_*` loses nothing.
MD_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|<>~])")
MD_CODE_RE = re.compile(r"`([^`\n]+)`")
MD_EMPHASIS_RES = (
    re.compile(r"(?<![\w*])\*\*(?=\S)(.+?)(?<=\S)\*\*(?![\w*])"),
    re.compile(r"(?<![\w])__(?=[^\s_])(.+?)(?<=[^\s_])__(?![\w])"),
    re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])"),
    re.compile(r"(?<![\w])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w])"),
)


class Line(NamedTuple):
    key: str      # what has to match for two editions to have the same line
    text: str     # the line as it is shown
    loose: str    # the same with code compared as loosely as prose is


def _typography(text: str) -> str:
    """Text with what a vendor's template decides taken out: quote and dash
    style, compatibility forms, runs of spaces."""
    return " ".join(unicodedata.normalize("NFKC", text).translate(DIFF_QUOTES).split())


def diff_key(line: str, literal: bool = False, exact: bool = True) -> str:
    """A line as compared between editions.

    In prose, emphasis, quote style, dash style and spacing change with the
    vendor's template from one release to the next and say nothing about the
    product, so they are not compared. Everything else is: case, numbers,
    every word, every character of an identifier or a pattern. Deleting each
    `_` and `*` outright made `data_*` and `data*` one key, and two entries
    that match different names "identical"; only markers that pair up around
    text are taken out.

    Code is not prose. What a code span holds, and a `literal` line -- one
    inside a fenced block -- is compared exactly as written: its asterisks
    are wildcards, not emphasis, and `print("a  b")` is not `print("a b")`.
    `exact=False` gives the key that treats code as loosely as prose, which
    is how a line that differs only in spacing or quote style is recognised
    and reported as that, instead of being hidden or shown as a rewording.
    """
    if literal:
        return line.rstrip() if exact else _typography(line)
    held: list[str] = []

    def hold(match: re.Match) -> str:
        held.append(match.group(1))
        return f"\x00{len(held) - 1}\x00"

    text = MD_CODE_RE.sub(hold, line)         # set aside before anything is normalised
    text = unicodedata.normalize("NFKC", text).translate(DIFF_QUOTES)
    for _ in range(3):                        # `**_bold italic_**` nests
        before = text
        for pattern in MD_EMPHASIS_RES:
            text = pattern.sub(r"\1", text)
        if text == before:
            break
    text = " ".join(MD_ESCAPE_RE.sub(r"\1", text).split())
    return re.sub(r"\x00(\d+)\x00",
                  lambda m: held[int(m.group(1))] if exact else _typography(held[int(m.group(1))]), text)


def without_label(body: str, breadcrumb: str | None) -> str:
    """A chunk's text without the breadcrumb line its converter put first:
    the chunk's own label, not the entry's text."""
    crumb = (breadcrumb or "").strip("*").strip()
    first, _, rest = body.partition("\n")
    return rest if crumb and first.strip().strip("*").strip() == crumb else body


def keyed_lines(text: str) -> list[Line]:
    """An entry's lines, keyed for comparison.

    The whole entry at once, not chunk by chunk: a chunk boundary can fall
    inside a fenced example, and whether a line is code has to survive it.
    Inside a fence every line that is not blank counts, punctuation included
    -- a `[` added around a list, or a `+` that became a `-`, is a change.
    In prose a line with no letter or digit is a rule or a table border.
    """
    lines, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            if line.strip():
                lines.append(Line(diff_key(line, literal=True), line.rstrip(),
                                  diff_key(line, literal=True, exact=False)))
            continue
        key = diff_key(line)
        if key and (re.search(r"[0-9A-Za-z]", key) or "`" in line):
            lines.append(Line(key, line.strip(), diff_key(line, exact=False)))
    return lines


def entry_lines(doc: sqlite3.Row, name: str) -> tuple[list[Line], str]:
    """Every line of one entry in one edition, and its pages. The whole
    entry, however long: a comparison cut where a lookup is cut would report
    two long entries as identical whenever they differ late."""
    chunks = CORPUS.db.execute(
        "SELECT body, breadcrumb, page_start, page_end FROM chunks"
        " WHERE slug = ? AND collection = ? AND entity = ? ORDER BY ord",
        (doc["slug"], doc["collection"], name),
    ).fetchall()
    lines = keyed_lines("\n".join(without_label(c["body"], c["breadcrumb"]) for c in chunks))
    starts = [c["page_start"] for c in chunks if c["page_start"] is not None]
    ends = [c["page_end"] for c in chunks if c["page_end"] is not None]
    pages = ""
    if starts:
        first, last = min(starts), max(ends or starts)
        pages = f"p. {first}" if first == last else f"pp. {first}–{last}"
    return lines, pages


def only_in(mine: list[Line], theirs: list[Line]) -> list[Line]:
    """Lines of `mine` with no counterpart in `theirs`, in reading order. A
    line there twice and here once is matched once."""
    available = Counter(line.key for line in theirs)
    out = []
    for line in mine:
        if available[line.key]:
            available[line.key] -= 1
        else:
            out.append(line)
    return out


def respaced(removed: list[Line], added: list[Line]) -> tuple[list[tuple[Line, Line]], list[Line], list[Line]]:
    """Pair off unmatched lines that are the same once spacing and quote
    style in code are set aside too. Returns the pairs and what is left of
    each side. In code such a difference can matter, so it is not dropped;
    but one name added to an aligned block moves every other line of it, and
    listing those as removed and added would bury the line that changed."""
    waiting: dict[str, list[int]] = {}
    for i, line in enumerate(added):
        waiting.setdefault(line.loose, []).append(i)
    pairs, left, used = [], [], set()
    for line in removed:
        candidates = waiting.get(line.loose)
        if candidates:
            i = candidates.pop(0)
            used.add(i)
            pairs.append((line, added[i]))
        else:
            left.append(line)
    return pairs, left, [line for i, line in enumerate(added) if i not in used]


def listed(items: list[str], budget: int, what: str) -> list[str]:
    """Items as a list that fits, saying how many it left out."""
    out, used = [], 0
    for n, item in enumerate(items):
        if n >= MAX_LISTED or used + len(item) > budget:
            out.append(f"- …and {len(items) - n} more {what} not shown")
            break
        out.append(f"- {item}")
        used += len(item) + 3
    return out


def compare_entry(old: sqlite3.Row, new: sqlite3.Row, name: str) -> str:
    def entity(doc: sqlite3.Row):
        return CORPUS.db.execute("SELECT name FROM entities WHERE slug = ? AND collection = ? AND name_lower = ?",  # noqa: F821
                                 (doc["slug"], doc["collection"], name.lower())).fetchone()

    def has_entities(doc: sqlite3.Row) -> bool:
        return bool(CORPUS.db.execute("SELECT 1 FROM entities WHERE slug = ? AND collection = ? LIMIT 1",  # noqa: F821
                                      (doc["slug"], doc["collection"])).fetchone())

    in_old, in_new = entity(old), entity(new)
    v_old, v_new = edition_name(old), edition_name(new)
    if not in_old and not in_new:
        has_entries = has_entities(old) or has_entities(new)
        raise ValueError(
            f"`{name}` is not an entry in the {v_old} or the {v_new} edition of `{new['doc_id']}`."
            if has_entries else
            f"`{new['doc_id']}` has no per-entry attribution, so `name` cannot select part of it. "
            "Leave `name` out to compare its headings, or search each edition with `search_docs` "
            "and `document`/`version`.")
    if not in_old or not in_new:
        there, other, missing = (new, old, v_old) if in_new else (old, new, v_new)
        # Absent from an edition that names its entries is evidence. Absent
        # from one that names none is not: a reference is attributed only
        # where enough entries are found, so one that shrank below that has
        # every command and no entity rows.
        if not has_entities(other):
            raise ValueError(
                f"`{name}` is an entry in the {edition_name(there)} edition of `{new['doc_id']}`, but the "
                f"{missing} edition has no per-entry attribution at all, so the index cannot say whether "
                "it is in that edition too. Leave `name` out to compare the editions' headings, or search "
                f"the {missing} edition with `search_docs` and `document`/`version`.")
        return (f"`{name}` is an entry in the {edition_name(there)} edition of `{new['doc_id']}` and "
                f"not in the {missing} edition: it was {'added' if in_new else 'removed'} between them.\n\n"
                f"Read it with `lookup_entity` and `document`: `{there['slug']}`.")

    old_lines, old_pages = entry_lines(old, in_old["name"])
    new_lines, new_pages = entry_lines(new, in_new["name"])
    removed, added = only_in(old_lines, new_lines), only_in(new_lines, old_lines)
    same = len(new_lines) - len(added)
    spacing, removed, added = respaced(removed, added)
    head = [
        f"# `{in_new['name']}`: {v_old} → {v_new}",
        f"{document_label(old['collection'], old['slug'])}" + (f" · {old_pages}" if old_pages else ""),
        f"{document_label(new['collection'], new['slug'])}" + (f" · {new_pages}" if new_pages else ""),
        "",
    ]
    scope = (f"All {len(old_lines)} and {len(new_lines)} lines of the entry were compared, not only the "
             "part a lookup returns. In prose, emphasis marks, quote style and spacing are ignored; code "
             "spans and fenced code are compared exactly. Line order is not compared.")
    if not (removed or added or spacing):
        return "\n".join(head + [f"No differences: every line of this entry is in both editions. {scope}"])
    counts = [f"{same} line(s) are in both editions", f"{len(removed)} only in {v_old}",
              f"{len(added)} only in {v_new}"]
    if spacing:
        counts.append(f"{len(spacing)} line(s) of code differ only in spacing or quote style")
    out = head + [", ".join(counts) + f". {scope} A reworded line appears on both sides.", ""]
    budget = MAX_SECTION_CHARS // 3
    if removed:
        out += [f"## Only in {v_old}"] + listed([l.text for l in removed], budget, f"line(s) only in {v_old}") + [""]
    if added:
        out += [f"## Only in {v_new}"] + listed([l.text for l in added], budget, f"line(s) only in {v_new}") + [""]
    if spacing:
        out += ["## Code that differs only in spacing or quote style", "```"]
        used = 0
        for n, (was, now) in enumerate(spacing):
            if n >= MAX_LISTED or used > budget:
                out.append(f"…and {len(spacing) - n} more pair(s) not shown")
                break
            out += [f"{v_old}: {was.text}", f"{v_new}: {now.text}"]
            used += len(was.text) + len(now.text)
        out += ["```", ""]
    return "\n".join(out).rstrip()


def heading_key(title: str) -> str:
    title = unicodedata.normalize("NFKC", title)
    unnumbered = HEADING_NUMBER_RE.sub("", title, count=1)
    return " ".join(HEADING_KEY_RE.findall((unnumbered if unnumbered.strip() else title).lower()))


def compare_listing(old: sqlite3.Row, new: sqlite3.Row) -> str:
    v_old, v_new = edition_name(old), edition_name(new)
    out = [f"# `{new['doc_id']}`: {v_old} → {v_new}", document_label(old["collection"], old["slug"]),
           document_label(new["collection"], new["slug"]), ""]

    def names(doc: sqlite3.Row) -> tuple[Counter, dict[str, str]]:
        shown = {r["name_lower"]: r["name"] for r in CORPUS.db.execute(  # noqa: F821
            "SELECT name, name_lower FROM entities WHERE slug = ? AND collection = ?", (doc["slug"], doc["collection"]))}
        return Counter(shown.keys()), shown

    def headings(row: sqlite3.Row) -> tuple[Counter, dict[str, str]]:
        """How many times each title occurs, and how it is written. Counted,
        because titles repeat -- every chapter has its Overview and its
        Limitations -- and a set of them cannot show that one more was added."""
        counts, shown = Counter(), {}
        for e in json.loads(row["toc_json"] or "[]"):
            title = (e.get("title") or "").strip()
            if heading_key(title):
                counts[heading_key(title)] += 1
                shown.setdefault(heading_key(title), title)
        return counts, shown

    def only(mine: Counter, theirs: Counter, shown: dict[str, str], other: str) -> list[str]:
        return [shown[k] if not theirs[k] else f"{shown[k]} ({mine[k] - theirs[k]} more than in {other})"
                for k in sorted(mine, key=lambda k: shown[k].lower()) if mine[k] > theirs[k]]

    (e_old, old_names), (e_new, new_names) = names(old), names(new)
    if e_old and e_new:
        a, b, a_shown, b_shown, what = e_old, e_new, old_names, new_names, "entries"
        caveat = ("Names only. An entry in both editions may still have changed: pass `name` to "
                  "compare one entry's text.")
    else:
        (a, a_shown), (b, b_shown), what = headings(old), headings(new), "headings"
        caveat = (("Only one of these editions names its entries, so entries could not be compared. "
                   if e_old or e_new else "")
                  + "Headings only, from the two PDFs' bookmark outlines, chapter numbering aside. A "
                  "renamed heading shows as one removed and one added, and text under a heading that "
                  "is in both may still have changed: search each edition with `search_docs` and "
                  "`document`/`version` to compare a topic.")
        if not a or not b:
            return "\n".join(out + ["One of these editions has no bookmark outline, so there are no "
                                    "headings to compare. Search each edition with `search_docs` and "
                                    "`document`/`version` instead."])
    added, removed = only(b, a, b_shown, v_old), only(a, b, a_shown, v_new)
    out.append(f"{sum(b.values()):,} {what} in {v_new} and {sum(a.values()):,} in {v_old}: "
               f"{sum((b - a).values())} added, {sum((a - b).values())} removed, "
               f"{sum((a & b).values()):,} in both. {caveat}")
    half = MAX_SECTION_CHARS // 2
    out += ["", f"## Added in {v_new}"] + (listed(added, half, f"{what} added") or ["(none)"])
    out += ["", f"## Removed since {v_old}"] + (listed(removed, half, f"{what} removed") or ["(none)"])
    return "\n".join(out)


def tool_compare_versions(args: dict) -> str:
    if not CORPUS.has_editions:
        raise ValueError("This index was built before editions were recorded. Rebuild it with "
                         "build_search_db.py.")
    collection = normalize_collection(args.get("collection"))
    new = resolve_document(args.get("document"), args.get("to_version"), collection)
    editions = editions_of(new["doc_id"], new["collection"])
    if len(editions) < 2:
        raise ValueError(f"Only one edition of `{new['doc_id']}` is in this corpus "
                         f"({edition_name(new)}), so there is nothing to compare it with.")
    if args.get("from_version") not in (None, ""):
        # By the slug of the edition already chosen, which no other document
        # has. Its doc_id can also be some other document's slug, and resolved
        # by that name the comparison would be against a different manual.
        old = resolve_document(new["slug"], args["from_version"], new["collection"])
    else:
        earlier = [e for e in editions if (e["version_sort"] or "") < (new["version_sort"] or "")]
        if not earlier:
            raise ValueError(f"{edition_name(new)} is the earliest edition of `{new['doc_id']}` here. "
                             f"Pass `from_version` and `to_version`. Editions: {describe_editions(editions)}.")
        old = earlier[-1]
    if old["slug"] == new["slug"]:
        raise ValueError("`from_version` and `to_version` name the same edition.")
    # Entries exist in the index only for the sections that could be read. A
    # section missing from one edition would show as an entry removed, or as
    # two entries with no differences, while the answer says the whole entry
    # was compared.
    for edition in (old, new):
        gap = (edition["section_count"] or 0) - (edition["indexed_count"] or 0)
        if gap > 0:
            raise ValueError(
                f"{gap} of {edition['section_count']} sections of "
                f"{document_label(edition['collection'], edition['slug'])} are "
                "not in the index, so a comparison could call text removed or unchanged only because "
                "it was not indexed. Rebuild the index with build_search_db.py once every section "
                "file is readable, then compare.")
    name = (args.get("name") or "").strip().strip("`")
    return compare_entry(old, new, name) if name else compare_listing(old, new)


TOOL_TITLES = {
    "search_docs": "Search the documentation",
    "get_section": "Read a section",
    "lookup_entity": "Look up an entry",
    "explain_log": "Explain a tool log",
    "check_script": "Check a Tcl script",
    "list_documents": "List documents",
    "get_toc": "Table of contents",
    "compare_versions": "Compare editions",
    "get_figure": "Show a figure",
    "get_page_image": "Show a PDF page",
}


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
    doc_schema = {"type": "string", "description": "An edition's slug, or a manual's name to mean its "
                                                   "current edition (see list_documents). Where two "
                                                   "collections have one of that name, pass `collection` too."}
    version_schema = {"type": "string", "description": "The tool release in use, e.g. '2025.2': read the "
                                                       "edition that applies to it instead. Needs "
                                                       "`document`. A release no edition covers is "
                                                       "refused, not approximated."}

    tools = [
        {
            "name": "search_docs",
            "description": (
                "Full-text search across the whole corpus. Use this first for any question "
                "about what these documents cover. Results are BM25-ranked and cite document, "
                "version, breadcrumb and page; each carries a section_id for get_section. Prefer "
                "this over answering from memory, and cite what you used. It searches the current "
                "edition of each manual; name `document` and `version` when the user is on another."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Words, an identifier, or a \"quoted phrase\". Underscores and hyphens are handled."},
                    "collection": coll_schema,
                    "document": {"type": "string", "description": "Restrict to one document: an edition's slug, or a manual's name for its current edition (see list_documents). Where two collections have one of that name, pass `collection` too."},
                    "version": version_schema,
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
                "continues past one chunk. It also lists the section's figures by figure_id. "
                "A long section costs many tokens: pass `query` (your question, or the words you "
                "are looking for) to get only the passages that match it, usually 4,000 to 6,000 "
                "characters, with the gaps marked."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "section_id": {"type": "integer", "description": "section_id from a search result."},
                    "file": {"type": "string", "description": "Path as shown in a search result."},
                    "context": {"type": "integer", "description": "Also return this many sections either side, 0-3 (default 0)."},
                    "query": {"type": "string", "description": "Return only the passages of a long section that match these words."},
                },
            },
        },
        {
            "name": "lookup_entity",
            "description": (
                "Look up one entry by exact name in a reference document -- a command, API "
                "function, part number, error code -- and return its whole entry reassembled "
                "from every chunk that belongs to it, with its page. Prefer this over search "
                "when you know the name: it will not attach one entry's details to another. It "
                "reads the current edition of each manual unless `document` names another."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exact entry name, e.g. 'set_scan_configuration', 'CMD-082', or a DRC rule as 'C1' or 'Rule C1'."},
                    "collection": coll_schema,
                    "document": doc_schema,
                    "version": version_schema,
                },
                "required": ["name"],
            },
        },
        {
            "name": "explain_log",
            "description": (
                "Explain a tool's log or transcript in one call: finds every message code "
                "(e.g. CMD-082) and DRC rule (e.g. 'Rule C1') in the pasted text, counts them, "
                "puts errors first, and returns each one's reference entry -- what it means and "
                "what to do next -- with document and page. Use it instead of one lookup_entity "
                "per code when the user pastes log output. Codes no manual here documents are "
                "listed, not guessed at."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The log lines, or the whole log, as printed."},
                    "collection": coll_schema,
                    "document": doc_schema,
                    "version": version_schema,
                },
                "required": ["text"],
            },
        },
        {
            "name": "check_script",
            "description": (
                "Check a Tcl tool script against the reference documents: lists every command and "
                "option it uses, and reports the commands no reference entry has and the options "
                "their entries do not show -- with line numbers, document and page, and the edition "
                "that does have a command this one lacks. Use it when asked to write, review or "
                "port a script, before trusting a command or option from memory. It reads the "
                "current edition of each manual unless `document` and `version` name the release "
                "the script is for; a command several manuals share is checked against all of "
                "them, so pass `document` when the script is for one tool. It checks names, not "
                "values or what the tool will do."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "script": {"type": "string", "description": "The Tcl script, as text."},
                    "collection": coll_schema,
                    "document": doc_schema,
                    "version": version_schema,
                },
                "required": ["script"],
            },
        },
        {
            "name": "list_documents",
            "description": (
                "List the documents in the corpus with slugs, titles, versions, sizes and whether "
                "they carry page numbers and entity attribution. Call this to find the right slug "
                "for a filtered search, to see which editions of a manual are here, or to say "
                "what is actually covered."
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
                    "document": doc_schema,
                    "version": version_schema,
                    "collection": coll_schema,
                    "max_level": {"type": "integer", "description": "Deepest heading level to show, 1-6 (default 2)."},
                    "contains": {"type": "string", "description": "Only entries whose title contains this text (any level)."},
                },
                "required": ["document"],
            },
        },
        {
            "name": "compare_versions",
            "description": (
                "What differs between two editions of one manual. Without `name`: the entries (in "
                "a reference document) or headings added and removed. With `name`: that entry's "
                "text in both editions, compared in full, returning the lines only one of them "
                "has. Use this for any question about what changed between releases instead of "
                "reading both and comparing by eye. It reports what the manuals say, which is not "
                "a release note: behaviour can change without the manual changing."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "document": doc_schema,
                    "from_version": {"type": "string", "description": "The earlier release (default: the edition just before `to_version`)."},
                    "to_version": {"type": "string", "description": "The later release (default: the manual's current edition)."},
                    "name": {"type": "string", "description": "Compare this one entry's text, e.g. 'set_scan_configuration'."},
                    "collection": coll_schema,
                },
                "required": ["document"],
            },
        },
        {
            "name": "get_figure",
            "description": (
                "Show one figure as an image, with its caption, page and the section it "
                "illustrates. get_section lists a section's figures by figure_id, and search "
                "results say how many a section has. Look at the figure rather than guessing "
                "what a diagram shows from the text around it."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "figure_id": {"type": "integer", "description": "figure_id from get_section."},
                },
                "required": ["figure_id"],
            },
        },
        {
            "name": "get_page_image",
            "description": (
                "Render one page of a document's source PDF as an image. Use it for a figure or "
                "table the text extraction mangled, or to see a page exactly as printed."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "document": doc_schema,
                    "version": version_schema,
                    "collection": coll_schema,
                    "page": {"type": "integer", "description": "1-based page number in the PDF."},
                },
                "required": ["document", "page"],
            },
        },
    ]
    for tool in tools:
        # Every tool only reads, and none reaches beyond the corpus. Clients that
        # follow MCP 2025-06-18 use these to skip a confirmation prompt; older
        # ones ignore the fields.
        tool["title"] = TOOL_TITLES[tool["name"]]
        tool["annotations"] = {"readOnlyHint": True, "openWorldHint": False}
    return tools


HANDLERS = {
    "search_docs": tool_search_docs,
    "get_section": tool_get_section,
    "lookup_entity": tool_lookup_entity,
    "explain_log": tool_explain_log,
    "check_script": tool_check_script,
    "list_documents": tool_list_documents,
    "get_toc": tool_get_toc,
    "compare_versions": tool_compare_versions,
    "get_figure": tool_get_figure,
    "get_page_image": tool_get_page_image,
}


class MethodNotFound(Exception):
    pass


def handle_request(method: str, params: dict) -> dict:
    if method == "initialize":
        asked = params.get("protocolVersion")
        client = params.get("clientInfo")
        if isinstance(client, dict) and isinstance(client.get("name"), str):
            CALL_STATE.client = client["name"][:80]
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
                "about it from these tools rather than from memory, and cite the document, its "
                "version and the page you used. A manual may be here in several editions: answers "
                "come from its current one unless you name another with `document` and `version`, "
                "and `compare_versions` says what changed between two. When a section lists "
                "figures, look at the ones that matter with get_figure rather than guessing what "
                "a diagram shows."
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
    returned().clear()
    started = time.perf_counter()
    try:
        result, is_error = handler(params.get("arguments") or {}), False
    except (CorpusMissing, ValueError) as exc:
        result, is_error = str(exc), True
    except Exception:
        log(traceback.format_exc())
        result, is_error = f"{name} failed unexpectedly; see the server log for the traceback.", True
    # Text tools return a string; image tools return their content blocks.
    content = result if isinstance(result, list) else [{"type": "text", "text": result}]
    record_call(name, params.get("arguments"), content, is_error, started)
    return {"content": content, "isError": is_error}


def resolve_db(explicit: str | None) -> Path:
    """--db wins, then $PDF_TO_RAG_DB, then an index beside this file, then
    one in the working directory.

    The scripts are shared across corpora while an index belongs to exactly one,
    so the server has to be told which -- unless it is the copy
    build_search_db.py places beside an index, which serves that one. The cwd
    fallback is a convenience for running it by hand from inside a corpus.
    """
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("PDF_TO_RAG_DB")
    if env:
        return Path(env).resolve()
    beside = Path(__file__).resolve().parent / "mcp-index.sqlite3"
    if beside.is_file():
        return beside
    return Path.cwd().resolve() / "mcp-index.sqlite3"


def process_message(msg) -> dict | None:
    """One decoded JSON-RPC message in; the response to send, or None where
    there is nothing to send (a notification, or a reply to something we
    never asked). Stdio and HTTP both end up here, so they answer alike."""
    if not isinstance(msg, dict):
        # A batch, a bare string, a number. The next request still has to
        # be answered, and this one is told why it was not.
        return {"jsonrpc": "2.0", "id": None, "error": {
            "code": -32600, "message": "Invalid Request: expected one JSON-RPC object (batches are not supported)"}}
    req_id, method = msg.get("id"), msg.get("method")
    if method is None:
        return None  # a response to something we sent; we send no requests
    params = msg.get("params")
    if params is not None and not isinstance(params, dict):
        if req_id is None:
            return None
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "Invalid params: expected an object"}}
    try:
        response = {"jsonrpc": "2.0", "id": req_id, "result": handle_request(method, params or {})}
    except MethodNotFound as exc:
        response = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {exc}"}}
    except Exception as exc:
        log(traceback.format_exc())
        response = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(exc)}}
    return None if req_id is None else response  # a notification is acknowledged by silence


# ----------------------------------------------------------------------
# HTTP: MCP's Streamable HTTP transport, the part a request/response server
# needs. One endpoint takes a POSTed JSON-RPC message and answers with JSON, or
# with 202 and no body where the message needs no answer. It opens no event
# stream (a GET is refused with 405, which the transport allows) and keeps no
# session, so any request can go to any thread, or to another process.

HTTP_PATH = "/mcp"
HEALTH_PATH = "/health"
HTTP_MAX_BODY = 2 * 1024 * 1024
TOKEN_ENV = "PDF_TO_RAG_TOKEN"
MIN_TOKEN_CHARS = 16


def load_tokens(token_file: str | None) -> list[str]:
    """The bearer tokens that are accepted: $PDF_TO_RAG_TOKEN, and one per
    line of the file (blank lines and `#` comments skipped)."""
    tokens = []
    if os.environ.get(TOKEN_ENV, "").strip():
        tokens.append(os.environ[TOKEN_ENV].strip())
    if token_file:
        tokens += [ln.strip() for ln in Path(token_file).read_text(encoding="utf-8").splitlines()
                   if ln.strip() and not ln.lstrip().startswith("#")]
    if any(len(t) < MIN_TOKEN_CHARS for t in tokens):
        raise ValueError(f"a token is shorter than {MIN_TOKEN_CHARS} characters. Make one with: "
                         "python -c \"import secrets; print(secrets.token_urlsafe(32))\"")
    return tokens


def split_host_port(spec: str) -> tuple[str, int]:
    spec = spec.strip()
    if spec.isdigit():
        return "127.0.0.1", int(spec)
    host, sep, port = spec.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"--http takes HOST:PORT (for example 127.0.0.1:8765), not {spec!r}")
    return host.strip("[]"), int(port)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False        # a name, or "" (every interface): reachable from elsewhere


def health() -> tuple[int, dict]:
    """GET /health: what this server is serving, and whether it can be trusted
    to cover the corpus. 200 even when the index is partial or stale, because
    answers carry that warning; 503 only where there is no index at all."""
    try:
        db = CORPUS.db
    except CorpusMissing:
        return 503, {"status": "no index", "index": str(CORPUS.db_path)}
    meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
    partial, stale = CORPUS._partial_warning().strip(), CORPUS.staleness_warning().strip()
    return 200, {
        "status": "degraded" if partial or stale else "ok",
        "server": SERVER_VERSION,
        "index": str(CORPUS.db_path),
        "built_at": meta.get("built_at"),
        "documents": db.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"],
        "chunks": db.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"],
        "partial": partial or None,
        "stale": stale or None,
    }


class McpHandler(BaseHTTPRequestHandler):
    """One request. `tokens` and `origins` are set on a subclass by serve_http."""

    protocol_version = "HTTP/1.1"
    server_version = "pdf-to-rag-mcp"
    timeout = 60            # seconds a client may stall mid-request before its thread is let go
    tokens: tuple[bytes, ...] = ()
    token_ids: tuple[str, ...] = ()           # token_id() of each in `tokens`, in order
    origins: frozenset = frozenset()

    def log_message(self, format, *args):           # noqa: A002 -- the base class's name
        log(f"{self.client_address[0]} {format % args}")

    def drain_body(self) -> None:
        """Read and drop a request body that is about to be refused unread.
        Closing a connection with the client's body still in flight resets it
        (on Windows before the client has read the reply). A body too large to
        drain is left, and that client gets the reset."""
        length = self.headers.get("Content-Length", "")
        if length.isdigit() and 0 < int(length) <= HTTP_MAX_BODY:
            try:
                self.rfile.read(int(length))
            except OSError:
                pass

    def reply(self, status: int, body=None, headers=(), close: bool = False) -> None:
        if close:
            self.drain_body()
        data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for key, value in headers:
            self.send_header(key, value)
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(data)

    def allowed(self) -> bool:
        """The bearer token, then the Origin. Answers the request itself if not."""
        # A connection can carry several requests on one thread, so who made
        # this one is set every time, and is nobody when no token is required.
        CALL_STATE.token_id = None
        if self.tokens:
            scheme, _, supplied = self.headers.get("Authorization", "").partition(" ")
            given = supplied.strip().encode("utf-8", "replace")
            who = None
            for token, label in zip(self.tokens, self.token_ids):
                if hmac.compare_digest(given, token):
                    who = label
            if scheme.lower() != "bearer" or who is None:
                self.reply(401, {"error": "a valid bearer token is required"},
                           [("WWW-Authenticate", 'Bearer realm="pdf-to-rag"')], close=True)
                return False
            CALL_STATE.token_id = who
        # A page in a browser can reach a server on the loopback address, and the
        # transport asks servers to refuse such a request. Clients that are not
        # browsers send no Origin.
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.origins:
            self.reply(403, {"error": f"origin {origin} is not allowed (see --allow-origin)"}, close=True)
            return False
        return True

    def route(self) -> str:
        return self.path.split("?", 1)[0]

    def do_GET(self) -> None:
        if not self.allowed():
            return
        if self.route() == HEALTH_PATH:
            try:
                status, body = health()
            except Exception as exc:
                log(traceback.format_exc())
                status, body = 500, {"status": "error", "detail": str(exc)}
            self.reply(status, body)
        elif self.route() == HTTP_PATH:
            self.reply(405, {"error": "this server does not open an event stream; POST a message"},
                       [("Allow", "POST")])
        else:
            self.reply(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        if not self.allowed():
            return
        if self.route() == HTTP_PATH:
            self.reply(405, {"error": "there are no sessions to end"}, [("Allow", "POST")])
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self.allowed():
            return
        if self.route() != HTTP_PATH:
            return self.reply(404, {"error": "not found"}, close=True)
        # Refusals before the body is read close the connection: the body is
        # still on the wire, and would be read as the start of the next request.
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self.reply(415, {"error": "Content-Type must be application/json"}, close=True)
        length = self.headers.get("Content-Length", "")
        if not length.isdigit():
            return self.reply(411, {"error": "Content-Length is required"}, close=True)
        if int(length) > HTTP_MAX_BODY:
            return self.reply(413, {"error": f"a message is at most {HTTP_MAX_BODY:,} bytes"}, close=True)
        try:
            msg = json.loads(self.rfile.read(int(length)).decode("utf-8"))
        except ValueError:
            return self.reply(400, {"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32700, "message": "Parse error: not JSON"}})
        # The header is checked once the message is read: `initialize` is where
        # the version is negotiated, and its reply names the one this server speaks.
        protocol = self.headers.get("MCP-Protocol-Version")
        negotiating = isinstance(msg, dict) and msg.get("method") == "initialize"
        if protocol and protocol not in KNOWN_PROTOCOLS and not negotiating:
            return self.reply(400, {"error": f"unsupported MCP-Protocol-Version {protocol}"})
        response = process_message(msg)
        if response is None:
            self.reply(202)
        else:
            self.reply(200 if isinstance(msg, dict) else 400, response)


def serve_http(spec: str, tokens: list[str], origins: list[str]) -> int:
    try:
        host, port = split_host_port(spec)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if not tokens and not is_loopback(host):
        print(f"Refusing to listen on {host or 'every interface'} without a token: anyone who can reach the port "
              f"could read the documentation. Set ${TOKEN_ENV}, or pass --token-file.", file=sys.stderr)
        return 2
    handler = type("Handler", (McpHandler,), {"tokens": tuple(t.encode("utf-8") for t in tokens),
                                               "token_ids": tuple(token_id(t.encode("utf-8")) for t in tokens),
                                               "origins": frozenset(origins)})
    server_class = type("Server", (ThreadingHTTPServer,), {
        "address_family": socket.AF_INET6 if ":" in host else socket.AF_INET, "daemon_threads": True})
    try:
        httpd = server_class((host, port), handler)
    except OSError as exc:
        print(f"Cannot listen on {spec}: {exc}", file=sys.stderr)
        return 2
    bound = httpd.server_address[1]
    shown = f"[{host}]" if ":" in host else (host or "0.0.0.0")
    log(f"listening on http://{shown}:{bound}{HTTP_PATH} "
        + ("(bearer token required)" if tokens else "(NO TOKEN: any process on this machine can read the documentation)"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def client_configs(url: str, name: str) -> str:
    """The two editors' settings for a server at `url`. The token is never in
    them: VS Code asks for it (and hides what is typed); Cursor reads it from
    the environment."""
    vscode = {
        "inputs": [{"type": "promptString", "id": "pdf-to-rag-token", "description": "Bearer token for the "
                    "documentation server", "password": True}],
        "servers": {name: {"type": "http", "url": url,
                           "headers": {"Authorization": "Bearer ${input:pdf-to-rag-token}"}}},
    }
    cursor = {"mcpServers": {name: {"url": url, "headers": {"Authorization": f"Bearer ${{env:{TOKEN_ENV}}}"}}}}
    return (f"# VS Code: .vscode/mcp.json\n{json.dumps(vscode, indent=2)}\n\n"
            f"# Cursor: .cursor/mcp.json (start Cursor with {TOKEN_ENV} set)\n{json.dumps(cursor, indent=2)}\n")


def main() -> int:
    global CORPUS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="path to the index built by build_search_db.py")
    ap.add_argument("--http", metavar="HOST:PORT", help="listen for MCP over HTTP here instead of reading stdio")
    ap.add_argument("--token-file", metavar="FILE", help="bearer tokens that are accepted, one per line "
                    f"(with --http; ${TOKEN_ENV} is accepted as well)")
    ap.add_argument("--allow-origin", action="append", default=[], metavar="ORIGIN",
                    help="let browser requests from this Origin through (with --http; none are by default)")
    ap.add_argument("--print-client-config", metavar="URL",
                    help="print VS Code and Cursor settings for a server at URL, and exit")
    ap.add_argument("--name", default="docs", help="the server's name in --print-client-config (default: docs)")
    args = ap.parse_args()

    if args.print_client_config:
        print(client_configs(args.print_client_config, args.name))
        return 0

    CORPUS = Corpus(resolve_db(args.db))
    configure_call_log()
    if CORPUS.available:
        log(f"serving {CORPUS.db_path} ({CORPUS.db_path.stat().st_size / 1048576:.0f} MB)")
    else:
        log(f"warning: no index at {CORPUS.db_path} -- run build_search_db.py")

    if args.http:
        try:
            tokens = load_tokens(args.token_file)
        except (ValueError, OSError) as exc:
            print(f"Cannot read the tokens: {exc}", file=sys.stderr)
            return 2
        return serve_http(args.http, tokens, args.allow_origin)

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
        response = process_message(msg)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
