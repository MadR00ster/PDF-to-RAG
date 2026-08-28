#!/usr/bin/env python3
"""
Build the search index that serves a converted corpus over MCP.

Walks every `<collection>/docs/<slug>/manifest.json` under the corpus root,
reads each section chunk off disk, and writes one SQLite file with an FTS5
full-text index. `mcp_server.py` queries that file; nothing else reads it.

Standard library only -- FTS5 ships inside Python's bundled SQLite, so a
corpus becomes queryable from an editor with no packages, no API key and no
network.

The index is a snapshot, not a live view: rerun this after converting,
reconverting or enriching anything. The build is atomic (temp file, then
rename), so a failed run leaves the previous index in place.

Layout it expects -- either shape works, and both are auto-detected:

    corpus/docs/<slug>/manifest.json                 single collection
    corpus/<collection>/docs/<slug>/manifest.json    several collections

Usage:
  python scripts/build_search_db.py --root "D:/Manuals"
  python scripts/build_search_db.py --root "D:/Manuals" --emit-vscode-config
  python scripts/build_search_db.py --root "D:/Manuals" --stats-only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DEFAULT_DB_NAME = "mcp-index.sqlite3"

# Reads are dominated by the filesystem, and on a cloud-synced corpus
# (OneDrive, Dropbox, iCloud) by hydrating placeholder files -- network
# latency, not disk or CPU -- so they parallelise well. Measured on a
# OneDrive corpus: ~1 file/s letting the sync client fetch them on its own
# schedule, ~22 files/s pulling them concurrently.
READ_THREADS = 32

SCHEMA = """
PRAGMA journal_mode = OFF;
PRAGMA synchronous = OFF;

CREATE TABLE documents (
    slug          TEXT PRIMARY KEY,
    collection    TEXT NOT NULL,
    collection_dir TEXT NOT NULL,
    title         TEXT NOT NULL,
    source_pdf    TEXT,
    page_count    INTEGER,
    section_count INTEGER,   -- sections this document has on disk
    indexed_count INTEGER,   -- how many of them made it into the index
    char_count    INTEGER,
    has_pages     INTEGER NOT NULL DEFAULT 0,
    has_entities  INTEGER NOT NULL DEFAULT 0,
    toc_json      TEXT
);

-- One row per section chunk. The four leading columns are searchable; the
-- rest are UNINDEXED so they cost storage but no index space, and can still
-- be filtered on in WHERE once MATCH has narrowed the candidate set.
CREATE VIRTUAL TABLE chunks USING fts5(
    heading,
    entity,
    breadcrumb,
    body,
    slug         UNINDEXED,
    collection   UNINDEXED,
    title        UNINDEXED,
    file         UNINDEXED,
    ord          UNINDEXED,
    level        UNINDEXED,
    page_start   UNINDEXED,
    page_end     UNINDEXED,
    chars        UNINDEXED,
    noise        UNINDEXED,
    tokenize     = "unicode61 remove_diacritics 2"
);

-- Exact entity lookup for reference documents. FTS tokenisation splits
-- `set_scan_configuration` into three tokens, which is what we want for prose
-- search but useless for "give me this exact entry", so the unsplit name lives
-- here with a plain index on it.
CREATE TABLE entities (
    name        TEXT NOT NULL,
    name_lower  TEXT NOT NULL,
    slug        TEXT NOT NULL,
    collection  TEXT NOT NULL,
    chunk_count INTEGER NOT NULL,
    page_start  INTEGER,
    page_end    INTEGER
);
CREATE INDEX idx_entities_lower ON entities(name_lower);
CREATE INDEX idx_entities_slug  ON entities(slug);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""

DOT_LEADER_RE = re.compile(r"\.\s?\.\s?\.\s?\.")
FRONT_MATTER_HEADINGS = (
    "contents", "table of contents", "feedback", "index",
    "list of figures", "list of tables", "about this",
)


def slugify(name: str) -> str:
    s = re.sub(r"[^0-9A-Za-z]+", "-", name).strip("-").lower()
    return s or "corpus"


def is_noise(heading: str, body: str) -> bool:
    """Flag front matter: contents pages, figure lists, feedback stubs.

    A contents page is a dense pile of the document's own section titles, so it
    matches almost any query and outranks the page that actually answers it --
    "DRC Rule K23 . . . 151" beats the text of rule K23. Flagged rather than
    dropped: the chunks stay searchable, just demoted at query time.
    """
    h = heading.lower().strip()
    if any(h.startswith(f) for f in FRONT_MATTER_HEADINGS):
        return True
    lines = [l for l in body.splitlines() if l.strip()]
    if len(lines) < 4:
        return False
    return sum(1 for l in lines if DOT_LEADER_RE.search(l)) >= len(lines) * 0.3


