#!/usr/bin/env python3
"""
Check a converted corpus against the output contract, whatever produced it.

The converters in this folder are one way to write docs/<slug>/. A newer
extractor, a converter a model writes for one awkward manual, or a hand repair
are others. This script does not care which: it reads what is on disk and says
whether it is safe to index. That is what lets a converter be replaced -- the
replacement is accepted when this passes and eval_search.py scores no worse,
not because its output looks right.

Read-only. It shares no code with the converters on purpose: a check that
imports the function it checks inherits that function's bugs, and the corpus
passes for the wrong reason.

Per document:
  contract     manifest fields, section files and full.md -- what
               build_index.py, build_search_db.py and mcp_server.py read. A
               break here is a crash, or a chunk the index leaves out without
               a word.
  pages        whole numbers in 1..page_count, start <= end, not running
               backwards from one chunk to the next.
  breadcrumbs  on every chunk, rooted at the document title, written once, and
               the same in the manifest as on the chunk's first line. Every
               ancestor must be a bookmark TOC entry that contains the next
               one, and where the chunk has pages, one whose TOC page span
               overlaps them.
  entities     the `command` (or `entity`) that owns a chunk: a TOC entry,
               the breadcrumb's last ancestor, its span overlapping the chunk's
               pages, named in the chunk's own text, and not contradicted by a
               heading naming a different entry.
  order        a chunk's deepest ancestor never moves backwards through the
               TOC, except to an entry that encloses where the last chunk was.
  furniture    no "Feedback" link, running title or page-numbered footer left
               in a chunk.
  figures      files on disk, sections that exist, pages inside the section's.
  content      sampled PDF pages whose words the chunks claiming that page do
               not contain -- text a converter dropped, or pages it
               misnumbered. Where the words are elsewhere in the document, it
               says so: that is a page error, not lost text. On a document
               without pages it can only compare with the whole document, so
               it catches text lost wholesale and not a single missing chunk.
Per collection:
  slugs two documents share (build_search_db.py stops on one), document
  folders build_index.py skips but build_search_db.py indexes, a stale
  index.json, PDFs neither converted nor marked superseded.
  editions     documents sharing a `doc_id` are releases of one manual. Each
               needs a version the others can be ordered against, no two the
               same, and a pin in current_versions.json has to name a release
               one of them applies to -- build_search_db.py stops on any of
               these. Two documents
               with one title and different doc_ids are reported too: both
               would answer every search.

Two of these are the independent signals SKILL.md's verification protocol
asks for, because coverage is not correctness: TOC page spans checked against
the pages a chunk claims, and the PDF's own text checked against the chunks. A
labelling that gives 99% of chunks an owner can be 17.5% wrong, and only a
signal the labeller did not use can tell.

What it cannot see: idempotency, which needs the converter run twice
(the suite in tests/ does that), and which of two same-titled TOC entries
with overlapping pages a breadcrumb meant.

Usage:
  python scripts/check_corpus.py "D:/Manuals"                     every collection
  python scripts/check_corpus.py "D:/Manuals/Tessent Manual"      one collection
  python scripts/check_corpus.py "D:/Manuals/Tessent Manual/docs/tshell-ref-2026-2"
  python scripts/check_corpus.py <root> --only syn2 --only vcs
  python scripts/check_corpus.py <root> --json run.json           keep a run to compare
  python scripts/check_corpus.py <root> --strict                  warnings fail too
  python scripts/check_corpus.py <root> --no-pdf                  skip the content check

Exit status: 0 no failures, 1 at least one failure (or with --strict, any
warning), 2 nothing found to check.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

BREADCRUMB_SEP = " › "
REQUIRED_FIELDS = ("slug", "title", "source_pdf", "page_count", "sections")
# Present is not enough: `"slug": null` passes a key check and then fails
# build_search_db.py's NOT NULL primary key.
FIELD_VALID = {
    "slug": lambda v: isinstance(v, str) and bool(v.strip()),
    "title": lambda v: isinstance(v, str) and bool(v.strip()),
    "source_pdf": lambda v: isinstance(v, str) and bool(v.strip()),
    "page_count": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 1,
    "sections": lambda v: isinstance(v, list),
}

# Reading thousands of section files is filesystem-bound, and on a synced
# folder (OneDrive) network-bound while placeholders hydrate. Same reasoning
# as build_search_db.py's READ_THREADS.
READ_THREADS = 32

# The chunker's limit is 9 KB and Docling's merged chunks run a median of
# 8.7 KB. Four times that means a block the final hard-wrap never split.
OVERSIZED = 40_000

# Below this share of owned chunks naming their owner, attribution is not
# coming from where the chunk is. The page-accurate rebuild measured 100% on
# PrimeTime's references; the text-scanning attribution it replaced would not
# have been caught by this alone, which is why the page check exists too.
ENTITY_NAMED_MIN = 0.90

# A running footer carrying a page label ("5-138 Software Version 2026.2")
# is a prose line repeated in at least this many chunks, and this share of
# them, whose number differs nearly every time. A page crosses most chunk
# boundaries, so a footer the converter missed lands in most chunks:
# Tessent's was in 3,131 of one reference's 4,511. Numbered content repeats
# far less -- example captions ("Example 14-27 test.v") in 4% of a guide's
# chunks -- and headings, table rows and code, which can repeat more, are
# never taken for footers.
FOOTER_MIN_CHUNKS = 10
FOOTER_MIN_SHARE = 0.10
FOOTER_VARIETY = 0.5
CODE_CHARS = re.compile(r"[=;{}`<>\[\]]")

# A sampled page is judged only with this many distinct words on it; a blank
# page or a full-page diagram says nothing about the converter.
MIN_PAGE_WORDS = 20
# A page is flagged when fewer than this share of its words are in the chunks
# claiming it. Measured on 32 real manuals: correct output scored 82-100% per
# page (stripped furniture costs a few words everywhere), and the only pages
# near 80% were ones whose own text layer is garbled ("testexistence
# testexistence"). Pages numbered three too high scored a median of 43-80%
# with some at 0%, so a systematic shift shows across a sample; one page off
# does not show at all, since chunks span pages. Without pages the comparison
# is with the whole document, and a deleted chunk still scored 82-100% there:
# that mode catches only text lost wholesale.
CONTENT_MIN = 0.75
DEFAULT_PDF_PAGES = 30

MAX_EXAMPLES = 3

FAIL, WARN = "FAIL", "WARN"

CHECKS = {
    # contract: what the index builders and the server read
    "manifest-unreadable": (FAIL, "manifest.json is not readable JSON, so every tool skips or stops on this document"),
    "manifest-missing-field": (FAIL, "manifest.json lacks a field build_index.py or build_search_db.py reads"),
    "manifest-invalid-field": (FAIL, "a required manifest field is null, empty or the wrong type; build_search_db.py rejects it or reads it wrongly"),
    "slug-mismatch": (WARN, "the manifest's slug is not its folder's name; build_index.py lists the folder, build_search_db.py the slug"),
    "toc-missing": (WARN, "no bookmark TOC in the manifest: get_toc has nothing to serve and no breadcrumb can be verified"),
    "section-without-file": (FAIL, "a section names no file; build_search_db.py drops it without a word"),
    "section-bad-path": (FAIL, "a section's file is not a relative path inside sections/: a reader would leave the document, or crash on it"),
    "section-missing-file": (FAIL, "a section's file is not on disk, so its text is not searchable"),
    "section-unreadable": (FAIL, "a section file that cannot be read as UTF-8 text"),
    "section-duplicate-file": (WARN, "two sections name the same file, so its text is indexed twice"),
    "section-orphan-file": (WARN, "a file in sections/ the manifest does not list; no tool will read it"),
    "chars-mismatch": (WARN, "the manifest's chars disagrees with the file: one was edited without the other"),
    "empty-chunk": (WARN, "a chunk with no text besides its breadcrumb"),
    "oversized-chunk": (WARN, f"a chunk over {OVERSIZED:,} chars: chunking never split it"),
    "full-md-missing": (FAIL, "no full.md, the un-chunked fallback for when a chunk boundary lands badly"),
    "source-pdf-missing": (WARN, "the source PDF is in neither source/ nor the collection folder; figures, page images and the content check cannot reach it"),
    # pages
    "no-pages": (WARN, "no chunk carries a page number, so no answer from this document can cite one"),
    "pages-partial": (WARN, "some chunks carry page numbers and some do not"),
    "page-invalid": (FAIL, "page_start/page_end outside 1..page_count, or start after end: a citation to a page that is not there"),
    "pages-backwards": (WARN, "page_start falls from one chunk to the next, though chunks are listed in reading order"),
    # breadcrumbs
    "breadcrumb-missing": (FAIL, "a chunk with no breadcrumb: retrieved alone, it does not say where it came from"),
    "breadcrumb-not-rooted": (FAIL, "a breadcrumb that does not start with the document title and ' › '; enrich_chunks.py will not recognise it and prepends another"),
    "breadcrumb-doubled": (FAIL, "a chunk that starts with two breadcrumb lines, the mark of a pass that is not idempotent"),
    "breadcrumb-file-mismatch": (WARN, "the chunk's first line is not its manifest breadcrumb: the reader and the index see different labels"),
    "ancestor-unverifiable": (WARN, "breadcrumb ancestors in a document with no bookmark TOC: nothing can confirm them"),
    "ancestor-not-in-toc": (FAIL, "a breadcrumb ancestor that is not a bookmark TOC entry: a guessed parent"),
    "ancestor-contradicts-pages": (FAIL, "a breadcrumb ancestor whose TOC pages do not overlap the chunk's pages: the label is wrong"),
    "ancestor-not-nested": (FAIL, "a breadcrumb whose ancestors do not contain one another in the TOC: at least one is not a parent"),
    "ancestor-out-of-order": (WARN, "a chunk's deepest ancestor comes earlier in the TOC than the last chunk's and does not enclose it, so one of the two labels is wrong"),
    # entities
    "entity-not-in-toc": (FAIL, "an owning entity that is not a TOC entry: attribution comes from the TOC or not at all"),
    "entity-contradicts-pages": (FAIL, "an owning entity whose TOC pages do not overlap the chunk's pages: the chunk documents something else"),
    "entity-breadcrumb-mismatch": (WARN, "the owning entity is not the breadcrumb's last ancestor: the index and the reader name different owners"),
    "entity-heading-mismatch": (WARN, "a chunk headed by one entry's name and owned by another: search finds it for the first, labelled as the second"),
    "entity-rarely-named": (WARN, f"fewer than {ENTITY_NAMED_MIN:.0%} of owned chunks name their owner: fragments too small to say what they document, or labels that did not come from the text's position"),
    # furniture
    "furniture": (WARN, "page furniture left in chunks: a Feedback link, the running title, or a page-numbered header or footer"),
    # figures
    "figures-unreadable": (FAIL, "figures.json is not readable JSON; build_search_db.py skips every figure in it"),
    "figure-file-missing": (FAIL, "a figure's image is not on disk; get_figure fails on it"),
    "figure-section-dangling": (FAIL, "a figure tied to a section the manifest does not list; the index leaves it unattached"),
    "figure-page-invalid": (FAIL, "a figure's page is outside 1..page_count"),
    "figure-outside-section": (WARN, "a figure whose page is outside its section's pages"),
    # content
    "content-gap": (WARN, f"sampled PDF pages with under {CONTENT_MIN:.0%} of their words in the chunks that claim them"),
    "content-unchecked": (WARN, "the content check could not run -- PyMuPDF is missing or the PDF would not open -- so text and page numbers went unchecked"),
    "page-count-mismatch": (FAIL, "the manifest's page_count is not the source PDF's: page numbers are checked against a document that is not this one"),
    # collection
    "duplicate-slug": (FAIL, "two documents share a slug; build_search_db.py stops on the second (documents.slug is its primary key)"),
    "hidden-document": (FAIL, "a manifest in a docs/ folder build_index.py skips (.old, .new, dot, underscore) that build_search_db.py still indexes; keep backups outside docs/"),
    "index-missing": (WARN, "no docs/index.json; run build_index.py"),
    "index-stale": (WARN, "docs/index.json disagrees with the manifests on disk; run build_index.py"),
    "pdf-unaccounted": (WARN, "a PDF in source/ or the collection folder that is neither converted nor listed in superseded.json"),
    # editions
    "edition-unordered": (FAIL, "a manual with several editions where one has no version with a number in it: which is newest cannot be told, and build_search_db.py stops"),
    "edition-duplicate-version": (FAIL, "two editions of one manual with the same version; build_search_db.py stops"),
    "pin-invalid": (FAIL, "current_versions.json is unreadable, or pins a doc_id that is not in this collection or a release none of its editions applies to; build_search_db.py stops"),
    "edition-ungrouped": (WARN, "two documents with the same title and different doc_ids: if they are editions of one manual, both answer every search"),
    "superseded-invalid": (WARN, "superseded.json is unreadable, an entry lacks file/superseded_by, it names a slug that is not here, or it lists a PDF that has since been converted"),
}


# ---------------------------------------------------------------- findings

class Findings:
    """Problems grouped by check, with a count and the first few examples."""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}

    def add(self, code: str, example: str | None = None) -> None:
        severity, message = CHECKS[code]
        f = self.items.setdefault(
            code, {"severity": severity, "check": code, "message": message, "count": 0, "examples": []})
        f["count"] += 1
        if example is not None and len(f["examples"]) < MAX_EXAMPLES:
            f["examples"].append(example)

    def worst(self) -> str:
        sev = {f["severity"] for f in self.items.values()}
        return FAIL if FAIL in sev else WARN if WARN in sev else "ok"

    def as_list(self) -> list[dict]:
        order = {FAIL: 0, WARN: 1}
        return sorted(self.items.values(), key=lambda f: (order[f["severity"]], f["check"]))


# ------------------------------------------------------------------- text

LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
NUMBERING_RE = re.compile(r"^[^0-9A-Za-z]*\d+(\.\d+)*\.?\s+")
TOKEN_RE = re.compile(r"[0-9a-z]+")


MARKS = str.maketrans("", "", "™℠®©")
TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")


def tokens(text: str) -> str:
    """Lowercase alphanumeric runs, space-joined. Emphasis, escapes, `_`, `-`
    and trademark signs are where extractors and title strings differ, not
    what the text says -- a manifest title without "®" must still match the
    footer that has one."""
    return " ".join(TOKEN_RE.findall(unicodedata.normalize("NFKC", text.translate(MARKS)).lower()))


def key(title: str) -> str:
    """A heading or TOC title as comparable words, leading numbering dropped:
    a bookmark says "Overview" where the page says "3.2 Overview"."""
    return tokens(NUMBERING_RE.sub("", unicodedata.normalize("NFKC", title).strip()))


def contains(haystack: str, needle: str) -> bool:
    return bool(needle) and f" {needle} " in f" {haystack} "


def words(text: str) -> set[str]:
    """Distinct words for the content check. Short tokens and bare numbers
    are page numbers, list markers and markup far more often than content."""
    return {w for w in TOKEN_RE.findall(unicodedata.normalize("NFKC", text).lower())
            if len(w) >= 4 and not w.isdigit()}


def line_core(line: str) -> str:
    """A line without the markup around it: links, highlight tags, emphasis."""
    s = LINK_RE.sub(r"\1", TAG_RE.sub("", line).strip())
    s = re.sub(r"^#{1,6}\s*", "", s)
    s = s.strip("*_` \t")
    return re.sub(r"\s+", " ", s)


def lines_outside_code(text: str):
    in_code = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if not in_code:
            yield line


def is_crumb_line(line: str, title: str) -> bool:
    s = line.strip()
    return len(s) > 2 and s.startswith("*") and s.endswith("*") and s.strip("*").strip().startswith(title)


def split_breadcrumb(crumb: str, title: str) -> list[str] | None:
    """The ancestors after the title, or None if the crumb is not rooted at it."""
    crumb, title = crumb.strip(), title.strip()
    if not title or not crumb.startswith(title):
        return None
    rest = crumb[len(title):]
    if not rest:
        return []
    if not rest.startswith(BREADCRUMB_SEP):
        return None
    return rest[len(BREADCRUMB_SEP):].split(BREADCRUMB_SEP)


# -------------------------------------------------------------------- TOC

class Toc:
    """The bookmark TOC as page spans in reading order.

    An entry runs from its page to the page where the next entry at its level
    or shallower starts (inclusive: that entry may start mid-page). Reading
    order is page, then TOC order -- a TOC listed alphabetically need not be
    in page order, and where two entries share a page, TOC order is document
    order.
    """

    def __init__(self, entries: list, page_count: int | None) -> None:
        rows = []
        for i, e in enumerate(entries or []):
            if not isinstance(e, dict):
                continue
            k = key(str(e.get("title") or ""))
            level, page = e.get("level"), e.get("page")
            if not k or not isinstance(level, int):
                continue
            rows.append((i, level, page if isinstance(page, int) and page >= 1 else None, k))
        self.size = len(rows)
        self.by_key: dict[str, list[int]] = {}
        for i, _, _, k in rows:
            self.by_key.setdefault(k, []).append(i)

        placed = sorted((r for r in rows if r[2] is not None), key=lambda r: (r[2], r[0]))
        levels = sorted({r[1] for r in placed})
        self.span: dict[int, tuple[int, int, int | None, int]] = {}  # i -> rank, start, end page, end rank
        nearest: dict[int, tuple[int, int]] = {}  # level -> next entry at that level or shallower
        for rank in range(len(placed) - 1, -1, -1):
            i, level, page, _ = placed[rank]
            nxt = nearest.get(level)
            end_page = nxt[0] if nxt else page_count
            self.span[i] = (rank, page, end_page, nxt[1] if nxt else len(placed))
            for lv in levels:
                if lv >= level:
                    nearest[lv] = (page, rank)

    def lookup(self, title: str) -> list[int]:
        return self.by_key.get(key(title), [])

    def overlapping(self, cands: list[int], pages: tuple[int, int] | None):
        """(candidates consistent with the pages, verdict): verdict is True if
        one overlaps, False if every one with a span misses, None if unknown."""
        if pages is None:
            return cands, None
        ps, pe = pages
        hits, known = [], False
        for c in cands:
            s = self.span.get(c)
            if s is None:
                continue
            known = True
            _, start, end, _ = s
            if start <= pe and (end is None or ps <= end):
                hits.append(c)
        if hits:
            return hits, True
        return cands, (False if known else None)

    def nests(self, outer: list[int], inner: list[int]) -> bool:
        """True if some candidate for the outer title contains some candidate
        for the inner one, or if the TOC gives no pages to tell."""
        so = [self.span[c] for c in outer if c in self.span]
        si = [self.span[c] for c in inner if c in self.span]
        if not so or not si:
            return True
        return any(o[0] < i[0] < o[3] for o in so for i in si)

    def describe(self, cands: list[int]) -> str:
        spans = [self.span[c] for c in cands if c in self.span][:3]
        return ", ".join(f"p{s[1]}-{s[2] if s[2] is not None else '?'}" for s in spans) or "no page"


# --------------------------------------------------------------- document

def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_text(path: Path):
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return UnicodeDecodeError
    except OSError:
        return None


def section_path(doc_dir: Path, name) -> Path | None:
    """Where a section's `file` points, or None unless it is a relative path
    inside sections/. build_search_db.py reads doc_dir / file as given: an
    absolute or `..` path would put a file from outside the document in the
    index, and a number would crash it."""
    if not isinstance(name, str) or not name.strip():
        return None
    parts = PurePosixPath(name.replace("\\", "/")).parts
    if len(parts) < 2 or parts[0] != "sections" or ".." in parts:
        return None
    return doc_dir.joinpath(*parts)


def valid_pages(sec: dict, page_count) -> tuple[str, tuple[int, int] | None]:
    ps, pe = sec.get("page_start"), sec.get("page_end")
    if ps is None and pe is None:
        return "none", None
    if not all(isinstance(p, int) and not isinstance(p, bool) for p in (ps, pe)):
        return "invalid", None
    upper = page_count if isinstance(page_count, int) and page_count >= 1 else None
    if ps < 1 or ps > pe or (upper is not None and pe > upper):
        return "invalid", None
    return "ok", (ps, pe)


def check_document(doc_dir: Path, collection_dir: Path, pdf_pages: int) -> dict:
    f = Findings()
    metrics: dict = {}
    result = {"collection": collection_dir.name, "folder": doc_dir.name, "slug": None,
              "path": str(doc_dir), "metrics": metrics, "findings": f}

    try:
        m = load_json(doc_dir / "manifest.json")
        if not isinstance(m, dict):
            raise ValueError("top level is not an object")
    except (OSError, ValueError) as exc:
        f.add("manifest-unreadable", str(exc)[:200])
        return result
    for field in REQUIRED_FIELDS:
        if field not in m:
            f.add("manifest-missing-field", field)
        elif not FIELD_VALID[field](m[field]):
            f.add("manifest-invalid-field", f"{field}: {m[field]!r}"[:120])
    result["slug"] = m.get("slug")
    if m.get("slug") is not None and m.get("slug") != doc_dir.name:
        f.add("slug-mismatch", f"folder {doc_dir.name!r}, manifest {m.get('slug')!r}")
    sections = m.get("sections")
    if not isinstance(sections, list):
        return result                 # reported above: missing or invalid

    title = str(m.get("title") or "").strip()
    page_count = m.get("page_count")
    toc = Toc(m.get("toc") or [], page_count if isinstance(page_count, int) else None)
    if not toc.size:
        f.add("toc-missing")
    if not (doc_dir / "full.md").is_file():
        f.add("full-md-missing")

    # ---- section files
    files = [s.get("file") if isinstance(s, dict) else None for s in sections]
    paths = [section_path(doc_dir, name) for name in files]
    with ThreadPoolExecutor(READ_THREADS) as pool:
        texts = list(pool.map(lambda p: read_text(p) if p else None, paths))
    listed = set()
    for s, name, path, text in zip(sections, files, paths, texts):
        if not name:
            f.add("section-without-file", str(s.get("heading") if isinstance(s, dict) else s)[:80])
            continue
        if path is None:
            f.add("section-bad-path", repr(name)[:120])
            continue
        if name in listed:
            f.add("section-duplicate-file", name)
        listed.add(name)
        if text is UnicodeDecodeError:
            f.add("section-unreadable", name)
        elif text is None:
            f.add("section-missing-file", name)
    on_disk = {f"sections/{p.name}" for p in (doc_dir / "sections").glob("*.md")} \
        if (doc_dir / "sections").is_dir() else set()
    for orphan in sorted(on_disk - listed):
        f.add("section-orphan-file", orphan)

    chunks = [(s, name, text) for s, name, text in zip(sections, files, texts)
              if isinstance(s, dict) and name and isinstance(text, str)]
    metrics["chunks"] = len(sections)
    sizes = [len(t) for _, _, t in chunks]
    metrics["median_chars"] = int(statistics.median(sizes)) if sizes else 0
    metrics["max_chars"] = max(sizes, default=0)

    # ---- per chunk
    n_pages = n_crumbs = n_ancestors = n_owned = n_named = 0
    prev_start = None
    pos = -1                      # reading-order rank of the last chunk's deepest ancestor
    page_of: dict[str, tuple[int, int] | None] = {}
    toc_empty = toc.size == 0
    owners = {key(str(s.get("command") or s.get("entity"))) for s, _, _ in chunks
              if s.get("command") or s.get("entity")}
    unnamed: list[str] = []
    feedback: list[str] = []
    title_lines: list[str] = []
    numbered: dict[str, set[str]] = {}     # digit-masked line -> the distinct lines behind it
    numbered_in: dict[str, set[str]] = {}  # digit-masked line -> chunks carrying it

    for s, name, text in chunks:
        if isinstance(s.get("chars"), int) and s["chars"] != len(text):
            f.add("chars-mismatch", f"{name}: manifest {s['chars']}, file {len(text)}")
        if len(text) > OVERSIZED:
            f.add("oversized-chunk", f"{name}: {len(text):,} chars")

        state, pages = valid_pages(s, page_count)
        page_of[name] = pages
        if state == "invalid":
            f.add("page-invalid", f"{name}: {s.get('page_start')}-{s.get('page_end')} of {page_count}")
        elif state == "ok":
            n_pages += 1
            if prev_start is not None and pages[0] < prev_start:
                f.add("pages-backwards", f"{name}: starts p{pages[0]} after a chunk starting p{prev_start}")
            prev_start = pages[0]

        lines = [ln for ln in text.splitlines() if ln.strip()]
        crumb = str(s.get("breadcrumb") or "").strip()
        body_lines = lines[1:] if lines and title and is_crumb_line(lines[0], title) else lines
        body = "\n".join(body_lines)
        if not TOKEN_RE.search(body.lower()):
            f.add("empty-chunk", name)
        if len(lines) >= 2 and title and is_crumb_line(lines[0], title) and is_crumb_line(lines[1], title):
            f.add("breadcrumb-doubled", name)

        # ---- furniture: gathered here, judged once the whole document is seen
        masked_here = set()
        for line in lines_outside_code(body):
            c = line_core(line)
            if c.lower() == "feedback":
                feedback.append(name)
            elif title and len(c) <= len(title) + 30 and tokens(title) and tokens(c).startswith(tokens(title)):
                title_lines.append(name)
            elif (len(c) <= 80 and re.search(r"\d", c) and len(re.findall(r"[A-Za-z]{2,}", c)) >= 2
                  and not line.lstrip().startswith(("#", "|")) and not CODE_CHARS.search(c)):
                masked_here.add((re.sub(r"\d+", "#", c), c))
        for masked, raw in masked_here:
            numbered.setdefault(masked, set()).add(raw)
            numbered_in.setdefault(masked, set()).add(name)

        # ---- breadcrumb
        entity = s.get("command") or s.get("entity")
        entity = str(entity).strip() if entity else None
        if not crumb:
            f.add("breadcrumb-missing", name)
            ancestors = None
        else:
            n_crumbs += 1
            ancestors = split_breadcrumb(crumb, title)
            if ancestors is None:
                f.add("breadcrumb-not-rooted", f"{name}: {crumb[:100]!r}")
            if lines and lines[0].strip() != f"*{crumb}*":
                f.add("breadcrumb-file-mismatch", f"{name}: file starts {lines[0].strip()[:80]!r}")

        # The entity, when it ends the breadcrumb, is judged by the entity
        # checks below rather than twice.
        chain: list[list[int]] = []
        if ancestors:
            n_ancestors += 1
            if toc_empty:
                f.add("ancestor-unverifiable", f"{name}: {' › '.join(ancestors)[:100]}")
            else:
                wrong = []
                for j, a in enumerate(ancestors):
                    is_owner = entity is not None and j == len(ancestors) - 1 and key(a) == key(entity)
                    cands = toc.lookup(a)
                    if not cands:
                        if not is_owner:
                            f.add("ancestor-not-in-toc", f"{name}: {a!r}")
                        continue
                    fits, verdict = toc.overlapping(cands, pages)
                    if verdict is False:
                        if not is_owner:
                            wrong.append(f"{a!r} is at {toc.describe(cands)}")
                        continue
                    chain.append(fits)
                if wrong:
                    f.add("ancestor-contradicts-pages", f"{name} p{pages[0]}-{pages[1]}: " + "; ".join(wrong))
                if any(not toc.nests(outer, inner) for outer, inner in zip(chain, chain[1:])):
                    f.add("ancestor-not-nested", f"{name}: {crumb[:140]!r}")

        # ---- entity
        if entity:
            n_owned += 1
            if contains(tokens(body), key(entity)):
                n_named += 1
            else:
                unnamed.append(f"{name} ({len(text)} chars)")
            heading = key(re.sub(r"\(intro\)", "", str(s.get("heading") or "")))
            if heading in owners and heading != key(entity):
                f.add("entity-heading-mismatch", f"{name}: headed {s.get('heading')!r}, owned by {entity!r}")
            if ancestors is not None and (not ancestors or key(ancestors[-1]) != key(entity)):
                f.add("entity-breadcrumb-mismatch", f"{name}: owner {entity!r}, breadcrumb {crumb[:80]!r}")
            if not toc_empty:
                cands = toc.lookup(entity)
                if not cands:
                    f.add("entity-not-in-toc", f"{name}: {entity!r}")
                else:
                    fits, verdict = toc.overlapping(cands, pages)
                    if verdict is False:
                        f.add("entity-contradicts-pages",
                              f"{name} p{pages[0]}-{pages[1]}: {entity!r} is at {toc.describe(cands)}")
                    else:
                        chain.append(fits)

        # ---- order
        # An entry enclosing where the last chunk was is preferred to one
        # ahead: titles repeat, and jumping to the next "Simulating the Design"
        # 1,300 entries on made every chunk after it look out of order.
        ranked = [toc.span[c] for c in (chain[-1] if chain else []) if c in toc.span]
        if ranked and not any(r[0] <= pos < r[3] for r in ranked):
            ahead = [r for r in ranked if r[0] >= pos]
            if ahead:
                pos = min(r[0] for r in ahead)
            else:
                f.add("ancestor-out-of-order", f"{name}: back to TOC entry #{min(r[0] for r in ranked)} "
                                               f"after #{pos}")

    # Furniture, judged across the document. A title line in one or two chunks
    # is the cover or an introduction naming the manual; a running footer
    # repeats. A repeated line whose number changes nearly every time it
    # appears is a page label ("5-138 Software Version 2026.2"): content
    # repeats its numbers, pages do not.
    dirty: dict[str, str] = {name: "a standalone 'Feedback' line" for name in feedback}
    if len(set(title_lines)) >= 3:
        for name in title_lines:
            dirty.setdefault(name, "the running title")
    floor = max(FOOTER_MIN_CHUNKS, FOOTER_MIN_SHARE * len(chunks))
    for masked, raws in numbered.items():
        carriers = numbered_in[masked]
        if len(carriers) >= floor and len(raws) >= FOOTER_VARIETY * len(carriers):
            example = min(raws)
            for name in carriers:
                dirty.setdefault(name, f"a page label like {example!r} (in {len(carriers)} chunks)")
    for name in sorted(dirty):
        f.add("furniture", f"{name}: {dirty[name]}")

    n = len(chunks) or 1
    metrics["pages"] = n_pages / n
    metrics["breadcrumbs"] = n_crumbs / n
    metrics["with_ancestors"] = n_ancestors / n
    metrics["owned"] = n_owned / n
    metrics["owner_named"] = n_named / n_owned if n_owned else None
    metrics["furniture_chunks"] = len(dirty)
    if chunks and n_pages == 0:
        f.add("no-pages")
    elif 0 < n_pages < len(chunks):
        f.add("pages-partial", f"{n_pages} of {len(chunks)} chunks")
    if n_owned and n_named / n_owned < ENTITY_NAMED_MIN:
        f.add("entity-rarely-named", f"{n_named} of {n_owned} ({n_named / n_owned:.0%}) do; "
                                     f"not: {', '.join(unnamed[:3])}")

    check_figures(doc_dir, page_count, page_of, listed, f, metrics)

    pdf = find_source_pdf(collection_dir, m.get("source_pdf"))
    if pdf is None:
        f.add("source-pdf-missing", str(m.get("source_pdf")))
    elif pdf_pages > 0:
        check_content(pdf, page_count, chunks, page_of, pdf_pages, f, metrics)
    return result


def check_figures(doc_dir: Path, page_count, page_of: dict, listed: set, f: Findings, metrics: dict) -> None:
    path = doc_dir / "figures.json"
    if not path.is_file():
        metrics["figures"] = 0
        return
    try:
        figures = load_json(path).get("figures", [])
    except (OSError, ValueError, AttributeError) as exc:
        f.add("figures-unreadable", str(exc)[:200])
        return
    metrics["figures"] = len(figures)
    attached = 0
    for fig in figures:
        if not isinstance(fig, dict):
            continue
        fid = fig.get("id") or fig.get("file")
        if not fig.get("file") or not (doc_dir / fig["file"]).is_file():
            f.add("figure-file-missing", str(fid))
        page = fig.get("page")
        if not (isinstance(page, int) and page >= 1 and (not isinstance(page_count, int) or page <= page_count)):
            f.add("figure-page-invalid", f"{fid}: page {page}")
            page = None
        section = fig.get("section")
        if not section:
            continue
        if section not in listed:
            f.add("figure-section-dangling", f"{fid}: {section}")
            continue
        attached += 1
        pages = page_of.get(section)
        if page is not None and pages and not pages[0] <= page <= pages[1]:
            f.add("figure-outside-section", f"{fid} on p{page}, {section} is p{pages[0]}-{pages[1]}")
    metrics["figures_attached"] = attached / len(figures) if figures else None


_pymupdf = None


def check_content(pdf: Path, page_count, chunks: list, page_of: dict, n_sample: int,
                  f: Findings, metrics: dict) -> None:
    """Sample PDF pages and look for their words in the chunks.

    With pages on the chunks, a page is compared with the chunks claiming it,
    which checks the page numbers as well as the text. Without, only with the
    whole document -- dropped text whose words appear somewhere else passes.
    """
    global _pymupdf
    if _pymupdf is None:
        try:
            import pymupdf
            _pymupdf = pymupdf
        except ImportError:
            _pymupdf = False
    # A check that did not run is reported, so --strict cannot pass what it
    # never looked at.
    metrics["content"] = None
    if not _pymupdf:
        f.add("content-unchecked", "PyMuPDF is not installed")
        return
    try:
        doc = _pymupdf.open(str(pdf))
        if doc.needs_pass:
            raise ValueError("encrypted")
    except Exception as exc:  # damaged or encrypted: a finding, not a crash
        f.add("content-unchecked", f"{pdf.name} would not open: {str(exc)[:120]}")
        return
    # valid_pages trusts the manifest's page_count, so check it against the PDF.
    if isinstance(page_count, int) and page_count != doc.page_count:
        f.add("page-count-mismatch", f"manifest {page_count}, {pdf.name} {doc.page_count}")

    chunk_words = [words(t) for _, _, t in chunks]
    everything = set().union(*chunk_words) if chunk_words else set()
    by_page = [page_of.get(name) for _, name, _ in chunks]
    paged = any(p is not None for p in by_page)
    total = doc.page_count
    n = min(n_sample, total)
    sample = sorted({1 + round(i * (total - 1) / (n - 1)) for i in range(n)}) if n > 1 else [1]

    scores = []
    for p in sample:
        pw = words(doc[p - 1].get_text())
        if len(pw) < MIN_PAGE_WORDS:
            continue
        anywhere = len(pw & everything) / len(pw)
        if paged:
            claimed = set().union(*(w for w, r in zip(chunk_words, by_page) if r and r[0] <= p <= r[1]))
            here = len(pw & claimed) / len(pw)
        else:
            here = anywhere
        scores.append(here)
        if here < CONTENT_MIN:
            if paged and anywhere >= CONTENT_MIN:
                why = f"{anywhere:.0%} elsewhere in the document: page numbers wrong"
            elif paged and not claimed:
                why = "no chunk claims this page"
            else:
                why = "text missing from the chunks"
            f.add("content-gap", f"p{p}: {here:.0%} of {len(pw)} words, {why}")
    doc.close()
    metrics["content"] = statistics.mean(scores) if scores else None
    metrics["content_mode"] = "page" if paged else "document"
    metrics["content_pages"] = len(scores)


# ------------------------------------------------------------- collection

def index_skips(name: str) -> bool:
    """build_index.py's rule for folders under docs/ that are not documents."""
    return name.endswith((".old", ".new")) or name.startswith((".", "_"))


