#!/usr/bin/env python3
"""
Write every chunk of a corpus as one JSON object per line, for a vector store,
a notebook or any pipeline that is not the MCP server:

  python scripts/export_chunks.py --root my-corpus --out chunks.jsonl
  python scripts/export_chunks.py --root my-corpus --out chunks.jsonl --current-only

A line carries the chunk's text exactly as it is on disk, the document it came
from (collection, slug, doc_id, version, whether it is the edition search
answers from) and what the converter knew about it: heading, breadcrumb, pages,
the entry it documents and, from Docling, how confident its ancestors are.
`entity`, `confidence`, `version`, `page_start` and `page_end` are null where
the manifest has none. `id` is `<collection>/<slug>/<file>` and is unique.

It stops on what build_search_db.py stops on -- an unreadable manifest, editions
that cannot be ordered, collection folders that clash -- and also on a section
file it cannot read: an export with holes in it is worse than none, since
nothing in it says what is missing. Nothing is written in those cases.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_search_db as bsd  # noqa: E402


def chunk_lines(root: Path, current_only: bool):
    """(JSON lines, problems) for every section of every document."""
    by_key: dict[str, list[str]] = {}
    for key, display, _docs in bsd.find_collections(root):
        by_key.setdefault(key, []).append(display)
    problems = [f"{', '.join(v)} are all collection '{k}'" for k, v in sorted(by_key.items()) if len(v) > 1]
    documents, failed = bsd.load_documents(root)
    problems += [f"{line}" for line in failed]
    if problems:
        return [], problems
    if not documents:
        return [], [f"No documents found under {root}"]
    edition_of, problems = bsd.resolve_editions(documents)
    if problems:
        return [], problems

    lines = []
    for key, _display, manifest_path, manifest in documents:
        slug = manifest["slug"]
        edition = edition_of[(key, slug)]
        if current_only and not edition["is_current"]:
            continue
        doc_dir = manifest_path.parent
        for ordinal, sec in enumerate(manifest.get("sections", [])):
            if not sec.get("file"):
                continue
            text = bsd.read_chunk(doc_dir / sec["file"])
            if text is None:
                problems.append((doc_dir / sec["file"]).relative_to(root).as_posix())
                continue
            lines.append(json.dumps({
                "id": f"{key}/{slug}/{sec['file']}",
                "collection": key,
                "slug": slug,
                "doc_id": edition["doc_id"],
                "version": edition["version"] or None,
                "version_and_later": bool(edition["version"]) and edition["version_and_later"],
                "is_current": bool(edition["is_current"]),
                "title": manifest.get("title") or slug,
                "file": sec["file"],
                "ord": ordinal,
                "heading": bsd.clean_heading(sec.get("heading") or ""),
                "breadcrumb": sec.get("breadcrumb") or None,
                "page_start": sec.get("page_start"),
                "page_end": sec.get("page_end"),
                "entity": sec.get("command") or sec.get("entity") or None,
                "confidence": sec.get("confidence"),
                "text": text,
            }))
    return lines, [f"section file not readable: {p}" for p in problems]


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", type=Path, help="corpus root (default: current directory)")
    ap.add_argument("--out", required=True, type=Path, help="the .jsonl file to write")
    ap.add_argument("--current-only", action="store_true",
                    help="only the edition of each manual that search answers from")
    args = ap.parse_args()

    root = args.root.resolve()
    if not root.is_dir():
        sys.exit(f"--root '{root}' is not a folder.")
    lines, problems = chunk_lines(root, args.current_only)
    if problems:
        print("Nothing was written:", file=sys.stderr)
        for p in problems[:30]:
            print(f"  !! {p}", file=sys.stderr)
        if len(problems) > 30:
            print(f"  ... and {len(problems) - 30} more", file=sys.stderr)
        return 1
    tmp = args.out.with_suffix(args.out.suffix + ".writing")
    tmp.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    os.replace(tmp, args.out)
    print(f"{len(lines):,} chunks -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
