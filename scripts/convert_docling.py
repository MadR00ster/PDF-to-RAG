#!/usr/bin/env python3
"""Convert a prose PDF with Docling, anchoring breadcrumbs to the bookmark TOC.

Same output layout as convert_manual.py -- docs/<slug>/{manifest.json, full.md,
sections/NNN-heading-slug.md} -- so build_index.py and build_search_db.py work
on it unchanged. What differs is the metadata each chunk carries:

  * page_start / page_end on every chunk, from Docling's provenance. The
    pymupdf4llm prose path carries none, and without pages a breadcrumb cannot
    be audited positionally at all.
  * breadcrumb built from the PDF's bookmark TOC rather than from detected
    headings, so an artifact like "Note:" or a shell transcript line can never
    become an ancestor. Docling offers those as headings; measurements are in
    references/extractor-benchmark.md.
  * confidence, saying how the ancestors were derived (see below).

For reference/dictionary documents use rebuild_reference.py instead -- it
attributes 99-100% of chunks to the right entry where anything derived from a
structure model's headings managed 16-62%. `pick_extractor.py` tells you which
shape you have.

How ancestors are resolved, and why there are two confidences:

  anchored  The chunk's own heading is confirmed by a TOC entry whose pages
            overlap the chunk's, so we know exactly which entry it belongs to
            and walk that entry's level stack. A same-titled entry elsewhere
            confirms nothing.
  page      The heading is unconfirmed (a real sub-bookmark heading, or an
            artifact). Fall back to the heading stack in force at the chunk's
            position. Held out, that is 60.5% correct at page granularity and
            82.9% once the chunk and the headings are located by y-coordinate
            on the page, which is what this does. Marked so a consumer can
            weigh it rather than trusting it equally.

Docling is NOT in requirements.txt: it is a multi-gigabyte install and runs at
~1.2s/page. Install it only for corpora that earn it.

Usage:
  python scripts/convert_docling.py manual.pdf --title "Widget User Guide"
  python scripts/convert_docling.py manual.pdf --title "..." --slug widget-2026
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

try:
    import pymupdf
except ImportError:
    sys.exit("Missing dependency. Run: pip install -r scripts/requirements.txt")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import editions  # noqa: E402

BREADCRUMB_SEP = " › "
DEFAULT_MAX_CHARS = 9000        # matches the chunk target used elsewhere here
WS = re.compile(r"\s+")
NL = chr(10)   # written this way so patch tooling cannot mangle an escape


def require_docling():
    try:
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.chunking import HybridChunker
    except ImportError:
        sys.exit(
            "Docling is not installed, and is deliberately not in requirements.txt\n"
            "(multi-gigabyte install, ~1.2s/page). Install it with:\n\n"
            "    pip install docling\n\n"
            "Or convert with the light path instead:\n"
            "    python scripts/convert_manual.py <pdf> --title ...\n"
            "Run scripts/pick_extractor.py <pdf> to see which one this document wants."
        )
    return DocumentConverter, PdfFormatOption, InputFormat, PdfPipelineOptions, HybridChunker


def normalize(s: str) -> str:
    s = re.sub(r"^#{1,6}\s*", "", (s or "").strip())
    for _ in range(3):
        t = re.sub(r"^(\*\*|__|\*|_|`)(.*?)\1$", r"\2", s.strip())
        if t == s:
            break
        s = t
    s = WS.sub(" ", s).strip()
    s = re.sub(r"^(chapter|appendix|section)\s+\w{1,4}\s*[:.]?\s*", "", s, flags=re.I)
    s = re.sub(r"^\d+(\.\d+)*\s*", "", s)
    return s.lower().strip()


def slugify(text: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^0-9A-Za-z]+", "-", normalize(text)).strip("-").lower()
    return (s[:maxlen].rstrip("-") or "section")


def dedupe(slug: str, seen: dict) -> str:
    seen[slug] = seen.get(slug, 0) + 1
    return slug if seen[slug] == 1 else f"{slug}-{seen[slug]}"


def locate(page, needle: str):
    """Topmost y of `needle` on `page`, or None. Used to place headings and
    chunks on the page so a mid-page section change resolves correctly."""
    if not needle:
        return None
    for probe in (needle, needle[:24], needle[:14]):
        try:
            hits = page.search_for(probe)
        except Exception:
            hits = None
        if hits:
            return min(r.y0 for r in hits)
    return None


def probe_text(s: str, n: int = 40) -> str:
    return WS.sub(" ", re.sub(r"[#*`_|>\\]", " ", s)).strip()[:n]


def build_toc_positions(doc, toc):
    """(page, y, level, title) for each TOC entry, y from the page itself."""
    out = []
    for lvl, title, page in toc:
        y = None
        if 1 <= page <= doc.page_count:
            y = locate(doc[page - 1], WS.sub(" ", title).strip())
        out.append((page, 0.0 if y is None else y, lvl, title))
    return out


def chain_for_entry(toc, index):
    """Ancestor chain of toc[index], itself last."""
    stack = []
    for i, (lvl, title, _page) in enumerate(toc):
        while stack and stack[-1][0] >= lvl:
            stack.pop()
        stack.append((lvl, title))
        if i == index:
            return [t for _, t in stack]
    return []


def chain_at(toc_pos, page, y):
    """Heading stack in force at (page, y). A heading below the chunk on the
    same page has not taken effect yet."""
    stack = []
    for p, ty, lvl, title in toc_pos:
        if p > page:
            break
        if p == page and y is not None and ty > y:
            continue
        while stack and stack[-1][0] >= lvl:
            stack.pop()
        stack.append((lvl, title))
    return [t for _, t in stack]


def toc_spans(toc, page_count):
    """(first page, last page) each TOC entry covers: from its own page to the
    page where the next entry at its level or shallower starts, inclusive,
    since that entry may start mid-page."""
    spans = []
    for i, (lvl, _title, page) in enumerate(toc):
        if page < 1:                  # a bookmark with no target covers nothing
            spans.append((page_count + 1, 0))
            continue
        end = next((p for l, _t, p in toc[i + 1:] if l <= lvl and p >= 1), page_count)
        spans.append((page, max(page, end)))
    return spans


def resolve_ancestors(c, toc, toc_pos, by_title, spans, doc):
    """Ancestors for one Docling chunk, plus how confidently they were derived.

    A heading matching a TOC title anchors the chunk only to an entry whose
    pages overlap the chunk's. Titles repeat -- "Syntax" and "Examples" under
    every construct, a feature named in an overview chapters before its own --
    and taking the nearest match regardless labelled a page-180 chunk with a
    section on page 190, and a page-83 overview with chapter 5 on page 150,
    both as `anchored`: the confidence consumers trust most.
    """
    pages = sorted(c["pages"])
    first = pages[0] if pages else None
    norm = normalize((c["headings"] or [""])[0])

    if norm and norm in by_title and first is not None:
        last = pages[-1]
        fits = [j for j in by_title[norm] if spans[j][0] <= last and first <= spans[j][1]]
        if fits:
            best = min(fits, key=lambda j: abs(toc[j][2] - first))
            return chain_for_entry(toc, best)[:-1], "anchored"
    if first is not None and 1 <= first <= doc.page_count:
        y = locate(doc[first - 1], probe_text(c["text"]))
        return chain_at(toc_pos, first, y), "page"
    return [], "none"


def common_lineage(a, b):
    """The chain to label a merged chunk with, or None if these do not belong
    together.

    Requiring identical chains is too strict, and fails on exactly the
    documents that need merging most. Docling's heading flips between a section
    and its sub-headings as it walks a page, so consecutive chunks of one
    section arrive with chains of different depth:

        ... > Entering PrimeLib Commands > Specifying Cells
        ... > Entering PrimeLib Commands
        ... > Entering PrimeLib Commands > Command Help

    Measured on a 6-level, densely bookmarked manual: only 4% of consecutive
    pairs had identical chains, while 67% were same-lineage and 10% siblings --
    so the strict rule blocked 77% of legitimate merges and left 71% of chunks
    under 1 KB.

    Merge when one chain continues the other, or when they differ only in their
    final element, and label the result with what they share. The merged chunk
    then claims only ancestry that is true of all of it, and the sub-headings
    it spans are written into the body.
    """
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    if n == min(len(a), len(b)):        # one continues the other
        return a[:n]
    if len(a) == len(b) and n == len(a) - 1:   # siblings under one parent
        return a[:n]
    return None


def merge_sections(resolved, max_chars):
    """Merge consecutive chunks that belong to the same section.

    Docling's HybridChunker is bounded by a 512-token tokenizer, so it emits
    ~500-char pieces -- far under the size the rest of this skill targets, and
    a corpus mixing 500-char and 9,000-char chunks retrieves unevenly.

    Confidence is merged pessimistically: a chunk whose ancestry is partly
    page-derived is labelled "page", because part of what it claims was not
    confirmed by the TOC. Blocking the merge on mismatched confidence instead
    would re-break the oscillating case, since the depth flip usually flips the
    confidence with it.
    """
    merged, buf = [], None
    for r in resolved:
        head = (r["headings"] or [""])[0]
        lineage = common_lineage(buf["ancestors"], r["ancestors"]) if buf else None
        if (lineage is not None
                and len(buf["text"]) + len(r["text"]) + 80 <= max_chars):
            if head and head != buf["last_heading"]:
                buf["text"] += "\n\n### " + head + "\n\n" + r["text"]
                buf["last_heading"] = head
            else:
                buf["text"] += "\n\n" + r["text"]
            buf["pages"] |= set(r["pages"])
            buf["ancestors"] = lineage
            if r["confidence"] != "anchored":
                buf["confidence"] = r["confidence"]
            continue
        if buf:
            merged.append(buf)
        buf = {"heading": head, "last_heading": head, "text": r["text"],
               "pages": set(r["pages"]), "ancestors": r["ancestors"],
               "confidence": r["confidence"]}
    if buf:
        merged.append(buf)
    return merged


def convert(plan: editions.Plan, title: str, max_chars: int) -> None:
    pdf_path, slug, out_root = plan.pdf, plan.slug, plan.out_root
    (DocumentConverter, PdfFormatOption, InputFormat,
     PdfPipelineOptions, HybridChunker) = require_docling()

    opts = PdfPipelineOptions()
    opts.do_ocr = False               # native text layer; OCR only adds time
    opts.do_table_structure = True
    conv = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})

    print(f"Extracting {pdf_path.name} with Docling (~1.2s/page) ...")
    result = conv.convert(str(pdf_path))
    dl_doc = result.document

    raw = []
    for ch in HybridChunker().chunk(dl_doc):
        pages = set()
        for item in getattr(ch.meta, "doc_items", None) or []:
            for prov in getattr(item, "prov", None) or []:
                if getattr(prov, "page_no", None) is not None:
                    pages.add(int(prov.page_no))
        raw.append({"text": ch.text,
                    "headings": list(getattr(ch.meta, "headings", None) or []),
                    "pages": sorted(pages)})

    doc = pymupdf.open(str(pdf_path))
    toc = doc.get_toc()
    toc_pos = build_toc_positions(doc, toc)
    by_title: dict[str, list[int]] = {}
    for i, (_lvl, t, _p) in enumerate(toc):
        by_title.setdefault(normalize(t), []).append(i)

    spans = toc_spans(toc, doc.page_count)
    for c in raw:
        c["ancestors"], c["confidence"] = resolve_ancestors(c, toc, toc_pos, by_title, spans, doc)

    chunks = merge_sections(raw, max_chars)
    print(f"Merged {len(raw)} Docling chunks into {len(chunks)} sections "
          f"(target {max_chars:,} chars)")

    out_dir = out_root / slug
    sections_dir = out_dir / "sections"
    sections_dir.mkdir(parents=True, exist_ok=True)

    seen: dict = {}
    entries, anchored, fell_back = [], 0, 0
    full_parts = []

    for i, c in enumerate(chunks, start=1):
        pages = sorted(c["pages"])
        anchored += c["confidence"] == "anchored"
        fell_back += c["confidence"] == "page"

        crumb = BREADCRUMB_SEP.join([title] + c["ancestors"])
        body = "*" + crumb + "*" + NL + NL + c["text"].strip() + NL
        filename = f"{i:04d}-{dedupe(slugify(c['heading'] or 'section'), seen)}.md"
        (sections_dir / filename).write_text(body, encoding="utf-8")
        full_parts.append(body)

        entries.append({
            "file": f"sections/{filename}",
            "heading": c["heading"] or "(untitled)",
            "level": 1,
            "chars": len(body),
            "page_start": pages[0] if pages else None,
            "page_end": pages[-1] if pages else None,
            "breadcrumb": crumb,
            "confidence": c["confidence"],
        })

    full_md = "\n\n".join(full_parts)
    (out_dir / "full.md").write_text(full_md, encoding="utf-8")

    manifest = editions.with_edition_fields({
        "source_pdf": plan.source_name,
        "title": title,
        "slug": slug,
        "page_count": doc.page_count,
        "extractor": "docling",
        "toc": [{"level": lvl, "title": t.strip(), "page": pg} for lvl, t, pg in toc],
        "sections": entries,
        "full_md_chars": len(full_md),
    }, plan.doc_id, plan.version)
    # ensure_ascii=True: a non-UTF-8 default locale would otherwise mangle this
    # for any tool that opens it without an explicit encoding= argument.
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    n = len(entries) or 1
    with_pages = sum(1 for e in entries if e["page_start"] is not None)
    print(f"Wrote {len(entries)} sections to {out_dir}")
    print(f"  pages on {with_pages}/{len(entries)} chunks ({with_pages / n * 100:.0f}%)")
    print(f"  breadcrumbs: {anchored} anchored ({anchored / n * 100:.0f}%), "
          f"{fell_back} page-fallback ({fell_back / n * 100:.0f}%)")
    if not toc:
        print("  !! no bookmark TOC: every ancestor is unverified")
    doc.close()
    editions.finish(plan)
    print(f'Next: python scripts/build_index.py "{plan.collection}"')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--title", required=True, help="Title as it should appear in index.json/README.md")
    ap.add_argument("--slug", help="docs/<slug> folder name (default: from the filename)")
    ap.add_argument("--out-root", type=Path,
                    help="Where to write docs/<slug>/ (default: docs/ in the PDF's collection)")
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                    help=f"Chunk size target when merging (default {DEFAULT_MAX_CHARS})")
    editions.add_arguments(ap)
    args = ap.parse_args()

    if not args.pdf.is_file():
        sys.exit(f"No such PDF: {args.pdf}")
    plan = editions.plan(args.pdf, args.slug, args.out_root, args.version, args.doc_id)
    if (plan.out_root / plan.slug).exists():
        sys.exit(f"{plan.out_root / plan.slug} already exists -- pick a different --slug or remove it first")

    convert(plan, args.title, args.max_chars)
    return 0


if __name__ == "__main__":
    sys.exit(main())
