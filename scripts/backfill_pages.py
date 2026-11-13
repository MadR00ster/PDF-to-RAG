#!/usr/bin/env python3
"""
Give an existing document's chunks page numbers without reconverting it.

A document converted before the converters recorded pages has `page_start` and
`page_end` nowhere, so no answer from it can cite a page and `extract_figures.py`
cannot tie a figure to a section by its page. Reconverting adds them but also
re-cuts the chunks, which moves search results that were measured and accepted
(references/retrieval-measurement.md, "Prose chunk size"). This script leaves
every body, file name, heading and breadcrumb as it is and writes only the two
page fields, found by looking for each chunk's opening and closing words in the
source PDF's own text.

How a chunk is placed. Chunks are slices of the document in reading order, so
the search runs forward: a chunk's opening words are looked for from where the
previous chunk began, and its closing words between its own start and the next
chunk's. Both texts are reduced to their letters and digits, lower case, with
every run of one repeated character collapsed to one, so that a text layer that
runs words together or drops a doubled letter ("comand") still matches. A chunk
is placed by whatever part of its first (and last) 40 characters matches.

Each chunk ends up in one of these states, counted in the report:

  matched     start found by its words. The end too, unless it says otherwise.
  end-from-next  start matched, closing words not found: the end page is the
              page the chunk after it starts on, an upper bound.
  bounded     too short to match (a chapter title, front matter), but the
              chunk before it ends on the page the one after it starts on, so
              its page is known.
  neighbour   too short or not found, and the neighbours leave it more than one
              page: it takes the page the next chunk starts on. A guess.
  unplaced    nothing to take a page from (no chunk was matched at all). Left
              without pages.

A neighbour page is a guess and is recorded as one: the section gets
`"pages_from": "neighbour"` (or `"page_end_from": "neighbour"` where only the
end is). The manifest also gets a `pages_backfill` record with the counts.

Bodies, `full.md` and file names are never written. The manifest is copied to
`.rebuild-backup/manifests-<date>-pre-pages/<slug>.manifest.json` before it is
rewritten. A document that already has pages on some or all chunks is left
alone: it was converted with them. `--redo` places again one this script gave
pages, and the backup keeps the manifest as it was before the first run.

  python scripts/backfill_pages.py "Tessent Manual" --dry-run
  python scripts/backfill_pages.py "Tessent Manual" --only atpg-gd-2025-2
  python scripts/backfill_pages.py <corpus root>

Then re-run extract_figures.py and ocr_figures.py for the documents it changed:
with pages, a figure on an unshared page ties to its section by that page.
"""
from __future__ import annotations

import argparse
import bisect
import json
import re
import shutil
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import editions  # noqa: E402
from _common import utf8_console  # noqa: E402

PROBE = 40             # characters of a chunk's start (or end) a match is made on
STEP = 20              # how far apart the probes slide into the chunk
REACH = 600            # how far into a chunk (from either end) a probe may begin
MIN_WORDS = 3          # a chunk with fewer words is a stub: no match is tried
MIN_CHARS = 16         # ...nor one with fewer letters and digits than this
NEAR = 150_000         # how far past the previous chunk's start the first search looks
SPAN = 0.6             # the least share of its own length a chunk spans in the page stream
AHEAD = 40_000          # how far past where the chunks before it leave off the first search looks
LEAD = 40              # a match this close to a chunk's start fixes its page where it matched
AGREE = 400            # a far match must be confirmed by a second probe this close

_REPEAT = re.compile(r"(.)\1+")
_WORD = re.compile(r"[A-Za-z0-9]+")


def normalise(text: str) -> str:
    """Letters and digits only, lower case, accents and ligatures expanded,
    each run of one repeated character cut to one."""
    text = unicodedata.normalize("NFKD", text.lower())
    kept = "".join(c for c in text if c.isalnum() and not unicodedata.combining(c))
    return _REPEAT.sub(r"\1", kept)


def chunk_text(raw: str) -> str:
    """A chunk's own words: the breadcrumb line the enrichment step puts first
    is not in the PDF."""
    lines = raw.replace("\r", "").split("\n")
    if lines and lines[0].startswith("*") and not lines[0].startswith("**") and lines[0].rstrip().endswith("*"):
        lines = lines[1:]
    return "\n".join(lines)


