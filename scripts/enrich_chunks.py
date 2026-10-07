#!/usr/bin/env python3
"""
Post-process existing docs/<slug>/sections/*.md chunks in place:

  1. Strip page furniture -- the running title / page number / "Feedback" /
     "Chapter N: ..." block that the PDF's header+footer injects at every
     page boundary, often mid-sentence.
  2. Prepend a breadcrumb line ("Manual > Chapter > Section") so a chunk
     retrieved on its own still says where it came from.

Both passes are idempotent: rerunning makes no further change.

Ancestors come from the PDF's bookmark TOC, never from heading levels: a
chunk whose opening heading is a TOC entry gets that entry's TOC parents,
and every other chunk gets the document title alone. See build_breadcrumbs
for how headings are matched to entries and what that measured. Breadcrumbs
a page-aware converter wrote (rebuild_reference.py, convert_docling.py) are
never touched; the manifest fields they write say which those are.

Usage:
  python scripts/enrich_chunks.py "Synopsys Manual" --dry-run
  python scripts/enrich_chunks.py "Synopsys Manual"
  python scripts/enrich_chunks.py "Tessent Manual" --skip tshell-ref-2026-2

Options:
  --dry-run     report what would change; write nothing
  --list-furniture
                print every distinct line deleted, or with --dry-run that
                would be, per manual with a count, most often first. Read it
                before running on text another tool produced.
  --skip SLUG   exclude a manual (repeatable); use for manuals queued for a
                full page-accurate reconversion, which redoes this anyway
  --only SLUG   restrict to one manual (repeatable)
"""
from __future__ import annotations

import argparse
import bisect
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import editions  # noqa: E402
from _common import BREADCRUMB_SEP, strip_emphasis, utf8_console  # noqa: E402


FENCE_RE = re.compile(r"^\s*```")
# pymupdf4llm renders a PDF heading as a markdown heading or as a line that
# is bold and nothing else.
HEADING_LINE_RE = re.compile(r"^\s*(#{1,6}\s+\S.*|\*\*[^*].*\*\*)\s*$")
# A bookmark says "Chapter 2  Scan and ATPG Basics" where the page heading
# says "Scan and ATPG Basics".
TOC_PREFIX_RE = re.compile(r"^(chapter|appendix|part)\s+[\w.]{1,4}\s*[:.\-–]?\s+", re.I)
BARE_NUM_RE = re.compile(r"^\s*\d{1,4}\s*$")
CHAPTER_HDR_RE = re.compile(r"^\s*(Chapter|Appendix|Section)\s+\w{1,4}\s*:", re.I)

# How close a bare page number / running chapter header must sit to a
# frequency-detected furniture line to be treated as furniture too.
ADJACENCY = 3

# A line is the running title when it starts with the title (a footer that
# adds the release: "Design Compiler User Guide V-2024.06") or is most of it
# (one the layout cut short). A line that only begins the title -- "Design
# Compiler" alone, the product name prose uses -- is content. This is the
# share of the title such a line has to cover.
TITLE_SHARE = 0.6

# What follows the title in a running footer is a release, a page number or
# a date. Three lower-case words in a row are a sentence that happens to start
# with the title, and are kept.
PROSE_RUN_RE = re.compile(r"\b[a-z]+\s+[a-z]+\s+[a-z]+\b")


def iter_lines_outside_code(text: str):
    """Yield (index, raw_line, in_code) tracking fenced code blocks."""
    in_code = False
    for i, raw in enumerate(text.splitlines()):
        if FENCE_RE.match(raw):
            in_code = not in_code
            yield i, raw, True  # the fence itself is never furniture
            continue
        yield i, raw, in_code


def looks_structural(line: str) -> bool:
    s = line.strip()
    return (
        not s
        or s.startswith(("|", ">", "-", "*", "+", "•"))
        or s.startswith("#")
        or FENCE_RE.match(s) is not None
    )


