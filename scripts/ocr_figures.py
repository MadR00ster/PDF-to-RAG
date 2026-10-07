#!/usr/bin/env python3
"""
OCR the figures that carry no text of their own, so words written only inside
an image become searchable.

extract_figures.py records the text drawn inside each figure as `labels`. That
works for vector diagrams, whose labels are real text. A raster figure -- a
screenshot, a pasted diagram, nearly every figure in some vendors' manuals -- has
none, so a question using the words written in it cannot find it. This reads
those words with Tesseract and stores them as `ocr` in figures.json, which
build_search_db.py indexes with the section the figure illustrates.

OCR of a diagram is noisy ("x_coord1" came back "~coordl"), but search needs
only some of the words to survive, and the image an agent is shown is untouched.
See references/retrieval-measurement.md for what it bought on a real corpus.

Additive and resumable: it reads only figures with no labels and no OCR yet,
and leaves every other field of figures.json alone. extract_figures.py starts
figures.json afresh on a rerun but carries OCR over for figures that have not
moved, so run this after it.

Needs Tesseract's language data (PyMuPDF bundles the engine): set
TESSDATA_PREFIX, pass --tessdata, or install Tesseract where it usually goes.

Usage:
  python scripts/ocr_figures.py "Tessent Manual"
  python scripts/ocr_figures.py "Tessent Manual" --only atpg-gd-2025-2 --jobs 4
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import editions  # noqa: E402
from _common import utf8_console  # noqa: E402

utf8_console()

try:
    import pymupdf
except ImportError:
    sys.exit("Missing dependency. Run: pip install -r scripts/requirements.txt")

DPI = 300  # the crops are 150 dpi; Tesseract reads small diagram labels far better at 300

TESSDATA_PLACES = (
    r"C:\Program Files\Tesseract-OCR\tessdata",
    r"C:\Program Files (x86)\Tesseract-OCR\tessdata",
    "/usr/share/tesseract-ocr/5/tessdata",
    "/usr/share/tesseract-ocr/4.00/tessdata",
    "/usr/share/tessdata",
    "/opt/homebrew/share/tessdata",
    "/usr/local/share/tessdata",
)


def find_tessdata(explicit: str | None = None, language: str = "eng") -> str | None:
    """The first tessdata folder that holds the language's model, or None."""
    for place in (explicit, os.environ.get("TESSDATA_PREFIX"), *TESSDATA_PLACES):
        if place and (Path(place) / f"{language}.traineddata").is_file():
            return str(place)
    return None


def ocr_document(job: tuple) -> dict:
    doc_dir, pdf, tessdata, language = job
    doc_dir = Path(doc_dir)
    path = doc_dir / "figures.json"
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    todo = [f for f in data.get("figures", []) if not f.get("labels") and "ocr" not in f]
    result = {"slug": doc_dir.name, "read": 0, "empty": 0, "failed": 0, "secs": 0.0}
    if not todo:
        return result

    started = time.time()
    doc = pymupdf.open(pdf)
    for f in todo:
        page = doc[f["page"] - 1]
        page.remove_rotation()
        try:
            pix = page.get_pixmap(dpi=DPI, clip=pymupdf.Rect(f["bbox"]), alpha=False)
            read = pymupdf.open("pdf", pix.pdfocr_tobytes(language=language, tessdata=tessdata))
            text = " ".join(read[0].get_text().split())
        except Exception:
            # Leave "ocr" unset so a rerun retries this figure. Recording an
            # empty string would mark a figure Tesseract never managed to read
            # as read-and-blank, and every later run would skip it -- one
            # transient failure making it unsearchable for good.
            result["failed"] += 1
            continue
        f["ocr"] = text[:1000]  # an empty string records a figure with no words in it
        result["read"] += 1
        result["empty"] += not text
    doc.close()
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    result["secs"] = time.time() - started
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("collection", type=Path, help="folder holding source/ and docs/, or a corpus root of several")
    ap.add_argument("--only", action="append", default=[], help="restrict to this slug (repeatable)")
    ap.add_argument("--skip", action="append", default=[], help="exclude this slug (repeatable)")
    ap.add_argument("--jobs", type=int, default=1, help="documents to read in parallel (default 1)")
    ap.add_argument("--tessdata", help="Tesseract language-data folder (default: TESSDATA_PREFIX or the usual places)")
    ap.add_argument("--language", default="eng", help="Tesseract language (default eng)")
    args = ap.parse_args()

    tessdata = find_tessdata(args.tessdata, args.language)
    if not tessdata:
        sys.exit(f"No Tesseract language data for '{args.language}' found. Install Tesseract, then set "
                 "TESSDATA_PREFIX to its tessdata folder or pass --tessdata.")

    root = args.collection.resolve()
    jobs = []
    for coll in editions.collections_under(root):
        for doc_dir in editions.document_dirs(coll / "docs"):
            fig_path, slug = doc_dir / "figures.json", doc_dir.name
            if not fig_path.is_file():
                continue
            if slug in args.skip or (args.only and slug not in args.only):
                continue
            manifest = json.loads((fig_path.parent / "manifest.json").read_text(encoding="utf-8-sig"))
            pdf = editions.find_source_pdf(coll, manifest.get("source_pdf"))
            if pdf is None:
                print(f"  !! {slug}: source PDF is in neither {editions.SOURCE_DIR}/ nor {coll.name}/ -- skipped")
                continue
            jobs.append((str(fig_path.parent), str(pdf), tessdata, args.language))
    if not jobs:
        sys.exit(f"No figures.json under {root / 'docs'} -- run extract_figures.py first.")

    print(f"{root.name}: OCR of figures without text, {len(jobs)} document(s)", flush=True)
    totals = {"read": 0, "empty": 0, "failed": 0}

    def report(r: dict) -> None:
        for key in totals:
            totals[key] += r[key]
        note = f"  !! {r['failed']} failed" if r["failed"] else ""
        print(f"  {r['slug'][:40]:40} {r['read']:>5} read, {r['empty']:>4} with no text  "
              f"{r['secs']:5.0f}s{note}", flush=True)

    if args.jobs > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(min(args.jobs, len(jobs))) as pool:
            for fut in as_completed([pool.submit(ocr_document, j) for j in jobs]):
                report(fut.result())
    else:
        for j in jobs:
            report(ocr_document(j))
    print(f"\n{totals['read']} figure(s) read, {totals['empty']} with no text found. "
          "Rebuild the index with build_search_db.py to search it.")
    if totals["failed"]:
        print(f"!! Tesseract failed on {totals['failed']} figure(s). They keep no `ocr` field, so "
              "rerunning retries exactly those; the pass is incomplete until it exits 0.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
