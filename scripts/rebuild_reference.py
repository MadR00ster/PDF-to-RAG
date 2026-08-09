#!/usr/bin/env python3
"""
Page-accurate reconversion for command-dictionary manuals (syn2, tshell-ref).

Why this exists, separately from convert_manual.py: in a command reference
the chunker splits one command's entry into several chunks (Description,
Arguments, Usage, Examples), and those chunks do not say which command they
belong to. Retrieving a bare "Arguments" chunk is not merely unhelpful -- a
model will confidently attach those flags to whatever command the user
asked about. Measured on the current corpus, ~2,000 chunks are orphaned this
way.

Recovering the owner from chunk text alone does not work: only 82.5% of
commands are ever anchored, and every miss silently inherits the *previous*
command (verified: a `tessent -shell` chunk labelled `tessent -diagserver`).

So this script goes through pages instead, which is exact:

  1. Extract with page_chunks=True, so every page's text is known separately.
  2. Concatenate into full.md, recording each page's character span.
  3. Chunk with the same rules as convert_manual.py, then map each chunk's
     character range back to a page range.
  4. The PDF's own TOC lists every command with its page (1,319 in
     tshell-ref, 1,334 in syn2 -- complete and authoritative). A chunk
     belongs to the last command whose page <= the chunk's first page.

Output matches convert_manual.py's layout, plus per-section `page_start`,
`page_end`, `command`, and `breadcrumb`.

Usage:
  python scripts/rebuild_reference.py "Synopsys Manual/syn2.pdf" \
      --title "Synthesis Tool Commands" --slug syn2 --replace
"""
from __future__ import annotations

import argparse
import bisect
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

try:
    import pymupdf
    import pymupdf4llm
except ImportError:
    sys.exit("Missing dependency. Run: pip install -r scripts/requirements.txt")

_HERE = Path(__file__).parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cm = _load("convert_manual")
ec = _load("enrich_chunks")

IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*(\s+-[a-z0-9_]+)*$", re.I)


def pick_command_level(toc: list) -> int | None:
    """The TOC level whose titles look most like command names.

    Necessary because the level differs per manual -- tshell-ref keeps
    commands at L3, syn2 at L1 (with SYNTAX/ARGUMENTS/DESCRIPTION at L2).
    Picking "the most populous level" gets syn2 wrong.
    """
    best, best_n = None, 0
    by_level: dict[int, list[str]] = {}
    for lvl, title, _page in toc:
        by_level.setdefault(lvl, []).append((title or "").strip())
    for lvl, titles in by_level.items():
        n = sum(1 for t in titles if IDENTIFIER_RE.match(t) and "_" in t or " -" in t)
        if n > best_n:
            best, best_n = lvl, n
    return best if best_n >= 20 else None