def eligible_furniture_candidate(raw: str, core: str) -> bool:
    """Gate on shape before frequency is even considered.

    Frequency alone is not enough: a DFT flow guide repeats real command
    names like `insert_dft` on their own line dozens of times, and an
    earlier version of this script happily classified those as furniture and
    would have deleted them. Running headers/footers are always multi-word
    prose; command names and identifiers are single tokens. That one
    distinction removes the whole false-positive class.
    """
    if not core or len(core) > 120 or not re.search(r"[A-Za-z]", core):
        return False
    if core.startswith("<!--") or core.endswith("-->"):
        return False  # figure-text delimiters are structure, not furniture
    if looks_structural(raw) and core.lower() != "feedback":
        return False
    if core.lower() == "feedback":
        return True
    if not re.search(r"\s", core):
        return False  # single token -> identifier/command name, keep it
    if re.fullmatch(r"[\w\s\-.]*[_(){};=]+[\w\s\-.]*", core):
        return False  # looks like code even outside a fence
    return True


def detect_furniture(section_texts: list[str], title: str) -> set[str]:
    """Frequency-detect the manual's running header/footer text, restricted
    to shape-eligible lines (see eligible_furniture_candidate).

    Lines matching the manual's own title are the classic running footer, so
    they qualify at a much lower count than arbitrary repeated prose.
    """
    title_core = re.sub(r"\s+", " ", strip_emphasis(title)).strip().lower()

    counts: Counter[str] = Counter()
    for text in section_texts:
        seen_here = set()
        for _, raw, in_code in iter_lines_outside_code(text):
            if in_code:
                continue
            core = strip_emphasis(raw)
            if eligible_furniture_candidate(raw, core):
                seen_here.add(core)
        counts.update(seen_here)

    # Precision over recall, deliberately. An earlier version also treated
    # "any line repeated often enough" as furniture, which deleted
    # genuine prose that reference manuals simply reuse a lot -- "Note the
    # following:" (127x), "where valid values are as follows:" (175x). A
    # missed furniture line costs a few wasted tokens; a deleted content
    # line is unrecoverable without reconverting the PDF. So the only things
    # that qualify are the "Feedback" link and the running title footer.
    furniture = set()
    head = title_core[:40]
    for line, n in counts.items():
        spaced = re.sub(r"\s+", " ", line).strip()
        norm = spaced.lower()
        is_title_line = title_core and (
            (norm.startswith(head) and not norm[len(head):len(head) + 1].isalnum()
             and not PROSE_RUN_RE.search(spaced[len(head):]))
            or (title_core.startswith(norm[:40]) and len(norm) >= TITLE_SHARE * len(title_core))
        )
        if norm == "feedback" or (is_title_line and n >= 3):
            furniture.add(line)
    return furniture


def strip_furniture(text: str, furniture: set[str],
                    dropped: list[str] | None = None) -> tuple[str, int]:
    """The text without its furniture, and how many lines that was. Each line
    removed is appended to `dropped`, when one is given."""
    lines = text.splitlines()
    drop = [False] * len(lines)

    for i, raw, in_code in iter_lines_outside_code(text):
        if in_code:
            continue
        core = strip_emphasis(raw)
        if core and core in furniture:
            drop[i] = True

    # Bare page numbers and running "Chapter N:" headers are only furniture
    # when they sit next to a confirmed furniture line -- a lone number
    # elsewhere could be real content.
    anchors = [i for i, d in enumerate(drop) if d]
    if anchors:
        anchor_set = set(anchors)
        for i, raw, in_code in iter_lines_outside_code(text):
            if in_code or drop[i] or not raw.strip():
                continue
            if BARE_NUM_RE.match(raw) or CHAPTER_HDR_RE.match(raw):
                near = any(
                    j in anchor_set
                    for j in range(i - ADJACENCY, i + ADJACENCY + 1)
                )
                if near:
                    drop[i] = True

    if dropped is not None:
        dropped.extend(ln.strip() for i, ln in enumerate(lines) if drop[i])
    kept = [ln for i, ln in enumerate(lines) if not drop[i]]
    out = "\n".join(kept)
    out = re.sub(r"\n{3,}", "\n\n", out).strip() + "\n"
    return out, sum(drop)


