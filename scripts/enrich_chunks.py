#!/usr/bin/env python3
"""
Post-process existing docs/<slug>/sections/*.md chunks in place:

  1. Strip page furniture -- the running title / page number / "Feedback" /
     "Chapter N: ..." block that the PDF's header+footer injects at every
     page boundary, often mid-sentence.
  2. Prepend a breadcrumb line ("Manual > Chapter > Section") so a chunk
     retrieved on its own still says where it came from.

Both passes are idempotent: rerunning makes no further change.

Breadcrumbs are only added to manuals whose heading levels form a usable
hierarchy. In a flat manual (notably tshell-ref, where 95% of chunks share
one level) there is nothing to walk, and a guessed parent would be worse
than none -- see references/failure-modes.md for why a wrong parent is
actively dangerous in a command reference.

Usage:
  python scripts/enrich_chunks.py "Synopsys Manual" --dry-run
  python scripts/enrich_chunks.py "Synopsys Manual"
  python scripts/enrich_chunks.py "Tessent Manual" --skip tshell-ref-2026-2

Options:
  --dry-run     report what would change; write nothing
  --skip SLUG   exclude a manual (repeatable); use for manuals queued for a
                full page-accurate reconversion, which redoes this anyway
  --only SLUG   restrict to one manual (repeatable)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

# A manual gets breadcrumbs only if fewer than this share of its chunks sit
# at a single heading level; above it, the "hierarchy" is an artifact.
MAX_FLATNESS = 0.50

BREADCRUMB_SEP = " › "  # single right-pointing angle quote
BREADCRUMB_RE = re.compile(r"^\*[^*\n]*›[^*\n]*\*\s*$")

FENCE_RE = re.compile(r"^\s*```")
BARE_NUM_RE = re.compile(r"^\s*\d{1,4}\s*$")
CHAPTER_HDR_RE = re.compile(r"^\s*(Chapter|Appendix|Section)\s+\w{1,4}\s*:", re.I)

# How close a bare page number / running chapter header must sit to a
# frequency-detected furniture line to be treated as furniture too.
ADJACENCY = 3


def strip_emphasis(line: str) -> str:
    s = line.strip()
    s = re.sub(r"^#{1,6}\s*", "", s).strip()
    for _ in range(3):
        s2 = re.sub(r"^(\*\*|__|\*|_|`)(.*?)\1$", r"\2", s.strip())
        if s2 == s:
            break
        s = s2
    return s.strip()


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


def detect_furniture(section_texts: list[str], min_count: int, title: str) -> set[str]:
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
    # "any line repeated >= min_count times" as furniture, which deleted
    # genuine prose that reference manuals simply reuse a lot -- "Note the
    # following:" (127x), "where valid values are as follows:" (175x). A
    # missed furniture line costs a few wasted tokens; a deleted content
    # line is unrecoverable without reconverting the PDF. So the only things
    # that qualify are the "Feedback" link and the running title footer.
    furniture = set()
    for line, n in counts.items():
        norm = re.sub(r"\s+", " ", line).strip().lower()
        is_title_line = title_core and (
            norm.startswith(title_core[:40]) or title_core.startswith(norm[:40])
        )
        if norm == "feedback" or (is_title_line and n >= 3):
            furniture.add(line)
    return furniture


def strip_furniture(text: str, furniture: set[str]) -> tuple[str, int]:
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

    kept = [ln for i, ln in enumerate(lines) if not drop[i]]
    out = "\n".join(kept)
    out = re.sub(r"\n{3,}", "\n\n", out).strip() + "\n"
    return out, sum(drop)


def toc_titles(toc: list[dict]) -> set[str]:
    """Normalized set of the PDF's own bookmark titles -- the one piece of
    genuinely authoritative structure available."""
    out = set()
    for t in toc or []:
        n = normalize_title(t.get("title") or "")
        if n:
            out.add(n)
    return out


def normalize_title(s: str) -> str:
    s = re.sub(r"\s+", " ", strip_emphasis(s)).strip()
    s = re.sub(r"^\d+(\.\d+)*\s*", "", s)  # drop leading chapter numbering
    return s.lower()


def build_breadcrumbs(sections: list[dict], manual_title: str, toc: set[str]) -> list[str]:
    """Walk sections in order maintaining a level stack, so each chunk gets
    'Manual > ancestor > ancestor'. Ancestors only -- the chunk's own heading
    is already the first line of its body.

    Only headings that appear in the PDF's bookmark TOC may become
    ancestors. The font-size heuristic happily promotes a procedure step
    ("Specify the libraries") or a stray running footer to a shallow level,
    and those then masquerade as chapters. Cross-checking against the TOC
    keeps roughly the half of headings that are real structure and discards
    the invented ones, so a breadcrumb is either right or absent.
    """
    crumbs = []
    manual_norm = normalize_title(manual_title)
    stack: list[tuple[int, str]] = []
    for s in sections:
        level = s.get("level") or 0
        heading = strip_emphasis(s.get("heading") or "")
        norm = normalize_title(heading)
        while stack and stack[-1][0] >= level:
            stack.pop()
        parts = [manual_title] + [h for _, h in stack]
        crumbs.append(BREADCRUMB_SEP.join(parts))
        # Only TOC-verified, non-self-referential headings become ancestors.
        if norm and norm in toc and norm != manual_norm and "(intro)" not in heading:
            stack.append((level, heading))
    return crumbs


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


def apply_breadcrumb(text: str, crumb: str, manual_title: str) -> str:
    lines = text.splitlines()
    first = next((i for i, l in enumerate(lines) if l.strip()), None)
    if first is not None and is_existing_breadcrumb(lines[first], manual_title):
        lines[first] = f"*{crumb}*"  # refresh in place
        return "\n".join(lines).strip() + "\n"
    return f"*{crumb}*\n\n" + text.lstrip()


def flatness(sections: list[dict]) -> float:
    if not sections:
        return 1.0
    lv = Counter(s.get("level") or 0 for s in sections)
    return lv.most_common(1)[0][1] / len(sections)


def process_manual(mdir: Path, dry_run: bool) -> dict:
    manifest_path = mdir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    sections = manifest["sections"]
    texts = [(mdir / s["file"]).read_text(encoding="utf-8") for s in sections]

    min_count = max(10, int(0.05 * len(sections)))
    furniture = detect_furniture(texts, min_count, manifest["title"])

    do_crumbs = flatness(sections) < MAX_FLATNESS
    crumbs = (
        build_breadcrumbs(sections, manifest["title"], toc_titles(manifest.get("toc")))
        if do_crumbs
        else None
    )

    changed = 0
    lines_dropped = 0
    chars_before = sum(len(t) for t in texts)
    samples: list[str] = []

    for idx, (s, text) in enumerate(zip(sections, texts)):
        new, dropped = strip_furniture(text, furniture)
        lines_dropped += dropped
        if crumbs:
            new = apply_breadcrumb(new, crumbs[idx], manifest["title"])
        if new != text:
            changed += 1
            if not dry_run:
                (mdir / s["file"]).write_text(new, encoding="utf-8")
            s["chars"] = len(new)
        if crumbs:
            s["breadcrumb"] = crumbs[idx]
        if len(samples) < 3 and dropped:
            samples.append(s["file"])

    chars_after = chars_before - 0
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
        "breadcrumbs": bool(crumbs),
        "flatness": flatness(sections),
        "furniture_sample": sorted(furniture, key=len, reverse=True)[:4],
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("vendor", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip", action="append", default=[])
    ap.add_argument("--only", action="append", default=[])
    args = ap.parse_args()

    docs = args.vendor.resolve() / "docs"
    if not docs.is_dir():
        sys.exit(f"No docs/ under {args.vendor}")

    results = []
    for manifest in sorted(docs.glob("*/manifest.json")):
        slug = manifest.parent.name
        if slug in args.skip or (args.only and slug not in args.only):
            continue
        results.append(process_manual(manifest.parent, args.dry_run))

    mode = "DRY RUN -- nothing written" if args.dry_run else "applied"
    print(f"{args.vendor.name} ({mode})\n")
    print(f"{'chunks':>7} {'changed':>8} {'lines':>7} {'crumbs':>7}  manual")
    for r in results:
        print(
            f"{r['chunks']:7d} {r['changed']:8d} {r['lines_dropped']:7d} "
            f"{'yes' if r['breadcrumbs'] else 'no':>7}  {r['slug']}"
        )
    print(
        f"\ntotals: {sum(r['chunks'] for r in results)} chunks, "
        f"{sum(r['changed'] for r in results)} changed, "
        f"{sum(r['lines_dropped'] for r in results)} furniture lines removed"
    )
    if args.dry_run:
        print("\nsample furniture patterns detected:")
        for r in results[:4]:
            for f in r["furniture_sample"]:
                print(f"   [{r['slug'][:22]:22s}] {f[:64]!r}")


if __name__ == "__main__":
    main()
