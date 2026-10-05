#!/usr/bin/env python3
"""
Build the search index that serves a converted corpus over MCP.

Walks every `<collection>/docs/<slug>/manifest.json` under the corpus root,
reads each section chunk off disk, and writes one SQLite file with an FTS5
full-text index. `mcp_server.py` queries that file; nothing else reads it.

Figures come along once extract_figures.py has run: each document's
figures.json becomes rows get_figure serves, and each figure's caption and
drawn labels are indexed with the section it illustrates.

Editions: documents sharing a `doc_id` are releases of one manual. All of them
are indexed, and exactly one per manual is marked current -- the newest, or
the one that applies to the release <collection>/current_versions.json pins. The server answers from the
current ones unless asked for another by version. A manual whose editions
cannot be ordered, or a pin on a version that is not here, stops the build:
answering from an edition nobody chose is worse than not rebuilding.

Standard library only -- FTS5 ships inside Python's bundled SQLite, so a
corpus becomes queryable from an editor with no packages, no API key and no
network.

The index is a snapshot, not a live view: rerun this after converting,
reconverting or enriching anything. The build is atomic (temp file, then
rename), so a failed run leaves the previous index in place.

--emit-vscode-config also copies mcp_server.py beside the index and writes
.vscode/mcp.json with paths relative to the corpus folder. The server is one
standard-library file, so index and server then travel together: the folder
can be synced to another machine and served there with nothing but Python,
and no path in its config names the machine that built it. Later builds
refresh the copy, so the server always matches the index it reads.

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
import shutil
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import editions  # noqa: E402

DEFAULT_DB_NAME = "mcp-index.sqlite3"
SERVER_NAME = "mcp_server.py"

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
    doc_id        TEXT NOT NULL,   -- the manual this is an edition of
    version       TEXT,            -- the tool release it applies to, as its cover prints it
    version_later INTEGER NOT NULL DEFAULT 0,   -- the cover says "and later"
    version_sort  TEXT,            -- the same, as text that orders a manual's editions
    is_latest     INTEGER NOT NULL DEFAULT 1,
    is_current    INTEGER NOT NULL DEFAULT 1,   -- the edition search answers from
    source_pdf    TEXT,
    source_path   TEXT,            -- the PDF relative to the corpus root, if it was found
    page_count    INTEGER,
    section_count INTEGER,   -- sections this document has on disk
    indexed_count INTEGER,   -- how many of them made it into the index
    char_count    INTEGER,
    has_pages     INTEGER NOT NULL DEFAULT 0,
    has_entities  INTEGER NOT NULL DEFAULT 0,
    figure_count  INTEGER NOT NULL DEFAULT 0,
    toc_json      TEXT
);

-- One row per section chunk. The five leading columns are searchable; the
-- rest are UNINDEXED so they cost storage but no index space, and can still
-- be filtered on in WHERE once MATCH has narrowed the candidate set.
-- `figures` holds the captions, drawn labels and OCR text of the figures tied
-- to the chunk, so a question about what a diagram shows can land on the
-- section it illustrates.
--
-- Porter stemming, because people ask about "options" and "toggling" where
-- the manual says "option" and "toggle". On a 57-question test set it lifted
-- reworded questions from 55% to 73% hit@5 and cost no identifier lookup:
-- `set_scan_configuration` stems the same way in the query and the text.
CREATE VIRTUAL TABLE chunks USING fts5(
    heading,
    entity,
    breadcrumb,
    body,
    figures,
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
    tokenize     = "porter unicode61 remove_diacritics 2"
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

-- Figures from extract_figures.py. section_ord ties one to the chunk it
-- illustrates, and is NULL when nothing tied it without guessing.
CREATE TABLE figures (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL,
    collection  TEXT NOT NULL,
    page        INTEGER,
    caption     TEXT,
    labels      TEXT,
    ocr         TEXT,            -- words ocr_figures.py read off a raster figure
    description TEXT,
    file        TEXT NOT NULL,   -- the crop, relative to the corpus root
    width       INTEGER,
    height      INTEGER,
    section_ord INTEGER,
    link        TEXT             -- how section_ord was decided: caption | page | context
);
CREATE INDEX idx_figures_section ON figures(slug, section_ord);

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


def load_documents(root: Path) -> tuple[list, list[str]]:
    """Every document's manifest, and the manifests that could not be used.

    The second list stops the build. A manifest that is skipped is a document
    missing from the index with nothing to say so, and since editions it is
    worse than that: if it was a manual's newest edition, an older one is
    marked current and every default answer quietly changes release. Which
    edition is current can only be decided from all of them.
    """
    documents, failed = [], []
    for key, display, docs in find_collections(root):
        for manifest_path in sorted(docs.glob("*/manifest.json")):
            manifest, why, delay = None, "", 0.3
            for attempt in range(3):
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                    break
                except OSError as exc:        # a cloud placeholder still hydrating: retry, as read_chunk does
                    why = str(exc)
                    if attempt < 2:
                        time.sleep(delay)
                        delay *= 2
                except ValueError as exc:     # not JSON, or not UTF-8: reading again will not help
                    why = str(exc)
                    break
            if manifest is not None and not (isinstance(manifest, dict) and manifest.get("slug")):
                manifest, why = None, "it is not a manifest with a slug"
            if manifest is None:
                failed.append(f"{manifest_path.relative_to(root).as_posix()}: {why}")
                continue
            documents.append((key, display, manifest_path, manifest))
    return documents, failed


def load_figures(doc_dir: Path) -> list[dict]:
    """extract_figures.py's output for one document; empty if it has not run."""
    path = doc_dir / "figures.json"
    if not path.is_file():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")).get("figures", [])
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  !! skipping {path}: {exc}", file=sys.stderr)
        return []