def normalize_title(s: str) -> str:
    s = re.sub(r"\s+", " ", strip_emphasis(s)).strip()
    s = TOC_PREFIX_RE.sub("", s)
    s = re.sub(r"^\d+(\.\d+)*\.?\s*", "", s)  # drop leading chapter numbering
    return s.lower()


def heading_lines(text: str, manual_title: str) -> list[tuple[bool, str]]:
    """(opens the chunk, line) for each heading line outside code, in order.
    A breadcrumb an earlier run wrote is not part of the text."""
    out, in_code, opening = [], False, True
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if FENCE_RE.match(raw):
            in_code, opening = not in_code, False
            continue
        if in_code:
            continue
        if opening and is_existing_breadcrumb(raw, manual_title):
            continue
        if HEADING_LINE_RE.match(raw):
            out.append((opening, raw))
        opening = False
    return out


def toc_chains(toc: list[tuple[int, str]], manual_norm: str) -> list[list[str]]:
    """Every TOC entry's chain of titles from the top, itself last. An entry
    repeating the manual's own title is left out: the crumb starts with it."""
    chains, stack = [], []
    for level, title in toc:
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        chains.append([t for _, t in stack if normalize_title(t) != manual_norm])
    return chains


def longest_in_order(pairs: list[tuple[int, int]], opening: set[int]) -> list[tuple[int, int]]:
    """The longest run of (heading line, TOC entry) matches increasing in
    both, pairs given in line order with each line's entries descending so a
    line matches at most one entry. Patience sorting, O(n log n).

    When a later line matches an entry already ending a run, the earlier
    match is kept unless only the later line opens a chunk (`opening`). Only
    an opening match gives a breadcrumb, so a section's `### Topic` further
    down must not displace the `## Topic` that opens it, and a passing
    mention before a section must not displace the section itself."""
    tails: list[int] = []
    tail_at: list[int] = []
    prev: list[int | None] = [None] * len(pairs)
    for n, (line, entry) in enumerate(pairs):
        pos = bisect.bisect_left(tails, entry)
        if pos < len(tails) and tails[pos] == entry:
            if pairs[tail_at[pos]][0] in opening or line not in opening:
                continue
        if pos == len(tails):
            tails.append(entry)
            tail_at.append(n)
        else:
            tails[pos] = entry
            tail_at[pos] = n
        prev[n] = tail_at[pos - 1] if pos else None
    run, n = [], (tail_at[-1] if tail_at else None)
    while n is not None:
        run.append(pairs[n])
        n = prev[n]
    return run[::-1]