class Placement:
    """Where one chunk was found. `state` is matched, end-from-next, bounded,
    neighbour or unplaced (the module docstring says what each means)."""

    def __init__(self):
        self.start: int | None = None       # first page
        self.end: int | None = None         # last page
        self.state = "unplaced"
        self.pos: int | None = None         # offset of the chunk's start in the page stream
        self.page_pos: int | None = None    # the offset its start page is read from
        self.end_pos: int | None = None

    def __repr__(self) -> str:
        return f"Placement({self.start}-{self.end}, {self.state})"


class Stream:
    """The PDF's pages as one normalised string, with where each page starts."""

    def __init__(self, page_texts: list[str]):
        parts, self.starts, at = [], [], 0
        for t in page_texts:
            n = normalise(t)
            self.starts.append(at)
            parts.append(n)
            at += len(n)
        self.text = "".join(parts)

    def page(self, pos: int) -> int:
        """1-based page holding offset `pos`."""
        pos = min(max(pos, 0), max(len(self.text) - 1, 0))
        return max(1, bisect.bisect_right(self.starts, pos))


def probes_from_start(c: str) -> list[tuple[int, str]]:
    n = len(c)
    size = min(PROBE, n)
    return [(o, c[o:o + size]) for o in range(0, max(0, min(REACH, n - size)) + 1, STEP)]


def probes_from_end(c: str) -> list[tuple[int, str]]:
    """(characters from the end to the probe's end, probe)."""
    n = len(c)
    size = min(PROBE, n)
    return [(o, c[n - o - size:n - o]) for o in range(0, max(0, min(REACH, n - size)) + 1, STEP)]


def occurrences(text: str, probe: str, lo: int, hi: int):
    """Offsets of `probe` in text[lo:hi + len(probe)]."""
    at = text.find(probe, lo, hi + len(probe))
    while at >= 0:
        yield at
        at = text.find(probe, at + 1, hi + len(probe))


def find_start(s: Stream, c: str, cursor: int, expected: int) -> tuple[int, int] | None:
    """Offset in the stream where the chunk begins, searching forward from
    `cursor`.

    Each probe slid along the chunk's opening gives an estimate of the start
    (where it matched, less how far into the chunk it was). One probe is not
    believed on its own while others disagree: a table's header row is the same
    in a dozen tables, and a page prints its running title and the chunk's own
    heading in an order the chunk does not have. The estimate two probes agree
    on is taken; failing that the one nearest where the chunks before it leave
    off (`expected`). A phrase can occur more than once (a "Related Topics" line
    names the next chapter), which is the other reason to prefer the nearest.

    Nothing near: look anywhere ahead, but only believe a match a second probe
    confirms, so that one repeated phrase cannot carry the search off and leave
    every later chunk behind it.

    Returns the estimate and the offset to read the start page from. They
    differ when the chunk opens with words the PDF does not print (a heading
    the converter made up, a marker): the estimate then falls a few characters
    early, on the previous page when the chunk starts at the top of one, so a
    match within the first LEAD characters is read where it matched."""
    ps = probes_from_start(c)
    near_end = cursor + max(NEAR, 6 * len(c))
    ests: list[tuple[int, int, int]] = []     # (estimated start, which probe, where it matched)

    def support(e: int) -> int:
        return len({k for o, k, _ in ests if abs(e - o) <= AGREE})

    # every probe is tried: the first to match may be a repeat of the chunk's
    # opening elsewhere, and the probes that follow it say which place is right
    reach = min(near_end, max(expected, cursor) + AHEAD + 3 * len(c))
    for k, (off, p) in enumerate(ps):
        for n, h in enumerate(occurrences(s.text, p, cursor, reach)):
            if n >= 200:
                break
            ests.append((h - off, k, h))
    if ests:
        best = max((e for e, _, _ in ests), key=lambda e: (support(e), -abs(e - expected)))
        # the earliest probe that agrees: its offset into the chunk is k * STEP
        k0, h0 = min((k, h) for e, k, h in ests if abs(e - best) <= AGREE)
        at = max(best, cursor)
        return at, (max(at, h0) if k0 * STEP <= LEAD else at)
    for i, (off, p) in enumerate(ps):
        hit = s.text.find(p, near_end)
        if hit < 0:
            continue
        guess = hit - off
        for off2, p2 in ps[i + 1:]:
            if off2 - off < PROBE:
                continue
            at = s.text.find(p2, max(guess, cursor), guess + off2 + AGREE + len(p2))
            if at >= 0 and abs((at - off2) - guess) <= AGREE:
                return max(guess, cursor), max(guess, cursor)
        if len(ps) == 1 and s.text.count(p, near_end) == 1:
            return max(guess, cursor), max(guess, cursor)
    return None


