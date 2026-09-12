#!/usr/bin/env python3
"""
Extract figures from each converted document's source PDF, so an agent can
look at a diagram instead of reading the label soup it became in the text.

For every docs/<slug>/ in a collection folder, this opens the original PDF
(the corpus layout keeps it beside docs/), finds figures with the layout model
pymupdf4llm itself uses, and writes:

  docs/<slug>/figures/pNNNNN-K.png   one crop per figure
  docs/<slug>/figures.json           page, box, caption, the text drawn inside
                                     the figure, and the section it belongs to

Section files and manifest.json are never touched, so this runs on an existing
corpus without reconverting anything. build_search_db.py reads figures.json and
mcp_server.py serves the crops through get_figure.

A figure is attached to a section by, in order of trust:

  caption  Its own caption ("Figure 62. Tri-State Bus Contention") appears as a
           line of that section. The List of Figures never matches: its rows
           carry dot leaders and page numbers.
  context  No caption, but the paragraph just above it on the page is found in
           a section shortly after the previous figure's, or else in exactly
           one section of the whole document.
  (none)   Neither. The crop is still written and served by page, just not
           attached to a section -- a guessed section would present the diagram
           as illustrating text it does not illustrate.

The context method is checked on every figure where both apply: the summary
reports how often it picks the same section the caption does, which is the
estimate of how far to trust the figures that only have context.

Detection keeps what the model calls a picture, refined the way pymupdf4llm
refines it, minus what is too small to be a figure (note icons, rules) and what
sits uncaptioned in the same place on many pages (logos). The model costs
~0.2 s a page, so pages with no sizeable image and no drawing in their body are
skipped without running it.

Usage:
  python scripts/extract_figures.py "Tessent Manual" --dry-run
  python scripts/extract_figures.py "Tessent Manual" --only atpg-gd-2025-2
  python scripts/extract_figures.py "Synopsys Manual" --jobs 4
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

try:
    import pymupdf
except ImportError:
    pymupdf = None
if pymupdf is None or importlib.util.find_spec("pymupdf.layout") is None:
    sys.exit("Figure extraction needs PyMuPDF's layout model, which pymupdf4llm >= 1.28\n"
             "installs. Run: pip install -r scripts/requirements.txt")

# Set by prepare(), which loads the layout model: importing it any earlier would
# create its sessions before their threads can be capped.
p4l_utils, TEXT_FLAGS = None, 0


def limit_onnx_threads(threads: int) -> None:
    """Cap the layout model's ONNX Runtime threads and stop idle ones spinning.

    pymupdf creates the model's sessions with ONNX Runtime's defaults: a thread
    per core, each busy-waiting between inferences. That suits one process.
    Several processes each claim every core and fight over them -- the same
    780-page guide took 283 s alone and 1,830 s as one of four jobs, slower in
    total than running them one at a time. Capped, a 586-page guide that took
    1,552 s as one of four uncapped jobs took 82 s as one of three. pymupdf
    exposes no setting, so this wraps the session constructor; if ONNX Runtime
    changes shape it does nothing.
    """
    try:
        import onnxruntime as ort
    except ImportError:
        return
    original = ort.InferenceSession
    if getattr(original, "threads_capped", False):
        return

    def capped(path_or_bytes, sess_options=None, providers=None, *args, **kwargs):
        options = sess_options or ort.SessionOptions()
        try:
            options.intra_op_num_threads = threads
            options.inter_op_num_threads = 1
            options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        except Exception:
            pass
        return original(path_or_bytes, options, providers, *args, **kwargs)

    capped.threads_capped = True
    ort.InferenceSession = capped


_prepared = False


def prepare(threads: int) -> None:
    """Load the layout model once per process, capping its threads first.

    Its ONNX sessions are created the moment pymupdf.layout is imported -- and
    importing pymupdf4llm imports it too -- so the cap has to be in place before
    either import, not merely before the model first runs. A cap installed after
    a top-level import silently does nothing.
    """
    global _prepared, p4l_utils, TEXT_FLAGS
    if _prepared:
        return
    if threads:
        limit_onnx_threads(threads)
    import pymupdf.layout
    pymupdf.layout.activate()
    try:  # pymupdf4llm's refinement of the model's boxes: a nicety, not a dependency
        from pymupdf4llm.helpers import utils
        from pymupdf4llm.helpers.document_layout import FLAGS
        p4l_utils, TEXT_FLAGS = utils, FLAGS
    except ImportError:
        pass
    _prepared = True


DPI = 150
MAX_PX = 1568        # long edge: under every current model's native limit, so no
                     # client downscales a crop and blurs its labels
MIN_SIDE = 36        # pt. Note icons are ~15 pt, horizontal rules ~3 pt tall
MIN_AREA = 4000      # pt^2
PAD = 4              # pt of margin kept around a crop
CAPTION_GAP = 90     # pt a caption may sit above or below its figure
CONTEXT_WINDOW = 6   # sections after the previous figure's to look for context
REPEAT_SHARE = 0.05  # uncaptioned boxes this common in one place are furniture
MARGIN_BAND = 0.1    # top and bottom share of a page, where running headers live

CAPTION_RE = re.compile(r"^\s*(figure|fig\.)\s*[A-Z]?[\d.-]*\d", re.I)
# "Figure 59 illustrates ..." cites a figure; a caption follows its number with
# punctuation ("Figure 59. Open DFM Hits") or a capitalised title ("Figure 58
# Valid Scan Cell ..."). Case matters here, so only the word "figure" ignores it.
SENTENCE_RE = re.compile(r"^\s*(?i:figure|fig\.)\s*[A-Z]?[\d.-]*\d\s+[a-z]")
LABEL_ONLY_RE = re.compile(r"^\s*(figure|fig\.)\s*[A-Z]?[\d.-]*\d[.:]?\s*$", re.I)
DOT_LEADER_RE = re.compile(r"\.\s?\.\s?\.\s?\.")
TEXTISH = ("caption", "text", "section-header", "list-item", "title")


def norm(text: str) -> str:
    """Lowercase alphanumerics only, so markdown emphasis, <br> and punctuation
    cannot stop a caption in a section file matching the same caption in the PDF."""
    return " ".join(re.findall(r"[0-9a-z]+", re.sub(r"<[^>]*>", " ", text).lower()))


def caption_lines(texts: list[str]) -> list[tuple[int, str]]:
    """(section index, normalized line) for every section line that reads as a caption."""
    out = []
    for i, text in enumerate(texts):
        for line in text.splitlines():
            if DOT_LEADER_RE.search(line):
                continue  # a List of Figures or contents row
            n = norm(line)
            if n.startswith(("figure ", "fig ")):
                out.append((i, n))
    return out


def same_caption(a: str, b: str) -> bool:
    """Equal, or one a prefix of the other where the shorter still names the
    figure and part of its title -- captions wrap across lines."""
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short.split()) >= 4 and long_.startswith(short + " ")


def may_have_figure(page) -> bool:
    """Cheap test before the model runs. A figure is a sizeable image or some
    drawing in the body of the page. A text page has only running-header rules
    and logos in its margins -- and sometimes a panel behind the whole text
    area, which Synopsys paints on every page."""
    band = page.rect.height * MARGIN_BAND
    body = pymupdf.Rect(page.rect.x0, page.rect.y0 + band, page.rect.x1, page.rect.y1 - band)
    half_page = abs(page.rect) / 2
    try:
        for info in page.get_image_info():
            r = pymupdf.Rect(info["bbox"])
            if r.intersects(body) and min(r.width, r.height) >= MIN_SIDE:
                return True
        thin = 0
        for d in page.get_cdrawings():
            r = pymupdf.Rect(d["rect"])
            if not r.intersects(body) or abs(r) > half_page:
                continue
            if min(r.width, r.height) > 3:
                return True
            thin += 1
            if thin >= 20:  # a diagram drawn purely in lines
                return True
    except Exception:
        return True  # when in doubt, let the model look
    return False


def page_boxes(page) -> list[tuple]:
    """The layout model's boxes as (x0, y0, x1, y1, class), refined the way
    pymupdf4llm refines them: a picture grows to cover the vector paths and
    text it intersects, which the raw box clips."""
    page.get_layout(return_raw=True)
    page.layout_information = [list(b["group_bbox"]) + [b["class_name"]]
                               for b in (page.layout_information or [])]
    if p4l_utils is not None:
        try:
            blocks = page.get_textpage(flags=TEXT_FLAGS,
                                       clip=pymupdf.INFINITE_RECT()).extractDICT()["blocks"]
            p4l_utils.clean_pictures(page, blocks)
            p4l_utils.add_image_orphans(page, blocks)
        except Exception:
            pass  # keep the model's own boxes
    return [tuple(b[:4]) + (b[4],) for b in page.layout_information]


def lines_in(words: list, rect) -> list[str]:
    """Text lines inside rect, from the page's one words extraction.
    get_text(clip=...) would extract the whole page again for every box."""
    lines: dict[tuple, list[str]] = {}
    for x0, y0, x1, y1, word, block, line, _ in words:
        if rect.contains(pymupdf.Point((x0 + x1) / 2, (y0 + y1) / 2)):
            lines.setdefault((block, line), []).append(word)
    return [" ".join(ws) for _, ws in sorted(lines.items())]


def caption_text(lines: list[str], caption_box: bool) -> str:
    """The caption from a box that starts "Figure N". A layout-model caption box
    holds only the caption. A plain text box may run on into a paragraph, so it
    gives its first line -- plus the next when the first is the bare label, as
    when "Figure 1" and its title are set apart by a tab."""
    if caption_box:
        text = " ".join(lines)
    elif LABEL_ONLY_RE.match(lines[0]) and len(lines) > 1:
        text = f"{lines[0]} {lines[1]}"
    else:
        text = lines[0]
    return " ".join(text.split())[:200]


def page_figures(page, boxes: list[tuple], stats: Counter) -> list[dict]:
    """Figures on one page, each with its caption and the paragraph above it."""
    pics = []
    for b in boxes:
        if b[4] != "picture":
            continue
        stats["pictures"] += 1
        r = pymupdf.Rect(b[:4]) & page.rect
        if min(r.width, r.height) < MIN_SIDE or r.width * r.height < MIN_AREA:
            stats["too_small"] += 1
            continue
        pics.append(r)
    if not pics:
        return []

    words = page.get_text("words")
    texts = []
    for b in boxes:
        if b[4] in TEXTISH:
            r = pymupdf.Rect(b[:4])
            lines = lines_in(words, r)
            if lines:
                texts.append((r, b[4], lines))

    # Each caption goes to the nearest figure it overlaps horizontally, a
    # layout-model "caption" box before a plain text box that starts "Figure".
    pairs = []
    for pi, pr in enumerate(pics):
        for ti, (tr, cls, lines) in enumerate(texts):
            if (not CAPTION_RE.match(lines[0]) or SENTENCE_RE.match(lines[0])
                    or min(tr.x1, pr.x1) <= max(tr.x0, pr.x0)):
                continue
            if tr.y1 <= pr.y0 + 2:
                gap = pr.y0 - tr.y1
            elif tr.y0 >= pr.y1 - 2:
                gap = tr.y0 - pr.y1
            else:
                continue
            if gap <= CAPTION_GAP:
                pairs.append((cls != "caption", gap, pi, ti))
    caption_of, used = {}, set()
    for _, _, pi, ti in sorted(pairs):
        if pi not in caption_of and ti not in used:
            caption_of[pi] = ti
            used.add(ti)

    out = []
    for k, pr in enumerate(pics):
        caption = ""
        if k in caption_of:
            _, cls, lines = texts[caption_of[k]]
            caption = caption_text(lines, cls == "caption")
        # The paragraph above, never a caption: context has to be evidence
        # independent of the caption for the summary's cross-check to mean anything.
        above = [t for i, t in enumerate(texts) if i not in used
                 and t[0].y1 <= pr.y0 + 2 and min(t[0].x1, pr.x1) > max(t[0].x0, pr.x0)]
        context = " ".join(max(above, key=lambda t: t[0].y1)[2]) if above else ""
        out.append({
            "page": page.number + 1,
            "rect": pr,
            "caption": caption,
            "labels": " ".join(" ".join(lines_in(words, pr)).split())[:600],
            "context": context,
        })
    return out


class Linker:
    """Ties figures to sections, walking both in document order."""

    def __init__(self, texts: list[str]):
        self.padded = [f" {norm(t)} " for t in texts]
        self.captions = caption_lines(texts)
        self.cursor = 0

    def by_caption(self, caption: str) -> int | None:
        key = norm(caption)
        hits = [i for i, line in self.captions if same_caption(key, line)]
        ahead = [i for i in hits if i >= self.cursor]
        if ahead:
            return ahead[0]
        return hits[0] if len(set(hits)) == 1 else None

    def by_context(self, context: str) -> int | None:
        """The section holding the paragraph above the figure. Just after the
        previous figure's section, the first match will do; anywhere else it
        must be the only one in the document. In a manual without captions
        nothing moves that window forward, and looking only inside it left 108
        of one guide's 113 figures unattached."""
        words = norm(context).split()
        if len(words) < 5:
            return None
        needle = " " + " ".join(words[-8:]) + " "
        near = range(self.cursor, min(len(self.padded), self.cursor + CONTEXT_WINDOW))
        for i in near:
            if needle in self.padded[i]:
                return i
        far = [i for i in range(len(self.padded)) if i not in near and needle in self.padded[i]]
        return far[0] if len(far) == 1 else None


