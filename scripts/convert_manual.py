#!/usr/bin/env python3
"""
Convert a vendor PDF manual into this repo's RAG doc format:

  docs/<slug>/manifest.json
  docs/<slug>/full.md
  docs/<slug>/sections/NNNN-heading-slug.md

Usage:
  python scripts/convert_manual.py "Tessent Manual/new_docs/newmanual.pdf" --title "Tessent Foo User's Manual"
  python scripts/convert_manual.py "Synopsys Manual/new_docs/dcug_V-2024.06.pdf" \
      --title "Design Compiler(R) User Guide" --slug dcug-v-2024-06

A PDF in <collection>/new_docs/ is moved to <collection>/source/ once its
conversion has been written. The release it documents is read from its first
pages into the manifest's `version`, and `doc_id` names the manual it is an
edition of -- see editions.py. The slug defaults to the filename, with that
release on the end.

For command-dictionary-style manuals (syn2, tshell-ref, ...) where individual
commands are marked with a bold name and no real heading, add --dictionary.

A command reference (one entry per command) is converted differently: the text
is split into one region per entry, and each chunk is written with the command
it documents. --shape says which; by default it is decided from the PDF's
bookmark outline, as pick_extractor.py does, and a document that is part of each
is refused. `rebuild_reference.py` runs the same reference conversion.

After converting, refresh the vendor folder's docs/index.json + docs/README.md:
  python scripts/build_index.py "Tessent Manual"

Requires: pymupdf4llm (pulls in pymupdf). See scripts/requirements.txt.

Chunking rules (SKILL.md's "Chunking" section is the spec this implements):

  1. Split on real H1/H2 chapter headings, plus standalone **bold** lines in
     --dictionary mode.
  2. Any resulting block over MAX_CHUNK is split again on the shallowest
     deeper heading level that actually appears inside it, recursing only
     into pieces that are still oversized.
  3. A block with no deeper heading falls back to blank-line paragraph
     packing, then to a hard wrap at a line boundary, so no chunk can
     exceed MAX_CHUNK by an unbounded amount.
  4. Sibling fragments produced by the same split are greedily packed back
     together, so a chapter that is merely choppy at one heading level does
     not explode into dozens of tiny files.

Every recursive step must strictly shrink its input -- see the no-progress
guard in split_oversized() for the case that violated this and caused an
infinite recursion in --dictionary mode.

Heading *levels* come from pymupdf4llm's font-size heuristic, not real
document structure, so they are a rough guide, not authoritative; `full.md`
is the un-chunked fallback whenever a boundary lands badly.
"""
from __future__ import annotations

import argparse
import bisect
import json
import re
import sys
from pathlib import Path
from typing import NamedTuple

try:
    import pymupdf
    import pymupdf4llm
except ImportError:
    sys.exit("Missing dependency. Run: pip install -r scripts/requirements.txt")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import editions  # noqa: E402
import enrich_chunks as ec  # noqa: E402
from _common import MIN_COMMANDS, detect_shape, looks_like_entry, pick_command_level  # noqa: E402

MAX_CHUNK = 9000


class Span(NamedTuple):
    """A chunk's place in the text it was cut from: offsets, not a copy."""
    heading: str
    level: int
    start: int
    end: int


TOP_HEADING_RE = re.compile(r"^(#{1,2})[ \t]+(.+?)[ \t]*$", re.MULTILINE)
BOLD_LINE_RE = re.compile(r"^\*\*([^*\n]{2,80})\*\*[ \t]*$", re.MULTILINE)


def heading_re(level: int) -> re.Pattern:
    return re.compile(rf"^(#{{{level}}})[ \t]+(.+?)[ \t]*$", re.MULTILINE)


def slugify(text: str, maxlen: int = 60) -> str:
    text = re.sub(r"[*_`]", "", text)
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    s = s[:maxlen].strip("-")
    return s or "section"