def find_end(s: Stream, c: str, start: int, limit: int) -> int | None:
    """Offset where the chunk's last words end, between its start and `limit`
    (where the next chunk begins): the first probe from the end that occurs,
    at the place nearest where the chunk's length puts its end. The end of the
    words found, not of the chunk: a chunk may close on a line the PDF prints
    elsewhere (the topics before and after it), and extrapolating from a probe
    past that line would put the end on a page the prose is not on."""
    expected = start + len(c)
    # a chunk takes at least about as much of the stream as it has characters
    # (the stream has the page furniture besides), so a repeat of its closing
    # words long before that is not its end
    floor = start + int(SPAN * len(c))
    for off, p in probes_from_end(c):
        hits = [h + len(p) for h in occurrences(s.text, p, max(start, floor - len(p)), limit - len(p))]
        if hits:
            return min(hits, key=lambda e: abs(e + off - expected))
    return None


def place(chunks: list[str], page_texts: list[str]) -> list[Placement]:
    """Page numbers for each chunk's text, from the PDF's page texts."""
    s = Stream(page_texts)
    norm = [normalise(chunk_text(c)) for c in chunks]
    words = [len(_WORD.findall(chunk_text(c))) for c in chunks]
    out = [Placement() for _ in chunks]
    cursor = 0
    expected = 0           # where the chunks before this one leave off
    for i, c in enumerate(norm):
        if words[i] >= MIN_WORDS and len(c) >= MIN_CHARS:
            found = find_start(s, c, cursor, expected)
            if found is not None:
                at, out[i].page_pos = found
                out[i].pos = at
                cursor = at
                expected = at
        expected += len(c)
    placed = [i for i, p in enumerate(out) if p.pos is not None]
    for k, i in enumerate(placed):
        p = out[i]
        nxt = out[placed[k + 1]].pos if k + 1 < len(placed) else len(s.text)
        p.start = s.page(p.page_pos)
        end = find_end(s, norm[i], p.pos, min(nxt + 20, len(s.text))) if len(norm[i]) >= MIN_CHARS else None
        if end is not None:
            p.end_pos = end
            p.end = max(p.start, s.page(end - 1))
            p.state = "matched"
        else:
            # the closing words were not found; the chunk ends no later than where the next begins
            p.end = max(p.start, s.page(nxt - 1))
            p.state = "end-from-next" if p.end != p.start else "matched"
            p.end_pos = None
    # the rest sit between two placed chunks
    for i, p in enumerate(out):
        if p.pos is not None:
            continue
        before = next((out[j] for j in range(i - 1, -1, -1) if out[j].pos is not None), None)
        after = next((out[j] for j in range(i + 1, len(out)) if out[j].pos is not None), None)
        if before and after and before.state == "matched" and before.end == after.start:
            p.start = p.end = after.start
            p.state = "bounded"
        elif after:
            p.start = p.end = after.start
            p.state = "neighbour"
        elif before:
            p.start = p.end = before.end
            p.state = "neighbour"
    return out


# --------------------------------------------------------------------- files

STATES = ("matched", "end-from-next", "bounded", "neighbour", "unplaced")


def has_pages(m: dict) -> bool:
    return any(s.get("page_start") is not None or s.get("page_end") is not None for s in m["sections"])


def page_texts_of(pdf: Path) -> list[str]:
    import pymupdf
    with pymupdf.open(pdf) as doc:
        return [page.get_text() for page in doc]