def build_pages(pdf_path: Path, title: str) -> tuple[str, list[int], list[int], int]:
    """Return (full_md, page_start_offsets, page_numbers, furniture_lines_removed).

    Furniture is stripped per page *before* concatenation, for two reasons:
    the running footer would otherwise become a chunk boundary in
    --dictionary mode (a standalone "**Feedback**" line is exactly the shape
    the splitter looks for, which produced one junk chunk per page), and
    stripping after concatenation would invalidate the page offsets this
    whole script depends on.
    """
    raw = pymupdf4llm.to_markdown(str(pdf_path), page_chunks=True)
    page_texts = [(p.get("text") or "") for p in raw]
    numbers = [
        (p.get("metadata") or {}).get("page") or (i + 1) for i, p in enumerate(raw)
    ]

    furniture = ec.detect_furniture(page_texts, max(10, len(page_texts) // 20), title)

    parts, starts, cursor, removed = [], [], 0, 0
    for text in page_texts:
        cleaned, n = ec.strip_furniture(text, furniture)
        removed += n
        starts.append(cursor)
        parts.append(cleaned)
        cursor += len(cleaned)
    return "".join(parts), starts, numbers, removed


def locate(full_md: str, body: str, cursor: int) -> int:
    """Character offset of `body` in full_md at/after cursor.

    Chunks are slices of full_md, but paragraph-packing can rejoin with
    slightly different whitespace, so match on a distinctive prefix rather
    than the whole body.
    """
    probe = body.strip()[:200]
    if not probe:
        return cursor
    pos = full_md.find(probe, cursor)
    if pos < 0:
        pos = full_md.find(probe)
    return pos if pos >= 0 else cursor


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("pdf", type=Path)
    ap.add_argument(
        "--title",
        help="defaults to the title already in docs/<slug>/manifest.json, which "
        "keeps it byte-stable across rebuilds and avoids shell-encoding "
        "trouble with characters like the trademark sign",
    )
    ap.add_argument("--slug", required=True)
    ap.add_argument("--out-root", type=Path)
    ap.add_argument(
        "--replace",
        action="store_true",
        help="overwrite an existing docs/<slug>/ (it is moved aside to <slug>.old first)",
    )
    args = ap.parse_args()

    pdf_path = args.pdf.resolve()
    out_root = (args.out_root or pdf_path.parent / "docs").resolve()
    out_dir = out_root / args.slug
    if out_dir.exists() and not args.replace:
        sys.exit(f"{out_dir} exists -- pass --replace to rebuild it")

    if not args.title:
        prior = out_dir / "manifest.json"
        if not prior.exists():
            sys.exit("--title is required when docs/<slug>/manifest.json does not exist")
        args.title = json.loads(prior.read_text(encoding="utf-8-sig"))["title"]
        print(f"[{args.slug}] title from existing manifest: {args.title!r}", flush=True)

    print(f"[{args.slug}] extracting {pdf_path.name} with page tracking ...", flush=True)
    full_md, page_starts, page_numbers, furn_removed = build_pages(pdf_path, args.title)
    doc = pymupdf.open(str(pdf_path))
    toc = doc.get_toc()
    print(
        f"[{args.slug}] {len(page_starts)} pages, {len(full_md):,} chars, "
        f"{furn_removed} furniture lines stripped pre-chunking",
        flush=True,
    )

    cmd_level = pick_command_level(toc)
    commands = []  # (page, name) sorted by page
    if cmd_level is not None:
        for lvl, title, page in toc:
            if lvl == cmd_level and (title or "").strip():
                commands.append((page, title.strip()))
        commands.sort()
    cmd_pages = [p for p, _ in commands]
    print(
        f"[{args.slug}] command level L{cmd_level}: {len(commands)} commands",
        flush=True,
    )

    # Split full.md into one region per command *before* chunking, so a
    # chunk can never span two commands. Without this the splitter simply
    # accumulates text up to MAX_CHUNK and happily merges several command
    # entries into one chunk -- measured at 72% of syn2 chunks straddling a
    # boundary, which makes the `command` field right at the chunk's start
    # and wrong by its end.
    page_index = {}
    for i, num in enumerate(page_numbers):
        page_index.setdefault(num, i)

    def command_offset(page: int, name: str, floor: int) -> int:
        """Offset where a command's entry starts: its page, refined to the
        line naming it when that can be found (two commands can share a
        page, and page granularity alone would merge them)."""
        idx = page_index.get(page)
        base = page_starts[idx] if idx is not None else floor
        base = max(base, floor)
        window_end = min(len(full_md), base + 40000)
        pat = re.compile(
            r"^[#*_ \t]*" + re.escape(name) + r"[*_ \t]*$", re.M
        )
        hit = pat.search(full_md, base, window_end)
        return hit.start() if hit else base

    regions = []  # (start, end, command|None)
    floor = 0
    starts = []
    for page, name in commands:
        off = command_offset(page, name, floor)
        starts.append((off, name))
        floor = off
    if starts and starts[0][0] > 0:
        regions.append((0, starts[0][0], None))
    elif not starts:
        regions.append((0, len(full_md), None))
    for i, (off, name) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(full_md)
        if end > off:
            regions.append((off, end, name))

    print(
        f"[{args.slug}] chunking {len(regions)} command regions ...", flush=True
    )
    chunks = []  # (heading, level, body, abs_offset, command)
    for r_start, r_end, name in regions:
        region = full_md[r_start:r_end]
        cursor = 0
        for heading, level, body in cm.chunk_markdown(region, dictionary=True):
            off = locate(region, body, cursor)
            cursor = max(cursor, off)
            chunks.append((heading, level, body, r_start + off, name))

    sections_dir = out_dir.with_name(out_dir.name + ".new") / "sections"
    sections_dir.mkdir(parents=True, exist_ok=True)
    staging = sections_dir.parent

    seen, entries = {}, []
    for i, (heading, level, body, off, command) in enumerate(chunks, start=1):
        pi_start = max(0, bisect.bisect_right(page_starts, off) - 1)
        pi_end = max(0, bisect.bisect_right(page_starts, off + len(body) - 1) - 1)
        p_start, p_end = page_numbers[pi_start], page_numbers[pi_end]

        crumb = args.title + (f"{ec.BREADCRUMB_SEP}{command}" if command else "")
        text = ec.apply_breadcrumb(body.strip() + "\n", crumb, args.title)

        fname = f"{i:04d}-{cm.dedupe_slug(cm.slugify(heading), seen)}.md"
        (sections_dir / fname).write_text(text, encoding="utf-8")
        entries.append(
            {
                "file": f"sections/{fname}",
                "heading": heading,
                "level": level,
                "chars": len(text),
                "page_start": p_start,
                "page_end": p_end,
                "command": command,
                "breadcrumb": crumb,
            }
        )

    (staging / "full.md").write_text(full_md, encoding="utf-8")
    (staging / "manifest.json").write_text(
        json.dumps(
            {
                "source_pdf": pdf_path.name,
                "title": args.title,
                "slug": args.slug,
                "page_count": doc.page_count,
                "toc": [
                    {"level": l, "title": (t or "").strip(), "page": p} for l, t, p in toc
                ],
                "sections": entries,
                "full_md_chars": len(full_md),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    if out_dir.exists():
        # Keep the backup OUTSIDE docs/ -- build_index.py globs docs/*/ for
        # manifests, so a docs/<slug>.old sitting there gets listed as a real
        # manual in index.json.
        backup_root = out_root.parent / ".rebuild-backup"
        backup_root.mkdir(exist_ok=True)
        old = backup_root / args.slug
        if old.exists():
            shutil.rmtree(old)
        out_dir.rename(old)
        print(
            f"[{args.slug}] previous version kept at "
            f"{old.relative_to(out_root.parent)}",
            flush=True,
        )
    staging.rename(out_dir)

    attributed = sum(1 for e in entries if e["command"])
    print(
        f"[{args.slug}] DONE: {len(entries)} chunks, "
        f"{attributed} ({100*attributed/max(len(entries),1):.1f}%) attributed to a command",
        flush=True,
    )


if __name__ == "__main__":
    main()