def dedupe_slug(slug: str, seen: dict) -> str:
    count = seen.get(slug, 0) + 1
    seen[slug] = count
    return slug if count == 1 else f"{slug}-{count}"


def top_matches(text: str, dictionary: bool) -> list[tuple[int, int, str]]:
    """Primary chapter split: H1/H2 headings, plus standalone **bold** lines
    in --dictionary mode."""
    matches = {m.start(): (len(m.group(1)), m.group(2).strip()) for m in TOP_HEADING_RE.finditer(text)}
    if dictionary:
        for m in BOLD_LINE_RE.finditer(text):
            matches.setdefault(m.start(), (2, f"**{m.group(1).strip()}**"))
    return sorted((pos, lvl, head) for pos, (lvl, head) in matches.items())


def next_heading_matches(text: str, min_level: int, dictionary: bool,
                         start: int = 0, end: int | None = None) -> list[tuple[int, int, str]]:
    """Matches for the shallowest heading level >= min_level that actually
    appears in text[start:end] (tries H(min_level), then H(min_level+1), ...
    up to H6); falls back to standalone **bold** lines in --dictionary mode if
    no numbered heading level matches at all. Positions are in `text`."""
    body = text[start:end]
    for lvl in range(min_level, 7):
        matches = [(start + m.start(), lvl, m.group(2).strip()) for m in heading_re(lvl).finditer(body)]
        if matches:
            return matches
    if dictionary:
        matches = [(start + m.start(), min_level, f"**{m.group(1).strip()}**") for m in BOLD_LINE_RE.finditer(body)]
        if matches:
            return matches
    return []


def split_at(text: str, matches: list[tuple[int, int, str]], start: int = 0, end: int | None = None) -> list[Span]:
    """Cut text[start:end] at each match position. Anything before the first
    match becomes an ('(intro)', 0) span."""
    end = len(text) if end is None else end
    if not matches:
        return [Span("(intro)", 0, start, end)] if text[start:end].strip() else []
    spans = []
    if matches[0][0] > start and text[start:matches[0][0]].strip():
        spans.append(Span("(intro)", 0, start, matches[0][0]))
    for i, (pos, level, heading) in enumerate(matches):
        spans.append(Span(heading, level, pos, matches[i + 1][0] if i + 1 < len(matches) else end))
    return spans