def build_breadcrumbs(texts: list[str], manual_title: str, toc: list[dict]) -> list[str]:
    """'Manual › ancestor › ancestor' for each chunk, the ancestors taken
    from the bookmark TOC. Ancestors only -- the chunk's own heading is
    already the first line of its body.

    Heading lines are matched to TOC entries by title, and only the longest
    set of matches that runs forward through both the text and the TOC is
    kept. Titles repeat ("Overview" in every chapter) and headings can name a
    section chapters before its own, so a match is trusted for being in
    order with the others, not for being a match. A chunk that opens with a
    kept match is that entry, and gets its TOC parents. Every other chunk
    gets the title alone.

    Heading levels play no part: pymupdf4llm guesses them from font size.
    The walk they replaced -- a level stack of TOC-confirmed headings -- was
    right for 14-23% of the chunks it gave a parent, measured against each
    chunk's page located in the PDF across 13 manuals: one unmatched
    chapter heading left the previous chapter, or a preface, as the parent
    of everything after it. This is right for 99.4-99.9% and gives a parent
    to 48-62% of chunks, where the old walk managed 22-45%. Letting a chunk
    without its own match inherit the section it follows would reach 93-96%
    of chunks at 89-95% right -- a wrong parent in one chunk of ten, which is
    the error this skill exists to prevent.
    """
    manual_norm = normalize_title(manual_title)
    entries = [(e["level"], re.sub(r"\s+", " ", e.get("title") or "").strip())
               for e in toc if isinstance(e, dict) and isinstance(e.get("level"), int)]
    chains = toc_chains(entries, manual_norm)
    by_title: dict[str, list[int]] = {}
    for j, (_, title) in enumerate(entries):
        norm = normalize_title(title)
        if norm and norm != manual_norm:
            by_title.setdefault(norm, []).append(j)

    opens: list[tuple[int, bool]] = []  # per heading line: (chunk, opens it)
    pairs: list[tuple[int, int]] = []
    for k, text in enumerate(texts):
        for opening, raw in heading_lines(text, manual_title):
            line = len(opens)
            opens.append((k, opening))
            for j in sorted(by_title.get(normalize_title(raw), []), reverse=True):
                pairs.append((line, j))

    opening = {line for line, (_, first) in enumerate(opens) if first}
    own = {opens[line][0]: j for line, j in longest_in_order(pairs, opening) if opens[line][1]}
    return [BREADCRUMB_SEP.join([manual_title] + (chains[own[k]][:-1] if k in own else []))
            for k in range(len(texts))]


def is_existing_breadcrumb(line: str, manual_title: str) -> bool:
    """A breadcrumb is an italic single line starting with the manual title.

    Keying off the title rather than the "›" separator matters: chunks whose
    ancestors were all rejected get a title-only breadcrumb with no
    separator in it, and an earlier separator-based check failed to
    recognize those on a rerun and prepended a second copy every time.
    """
    s = line.strip()
    if not (s.startswith("*") and s.endswith("*") and len(s) > 2):
        return False
    return s[1:-1].strip().startswith(manual_title.strip())


# Manifest fields only the page-aware converters write, each alongside a
# breadcrumb built from better evidence than the heading walk here:
# rebuild_reference.py's `command` comes from the PDF's page map, and
# convert_docling.py's `confidence` from its bookmark TOC.
CONVERTER_BREADCRUMB_FIELDS = ("command", "confidence")


def converter_owns_breadcrumb(section: dict, manifest: dict | None = None) -> bool:
    """True if a page-aware converter wrote this chunk's breadcrumb.

    Those are always left alone, and every other breadcrumb is rewritten --
    including this script's own earlier ones, which the TOC walk exists to
    correct. The provenance is a field, not the breadcrumb's shape: a guard
    that kept any breadcrumb naming ancestors once protected `Title › command`
    from a title-only pass, and would now protect every wrong parent the old
    heading walk wrote. The field is the manifest's converter.owns_breadcrumbs;
    a manifest written before it is read by its sections' own fields.
    """
    declared = ((manifest or {}).get("converter") or {}).get("owns_breadcrumbs")
    if isinstance(declared, bool):
        return declared
    return any(f in section for f in CONVERTER_BREADCRUMB_FIELDS)


def apply_breadcrumb(text: str, crumb: str, manual_title: str) -> str:
    lines = text.splitlines()
    first = next((i for i, l in enumerate(lines) if l.strip()), None)
    if first is not None and is_existing_breadcrumb(lines[first], manual_title):
        lines[first] = f"*{crumb}*"  # refresh in place
        return "\n".join(lines).strip() + "\n"
    return f"*{crumb}*\n\n" + text.lstrip()