def backfill_document(doc_dir: Path, collection: Path, dry_run: bool, backup: Path, redo: bool = False) -> dict:
    """Place one document's chunks and, unless `dry_run`, write its manifest."""
    path = doc_dir / "manifest.json"
    m = json.loads(path.read_text(encoding="utf-8-sig"))
    info = {"slug": doc_dir.name, "chunks": len(m["sections"]), "counts": {k: 0 for k in STATES}, "note": ""}
    if has_pages(m) and not (redo and "pages_backfill" in m):
        info["note"] = "already has pages" + (" (backfilled; --redo places them again)" if "pages_backfill" in m else "")
        return info
    pdf = editions.find_source_pdf(collection, m.get("source_pdf"))
    if pdf is None:
        info["note"] = f"source PDF {m.get('source_pdf')!r} not found"
        return info
    chunks = [(doc_dir / s["file"]).read_text(encoding="utf-8") for s in m["sections"]]
    placed = place(chunks, page_texts_of(pdf))
    for p in placed:
        info["counts"][p.state] += 1
    info["placements"] = placed
    if dry_run:
        return info
    for s, p in zip(m["sections"], placed):
        s.pop("pages_from", None)
        s.pop("page_end_from", None)
        if p.start is None:
            continue
        s["page_start"], s["page_end"] = p.start, p.end
        if p.state == "neighbour":
            s["pages_from"] = "neighbour"
        elif p.state == "end-from-next":
            s["page_end_from"] = "neighbour"
    m["pages_backfill"] = {"script": Path(sys.argv[0]).name, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                           **{k.replace("-", "_"): v for k, v in info["counts"].items()}}
    backup.mkdir(parents=True, exist_ok=True)
    kept = backup / f"{doc_dir.name}.manifest.json"
    if not kept.exists():
        shutil.copy2(path, kept)
    tmp = path.with_name("manifest.json.tmp")
    tmp.write_text(editions.dump_manifest(m), encoding="utf-8")
    tmp.replace(path)
    return info


def main() -> int:
    utf8_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path, help="a collection (holding source/ and docs/) or a corpus root of several")
    ap.add_argument("--only", action="append", default=[], help="restrict to this slug (repeatable)")
    ap.add_argument("--skip", action="append", default=[], help="exclude this slug (repeatable)")
    ap.add_argument("--redo", action="store_true",
                    help="place again a document this script already gave pages (the first backup is kept)")
    ap.add_argument("--dry-run", action="store_true", help="place and report, write nothing")
    args = ap.parse_args()

    collections = editions.collections_under(args.path.resolve())
    if not collections:
        sys.exit(f"No docs/ under {args.path}")
    rows = []
    for collection in collections:
        backup = collection / editions.BACKUP_DIR / f"manifests-{time.strftime('%Y-%m-%d')}-pre-pages"
        for doc_dir in editions.document_dirs(collection / "docs"):
            if doc_dir.name in args.skip or (args.only and doc_dir.name not in args.only):
                continue
            print(f"[{collection.name}/{doc_dir.name}] ...", flush=True)
            rows.append((collection, backfill_document(doc_dir, collection, args.dry_run, backup, args.redo)))

    print(f"\n{'matched':>8} {'end-next':>8} {'bounded':>8} {'neighbour':>9} {'unplaced':>8}  document")
    totals = {k: 0 for k in STATES}
    for collection, r in rows:
        c = r["counts"]
        for k in STATES:
            totals[k] += c[k]
        print(f"{c['matched']:8d} {c['end-from-next']:8d} {c['bounded']:8d} {c['neighbour']:9d} "
              f"{c['unplaced']:8d}  {collection.name}/{r['slug']}  {r['note']}")
    print(f"{totals['matched']:8d} {totals['end-from-next']:8d} {totals['bounded']:8d} "
          f"{totals['neighbour']:9d} {totals['unplaced']:8d}  total")
    print(f"\n{totals['neighbour']} chunk(s) took their pages from a neighbour"
          + (" (flagged `pages_from` in the manifest)" if not args.dry_run else " (would be flagged)"))
    if args.dry_run:
        print("DRY RUN -- nothing written")
    else:
        print("Next: extract_figures.py, ocr_figures.py, build_index.py, build_search_db.py, check_corpus.py --strict.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
