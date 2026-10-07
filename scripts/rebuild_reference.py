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
     belongs to the last command whose page <= the chunk's first page,
     unless a shallower TOC entry (a chapter, an appendix, the licence)
     starts in between -- then it belongs to none.

Output matches convert_manual.py's layout, plus per-section `page_start`,
`page_end`, `command`, and `breadcrumb`.

Usage:
  python scripts/rebuild_reference.py "Synopsys Manual/syn2.pdf" \
      --title "Synthesis Tool Commands" --slug syn2 --replace
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

try:
    import pymupdf
except ImportError:
    sys.exit("Missing dependency. Run: pip install -r scripts/requirements.txt")

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.resolve()))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cm = _load("convert_manual")
ec = _load("enrich_chunks")
editions = cm.editions
from _common import MIN_COMMANDS, IDENTIFIER_RE, MESSAGE_CODE_RE, pick_command_level  # noqa: E402,F401

def build_pages(pdf_path: Path, title: str) -> tuple[str, list[int], list[int], int]:
    """Return (full_md, page_start_offsets, page_numbers, furniture_lines_removed).

    Furniture is stripped per page *before* concatenation, for two reasons:
    the running footer would otherwise become a chunk boundary in
    --dictionary mode (a standalone "**Feedback**" line is exactly the shape
    the splitter looks for, which produced one junk chunk per page), and
    stripping after concatenation would invalidate the page offsets this
    whole script depends on.
    """
    page_texts, numbers = cm.page_markdown(pdf_path)

    furniture = ec.detect_furniture(page_texts, title)

    parts, starts, cursor, removed = [], [], 0, 0
    for text in page_texts:
        cleaned, n = ec.strip_furniture(text, furniture)
        removed += n
        starts.append(cursor)
        parts.append(cleaned)
        cursor += len(cleaned)
    return "".join(parts), starts, numbers, removed


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
    editions.add_arguments(ap)
    ap.add_argument(
        "--replace",
        action="store_true",
        help="overwrite an existing docs/<slug>/ (the previous one is kept in .rebuild-backup/<slug>/)",
    )
    ap.add_argument(
        "--command-level", type=int, metavar="N",
        help="the TOC level whose entries are the commands. By default it is the level "
        "where 20 or more titles look like commands (an underscore, ' -', or a message "
        "code); a reference whose entries are plain words has none, and is declined",
    )
    args = ap.parse_args()

    pdf_path = args.pdf.resolve()
    if not pdf_path.is_file():
        sys.exit(f"No such file: {pdf_path}")
    out_root = (args.out_root or editions.collection_of(pdf_path) / "docs").resolve()
    out_dir = out_root / args.slug
    if out_dir.exists() and not args.replace:
        sys.exit(f"{out_dir} exists -- pass --replace to rebuild it")

    # A rebuild keeps what the document already is: a title, doc_id or version
    # set by hand must survive --replace.
    prior = {}
    if (out_dir / "manifest.json").exists():
        prior = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8-sig"))
    if not args.title:
        if "title" not in prior:
            sys.exit("--title is required when docs/<slug>/manifest.json does not exist")
        args.title = prior["title"]
        print(f"[{args.slug}] title from existing manifest: {args.title!r}", flush=True)
    plan = editions.plan(pdf_path, args.slug, out_root, args.version or prior.get("version"),
                         args.doc_id or prior.get("doc_id"),
                         later=not args.version and prior.get("version_and_later") is True)

    print(f"[{args.slug}] extracting {pdf_path.name} with page tracking ...", flush=True)
    full_md, page_starts, page_numbers, furn_removed = build_pages(pdf_path, args.title)
    doc = pymupdf.open(str(pdf_path))
    toc = doc.get_toc()
    print(
        f"[{args.slug}] {len(page_starts)} pages, {len(full_md):,} chars, "
        f"{furn_removed} furniture lines stripped pre-chunking",
        flush=True,
    )

    levels = sorted({lvl for lvl, _t, _p in toc})
    if args.command_level is not None:
        if args.command_level not in levels:
            sys.exit(f"no TOC entry is at level {args.command_level}; levels here: "
                     f"{', '.join(map(str, levels)) or 'none (no bookmark outline)'}")
        cmd_level, chosen_by = args.command_level, "option"
    else:
        cmd_level, chosen_by = pick_command_level(toc), "toc"
    # Every TOC entry at the command level starts a command's entry. Every
    # shallower one -- a chapter, an appendix, the licence -- ends the entry
    # before it and starts a region that belongs to no command. Without those
    # ends the last command in a chapter owned everything up to the next
    # command: the next chapter's introduction, the appendices, the licence,
    # all served by lookup_entity as part of that command.
    boundaries = []  # (page, toc_index, title, command|None)
    if cmd_level is not None:
        for i, (lvl, title, page) in enumerate(toc):
            title = (title or "").strip()
            if not title:
                continue
            if lvl == cmd_level:
                boundaries.append((page, i, title, title))
            elif lvl < cmd_level and page >= 1:
                boundaries.append((page, i, title, None))
        # By page, then TOC order: where two entries share a page, TOC order
        # is document order and alphabetical order need not be.
        boundaries.sort()
    n_commands = sum(1 for b in boundaries if b[3])
    if cmd_level is None:
        print(
            f"[{args.slug}] !! no TOC level has {MIN_COMMANDS} titles shaped like commands "
            "(an underscore, \" -\", or a message code), so no chunk is attributed to one. "
            "If this is a reference, pass --command-level N with the TOC level its entries are at.",
            flush=True,
        )
    else:
        print(
            f"[{args.slug}] command level L{cmd_level}: {n_commands} commands",
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

    def entry_offset(page: int, title: str, floor: int) -> int:
        """Offset where a TOC entry starts: its page, refined to the line
        naming it when that can be found (two entries can share a page, and
        page granularity alone would merge them)."""
        idx = page_index.get(page)
        base = page_starts[idx] if idx is not None else floor
        base = max(base, floor)
        window_end = min(len(full_md), base + 40000)
        pat = re.compile(
            r"^[#*_ \t]*" + re.escape(title) + r"[*_ \t]*$", re.M
        )
        hit = pat.search(full_md, base, window_end)
        return hit.start() if hit else base

    regions = []  # (start, end, command|None)
    floor = 0
    starts = []
    for page, _i, title, command in boundaries:
        off = entry_offset(page, title, floor)
        starts.append((off, command))
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
        for span in cm.chunk_spans(region, dictionary=True):
            chunks.append((span.heading, span.level, region[span.start:span.end], r_start + span.start, name))

    staging = editions.staging_dir(out_root, args.slug)
    sections_dir = staging / "sections"
    sections_dir.mkdir()

    seen, entries = {}, []
    for i, (heading, level, body, off, command) in enumerate(chunks, start=1):
        c_start, c_end = cm.trim_span(full_md, off, off + len(body))
        p_start, p_end = cm.page_range(page_starts, page_numbers, c_start, max(c_end, c_start + 1))

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
            editions.with_edition_fields({
                "source_pdf": plan.source_name,
                "title": args.title,
                "slug": args.slug,
                "page_count": doc.page_count,
                "toc": [
                    {"level": l, "title": (t or "").strip(), "page": p} for l, t, p in toc
                ],
                "attribution": {
                    "status": "attributed" if cmd_level is not None else "declined",
                    "command_level": cmd_level,
                    "chosen_by": chosen_by,
                },
                "sections": entries,
                "full_md_chars": len(full_md),
            }, plan.doc_id, plan.version, plan.later),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    doc.close()

    # The previous version is kept outside docs/, in .rebuild-backup/, so no
    # index builder lists it as a manual of its own.
    old = editions.publish(staging, out_dir)
    if old:
        print(
            f"[{args.slug}] previous version kept at "
            f"{old.relative_to(out_root.parent)}",
            flush=True,
        )
    editions.finish(plan)

    attributed = sum(1 for e in entries if e["command"])
    print(
        f"[{args.slug}] DONE: {len(entries)} chunks, "
        f"{attributed} ({100*attributed/max(len(entries),1):.1f}%) attributed to a command",
        flush=True,
    )


if __name__ == "__main__":
    main()