def render(page, rect, path: Path, dpi: int, max_px: int) -> tuple[int, int]:
    clip = (rect + (-PAD, -PAD, PAD, PAD)) & page.rect
    zoom = min(dpi / 72, max_px / max(clip.width, clip.height, 1))
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
    pix.save(str(path))
    return pix.width, pix.height


def extract(doc_dir: Path, pdf_path: Path, dry_run: bool, dpi: int, max_px: int) -> dict:
    manifest = json.loads((doc_dir / "manifest.json").read_text(encoding="utf-8-sig"))
    sections = manifest.get("sections", [])
    texts = [(doc_dir / s["file"]).read_text(encoding="utf-8", errors="replace") for s in sections]

    stats = Counter()
    doc = pymupdf.open(pdf_path)
    found = []
    for page in doc:
        stats["pages"] += 1
        if page.number and page.number % 500 == 0:
            print(f"    {doc_dir.name}: page {page.number}/{doc.page_count}", file=sys.stderr, flush=True)
        if not may_have_figure(page):
            continue
        stats["analysed"] += 1
        page.remove_rotation()
        try:
            boxes = page_boxes(page)
        except Exception:
            stats["layout_errors"] += 1
            continue
        found.extend(page_figures(page, boxes, stats))

    # A logo or border graphic sits in the same place on page after page.
    def spot(f):
        return tuple(round(v / 4) for v in f["rect"])
    spots = Counter(spot(f) for f in found)
    limit = max(5, REPEAT_SHARE * doc.page_count)
    figures = []
    for f in found:
        if not f["caption"] and spots[spot(f)] >= limit:
            stats["repeated"] += 1
        else:
            figures.append(f)

    out_dir = doc_dir / "figures"
    if not dry_run:
        out_dir.mkdir(exist_ok=True)
    linker = Linker(texts)
    entries, per_page, unlinked, disagree = [], Counter(), [], []
    for f in figures:
        section = how = None
        if f["caption"]:
            section = linker.by_caption(f["caption"])
            if section is not None:
                how = "caption"
                check = linker.by_context(f["context"]) if f["context"] else None
                if check is not None:
                    stats["check_agree" if check == section else "check_disagree"] += 1
                    if check != section and len(disagree) < 3:
                        disagree.append(f"p.{f['page']} {f['caption'][:40]}: caption -> "
                                        f"{sections[section]['file']}, context -> {sections[check]['file']}")
        if section is None and f["context"]:
            section = linker.by_context(f["context"])
            how = "context" if section is not None else None
        if section is not None:
            linker.cursor = section
        elif f["caption"] and len(unlinked) < 3:
            unlinked.append(f"p.{f['page']} {f['caption'][:70]}")
        stats[f"link_{how}"] += 1
        stats["captioned"] += bool(f["caption"])

        fid = f"p{f['page']:05d}-{per_page[f['page']]}"
        per_page[f["page"]] += 1
        entry = {
            "id": fid,
            "page": f["page"],
            "bbox": [round(v, 1) for v in f["rect"]],
            "file": f"figures/{fid}.png",
            "caption": f["caption"] or None,
            "labels": f["labels"] or None,
            "section": sections[section]["file"] if section is not None else None,
            "link": how,
        }
        if not dry_run:
            page = doc[f["page"] - 1]
            page.remove_rotation()
            entry["width"], entry["height"] = render(page, f["rect"], out_dir / f"{fid}.png", dpi, max_px)
        entries.append(entry)

    if not dry_run:
        index_path = doc_dir / "figures.json"
        stale = set()
        if index_path.is_file():  # only ever remove crops this script wrote
            old = json.loads(index_path.read_text(encoding="utf-8-sig")).get("figures", [])
            stale = {e["file"] for e in old} - {e["file"] for e in entries}
            # OCR is slow to redo: keep ocr_figures.py's reading of any figure
            # whose crop has not moved.
            read = {e["id"]: e for e in old if "ocr" in e}
            for e in entries:
                if e["id"] in read and read[e["id"]].get("bbox") == e["bbox"]:
                    e["ocr"] = read[e["id"]]["ocr"]
        index_path.write_text(json.dumps({
            "slug": manifest.get("slug", doc_dir.name),
            "source_pdf": manifest.get("source_pdf"),
            "generator": "extract_figures.py",
            "dpi": dpi,
            "max_px": max_px,
            "figures": entries,
        }, indent=2) + "\n", encoding="utf-8")
        for rel in stale:
            (doc_dir / rel).unlink(missing_ok=True)
    doc.close()
    stats["figures"] = len(entries)
    stats["figure_pages"] = len(per_page)
    return {"slug": doc_dir.name, "stats": stats, "unlinked": unlinked, "disagree": disagree}