def process_manual(mdir: Path, dry_run: bool) -> dict:
    manifest_path = mdir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    sections = manifest["sections"]
    texts = [(mdir / s["file"]).read_text(encoding="utf-8") for s in sections]

    furniture = detect_furniture(texts, manifest["title"])

    # Without a bookmark TOC nothing can confirm an ancestor, so every chunk
    # gets the title alone: it still says which document it came from.
    toc = manifest.get("toc") or []
    crumb_mode = "walk" if toc else "title"
    crumbs = build_breadcrumbs(texts, manifest["title"], toc)

    changed = 0
    lines_dropped = 0
    removed: Counter[str] = Counter()

    for idx, (s, text) in enumerate(zip(sections, texts)):
        gone: list[str] = []
        new, dropped = strip_furniture(text, furniture, gone)
        removed.update(gone)
        lines_dropped += dropped
        # Never overwrite a breadcrumb a page-aware converter wrote.
        keep_existing = converter_owns_breadcrumb(s, manifest)
        if not keep_existing:
            new = apply_breadcrumb(new, crumbs[idx], manifest["title"])
        if new != text:
            changed += 1
            if not dry_run:
                (mdir / s["file"]).write_text(new, encoding="utf-8")
            # A length a manifest still carries from before chunks stopped
            # recording one is now wrong, and nothing reads it: the file is the copy.
            s.pop("chars", None)
        if not keep_existing:
            s["breadcrumb"] = crumbs[idx]

    if not dry_run:
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

    return {
        "slug": mdir.name,
        "chunks": len(sections),
        "changed": changed,
        "lines_dropped": lines_dropped,
        "furniture_patterns": len(furniture),
        "breadcrumbs": crumb_mode,
        "furniture_sample": sorted(furniture, key=len, reverse=True)[:4],
        "lines_removed": removed,
    }


def main() -> None:
    # These documents are full of characters like "™" and "›" that a Windows
    # console's legacy default code page (cp1252, cp950, ...) cannot encode --
    # printing a furniture sample containing one killed --dry-run mid-report.
    utf8_console()

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("vendor", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list-furniture", action="store_true",
                    help="print every distinct line deleted (or, with --dry-run, that would be), "
                    "per manual, most often first")
    ap.add_argument("--skip", action="append", default=[])
    ap.add_argument("--only", action="append", default=[])
    args = ap.parse_args()

    root = args.vendor.resolve()
    collections = editions.collections_under(root)
    if not collections:
        sys.exit(f"No docs/ under {args.vendor}")
    for vendor in collections:
        report_collection(vendor, args)


def report_collection(vendor: Path, args) -> None:
    results = []
    for doc_dir in editions.document_dirs(vendor / "docs"):     # backups and interrupted builds are not documents
        slug = doc_dir.name
        if slug in args.skip or (args.only and slug not in args.only):
            continue
        results.append(process_manual(doc_dir, args.dry_run))

    mode = "DRY RUN -- nothing written" if args.dry_run else "applied"
    print(f"{vendor.name} ({mode})\n")
    print(f"{'chunks':>7} {'changed':>8} {'lines':>7} {'crumbs':>7}  manual")
    for r in results:
        print(
            f"{r['chunks']:7d} {r['changed']:8d} {r['lines_dropped']:7d} "
            f"{r['breadcrumbs']:>7}  {r['slug']}"
        )
    print(
        f"\ntotals: {sum(r['chunks'] for r in results)} chunks, "
        f"{sum(r['changed'] for r in results)} changed, "
        f"{sum(r['lines_dropped'] for r in results)} furniture lines removed"
    )
    if args.list_furniture:
        for r in results:
            print(f"\n{r['slug']}: {len(r['lines_removed'])} distinct lines "
                  f"{'would be ' if args.dry_run else ''}deleted")
            for line, n in sorted(r["lines_removed"].items(), key=lambda kv: (-kv[1], kv[0])):
                print(f"  {n:6d}x  {line[:100]!r}")
    if args.dry_run:
        print("\nsample furniture patterns detected:")
        for r in results[:4]:
            for f in r["furniture_sample"]:
                print(f"   [{r['slug'][:22]:22s}] {f[:64]!r}")


if __name__ == "__main__":
    main()