def clean_heading(raw: str) -> str:
    """Manifest headings keep their markdown emphasis (`**Foo**`); drop it."""
    s = raw.strip()
    for _ in range(3):
        stripped = s
        for mark in ("**", "__", "*", "_", "`"):
            if stripped.startswith(mark) and stripped.endswith(mark) and len(stripped) > 2 * len(mark):
                stripped = stripped[len(mark):-len(mark)].strip()
        if stripped == s:
            break
        s = stripped
    return s


def read_chunk(path: Path, retries: int = 3) -> str | None:
    """Read one section file, tolerating cloud-storage placeholder hydration.

    In a synced folder a file may be a cloud-only stub. The first read triggers
    a download that can be slow or fail outright ("the cloud operation was
    unsuccessful") while the sync client is busy. Retrying with a short backoff
    clears the transient ones. The backoff stays short on purpose: when the
    client cannot hydrate at all, every such file fails instantly, and a
    generous backoff turns a few thousand hopeless reads into an hour of
    waiting. Anything still unreadable is reported rather than silently indexed
    as absent -- a chunk missing from the index is a question the server
    answers with "no matches" instead of the page that exists on disk.
    """
    delay = 0.3
    for attempt in range(retries):
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            if attempt == retries - 1:
                return None
            time.sleep(delay)
            delay *= 2
    return None


def find_collections(root: Path) -> list[tuple[str, str, Path]]:
    """Locate every `docs/` tree under the corpus root.

    Returns (key, display_dir, docs_path). A corpus with one `docs/` at its
    root is one collection named after the root folder; a corpus that groups
    documents into subfolders gets one collection per subfolder. Nothing is
    hardcoded, so a new subfolder is picked up with no change here.
    """
    found: list[tuple[str, str, Path]] = []
    if (root / "docs").is_dir() and any((root / "docs").glob("*/manifest.json")):
        found.append((slugify(root.name), ".", root / "docs"))
        return found
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        docs = sub / "docs"
        if docs.is_dir() and any(docs.glob("*/manifest.json")):
            found.append((slugify(sub.name), sub.name, docs))
    return found