def run_job(job: tuple) -> dict:
    doc_dir, pdf, dry_run, dpi, max_px, threads = job
    prepare(threads)
    started = time.time()
    result = extract(Path(doc_dir), Path(pdf), dry_run, dpi, max_px)
    result["secs"] = time.time() - started
    return result


def report(r: dict) -> None:
    s = r["stats"]
    print(f"  {r['slug'][:32]:32} {s['pages']:>5} pp  {s['analysed']:>5} analysed  "
          f"{s['figures']:>4} figures on {s['figure_pages']:>4} pp ({s['captioned']:>4} captioned)  "
          f"linked: {s['link_caption']:>4} caption {s['link_context']:>4} context "
          f"{s['link_None']:>4} none  {r['secs']:5.0f}s", flush=True)
    for u in r["unlinked"]:
        print(f"      unlinked caption: {u}")
    for d in r["disagree"]:
        print(f"      context disagrees: {d}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("collection", type=Path, help="folder holding the source PDFs and docs/")
    ap.add_argument("--only", action="append", default=[], help="restrict to this slug (repeatable)")
    ap.add_argument("--skip", action="append", default=[], help="exclude this slug (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="detect and link, write nothing")
    ap.add_argument("--jobs", type=int, default=1, help="documents to process in parallel (default 1)")
    ap.add_argument("--threads", type=int, default=0,
                    help="layout-model threads per job (default: the cores shared among the jobs)")
    ap.add_argument("--dpi", type=int, default=DPI, help=f"crop resolution (default {DPI})")
    ap.add_argument("--max-px", type=int, default=MAX_PX, help=f"cap on a crop's long edge (default {MAX_PX})")
    args = ap.parse_args()

    coll = args.collection.resolve()
    docs = coll / "docs"
    if not docs.is_dir():
        sys.exit(f"No docs/ under {coll}")

    jobs = []
    for mp in sorted(docs.glob("*/manifest.json")):
        slug = mp.parent.name
        if slug.endswith((".old", ".new")) or slug.startswith((".", "_")):
            continue
        if slug in args.skip or (args.only and slug not in args.only):
            continue
        source = json.loads(mp.read_text(encoding="utf-8-sig")).get("source_pdf") or ""
        pdf = coll / source
        if not pdf.is_file():
            print(f"  !! {slug}: source PDF {source!r} is not beside docs/ -- skipped")
            continue
        jobs.append((str(mp.parent), str(pdf), args.dry_run, args.dpi, args.max_px))

    workers = max(1, min(args.jobs, len(jobs)))
    threads = args.threads or ((os.cpu_count() or 2) // workers if workers > 1 else 0)
    jobs = [j + (threads,) for j in jobs]

    print(f"{coll.name} ({'DRY RUN -- nothing written' if args.dry_run else 'writing'}), "
          f"{len(jobs)} document(s)" + (f", {workers} at a time, {threads} threads each" if workers > 1 else ""),
          flush=True)
    totals = Counter()
    if workers > 1:
        with ProcessPoolExecutor(workers) as pool:
            for fut in as_completed([pool.submit(run_job, j) for j in jobs]):
                r = fut.result()
                totals.update(r["stats"])
                report(r)
    else:
        for j in jobs:
            r = run_job(j)
            totals.update(r["stats"])
            report(r)

    checked = totals["check_agree"] + totals["check_disagree"]
    print(f"\ntotals: {totals['figures']} figures from {totals['pages']} pages "
          f"({totals['analysed']} analysed); dropped {totals['too_small']} too small, "
          f"{totals['repeated']} repeated; linked {totals['link_caption']} by caption, "
          f"{totals['link_context']} by context, {totals['link_None']} not at all"
          + (f"; {totals['layout_errors']} pages failed layout" if totals["layout_errors"] else ""))
    if checked:
        print(f"context check: on {checked} captioned figures that also have context, the context "
              f"method picks the caption's section {totals['check_agree'] / checked:.1%} of the time")
    return 0


if __name__ == "__main__":
    sys.exit(main())