def hard_wrap(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Absolute last resort for text with no blank line to break on (a huge
    table, or a wall of prose pymupdf4llm emitted without paragraph breaks):
    cut at the last newline before MAX_CHUNK, or mid-line if even that
    doesn't exist. Keeps a pathological input from becoming one enormous
    chunk -- every boundary here is arbitrary, so `full.md` is the fallback
    if one lands badly. Returns (start, end) pairs covering text[start:end]."""
    if end - start <= MAX_CHUNK:
        return [(start, end)]
    pieces = []
    while end - start > MAX_CHUNK:
        cut = text.rfind("\n", start, start + MAX_CHUNK) - start
        if cut <= 0:
            cut = MAX_CHUNK
        pieces.append((start, start + cut))
        start += cut
    if start < end:
        pieces.append((start, end))
    return pieces


def split_by_paragraph(text: str, heading: str, level: int, start: int, end: int) -> list[Span]:
    """Last resort when a block has no deeper headings to split on: pack
    blank-line-separated paragraphs up to MAX_CHUNK each, hard-wrapping any
    single paragraph that is itself over the limit. A piece runs from its first
    paragraph's start to its last paragraph's end; the blank lines between
    pieces belong to neither."""
    pieces: list[tuple[int, int]] = []
    first = last_end = None      # the open piece
    cur_len = 0
    offset = start
    for p in text[start:end].split("\n\n"):
        p_len = len(p) + 2
        if first is not None and cur_len + p_len > MAX_CHUNK:
            pieces.append((first, last_end))
            first, cur_len = None, 0
        if first is None:
            first = offset
        last_end = offset + len(p)
        cur_len += p_len
        offset += p_len
    if first is not None:
        pieces.append((first, last_end))

    pieces = [w for a, b in pieces for w in hard_wrap(text, a, b)]

    if len(pieces) <= 1:
        return [Span(heading, level, start, end)]
    return [Span(heading if i == 0 else f"{heading} (cont.)", level, a, b) for i, (a, b) in enumerate(pieces)]


def pack_adjacent(leaves: list[Span]) -> list[Span]:
    """Greedily merge consecutive sibling fragments (from the same split
    operation) so they land closer to MAX_CHUNK, instead of writing one file
    per small fragment (e.g. a chapter whose only sub-structure pymupdf4llm
    found is a run of small bold run-in phrases). A merged span runs from its
    first part to its last, so it holds whatever lay between them, and its size
    is measured that way."""
    packed: list[Span] = []
    group: Span | None = None
    for leaf in leaves:
        if group and leaf.end - group.start > MAX_CHUNK:
            packed.append(group)
            group = None
        group = leaf if group is None else Span(group.heading, group.level, group.start, leaf.end)
    if group:
        packed.append(group)
    return packed


def split_oversized(text: str, heading: str, level: int, dictionary: bool, start: int, end: int) -> list[Span]:
    """Recursively break a >MAX_CHUNK block into leaf fragments by descending
    one heading level at a time: split on the shallowest deeper heading level
    that actually appears, then only recurse further into whichever pieces
    are still oversized. A piece that already fits stops there even if it
    contains its own (now-irrelevant) deeper headings. Siblings produced by
    the same split are then packed back together (see pack_adjacent) so a
    chapter that's only choppy at one heading level doesn't turn into dozens
    of tiny files."""
    if end - start <= MAX_CHUNK:
        return [Span(heading, level, start, end)]

    matches = next_heading_matches(text, level + 1, dictionary, start, end)
    blocks = split_at(text, matches, start, end) if matches else []

    # No-progress guard. If the "split" failed to actually divide the body
    # into 2+ pieces, recursing would re-derive the same single block
    # forever. The way this happens in practice: in --dictionary mode a
    # command entry longer than MAX_CHUNK, with no headings of its own,
    # re-matches the very bold command name it already starts with (at
    # offset 0), so split_at hands back one block identical to the input.
    # Paragraph packing is the correct fallback and never recurses.
    if len(blocks) < 2:
        return split_by_paragraph(text, heading, level, start, end)

    children = []
    for block in blocks:
        h, lvl = block.heading, block.level
        if h == "(intro)":
            h, lvl = (f"{heading} (intro)" if heading not in (None, "(intro)") else "(intro)"), level
        children.extend(split_oversized(text, h, lvl, dictionary, block.start, block.end))
    return pack_adjacent(children)


def page_markdown(pdf_path: Path) -> tuple[list[str], list[int]]:
    """Each page's markdown and its 1-based page number.

    pymupdf4llm 1.28 names the number `page_number`; earlier releases
    called it `page`. Where neither is there, a page's place in the list
    is its number -- true only while every page comes back in order, so
    that is checked: one page dropped would misnumber every page after it,
    and a citation to the wrong page is worse than none.
    """
    raw = pymupdf4llm.to_markdown(str(pdf_path), page_chunks=True)
    with pymupdf.open(str(pdf_path)) as doc:
        expected = doc.page_count
    texts, numbers = [], []
    for i, page in enumerate(raw):
        meta = page.get("metadata") or {}
        n = meta.get("page_number", meta.get("page"))
        texts.append(page.get("text") or "")
        numbers.append(i + 1 if n is None else n)
    if numbers != list(range(1, expected + 1)):
        sys.exit(f"pymupdf4llm returned pages {numbers[:5]}... ({len(numbers)} of them) for the "
                 f"{expected}-page {pdf_path.name}. Page numbers taken from that would be wrong, "
                 "so nothing was written.")
    return texts, numbers


def chunk_spans(md_text: str, dictionary: bool) -> list[Span]:
    """The chunks of `md_text`, each as the span it was cut from. A chunk's body
    is exactly md_text[span.start:span.end], so anything that has to know where
    a chunk sits in the text (its page, above all) reads it from the span
    instead of looking the body up again."""
    chunks = []
    for span in split_at(md_text, top_matches(md_text, dictionary)):
        if span.end - span.start <= MAX_CHUNK:
            chunks.append(span)
        else:
            chunks.extend(split_oversized(md_text, span.heading, span.level, dictionary, span.start, span.end))
    return chunks


def chunk_markdown(md_text: str, dictionary: bool) -> list[tuple[str, int, str]]:
    return [(s.heading, s.level, md_text[s.start:s.end]) for s in chunk_spans(md_text, dictionary)]


def trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    """text[start:end] without the whitespace around it. A piece cut at a
    newline begins with that newline, which can be the last character of the
    page before; a chunk is on the page its first word is."""
    body = text[start:end]
    lead = len(body) - len(body.lstrip())
    return start + lead, start + lead + len(body.strip())


def page_range(page_starts: list[int], numbers: list[int], start: int, end: int) -> tuple[int, int]:
    """First and last page number of text[start:end], given each page's start
    offset in that text and its number. Pass a span through trim_span first."""
    first = max(0, bisect.bisect_right(page_starts, start) - 1)
    last = max(0, bisect.bisect_right(page_starts, end - 1) - 1)
    return numbers[first], numbers[last]


def join_pages(texts: list[str], furniture: set[str]) -> tuple[str, list[int], int]:
    """(full_md, each page's start offset in it, furniture lines removed).

    Furniture is stripped per page *before* concatenation, for two reasons:
    the running footer would otherwise become a chunk boundary in
    --dictionary mode (a standalone "**Feedback**" line is exactly the shape
    the splitter looks for, which produced one junk chunk per page), and
    stripping after concatenation would invalidate the page offsets every
    page number depends on."""
    parts, starts, cursor, removed = [], [], 0, 0
    for text in texts:
        cleaned, n = ec.strip_furniture(text, furniture)
        removed += n
        starts.append(cursor)
        parts.append(cleaned)
        cursor += len(cleaned)
    return "".join(parts), starts, removed


def convert(plan: editions.Plan, title: str, dictionary: bool) -> None:
    pdf_path, slug, out_root = plan.pdf, plan.slug, plan.out_root
    print(f"Extracting markdown from {pdf_path.name} with page tracking ...")
    texts, numbers = page_markdown(pdf_path)
    md_text, page_starts, removed = join_pages(texts, ec.detect_furniture(texts, title))
    print(f"{len(page_starts)} pages, {len(md_text):,} chars, {removed} furniture lines stripped pre-chunking")
    doc = pymupdf.open(str(pdf_path))

    print("Chunking ...")
    spans = chunk_spans(md_text, dictionary)

    out_dir = out_root / slug
    staging = editions.staging_dir(out_root, slug)
    sections_dir = staging / "sections"
    sections_dir.mkdir()

    seen_slugs: dict = {}
    section_entries = []
    for i, span in enumerate(spans, start=1):
        file_slug = dedupe_slug(slugify(span.heading), seen_slugs)
        filename = f"{i:04d}-{file_slug}.md"
        body = md_text[span.start:span.end].strip() + "\n"
        (sections_dir / filename).write_text(body, encoding="utf-8")
        c_start, c_end = trim_span(md_text, span.start, span.end)
        p_start, p_end = page_range(page_starts, numbers, c_start, max(c_end, c_start + 1))
        section_entries.append(
            {"file": f"sections/{filename}", "heading": span.heading, "level": span.level,
             "page_start": p_start, "page_end": p_end}
        )

    (staging / "full.md").write_text(md_text, encoding="utf-8")

    manifest = editions.with_edition_fields({
        "source_pdf": plan.source_name,
        "title": title,
        "slug": slug,
        "page_count": doc.page_count,
        "toc": [{"level": lvl, "title": t.strip(), "page": pg} for lvl, t, pg in doc.get_toc()],
        "sections": section_entries,
        "full_md_chars": len(md_text),
        "schema_version": editions.MANIFEST_SCHEMA_VERSION,
        "converter": editions.converter_record(Path(sys.argv[0]).name, "pymupdf4llm", "pymupdf4llm", False),
    }, plan.doc_id, plan.version, plan.later)
    doc.close()
    # ensure_ascii=True: this machine's Python defaults to a non-UTF-8
    # locale (cp950), so escaping non-ASCII keeps manifest.json readable by
    # any tool that opens it without an explicit encoding= argument.
    (staging / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    editions.publish(staging, out_dir)

    print(f"Wrote {len(section_entries)} sections to {out_dir}")
    print(f"Pages: {manifest['page_count']}   full.md chars: {len(md_text)}")
    editions.finish(plan)
    print(f'Next: python scripts/build_index.py "{plan.collection}"')


def convert_reference(plan: editions.Plan, title: str, command_level: int | None,
                      prose_outside_entries: bool = False) -> None:
    """A command reference: the same pages and chunker as `convert`, but the
    text is first split into one region per entry, so a chunk can belong to
    exactly one command, and each chunk is written with the command it
    documents. `command_level` names the TOC level of the entries; where it is
    not given the level is picked from the TOC, and where none qualifies no
    chunk is attributed, which the manifest records.

    A chunk's breadcrumb is the TOC chain down to its entry: the document's
    title, the chapters the entry sits in, and the command last. A region that
    belongs to no command but starts at a shallower entry (a chapter, an
    appendix) takes that entry's chain.

    `prose_outside_entries` is for a document that is part reference: only the
    titles at the command level that look like commands are commands, and the
    rest of the text is chunked as prose. It changes what is attributed, so it
    is off unless asked for."""
    pdf_path, slug, out_root = plan.pdf, plan.slug, plan.out_root
    out_dir = out_root / slug
    print(f"[{slug}] extracting {pdf_path.name} with page tracking ...", flush=True)
    texts, page_numbers = page_markdown(pdf_path)
    full_md, page_starts, furn_removed = join_pages(texts, ec.detect_furniture(texts, title))
    doc = pymupdf.open(str(pdf_path))
    toc = doc.get_toc()
    print(
        f"[{slug}] {len(page_starts)} pages, {len(full_md):,} chars, "
        f"{furn_removed} furniture lines stripped pre-chunking",
        flush=True,
    )

    levels = sorted({lvl for lvl, _t, _p in toc})
    if command_level is not None:
        if command_level not in levels:
            sys.exit(f"no TOC entry is at level {command_level}; levels here: "
                     f"{', '.join(map(str, levels)) or 'none (no bookmark outline)'}")
        cmd_level, chosen_by = command_level, "option"
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
        for i, (lvl, entry, page) in enumerate(toc):
            entry = (entry or "").strip()
            if not entry:
                continue
            if lvl == cmd_level and not (prose_outside_entries and not looks_like_entry(entry)):
                boundaries.append((page, i, entry, entry))
            elif lvl <= cmd_level and page >= 1:
                boundaries.append((page, i, entry, None))
        # By page, then TOC order: where two entries share a page, TOC order
        # is document order and alphabetical order need not be.
        boundaries.sort()
    n_commands = sum(1 for b in boundaries if b[3])
    if cmd_level is None:
        print(
            f"[{slug}] !! no TOC level has {MIN_COMMANDS} titles shaped like commands "
            "(an underscore, \" -\", or a message code), so no chunk is attributed to one. "
            "If this is a reference, pass --command-level N with the TOC level its entries are at.",
            flush=True,
        )
    else:
        print(
            f"[{slug}] command level L{cmd_level}: {n_commands} commands",
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

    def entry_offset(page: int, entry: str, floor: int) -> int:
        """Offset where a TOC entry starts: its page, refined to the line
        naming it when that can be found (two entries can share a page, and
        page granularity alone would merge them)."""
        idx = page_index.get(page)
        base = page_starts[idx] if idx is not None else floor
        base = max(base, floor)
        window_end = min(len(full_md), base + 40000)
        pat = re.compile(
            r"^[#*_ \t]*" + re.escape(entry) + r"[*_ \t]*$", re.M
        )
        hit = pat.search(full_md, base, window_end)
        return hit.start() if hit else base

    # Each TOC entry's chain from the top, itself last, without any entry that
    # repeats the document's title: the breadcrumb starts with that already.
    manual_norm = ec.normalize_title(title)
    chains, stack = [], []
    for lvl, name, _page in toc:
        name = (name or "").strip()
        while stack and stack[-1][0] >= lvl:
            stack.pop()
        stack.append((lvl, name))
        chains.append([t for _l, t in stack if t and ec.normalize_title(t) != manual_norm])

    regions = []  # (start, end, command|None, TOC entry the region starts at|None)
    floor = 0
    starts = []
    for page, i, entry, command in boundaries:
        off = entry_offset(page, entry, floor)
        starts.append((off, command, i))
        floor = off
    if starts and starts[0][0] > 0:
        regions.append((0, starts[0][0], None, None))
    elif not starts:
        regions.append((0, len(full_md), None, None))
    for n, (off, name, i) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(full_md)
        if end > off:
            regions.append((off, end, name, i))

    print(
        f"[{slug}] chunking {len(regions)} command regions ...", flush=True
    )
    chunks = []  # (heading, level, body, abs_offset, command, breadcrumb)
    for r_start, r_end, name, entry_index in regions:
        region = full_md[r_start:r_end]
        crumb = ec.BREADCRUMB_SEP.join([title, *(chains[entry_index] if entry_index is not None else [])])
        for span in chunk_spans(region, dictionary=name is not None or not prose_outside_entries):
            chunks.append((span.heading, span.level, region[span.start:span.end], r_start + span.start, name, crumb))

    staging = editions.staging_dir(out_root, slug)
    sections_dir = staging / "sections"
    sections_dir.mkdir()

    seen, entries = {}, []
    for i, (heading, level, body, off, command, crumb) in enumerate(chunks, start=1):
        c_start, c_end = trim_span(full_md, off, off + len(body))
        p_start, p_end = page_range(page_starts, page_numbers, c_start, max(c_end, c_start + 1))

        text = ec.apply_breadcrumb(body.strip() + "\n", crumb, title)

        fname = f"{i:04d}-{dedupe_slug(slugify(heading), seen)}.md"
        (sections_dir / fname).write_text(text, encoding="utf-8")
        entries.append(
            {
                "file": f"sections/{fname}",
                "heading": heading,
                "level": level,
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
                "title": title,
                "slug": slug,
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
                "schema_version": editions.MANIFEST_SCHEMA_VERSION,
                "converter": editions.converter_record(Path(sys.argv[0]).name, "pymupdf4llm", "pymupdf4llm", True),
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
            f"[{slug}] previous version kept at "
            f"{old.relative_to(out_root.parent)}",
            flush=True,
        )
    editions.finish(plan)

    attributed = sum(1 for e in entries if e["command"])
    print(
        f"[{slug}] DONE: {len(entries)} chunks, "
        f"{attributed} ({100*attributed/max(len(entries),1):.1f}%) attributed to a command",
        flush=True,
    )


def main(shape: str | None = None, description: str | None = None) -> None:
    """The command line. `shape` fixes it ("reference" for rebuild_reference.py,
    which keeps its own command line); None lets --shape choose."""
    ap = argparse.ArgumentParser(description=description or __doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path, help="Path to the source PDF")
    ap.add_argument("--title", help="Manual title, as it should appear in index.json/README.md. "
                    "Defaults to the title already in docs/<slug>/manifest.json, which keeps it "
                    "byte-stable across rebuilds and avoids shell-encoding trouble with characters "
                    "like the trademark sign")
    ap.add_argument("--slug", required=shape is not None,
                    help="docs/<slug> folder name (default: derived from the PDF filename)")
    if shape is None:
        ap.add_argument("--shape", choices=("auto", "prose", "reference"), default="auto",
                        help="a prose manual, or a command reference whose entries each get their own "
                        "chunks and name (default: decided from the PDF's bookmark outline; a "
                        "document that is part of each is refused, and needs this said)")
        ap.add_argument(
            "--dictionary",
            action="store_true",
            help="Also split on standalone **bold** lines (for command-dictionary manuals like syn2/tshell-ref)",
        )
    ap.add_argument("--command-level", type=int, metavar="N",
                    help="a reference's entries are at TOC level N. By default it is the level where "
                    "20 or more titles look like commands (an underscore, ' -', or a message code); "
                    "a reference whose entries are plain words has none, and is declined")
    ap.add_argument("--prose-outside-entries", action="store_true",
                    help="for a document that is part reference: only the titles at the command level "
                    "that look like commands are attributed, and the rest is chunked as prose. It "
                    "changes what is attributed; measure before relying on it")
    ap.add_argument("--out-root", type=Path,
                    help="Where to write docs/<slug>/ (default: docs/ in the PDF's collection)")
    editions.add_arguments(ap)
    ap.add_argument("--replace", action="store_true",
                    help="overwrite an existing docs/<slug>/ (the previous one is kept in .rebuild-backup/<slug>/)")
    args = ap.parse_args()
    shape = shape or args.shape
    dictionary = getattr(args, "dictionary", False)

    pdf_path = args.pdf.resolve()
    if not pdf_path.is_file():
        sys.exit(f"No such file: {pdf_path}")
    out_root = (args.out_root or editions.collection_of(pdf_path) / "docs").resolve()

    def refuse_existing(slug: str) -> None:
        if (out_root / slug).exists() and not args.replace:
            sys.exit(f"{out_root / slug} exists -- pass --replace to rebuild it")
    if args.slug:
        refuse_existing(args.slug)

    # A rebuild keeps what the document already is: a title, doc_id or version
    # set by hand must survive --replace.
    prior = {}
    if args.slug and (out_root / args.slug / "manifest.json").exists():
        prior = json.loads((out_root / args.slug / "manifest.json").read_text(encoding="utf-8-sig"))
    title = args.title
    if not title:
        if "title" not in prior:
            sys.exit("--title is required when docs/<slug>/manifest.json does not exist")
        title = prior["title"]
        print(f"[{args.slug}] title from existing manifest: {title!r}", flush=True)

    if shape == "auto":
        with pymupdf.open(str(pdf_path)) as doc:
            found, why, _level = detect_shape(doc.get_toc(), doc.page_count)
        print(f"shape: {found} ({why})")
        if found == "mixed":
            sys.exit("This is part prose and part reference. Convert it with --shape prose or --shape "
                     "reference, whichever holds most of it, or split the PDF.")
        shape = found
    if shape == "prose" and (args.command_level is not None or args.prose_outside_entries):
        sys.exit("--command-level and --prose-outside-entries are for a reference; this is converted as prose")

    plan = editions.plan(pdf_path, args.slug, out_root, args.version or prior.get("version"),
                         args.doc_id or prior.get("doc_id"),
                         later=not args.version and prior.get("version_and_later") is True)
    refuse_existing(plan.slug)

    if shape == "reference":
        convert_reference(plan, title, args.command_level, args.prose_outside_entries)
    else:
        convert(plan, title, dictionary)


if __name__ == "__main__":
    main()
