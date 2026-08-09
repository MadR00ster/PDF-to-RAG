#!/usr/bin/env python3
"""
Convert a vendor PDF manual into this repo's RAG doc format:

  docs/<slug>/manifest.json
  docs/<slug>/full.md
  docs/<slug>/sections/NNN-heading-slug.md

Usage:
  python scripts/convert_manual.py "Tessent Manual/newmanual.pdf" --title "Tessent Foo User's Manual"
  python scripts/convert_manual.py "Synopsys Manual/dcug_V-2024.06.pdf" \
      --title "Design Compiler(R) User Guide" --slug dcug-v-2024-06

For command-dictionary-style manuals (syn2, tshell-ref, ...) where individual
commands are marked with a bold name and no real heading, add --dictionary.

After converting, refresh the vendor folder's docs/index.json + docs/README.md:
  python scripts/build_index.py "Tessent Manual"

Requires: pymupdf4llm (pulls in pymupdf). See scripts/requirements.txt.

Chunking rules (see each vendor folder's CLAUDE.md for the prose spec this
implements):

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
import json
import re
import sys
from pathlib import Path

try:
    import pymupdf
    import pymupdf4llm
except ImportError:
    sys.exit("Missing dependency. Run: pip install -r scripts/requirements.txt")

MAX_CHUNK = 9000

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


def next_heading_matches(text: str, min_level: int, dictionary: bool) -> list[tuple[int, int, str]]:
    """Matches for the shallowest heading level >= min_level that actually
    appears in `text` (tries H(min_level), then H(min_level+1), ... up to
    H6); falls back to standalone **bold** lines in --dictionary mode if no
    numbered heading level matches at all."""
    for lvl in range(min_level, 7):
        matches = [(m.start(), lvl, m.group(2).strip()) for m in heading_re(lvl).finditer(text)]
        if matches:
            return matches
    if dictionary:
        matches = [(m.start(), min_level, f"**{m.group(1).strip()}**") for m in BOLD_LINE_RE.finditer(text)]
        if matches:
            return matches
    return []


def split_at(text: str, matches: list[tuple[int, int, str]]) -> list[tuple[str, int, str]]:
    """Slice `text` at each match position. Anything before the first match
    becomes an ('(intro)', 0, ...) block."""
    if not matches:
        return [("(intro)", 0, text)] if text.strip() else []
    blocks = []
    if matches[0][0] > 0 and text[: matches[0][0]].strip():
        blocks.append(("(intro)", 0, text[: matches[0][0]]))
    for i, (pos, level, heading) in enumerate(matches):
        end = matches[i + 1][0] if i + 1 < len(matches) else len(text)
        blocks.append((heading, level, text[pos:end]))
    return blocks


def hard_wrap(text: str) -> list[str]:
    """Absolute last resort for text with no blank line to break on (a huge
    table, or a wall of prose pymupdf4llm emitted without paragraph breaks):
    cut at the last newline before MAX_CHUNK, or mid-line if even that
    doesn't exist. Keeps a pathological input from becoming one enormous
    chunk -- every boundary here is arbitrary, so `full.md` is the fallback
    if one lands badly."""
    if len(text) <= MAX_CHUNK:
        return [text]
    pieces = []
    rest = text
    while len(rest) > MAX_CHUNK:
        cut = rest.rfind("\n", 0, MAX_CHUNK)
        if cut <= 0:
            cut = MAX_CHUNK
        pieces.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        pieces.append(rest)
    return pieces


def split_by_paragraph(heading: str, level: int, body: str) -> list[tuple[str, int, str]]:
    """Last resort when a block has no deeper headings to split on: pack
    blank-line-separated paragraphs up to MAX_CHUNK each, hard-wrapping any
    single paragraph that is itself over the limit."""
    paragraphs = body.split("\n\n")
    pieces: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for p in paragraphs:
        p_len = len(p) + 2
        if cur and cur_len + p_len > MAX_CHUNK:
            pieces.append("\n\n".join(cur))
            cur, cur_len = [], 0
        cur.append(p)
        cur_len += p_len
    if cur:
        pieces.append("\n\n".join(cur))

    pieces = [w for piece in pieces for w in hard_wrap(piece)]

    if len(pieces) <= 1:
        return [(heading, level, body)]
    return [(heading if i == 0 else f"{heading} (cont.)", level, piece) for i, piece in enumerate(pieces)]


def pack_adjacent(leaves: list[tuple[str, int, str]]) -> list[tuple[str, int, str]]:
    """Greedily merge consecutive sibling fragments (from the same split
    operation) so they land closer to MAX_CHUNK, instead of writing one file
    per small fragment (e.g. a chapter whose only sub-structure pymupdf4llm
    found is a run of small bold run-in phrases)."""
    packed = []
    group_heading = group_level = None
    group_parts: list[str] = []
    group_len = 0
    for heading, level, body in leaves:
        if group_parts and group_len + len(body) > MAX_CHUNK:
            packed.append((group_heading, group_level, "".join(group_parts)))
            group_parts, group_len = [], 0
            group_heading = group_level = None
        if group_heading is None:
            group_heading, group_level = heading, level
        group_parts.append(body)
        group_len += len(body)
    if group_parts:
        packed.append((group_heading, group_level, "".join(group_parts)))
    return packed


def split_oversized(heading: str, level: int, body: str, dictionary: bool) -> list[tuple[str, int, str]]:
    """Recursively break a >MAX_CHUNK block into leaf fragments by descending
    one heading level at a time: split on the shallowest deeper heading level
    that actually appears, then only recurse further into whichever pieces
    are still oversized. A piece that already fits stops there even if it
    contains its own (now-irrelevant) deeper headings. Siblings produced by
    the same split are then packed back together (see pack_adjacent) so a
    chapter that's only choppy at one heading level doesn't turn into dozens
    of tiny files."""
    if len(body) <= MAX_CHUNK:
        return [(heading, level, body)]

    matches = next_heading_matches(body, level + 1, dictionary)
    blocks = split_at(body, matches) if matches else []

    # No-progress guard. If the "split" failed to actually divide the body
    # into 2+ pieces, recursing would re-derive the same single block
    # forever. The way this happens in practice: in --dictionary mode a
    # command entry longer than MAX_CHUNK, with no headings of its own,
    # re-matches the very bold command name it already starts with (at
    # offset 0), so split_at hands back one block identical to the input.
    # Paragraph packing is the correct fallback and never recurses.
    if len(blocks) < 2:
        return split_by_paragraph(heading, level, body)

    children = []
    for h, lvl, chunk in blocks:
        if h == "(intro)":
            h, lvl = (f"{heading} (intro)" if heading not in (None, "(intro)") else "(intro)"), level
        children.extend(split_oversized(h, lvl, chunk, dictionary))
    return pack_adjacent(children)


def chunk_markdown(md_text: str, dictionary: bool) -> list[tuple[str, int, str]]:
    top_blocks = split_at(md_text, top_matches(md_text, dictionary))
    chunks = []
    for heading, level, body in top_blocks:
        if len(body) <= MAX_CHUNK:
            chunks.append((heading, level, body))
        else:
            chunks.extend(split_oversized(heading, level, body, dictionary))
    return chunks


def convert(pdf_path: Path, title: str, slug: str, dictionary: bool, out_root: Path) -> None:
    print(f"Extracting markdown from {pdf_path.name} ...")
    md_text = pymupdf4llm.to_markdown(str(pdf_path))
    doc = pymupdf.open(str(pdf_path))

    print("Chunking ...")
    chunks = chunk_markdown(md_text, dictionary)

    out_dir = out_root / slug
    sections_dir = out_dir / "sections"
    sections_dir.mkdir(parents=True, exist_ok=True)

    seen_slugs: dict = {}
    section_entries = []
    for i, (heading, level, body) in enumerate(chunks, start=1):
        file_slug = dedupe_slug(slugify(heading), seen_slugs)
        filename = f"{i:03d}-{file_slug}.md"
        body = body.strip() + "\n"
        (sections_dir / filename).write_text(body, encoding="utf-8")
        section_entries.append(
            {"file": f"sections/{filename}", "heading": heading, "level": level, "chars": len(body)}
        )

    (out_dir / "full.md").write_text(md_text, encoding="utf-8")

    manifest = {
        "source_pdf": pdf_path.name,
        "title": title,
        "slug": slug,
        "page_count": doc.page_count,
        "toc": [{"level": lvl, "title": t.strip(), "page": pg} for lvl, t, pg in doc.get_toc()],
        "sections": section_entries,
        "full_md_chars": len(md_text),
    }
    # ensure_ascii=True: this machine's Python defaults to a non-UTF-8
    # locale (cp950), so escaping non-ASCII keeps manifest.json readable by
    # any tool that opens it without an explicit encoding= argument.
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    print(f"Wrote {len(section_entries)} sections to {out_dir}")
    print(f"Pages: {doc.page_count}   full.md chars: {len(md_text)}")
    print(f'Next: python scripts/build_index.py "{pdf_path.parent}"')


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
    ap.add_argument("--out-root", type=Path, help="Where to write docs/<slug>/ (default: <pdf's folder>/docs)")
    args = ap.parse_args()

    pdf_path = args.pdf.resolve()
    if not pdf_path.exists():
        sys.exit(f"No such file: {pdf_path}")

    slug = args.slug or slugify(pdf_path.stem, maxlen=40)
    out_root = (args.out_root or pdf_path.parent / "docs").resolve()

    out_dir = out_root / slug
    if out_dir.exists():
        sys.exit(f"{out_dir} already exists -- pick a different --slug or remove it first")

    convert(pdf_path, args.title, slug, args.dictionary, out_root)


if __name__ == "__main__":
    main()