def find_source_pdf(collection_dir: Path, name) -> Path | None:
    """Where a collection keeps a converted document's PDF: source/, or the
    collection folder itself (every PDF's place before source/ existed). Not
    new_docs/: a PDF waiting there under the same name is as likely the next
    release, and checking this document's pages against it proves nothing."""
    if not isinstance(name, str) or not name.strip():
        return None
    for folder in (collection_dir / "source", collection_dir):
        if (folder / name).is_file():
            return folder / name
    return None


def version_numbers(version) -> tuple | None:
    """The numbers editions are ordered by, or None when a version has none.
    Where there is a year: year, release, any further dotted components, then
    -1 and the service pack ("2026.1.1" is not "2026.1.2", and neither is
    "2026.1"). Otherwise the numbers as written. The same rule as
    editions.version_key, written out again because this file shares no code
    with what it checks; tests hold the two together."""
    v = re.sub(r"\s+", "", str(version or "")).upper().replace("_", ".")
    m = re.search(r"(20\d\d)\.(\d{1,2})((?:\.\d+)*)", v)
    if m:
        sp = re.search(r"SP(\d+)(?:-(\d+))?", v)
        further = tuple(int(n) for n in m.group(3).split(".") if n)
        return (int(m.group(1)), int(m.group(2)), *further, -1, int(sp.group(1)) if sp else 0,
                int(sp.group(2)) if sp and sp.group(2) else 0)
    return tuple(int(n) for n in re.findall(r"\d+", v)) or None