def resolve_editions(documents: list) -> tuple[dict[str, dict], list[str]]:
    """{slug: doc_id, version, is_latest, is_current} for every document, and
    whatever stops that being decided. Editions are grouped within a
    collection: two vendors may both have a `user-guide`."""
    by_collection: dict[Path, list[dict]] = {}
    for _key, _display, manifest_path, manifest in documents:
        slug = manifest["slug"]
        by_collection.setdefault(manifest_path.parent.parent.parent, []).append(
            {"slug": slug, "doc_id": manifest.get("doc_id") or slug, "version": manifest.get("version"),
             # Exactly true, as check_corpus.py reads it: "false" in quotes is
             # a string, and a string is not a claim.
             "version_and_later": manifest.get("version_and_later") is True})
    resolved, problems = {}, []
    for collection_dir, docs in by_collection.items():
        try:
            pins = editions.load_pins(collection_dir)
        except ValueError as exc:
            pins = {}
            problems.append(str(exc))
        problems += [f"{collection_dir.name}: {p}" for p in editions.resolve_editions(docs, pins)]
        resolved.update((d["slug"], d) for d in docs)
    return resolved, problems


def build(root: Path, out_path: Path, stats_only: bool = False, figure_text: bool = True) -> int:
    documents, failed = load_documents(root)
    if failed:
        print("Manifests that could not be read:", file=sys.stderr)
        for line in failed:
            print(f"  !! {line}", file=sys.stderr)
        print("Nothing was written. An index built without these would leave their documents out, and "
              "could answer from an older edition of a manual whose newest is among them. Fix or remove "
              "them, then rerun.", file=sys.stderr)
        return 1
    if not documents:
        print(
            f"No documents found under {root}.\n"
            "Expected <root>/docs/<slug>/manifest.json or "
            "<root>/<collection>/docs/<slug>/manifest.json -- convert something first.",
            file=sys.stderr,
        )
        return 1

    edition_of, problems = resolve_editions(documents)
    if problems:
        print("Editions that cannot be ordered, or pins that match nothing:", file=sys.stderr)
        for p in problems:
            print(f"  !! {p}", file=sys.stderr)
        print("Nothing was written. Set the missing versions (editions.py stamp --set) or fix "
              f"{editions.PINS_FILE}, then rerun.", file=sys.stderr)
        return 1

    if stats_only:
        total = 0
        for key, _display, _mpath, manifest in documents:
            n = len(manifest.get("sections", []))
            total += n
            e = edition_of[manifest["slug"]]
            note = "" if e["is_current"] else "  (not current)"
            shown = editions.display_version(e["version"], e["version_and_later"])
            print(f"{key:20s} {manifest['slug']:42s} {shown:16s} {n:5d} chunks{note}")
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
    total_figures = 0
    unreadable: list[str] = []

    for key, display, manifest_path, manifest in documents:
        slug = manifest["slug"]
        title = manifest.get("title") or slug
        doc_dir = manifest_path.parent
        sections = manifest.get("sections", [])

        rows = []
        entity_spans: dict[str, list] = {}
        chars_here = 0
        has_pages = False

        wanted = [(i, s) for i, s in enumerate(sections) if s.get("file")]
        with ThreadPoolExecutor(READ_THREADS) as pool:
            bodies = list(pool.map(lambda pair: read_chunk(doc_dir / pair[1]["file"]), wanted))

        figure_rows = [f for f in load_figures(doc_dir) if f.get("file")]
        ord_of = {s["file"]: i for i, s in wanted}
        figure_words: dict[str, list[str]] = {}
        for f in figure_rows if figure_text else ():
            if f.get("section"):
                figure_words.setdefault(f["section"], []).append(
                    " ".join(x for x in (f.get("caption"), f.get("labels"), f.get("ocr"),
                                         f.get("description")) if x))

        for (ordinal, sec), body in zip(wanted, bodies):
            path = doc_dir / sec["file"]
            if body is None:
                unreadable.append(str(path.relative_to(root)))
                continue

            heading = clean_heading(sec.get("heading") or "")
            entity = sec.get("command") or sec.get("entity") or ""
            page_start = sec.get("page_start")
            page_end = sec.get("page_end")
            has_pages = has_pages or page_start is not None

            rows.append((
                heading,
                entity,
                sec.get("breadcrumb") or "",
                body,
                " ".join(figure_words.get(sec["file"], [])),
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
            "INSERT INTO chunks (heading, entity, breadcrumb, body, figures, slug, collection,"
            " title, file, ord, level, page_start, page_end, chars, noise)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        edition = edition_of[slug]
        pdf = editions.find_source_pdf(doc_dir.parent.parent, manifest.get("source_pdf"))
        db.execute(
            "INSERT INTO documents (slug, collection, collection_dir, title, doc_id, version,"
            " version_later, version_sort, is_latest, is_current, source_pdf, source_path,"
            " page_count, section_count, indexed_count, char_count, has_pages,"
            " has_entities, figure_count, toc_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                slug, key, display, title, edition["doc_id"], edition["version"],
                1 if edition["version"] and edition["version_and_later"] else 0,
                editions.version_sort(edition["version"]),
                1 if edition["is_latest"] else 0, 1 if edition["is_current"] else 0,
                manifest.get("source_pdf"),
                pdf.relative_to(root).as_posix() if pdf else None,
                manifest.get("page_count"), len(wanted), len(rows), chars_here,
                1 if has_pages else 0,
                1 if entity_spans else 0,
                len(figure_rows),
                json.dumps(manifest.get("toc", []), ensure_ascii=False),
            ),
        )
        if entity_spans:
            db.executemany(
                "INSERT INTO entities (name, name_lower, slug, collection, chunk_count,"
                " page_start, page_end) VALUES (?,?,?,?,?,?,?)",
                [(n, n.lower(), slug, key, s[0], s[1], s[2]) for n, s in sorted(entity_spans.items())],
            )
        if figure_rows:
            db.executemany(
                "INSERT INTO figures (slug, collection, page, caption, labels, ocr, description, file,"
                " width, height, section_ord, link) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(slug, key, f.get("page"), f.get("caption"), f.get("labels"), f.get("ocr"), f.get("description"),
                  (doc_dir / f["file"]).relative_to(root).as_posix(), f.get("width"), f.get("height"),
                  ord_of.get(f.get("section")), f.get("link")) for f in figure_rows],
            )

        total_chunks += len(rows)
        total_chars += chars_here
        total_figures += len(figure_rows)
        gap = len(wanted) - len(rows)
        note = f"  !! {gap} unreadable" if gap else ""
        figs = f"  {len(figure_rows):>5} figures" if figure_rows else ""
        if not edition["is_current"]:
            note += "  (not current)"
        print(f"  {key:18s} {slug:40s} {len(rows):5d} chunks  {chars_here:>10,} chars{figs}{note}")

    for k, v in (
        ("built_at", time.strftime("%Y-%m-%dT%H:%M:%S")),
        ("root", str(root)),
        # Where the corpus is from the index, when the index is inside it:
        # "." by default, ".." for --out <root>/indexes/x.sqlite3. A corpus
        # that is moved or synced takes its index along, and the server finds
        # the figures and PDFs from this rather than from the path above,
        # which names the machine that built it.
        ("root_from_index", os.path.relpath(root, out_path.parent)
            if out_path.resolve().is_relative_to(root) else ""),
        ("corpus_name", root.name),
        ("chunks", str(total_chunks)),
        ("figures", str(total_figures)),
        ("figure_text", "1" if figure_text else "0"),
        ("unreadable", str(len(unreadable))),
        ("schema_version", "3"),
    ):
        db.execute("INSERT INTO meta (key, value) VALUES (?,?)", (k, v))

    db.execute("INSERT INTO chunks(chunks) VALUES ('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()
    os.replace(tmp_path, out_path)

    print(
        f"\n{len(documents)} documents, {total_chunks:,} chunks, {total_chars:,} chars,"
        f" {total_figures:,} figures"
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


COPY_NOTE = (
    "# A copy, placed here by PDF-to-RAG's build_search_db.py so this folder can be\n"
    "# served from any machine it is synced to. Do not edit it: the next index\n"
    "# build replaces it. The original is scripts/mcp_server.py in that repo.\n"
)


def install_server(root: Path) -> Path:
    """Copy mcp_server.py to the corpus root, beside the index it serves.

    A config that names this checkout by path works on one machine. The
    server needs nothing but the standard library and the index, so a copy
    beside the index makes the corpus folder self-contained instead.
    """
    source = Path(__file__).resolve().parent / SERVER_NAME
    first, rest = source.read_text(encoding="utf-8").split("\n", 1)
    target = root / SERVER_NAME
    target.write_text(f"{first}\n{COPY_NOTE}{rest}", encoding="utf-8")
    shutil.copymode(source, target)
    return target


def emit_vscode_config(root: Path, db_path: Path) -> Path:
    """Write .vscode/mcp.json so VS Code picks the corpus up on folder open.

    Every path is relative to the workspace folder -- the server copy
    install_server() places at the corpus root, and the index when it is
    inside the corpus -- so the file is right on any machine the corpus is
    opened on. `python` rather than this interpreter's path, for the same
    reason.
    """
    config_path = root / ".vscode" / "mcp.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    existing = {}
    if config_path.is_file():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            print(f"  !! {config_path} is not valid JSON; leaving it alone", file=sys.stderr)
            return config_path

    try:
        db_arg = "${workspaceFolder}/" + db_path.relative_to(root).as_posix()
    except ValueError:
        db_arg = str(db_path)             # --out put the index outside the corpus

    def same_path(arg: str, path: Path) -> bool:
        text = str(arg).replace("${workspaceFolder}", str(root))
        return os.path.normcase(os.path.abspath(text)) == os.path.normcase(os.path.abspath(path))

    def serves_this_index(cfg) -> bool:
        """Whether an entry is this corpus's server: by the index it opens,
        or with no --db, by being the copy that sits beside this index.
        Running a file called mcp_server.py is not enough -- a workspace can
        register another corpus the same way, and rewriting that entry would
        point its tools at this index."""
        if not isinstance(cfg, dict):
            return False
        args = [str(a) for a in cfg.get("args") or []]
        if "--db" in args[:-1]:
            return same_path(args[args.index("--db") + 1], db_path)
        return db_path == root / DEFAULT_DB_NAME and any(same_path(a, root / SERVER_NAME) for a in args)

    servers = existing.setdefault("servers", {})
    # This corpus's entry is rewritten under whatever name it was given;
    # every other entry is left as it is.
    mine = [name for name, cfg in servers.items() if serves_this_index(cfg)]
    name = mine[0] if mine else slugify(root.name) + "-docs"
    while name in servers and not serves_this_index(servers[name]):
        name += "-2"
    servers[name] = {
        "type": "stdio",
        "command": "python",
        "args": ["${workspaceFolder}/" + SERVER_NAME, "--db", db_arg],
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
        help="also copy mcp_server.py to <root> and write <root>/.vscode/mcp.json, "
             "with paths relative to the corpus, so VS Code finds the server",
    )
    ap.add_argument(
        "--no-figure-text",
        action="store_true",
        help="serve figures but keep their captions and labels out of search "
        "(for measuring what that text adds)",
    )
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"--root '{root}' is not a folder.", file=sys.stderr)
        return 1
    out_path = Path(args.out).resolve() if args.out else root / DEFAULT_DB_NAME

    print(f"Indexing corpus at {root}")
    code = build(root, out_path, stats_only=args.stats_only, figure_text=not args.no_figure_text)

    built = not args.stats_only and code != 1 and out_path.exists()
    # A copy already at the root is refreshed by every build: an index and a
    # server from different versions of this repo need not agree on the schema.
    if built and (args.emit_vscode_config or (root / SERVER_NAME).is_file()):
        print(f"\nServer copy -> {install_server(root)}")
    if built and args.emit_vscode_config:
        written = emit_vscode_config(root, out_path)
        print(f"VS Code config -> {written}\nReload the window; the server appears in Agent mode's tool picker.")
    return code


if __name__ == "__main__":
    sys.exit(main())
