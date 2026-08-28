#!/usr/bin/env python3
"""Pre-flight: inspect a PDF and recommend how to convert it.

Every signal here is read from the PDF itself in a second or two, before any
extraction runs, so a corpus can be triaged up front instead of discovered the
hard way. Measurements behind the thresholds are in
references/extractor-benchmark.md.

What it decides, and how confidently:

  * Which converter -- from document shape. Well evidenced: prose and reference
    documents scored oppositely on the benchmark, by wide margins.
  * How much of the result will be trustworthy -- from bookmark density and how
    often two sections start on one page. Directional, fitted to seven slices;
    treat as a band, not a number.

It does not open the extractor or guess at content. When a signal is missing
(no bookmarks, no text layer) it says so rather than recommending anyway.

Usage:
  python scripts/pick_extractor.py corpus/*.pdf
  python scripts/pick_extractor.py manual.pdf --verbose
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:  # older wheels
    try:
        import fitz
    except ImportError:
        sys.exit("Missing dependency. Run: pip install -r scripts/requirements.txt")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# A title that looks like a command / API entry rather than a prose heading.
# Same shape of test rebuild_reference.py uses to find its command level.
IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]*$")

DOCLING_SECONDS_PER_PAGE = 1.2   # measured, CPU, no OCR


def looks_like_entry(title: str) -> bool:
    t = (title or "").strip()
    if not t or " " in t and not t.split()[0].endswith(("_",)):
        # Allow "tessent -shell" style two-token entries, reject prose.
        parts = t.split()
        if not (len(parts) == 2 and parts[1].startswith("-")):
            return "_" in t and IDENTIFIER_RE.match(t.split()[0] or "") is not None
    return bool(IDENTIFIER_RE.match(t)) and ("_" in t or t.islower())


# A dictionary is substantially *made of* entries, so they recur every few
# pages. Without this, a prose manual with one appendix listing 22 option names
# at L6 was classified as a reference -- 22 entries across 1,648 pages.
MIN_ENTRIES_PER_PAGE = 0.10


def detect_shape(toc, pages: int) -> tuple[str, str]:
    """Reference/dictionary if some TOC level is mostly identifier-like titles.

    Keyed on a level rather than the whole TOC because a dictionary keeps its
    entries at one depth (tshell-ref at L3, syn2 at L1) under prose chapter
    headings that would otherwise dilute the signal. Density then separates a
    real dictionary from a prose manual that happens to list some identifiers.
    """
    by_level: dict[int, list[str]] = {}
    for lvl, title, _page in toc:
        by_level.setdefault(lvl, []).append(title or "")
    best = None
    for lvl, titles in sorted(by_level.items()):
        n = sum(1 for t in titles if looks_like_entry(t))
        if n >= 20 and n >= 0.3 * len(titles):
            if best is None or n > best[1]:
                best = (lvl, n, len(titles))
    if not best:
        return "prose", "no level is dominated by identifier-like titles"

    lvl, n, total = best
    per_page = n / max(pages, 1)
    if per_page < MIN_ENTRIES_PER_PAGE:
        return "prose", (f"L{lvl} has {n} identifier-like titles but only "
                         f"{per_page:.3f}/page -- a list inside a prose manual")
    if per_page < 0.15:
        return "mixed", (f"L{lvl}: {n}/{total} titles look like entries, {per_page:.2f}/page")
    return "reference", f"L{lvl}: {n}/{total} titles look like entries, {per_page:.2f}/page"


def anchoring_band(density: float) -> tuple[str, str]:
    if density < 0.5:
        return "low", "~15-25% of chunks anchor; most ancestors come from the fallback"
    if density < 1.0:
        return "moderate", "~50% anchor"
    return "high", "~60-88% anchor"


def fallback_band(multi_share: float) -> tuple[str, str]:
    if multi_share < 0.10:
        return "strong", "~91-99% correct"
    if multi_share < 0.40:
        return "good", "~82-93% correct"
    return "weak", "~66-85% correct -- prefer not to lean on it"


def has_text_layer(doc, sample: int = 10) -> tuple[bool, int]:
    pages = range(0, doc.page_count, max(1, doc.page_count // sample))
    counts = []
    for i in list(pages)[:sample]:
        try:
            counts.append(len(doc[i].get_text().strip()))
        except Exception:
            counts.append(0)
    avg = sum(counts) // max(len(counts), 1)
    return avg >= 100, avg


def report(path: Path, verbose: bool) -> None:
    try:
        doc = fitz.open(path)
    except Exception as exc:
        print(f"{path.name}: cannot open ({exc})")
        return

    toc = doc.get_toc()
    pages = doc.page_count
    text_ok, avg_chars = has_text_layer(doc)

    # Benchmark slices are all named slice.pdf; show the parent when
    # the basename alone would not identify the file.
    label = f"{path.parent.name}/{path.name}" if path.name == "slice.pdf" else path.name
    print(f"\n{label}")
    print(f"  {pages:,} pages · {len(toc):,} TOC entries · "
          f"{len(set(l for l, _, _ in toc)) if toc else 0} levels · "
          f"{'text layer ok' if text_ok else f'NO TEXT LAYER (~{avg_chars} chars/page)'}")

    if not text_ok:
        print("  => Scanned or image-only. OCR first; neither extractor produces")
        print("     anything useful until there is a text layer.")
        doc.close()
        return

    if not toc:
        print("  => No bookmark outline. TOC verification is unavailable, so")
        print("     breadcrumbs cannot be confirmed by any path here. Docling still")
        print("     gives page numbers; treat every ancestor as unverified, and")
        print("     consider deriving structure from the printed contents pages.")
        doc.close()
        return

    shape, why = detect_shape(toc, pages)
    density = len(toc) / max(pages, 1)
    starts = Counter(p for _, _, p in toc)
    multi = sum(1 for _p, c in starts.items() if c >= 2)
    multi_share = multi / max(len(starts), 1)

    print(f"  shape: {shape:10s} ({why})")

    if shape == "mixed":
        print("  => Mixed: a prose manual with a substantial entry section.")
        print("     Inspect before converting. If the entries sit in their own")
        print("     chapters, convert those with rebuild_reference.py and the rest")
        print("     as prose, rather than forcing one path over the whole document.")
        doc.close()
        return

    if shape == "reference":
        print("  => rebuild_reference.py")
        print(f"     Measured 99-100% entity attribution; anything derived from a")
        print(f"     structure model's headings managed 16-62% on the same documents.")
    else:
        a_band, a_note = anchoring_band(density)
        f_band, f_note = fallback_band(multi_share)
        print(f"  bookmark density: {density:.2f}/page -> anchoring {a_band} ({a_note})")
        print(f"  pages starting 2+ sections: {multi_share * 100:.0f}% -> fallback {f_band} ({f_note})")
        print(f"  => convert_docling.py  (Docling + TOC-anchored breadcrumbs)"
              f"{'  (but mark fallback ancestors clearly -- most chunks use them)' if a_band == 'low' else ''}")
        est = pages * DOCLING_SECONDS_PER_PAGE
        print(f"     est. {est / 60:.0f} min at ~{DOCLING_SECONDS_PER_PAGE}s/page"
              f"{'  -- consider pymupdf4llm if throughput matters more than pages' if est > 3600 else ''}")

    if verbose:
        lv = Counter(l for l, _, _ in toc)
        print(f"     TOC levels: {dict(sorted(lv.items()))}")
        print(f"     producer: {doc.metadata.get('producer') or '?'}")
    doc.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdfs", nargs="+", type=Path)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    files = [p for p in args.pdfs if p.is_file()]
    if not files:
        print("No readable PDFs given.", file=sys.stderr)
        return 1
    for p in files:
        report(p, args.verbose)
    print(f"\n{len(files)} PDF(s). Thresholds come from seven slices across five PDF")
    print("producers -- see references/extractor-benchmark.md. Check a sample of your")
    print("own corpus rather than trusting the bands wholesale.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