def same_release(a, b) -> bool:
    """Two versions naming one release, as build_search_db.py decides it: by
    their numbers ("2026.3" is "Y-2026.03"), or as written where a version
    has none."""
    na, nb = version_numbers(a), version_numbers(b)
    if na is None or nb is None:
        text_a, text_b = (re.sub(r"\s+", "", str(v or "")).upper().replace("_", ".") for v in (a, b))
        return bool(text_a) and text_a == text_b
    return na == nb


def check_editions(collection_dir: Path, manifests: dict, f: Findings) -> dict[str, bool]:
    """Raise what is wrong with the collection's editions, and return which
    document folders are current -- for the manuals where that can be told."""
    lines: dict[str, list[tuple[str, object]]] = {}
    later: dict[str, set] = {}
    titles: dict[str, list[tuple[str, str]]] = {}
    for folder, m in sorted(manifests.items()):
        doc_id = str(m.get("doc_id") or m.get("slug") or folder)
        lines.setdefault(doc_id, []).append((folder, m.get("version")))
        if m.get("version_and_later") is True:
            later.setdefault(doc_id, set()).add(version_numbers(m.get("version")))
        # The release is part of some titles ("VCS User Guide, Version
        # T-2022.06") and is exactly what two editions' titles differ by.
        title = str(m.get("title") or "")
        if m.get("version"):
            title = title.replace(str(m["version"]), " ")
        if key(title):
            titles.setdefault(key(title), []).append((folder, doc_id))

    chosen: dict[str, str] = {}             # doc_id -> the folder that is current
    by_numbers: dict[str, dict[tuple, str]] = {}
    for doc_id, editions in lines.items():
        if len(editions) < 2:
            chosen[doc_id] = editions[0][0]
            by_numbers[doc_id] = {version_numbers(editions[0][1]): editions[0][0]}
            continue
        seen: dict[tuple, str] = {}
        sound = True
        for folder, version in editions:
            numbers = version_numbers(version)
            if numbers is None:
                f.add("edition-unordered", f"{doc_id}: {folder} has version {version!r}")
                sound = False
            elif numbers in seen:
                f.add("edition-duplicate-version", f"{doc_id}: {seen[numbers]} and {folder} are both {version}")
                sound = False
            else:
                seen[numbers] = folder
        if sound:
            chosen[doc_id] = seen[max(seen)]
            by_numbers[doc_id] = seen
    for same in titles.values():
        if len({doc_id for _folder, doc_id in same}) > 1:
            f.add("edition-ungrouped", ", ".join(f"{folder} (doc_id {doc_id})" for folder, doc_id in same)[:200])

    def current() -> dict[str, bool]:
        return {folder: folder == chosen[doc_id] for doc_id in chosen for folder, _v in lines[doc_id]}

    pins_path = collection_dir / "current_versions.json"
    if not pins_path.is_file():
        return current()
    try:
        pins = load_json(pins_path)
        if not isinstance(pins, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as exc:
        f.add("pin-invalid", str(exc)[:100])
        return {}
    for doc_id, version in pins.items():
        if doc_id not in lines:
            f.add("pin-invalid", f"{doc_id!r} is not a doc_id here")
            continue
        # A pin is a version as text; the index build stops on anything else.
        if not isinstance(version, str) or not version.strip():
            f.add("pin-invalid", f"{doc_id} pinned to {version!r}, which is not a version written as text")
            chosen.pop(doc_id, None)
            continue
        # It names the tool release in use. An edition for that release
        # applies to it; so does the nearest earlier edition, if its cover
        # says "and later".
        wanted = version_numbers(version)
        have = {version_numbers(v) for _folder, v in lines[doc_id]} - {None}
        earlier = [n for n in have if wanted is not None and n < wanted]
        exact = [folder for folder, v in lines[doc_id] if same_release(version, v)]
        if exact:
            pinned = exact[0]
        elif earlier and max(earlier) in later.get(doc_id, ()):
            pinned = by_numbers.get(doc_id, {}).get(max(earlier))
        else:
            f.add("pin-invalid", f"{doc_id} pinned to {version!r}; editions here: "
                                 + ", ".join(str(v) for _folder, v in lines[doc_id]))
            chosen.pop(doc_id, None)
            continue
        if doc_id in chosen and pinned:
            chosen[doc_id] = pinned
    return current()


def check_collection(collection_dir: Path, doc_dirs: list[Path], hidden: list[Path], reports: list[dict]) -> Findings:
    f = Findings()
    for h in hidden:
        f.add("hidden-document", f"docs/{h.name}")

    docs = collection_dir / "docs"
    manifests = {}
    for r in reports:
        m = None
        try:
            m = load_json(Path(r["path"]) / "manifest.json")
        except (OSError, ValueError):
            pass
        if isinstance(m, dict):
            manifests[r["folder"]] = m

    index_path = docs / "index.json"
    listed = None
    if not index_path.is_file():
        f.add("index-missing")
    else:
        try:
            listed = {e["slug"]: e for e in load_json(index_path).get("manuals", [])}
        except (OSError, ValueError, AttributeError, KeyError, TypeError) as exc:
            f.add("index-stale", f"unreadable: {str(exc)[:100]}")
            listed = None
        if listed is not None:
            for folder in sorted(set(manifests) - set(listed)):
                f.add("index-stale", f"{folder} is on disk but not in index.json")
            for slug in sorted(set(listed) - {d.name for d in doc_dirs}):
                f.add("index-stale", f"{slug} is in index.json but not on disk")
            # Every field build_index.py writes, so an edited title or page
            # count is caught as well as a changed number of sections.
            for folder, m in sorted(manifests.items()):
                e = listed.get(folder)
                if not e:
                    continue
                expected = {"title": m.get("title"), "source_pdf": m.get("source_pdf"),
                            "page_count": m.get("page_count"),
                            "section_count": len(m["sections"]) if isinstance(m.get("sections"), list) else None,
                            "full_md": f"{folder}/full.md", "sections_dir": f"{folder}/sections/"}
                # Only where the manifest says: an index written before
                # editions were recorded is not stale for lacking them.
                expected.update({k: m[k] for k in ("doc_id", "version") if m.get(k)})
                if m.get("version_and_later") is True:
                    expected["version_and_later"] = True
                for field, want in expected.items():
                    if e.get(field) != want:
                        f.add("index-stale", f"{folder}: index.json {field} {e.get(field)!r}, "
                                             f"on disk {want!r}"[:160])

    superseded, sup_path = [], collection_dir / "superseded.json"
    if sup_path.is_file():
        try:
            superseded = load_json(sup_path)
            if not isinstance(superseded, list):
                raise ValueError("not a list")
        except (OSError, ValueError) as exc:
            f.add("superseded-invalid", str(exc)[:100])
            superseded = []
    slugs = {d.name for d in doc_dirs}
    converted = {str(m.get("source_pdf")): folder for folder, m in manifests.items()}
    for e in superseded:
        if not isinstance(e, dict) or not e.get("file") or not e.get("superseded_by"):
            f.add("superseded-invalid", f"entry {e!r}"[:120])
        elif e["superseded_by"] not in slugs:
            f.add("superseded-invalid", f"{e['file']} superseded by {e['superseded_by']!r}, which is not here")
        elif e["file"] in converted:
            # Set aside once, converted as an edition since: build_index.py
            # would list it both as a manual and as a PDF that was not converted.
            f.add("superseded-invalid", f"{e['file']} is converted as {converted[e['file']]}; "
                                        "remove its entry")

    accounted = {str(m.get("source_pdf")) for m in manifests.values()} | \
                {str(e.get("file")) for e in superseded if isinstance(e, dict)}
    # new_docs/ is where PDFs wait to be converted, so nothing there is amiss.
    # By suffix in any case: a glob for *.pdf misses X.PDF where names are
    # case-sensitive.
    for folder in (collection_dir / "source", collection_dir):
        for pdf in sorted(folder.iterdir()) if folder.is_dir() else []:
            if pdf.is_file() and pdf.suffix.lower() == ".pdf" and pdf.name not in accounted:
                f.add("pdf-unaccounted", str(pdf.relative_to(collection_dir)))

    # index.json also says which edition of each manual is current. A pin
    # changed and only the search index rebuilt leaves that saying otherwise.
    for folder, is_current in sorted(check_editions(collection_dir, manifests, f).items()):
        e = (listed or {}).get(folder)
        if e is not None and "current" in e and bool(e["current"]) != is_current:
            f.add("index-stale", f"{folder}: index.json has it as {'' if e['current'] else 'not '}the "
                                 "current edition; the manifests and current_versions.json say otherwise")
    return f


def find_targets(path: Path):
    """[(collection_dir, doc_dirs, hidden_dirs)], and whether collection-level
    checks apply. Discovery follows build_search_db.py: <root>/docs is one
    collection, otherwise every <root>/<collection>/docs."""
    if (path / "manifest.json").is_file():
        return [(path.parent.parent, [path], [])], False
    if (path / "docs").is_dir():
        collections = [path]
    else:
        collections = sorted(p for p in path.iterdir() if p.is_dir() and (p / "docs").is_dir())
    out = []
    for c in collections:
        found = sorted(d for d in (c / "docs").iterdir() if d.is_dir() and (d / "manifest.json").is_file())
        if found:
            out.append((c, [d for d in found if not index_skips(d.name)], [d for d in found if index_skips(d.name)]))
    return out, True


# ----------------------------------------------------------------- report

def pct(x) -> str:
    return "-" if x is None else f"{x:.0%}"


def print_report(root: Path, results: list[dict], collection_findings: dict, pdf_pages: int) -> None:
    content = f"{pdf_pages} sampled PDF pages each" if pdf_pages else "content check off"
    print(f"Checked {len(results)} documents under {root}  ({content})\n")
    by_collection: dict[str, list[dict]] = {}
    for r in results:
        by_collection.setdefault(r["collection"], []).append(r)
    head = f"  {'document':42s} {'chunks':>6s} {'pages':>6s} {'crumbs':>6s} {'ances':>6s} {'owned':>6s} " \
           f"{'named':>6s} {'figs':>5s} {'content':>9s}  status"
    for coll, rows in by_collection.items():
        print(coll)
        print(head)
        for r in rows:
            mt = r["metrics"]
            content_col = "-" if mt.get("content") is None else \
                f"{mt['content']:.0%} {'pg' if mt.get('content_mode') == 'page' else 'doc'}"
            print(f"  {r['folder'][:42]:42s} {mt.get('chunks', 0):6d} {pct(mt.get('pages')):>6s} "
                  f"{pct(mt.get('breadcrumbs')):>6s} {pct(mt.get('with_ancestors')):>6s} "
                  f"{pct(mt.get('owned') or None):>6s} {pct(mt.get('owner_named')):>6s} "
                  f"{mt.get('figures', 0):5d} {content_col:>9s}  {r['findings'].worst()}")
        print()

    problems = [(f"{r['collection']}/{r['folder']}", r["findings"]) for r in results]
    problems += [(f"{c} (collection)", cf) for c, cf in collection_findings.items()]
    problems = [(where, fs) for where, fs in problems if fs.items]
    if not problems:
        print("No problems found.")
        return
    print("Problems")
    for where, fs in problems:
        for item in fs.as_list():
            print(f"  {item['severity']}  {where}  {item['check']}  x{item['count']}")
            print(f"        {item['message']}")
            for ex in item["examples"]:
                print(f"        e.g. {ex}")
    print()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path, help="corpus root, one collection, or one docs/<slug> folder")
    ap.add_argument("--only", action="append", metavar="SLUG", help="check only this document (repeatable)")
    ap.add_argument("--json", type=Path, metavar="FILE", help="also write the full report as JSON")
    ap.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    ap.add_argument("--pdf-pages", type=int, default=DEFAULT_PDF_PAGES, metavar="N",
                    help=f"PDF pages sampled per document for the content check (default {DEFAULT_PDF_PAGES})")
    ap.add_argument("--no-pdf", action="store_true", help="skip the content check")
    args = ap.parse_args()

    root = args.path.resolve()
    if not root.is_dir():
        print(f"{root} is not a folder", file=sys.stderr)
        return 2
    targets, collection_checks = find_targets(root)
    pdf_pages = 0 if args.no_pdf else max(0, args.pdf_pages)

    results, collection_findings = [], {}
    for collection_dir, doc_dirs, hidden in targets:
        chosen = [d for d in doc_dirs if not args.only or d.name in args.only]
        reports = [check_document(d, collection_dir, pdf_pages) for d in chosen]
        results += reports
        if collection_checks and not args.only:
            collection_findings[collection_dir.name] = check_collection(collection_dir, doc_dirs, hidden, reports)

    if not results:
        print(f"No documents found under {root}. Expected <root>/docs/<slug>/manifest.json, "
              "<root>/<collection>/docs/<slug>/manifest.json, or a docs/<slug> folder.", file=sys.stderr)
        return 2

    # A slug shared anywhere under the root stops build_search_db.py, whichever
    # collections the two copies sit in.
    if collection_checks and not args.only:
        seen: dict[str, list[str]] = {}
        for collection_dir, doc_dirs, hidden in targets:
            for d in doc_dirs + hidden:
                try:
                    slug = load_json(d / "manifest.json").get("slug")
                except (OSError, ValueError, AttributeError):
                    continue
                if slug:
                    seen.setdefault(slug, []).append(f"{collection_dir.name}/docs/{d.name}")
        for slug, where in seen.items():
            if len(where) > 1:
                owner = where[1].split("/docs/")[0]
                collection_findings.setdefault(owner, Findings()).add(
                    "duplicate-slug", f"{slug!r} in {', '.join(where)}")

    if pdf_pages and _pymupdf is False:
        print("note: pymupdf is not installed, so the content check did not run "
              "(pip install -r scripts/requirements.txt)\n")
    print_report(root, results, collection_findings, pdf_pages)

    all_findings = [r["findings"] for r in results] + list(collection_findings.values())
    fails = sum(1 for fs in all_findings for i in fs.items.values() if i["severity"] == FAIL)
    warns = sum(1 for fs in all_findings for i in fs.items.values() if i["severity"] == WARN)
    print(f"{fails} failing check(s), {warns} warning(s) across {len(results)} document(s).")

    if args.json:
        payload = {
            "root": str(root),
            "pdf_pages": pdf_pages,
            "documents": [{**{k: v for k, v in r.items() if k != "findings"},
                           "status": r["findings"].worst(), "problems": r["findings"].as_list()}
                          for r in results],
            "collections": {c: fs.as_list() for c, fs in collection_findings.items()},
            "failing_checks": fails,
            "warnings": warns,
        }
        args.json.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return 1 if fails or (args.strict and warns) else 0


if __name__ == "__main__":
    sys.exit(main())