def load_documents(root: Path):
    for key, display, docs in find_collections(root):
        for manifest_path in sorted(docs.glob("*/manifest.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"  !! skipping {manifest_path}: {exc}", file=sys.stderr)
                continue
            yield key, display, manifest_path, manifest


def build(root: Path, out_path: Path, stats_only: bool = False) -> int:
    documents = list(load_documents(root))
    if not documents:
        print(
            f"No documents found under {root}.\n"
            "Expected <root>/docs/<slug>/manifest.json or "
            "<root>/<collection>/docs/<slug>/manifest.json -- convert something first.",
            file=sys.stderr,
        )
        return 1

    if stats_only:
        total = 0
        for key, _display, _mpath, manifest in documents:
            n = len(manifest.get("sections", []))
            total += n
            print(f"{key:20s} {manifest['slug']:42s} {n:5d} chunks")
        print(f"\n{len(documents)} documents, {total} chunks")
        return 0

    tmp_path = out_path.with_suffix(".building")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if tmp_path.exists():
        tmp_path.unlink()

    started = time.time()
    db = sqlite3.connect(tmp_path)
    db.executescript(SCHEMA)

    total_chunks = 0
    total_chars = 0
    unreadable: list[str] = []

    for key, display, manifest_path, manifest in documents:
        slug = manifest["slug"]
        title = manifest.get("title") or slug
        doc_dir = manifest_path.parent
        sections = manifest.get("sections", [])

        rows = []
        entity_spans: dict[str, list] = {}
        chars_here = 0

        wanted = [(i, s) for i, s in enumerate(sections) if s.get("file")]
        with ThreadPoolExecutor(READ_THREADS) as pool:
            bodies = list(pool.map(lambda pair: read_chunk(doc_dir / pair[1]["file"]), wanted))

        for (ordinal, sec), body in zip(wanted, bodies):
            path = doc_dir / sec["file"]
            if body is None:
                unreadable.append(str(path.relative_to(root)))
                continue

            heading = clean_heading(sec.get("heading") or "")
            entity = sec.get("command") or sec.get("entity") or ""
            page_start = sec.get("page_start")
            page_end = sec.get("page_end")

            rows.append((
                heading,
                entity,
                sec.get("breadcrumb") or "",
                body,
                slug,
                key,
                title,
                path.relative_to(root).as_posix(),
                ordinal,
                sec.get("level"),
                page_start,
                page_end,
                sec.get("chars") or len(body),
                1 if is_noise(heading, body) else 0,
            ))
            chars_here += len(body)

            if entity:
                span = entity_spans.setdefault(entity, [0, None, None])
                span[0] += 1
                if page_start is not None:
                    span[1] = page_start if span[1] is None else min(span[1], page_start)
                if page_end is not None:
                    span[2] = page_end if span[2] is None else max(span[2], page_end)

        db.executemany(
            "INSERT INTO chunks (heading, entity, breadcrumb, body, slug, collection,"
            " title, file, ord, level, page_start, page_end, chars, noise)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        db.execute(
            "INSERT INTO documents (slug, collection, collection_dir, title, source_pdf,"
            " page_count, section_count, indexed_count, char_count, has_pages,"
            " has_entities, toc_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                slug, key, display, title, manifest.get("source_pdf"),
                manifest.get("page_count"), len(wanted), len(rows), chars_here,
                1 if any(r[10] is not None for r in rows) else 0,
                1 if entity_spans else 0,
                json.dumps(manifest.get("toc", []), ensure_ascii=False),
            ),
        )
        if entity_spans:
            db.executemany(
                "INSERT INTO entities (name, name_lower, slug, collection, chunk_count,"
                " page_start, page_end) VALUES (?,?,?,?,?,?,?)",
                [(n, n.lower(), slug, key, s[0], s[1], s[2]) for n, s in sorted(entity_spans.items())],
            )

        total_chunks += len(rows)
        total_chars += chars_here
        gap = len(wanted) - len(rows)
        note = f"  !! {gap} unreadable" if gap else ""
        print(f"  {key:18s} {slug:40s} {len(rows):5d} chunks  {chars_here:>10,} chars{note}")

    for k, v in (
        ("built_at", time.strftime("%Y-%m-%dT%H:%M:%S")),
        ("root", str(root)),
        ("corpus_name", root.name),
        ("chunks", str(total_chunks)),
        ("unreadable", str(len(unreadable))),
        ("schema_version", "1"),
    ):
        db.execute("INSERT INTO meta (key, value) VALUES (?,?)", (k, v))

    db.execute("INSERT INTO chunks(chunks) VALUES ('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()
    os.replace(tmp_path, out_path)

    print(
        f"\n{len(documents)} documents, {total_chunks:,} chunks, {total_chars:,} chars"
        f"\n-> {out_path}  ({out_path.stat().st_size / 1048576:.1f} MB, {time.time() - started:.1f}s)"
    )

    if unreadable:
        print(f"\n!! {len(unreadable)} section file(s) could not be read and are NOT indexed:")
        for p in unreadable[:20]:
            print(f"     {p}")
        if len(unreadable) > 20:
            print(f"     ... and {len(unreadable) - 20} more")
        print(
            "   On cloud-synced storage this usually means the files were still\n"
            "   placeholders. Make the corpus available offline, let sync finish,\n"
            "   then rerun. The index is usable meanwhile, and the server reports\n"
            "   its own incompleteness rather than answering as if nothing is missing."
        )
        return 2
    return 0


def emit_vscode_config(root: Path, db_path: Path) -> Path:
    """Write .vscode/mcp.json so VS Code picks the corpus up on folder open.

    Points at this checkout's mcp_server.py with an explicit --db, because the
    scripts are shared across corpora while each index belongs to one.
    """
    server = (Path(__file__).resolve().parent / "mcp_server.py")
    config_path = root / ".vscode" / "mcp.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    existing = {}
    if config_path.is_file():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            print(f"  !! {config_path} is not valid JSON; leaving it alone", file=sys.stderr)
            return config_path

    servers = existing.setdefault("servers", {})
    servers[slugify(root.name) + "-docs"] = {
        "type": "stdio",
        "command": "python",
        "args": [str(server), "--db", str(db_path)],
    }
    config_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return config_path


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--root", default=".", help="corpus root (default: current directory)")
    ap.add_argument("--out", help=f"index path (default: <root>/{DEFAULT_DB_NAME})")
    ap.add_argument("--stats-only", action="store_true", help="report what would be indexed, write nothing")
    ap.add_argument(
        "--emit-vscode-config",
        action="store_true",
        help="also write <root>/.vscode/mcp.json so VS Code finds the server",
    )
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"--root '{root}' is not a folder.", file=sys.stderr)
        return 1
    out_path = Path(args.out).resolve() if args.out else root / DEFAULT_DB_NAME

    print(f"Indexing corpus at {root}")
    code = build(root, out_path, stats_only=args.stats_only)

    if args.emit_vscode_config and not args.stats_only and out_path.exists():
        written = emit_vscode_config(root, out_path)
        print(f"\nVS Code config -> {written}\nReload the window; the server appears in Agent mode's tool picker.")
    return code


if __name__ == "__main__":
    sys.exit(main())
