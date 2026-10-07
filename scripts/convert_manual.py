#!/usr/bin/env python3
"""
Convert a vendor PDF manual into this repo's RAG doc format:

  docs/<slug>/manifest.json
  docs/<slug>/full.md
  docs/<slug>/sections/NNN-heading-slug.md

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
        filename = f"{i:03d}-{file_slug}.md"
        body = md_text[span.start:span.end].strip() + "\n"
        (sections_dir / filename).write_text(body, encoding="utf-8")
        c_start, c_end = trim_span(md_text, span.start, span.end)
        p_start, p_end = page_range(page_starts, numbers, c_start, max(c_end, c_start + 1))
        section_entries.append(
            {"file": f"sections/{filename}", "heading": span.heading, "level": span.level, "chars": len(body),
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path, help="Path to the source PDF")
    ap.add_argument("--title", required=True, help="Manual title, as it should appear in index.json/README.md")
    ap.add_argument("--slug", help="docs/<slug> folder name (default: derived from the PDF filename)")
    ap.add_argument(
        "--dictionary",
        action="store_true",
        help="Also split on standalone **bold** lines (for command-dictionary manuals like syn2/tshell-ref)",
    )
    ap.add_argument("--out-root", type=Path,
                    help="Where to write docs/<slug>/ (default: docs/ in the PDF's collection)")
    editions.add_arguments(ap)
    args = ap.parse_args()

    pdf_path = args.pdf.resolve()
    if not pdf_path.exists():
        sys.exit(f"No such file: {pdf_path}")

    plan = editions.plan(pdf_path, args.slug, args.out_root, args.version, args.doc_id)
    out_dir = plan.out_root / plan.slug
    if out_dir.exists():
        sys.exit(f"{out_dir} already exists -- pick a different --slug or remove it first")

    convert(plan, args.title, args.dictionary)


if __name__ == "__main__":
    main()
