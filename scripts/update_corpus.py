#!/usr/bin/env python3
"""
Bring a corpus up to date from the PDFs waiting in its collections' new_docs/,
in one command that stops wherever a step would otherwise have to guess.

  python scripts/update_corpus.py my-corpus --dry-run    # the plan; changes nothing
  python scripts/update_corpus.py my-corpus              # convert, check, index
  python scripts/update_corpus.py my-corpus --questions eval/questions.jsonl

It runs the scripts the long form in README.md runs, in the same order, and
prints each one's command line before running it, so any step can be rerun by
hand and nothing happens that the long form would not do:

  1. Each PDF in new_docs/: pick_extractor.py's verdict and a title, then
     convert_manual.py --shape prose|reference, or convert_docling.py for
     prose where Docling is installed. A new edition of a manual takes the
     converter its other editions were converted with.
  2. The new documents: enrich_chunks.py (prose only), extract_figures.py,
     ocr_figures.py (skipped, and said, without Tesseract's language data),
     then build_index.py for each collection they are in.
  3. check_corpus.py --strict on the new documents. A FAIL stops the run
     here, before the index is touched.
  4. build_search_db.py into a candidate beside the index, mcp_smoke_test.py
     on the candidate and, with --questions, eval_search.py on the index and
     on the candidate. The candidate replaces the index only if the smoke
     test passes and neither hit@5 nor MRR drops (--accept-regression takes
     a drop). A refused candidate is kept beside the index, to look at.
  5. A report on one screen.

The new documents are every document the current index does not hold as it
is on disk: those converted now, and any a run that stopped left behind or
that was converted by hand since. A rerun after a stop carries on from there.
Of those, enrich_chunks.py rewrites only prose that has not been enriched,
and extract_figures.py reads only documents with no figures.json yet: a
document finished earlier is checked and indexed, never edited again.

A PDF this cannot decide about stays in new_docs/, untouched, and the report
says what it needs:
  - no text layer, or no bookmark outline (pick_extractor.py names no converter)
  - a mixed document, or a reference whose command level the converter would
    pick differently from pick_extractor.py, or not at all
  - prose while Docling is not installed: --prose-converter convert_manual
    converts prose with convert_manual.py instead
  - no title. The PDF's metadata title is taken when its first pages print
    it; failing that, the cover's largest text when later pages print it as
    a line of its own, the way a running title is. Otherwise it waits for
    one in --titles.
  - anything editions.py refuses: a release already converted, a file already
    filed, a second edition with no version
  - a docs/<slug> that already exists. Nothing here replaces or deletes a
    document; reconverting one is --replace, by hand.

--titles names a JSON file of decisions, keyed by the PDF's filename, or by
"<collection folder>/<filename>" where two collections wait on one name:

  {"guide.pdf": "Widget User Guide",
   "commands.pdf": {"title": "Widget Command Reference", "command_level": 2},
   "manual.pdf": {"shape": "prose", "converter": "convert_manual"}}

A string is a title. An object may hold title, shape (prose or reference),
command_level, converter (convert_manual or convert_docling, for prose),
version and doc_id: each is the converter option of that name.

Exit status: 0 when every waiting PDF was converted and the index is up to
date; 2 when the index is up to date but some PDFs still wait on a decision;
1 when the run stopped before replacing the index.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter, deque
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import editions  # noqa: E402
from _common import utf8_console  # noqa: E402

try:
    import pymupdf
except ImportError:
    sys.exit("Missing dependency. Run: pip install -r scripts/requirements.txt")

import build_search_db  # noqa: E402
import ocr_figures  # noqa: E402
import pick_extractor  # noqa: E402

PY = sys.executable

# A cover's largest text is taken as the title only when at least this share
# of later pages print it as a line of its own, as a running header or footer
# does. Measured on 36 vendor PDFs: as a line of its own (running_share),
# 95-100% of sampled pages for 29 of the 33 covers whose text is the title
# and 0% for the other four, whose footers add more to the line; 0% for the
# three that are not the title (a slide deck's banner, a title run on into
# "For Use With ..."). Every one of the 36 had its title from the metadata
# first, or waited.
RUNNING_TITLE_SHARE = 0.3
RUNNING_SAMPLE = 40

# Every script the long form runs, in its order (README.md's "The same, step
# by step" and SKILL.md's "Adding one document later"). An earlier wrapper
# fell four features behind the scripts it wrapped; test_53i fails when the
# long form names a script that is not here.
PIPELINE = ("pick_extractor.py", "convert_manual.py", "convert_docling.py", "enrich_chunks.py",
            "extract_figures.py", "ocr_figures.py", "build_index.py", "check_corpus.py",
            "build_search_db.py", "mcp_smoke_test.py", "eval_search.py")

CONVERTER_CHOICES = ("convert_manual", "convert_docling")
DECISION_TYPES = {"title": str, "shape": str, "command_level": int, "converter": str,
                  "version": str, "doc_id": str}


# ------------------------------------------------------------------ titles

def title_words(text: str | None) -> str:
    """A title as its words: lowercase letters and digits only, with a typed
    (R) or (TM) dropped, so "Compiler®" on a cover and "Compiler(R)" in the
    metadata read the same, and so do "User's" and "User’s"."""
    text = re.sub(r"\((?:R|TM|C)\)", " ", text or "", flags=re.I)
    return " ".join(re.findall(r"[0-9a-z]+", text.casefold()))


def contains(words: str, text_words: str) -> bool:
    return bool(words) and f" {words} " in f" {text_words} "


def implausible(title: str) -> str | None:
    """Why `title` cannot be a document's title, or None. Junk such as a
    file name or "Microsoft Word - x.docx" is not caught here but by the
    PDF itself: a title is taken only where the document prints it."""
    if sum(c.isalpha() for c in title) < 3:
        return "has no words in it"
    if len(title) > 200:
        return "is too long to be a title"
    return None


def cover_text(page) -> str | None:
    """The text set in the largest type on a page, in reading order."""
    spans = [(s["size"], " ".join(s["text"].split()))
             for block in page.get_text("dict")["blocks"] for line in block.get("lines", ())
             for s in line["spans"] if sum(c.isalpha() for c in s["text"]) >= 2]
    if not spans:
        return None
    top = max(size for size, _text in spans)
    return " ".join(text for size, text in spans if size >= top - 0.5) or None


# What a running header or footer prints beside a title on its line: a
# version, a release letter, a page number ("v2026.2", "Version Y-2026.03", "5-138").
BESIDE_TITLE_RE = re.compile(r"^(?:software|version|release|page|v?\d+|[a-z])$")


def core(words: str) -> str:
    """A line's words without the version and page numbers at either end."""
    w = words.split()
    while w and BESIDE_TITLE_RE.match(w[0]):
        w.pop(0)
    while w and BESIDE_TITLE_RE.match(w[-1]):
        w.pop()
    return " ".join(w)


def running_share(doc, words: str) -> float:
    """The share of a sample of the pages after the first with a line that is
    `words`, give or take a version or page number beside it. A whole line,
    not a part of one: a product name set largest on a cover ("PrimeTime")
    is in every footer that names the manual ("PrimeTime User Guide"), and
    is still not the title."""
    later = list(range(1, doc.page_count))
    target = core(words)
    if not later or not target:
        return 0.0
    sample = later[::max(1, len(later) // RUNNING_SAMPLE)][:RUNNING_SAMPLE]
    return sum(1 for i in sample
               if any(core(title_words(line)) == target for line in doc[i].get_text().splitlines())) / len(sample)


def find_title(doc) -> tuple[str | None, str]:
    """(title, where it was read) or (None, why none was taken).

    Two places, each taken only where the PDF itself confirms it: the
    metadata title when the first pages print it, and failing that the
    cover's largest text when it is the running title of later pages, a line
    of its own and not only part of one. A
    title that is wrong does more than mislabel: the converters strip the
    lines that repeat it as page furniture."""
    first = title_words("\n".join(doc[i].get_text() for i in range(min(editions.COVER_PAGES, doc.page_count))))
    why = []
    meta = " ".join(((doc.metadata or {}).get("title") or "").split())
    if not meta:
        why.append("the PDF's metadata has no title")
    elif implausible(meta):
        why.append(f"its metadata title {meta!r} {implausible(meta)}")
    elif not contains(title_words(meta), first):
        why.append(f"its metadata title {meta!r} is not printed on its first pages")
    else:
        return meta, "the PDF's metadata, printed on its cover"
    cover = cover_text(doc[0]) if doc.page_count else None
    if not cover:
        why.append("its first page has no text")
    elif implausible(cover):
        why.append(f"its cover's largest text {cover!r} {implausible(cover)}")
    elif running_share(doc, title_words(cover)) < RUNNING_TITLE_SHARE:
        why.append(f"its cover's largest text {cover!r} is not a line of its own on later pages, as a "
                   "running title is")
    else:
        return cover, "the cover, repeated as the running title"
    return None, "; ".join(why)


def load_decisions(path: Path) -> dict[str, dict]:
    """--titles: {name: title or {option: value}}. Raises ValueError on
    anything else: a decision that is silently ignored is a guess."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} is not readable JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be an object keyed by PDF filename")
    out = {}
    for name, value in data.items():
        value = {"title": value} if isinstance(value, str) else value
        if not isinstance(value, dict):
            raise ValueError(f"{path}: {name!r} must be a title or an object")
        unknown = sorted(set(value) - set(DECISION_TYPES))
        if unknown:
            raise ValueError(f"{path}: {name!r} has {', '.join(unknown)}; the options are "
                             f"{', '.join(DECISION_TYPES)}")
        for key, kind in DECISION_TYPES.items():
            if key in value and (not isinstance(value[key], kind) or isinstance(value[key], bool)
                                 or (kind is str and not value[key].strip())):
                raise ValueError(f"{path}: {name!r}: {key} must be a{' non-empty' if kind is str else 'n'} "
                                 f"{kind.__name__}")
        if value.get("shape") not in (None, "prose", "reference"):
            raise ValueError(f"{path}: {name!r}: shape must be prose or reference")
        if value.get("converter") not in (None, *CONVERTER_CHOICES):
            raise ValueError(f"{path}: {name!r}: converter must be {' or '.join(CONVERTER_CHOICES)}")
        if "command_level" in value and value["command_level"] < 1:
            raise ValueError(f"{path}: {name!r}: command_level is a TOC level, 1 or more")
        if value.get("converter") and value.get("shape") == "reference":
            raise ValueError(f"{path}: {name!r}: converter is for prose; a reference is converted "
                             "with convert_manual.py --shape reference")
        out[name] = value
    return out


# ------------------------------------------------------------------ intake

class Intake:
    """One PDF waiting in new_docs/, and what will be done with it. (Plain
    classes here, not dataclasses: tests load this file without importing it.)"""

    def __init__(self, collection: Path, pdf: Path):
        self.collection, self.pdf = collection, pdf
        self.shape: str | None = None
        self.why = ""
        self.converter: str | None = None
        self.title: str | None = None
        self.title_from = ""
        self.slug: str | None = None
        self.doc_id: str | None = None
        self.version: str | None = None
        self.edition: list[str] = []   # what editions.plan said
        self.held: list[str] = []      # why it waits; empty when it is converted
        self.command: list[str] | None = None

    @property
    def label(self) -> str:
        return f"{self.collection.name}/{editions.INBOX_DIR}/{self.pdf.name}"


def plan_pdf(coll: Path, pdf: Path, decision: dict, prose_converter: str | None, docling: bool) -> Intake:
    """Decide everything about one PDF before anything is written. Every
    reason it has to wait is collected, so one pass over --titles answers
    them all."""
    it = Intake(coll, pdf)
    extra: list[str] = []
    try:
        doc = pymupdf.open(str(pdf))
    except Exception as exc:  # damaged or encrypted
        it.held.append(f"cannot be opened ({str(exc)[:80]})")
        return it
    with doc:
        a = pick_extractor.assess(doc)
        it.shape, it.why = decision.get("shape") or a["verdict"], a["why"] or ""
        if a["verdict"] == "no-text":
            it.held.append("it has no text layer (scanned or image-only): OCR it first, as no converter "
                           "here can read it")
        elif it.shape == "no-toc":
            it.held.append('it has no bookmark outline, so pick_extractor.py names no converter and no '
                           'breadcrumb can be checked. Set "shape" for it in --titles, or convert it by hand')
        elif it.shape == "mixed":
            it.held.append(f'it is part prose, part reference ({a["why"]}). Look at it, then set "shape" '
                           'for it in --titles, or split the PDF')
        elif it.shape == "reference":
            level = decision.get("command_level")
            if level is None and a["verdict"] in ("reference", "mixed") and a["picked"] != a["level"]:
                it.held.append(
                    ("no TOC level has 20 titles shaped like commands, so the converter would attribute "
                     "no chunk to one" if a["picked"] is None else
                     f"the converter would take L{a['picked']} for commands")
                    + f", where pick_extractor.py found entries at L{a['level']}. Check which is right and "
                      f'set "command_level" for it in --titles')
            it.converter = "convert_manual.py"
            extra = ["--shape", "reference"] + (["--command-level", str(level)] if level is not None else [])
        if decision.get("title"):
            it.title, it.title_from = decision["title"].strip(), "--titles"
        else:
            it.title, it.title_from = find_title(doc)
            if it.title is None:
                it.held.append(f"no title: {it.title_from}. Give it one in --titles")

    # What the converter will be told about the edition, by the function it
    # calls itself. It writes nothing; where it would stop the converter, it
    # stops this PDF here instead.
    said = io.StringIO()
    try:
        with contextlib.redirect_stdout(said):
            p = editions.plan(pdf, None, None, decision.get("version"), decision.get("doc_id"))
    except SystemExit as exc:
        it.held.append(str(exc.code))
        p = None
    it.edition = [line.strip() for line in said.getvalue().splitlines() if line.strip()]
    # The converter would carry on past these; what they warn of (two editions
    # search would treat as unrelated manuals) is worse than waiting.
    it.held += [f"editions.py: {line.removeprefix('!!').strip()}" for line in it.edition if line.startswith("!!")]
    if p is not None:
        it.slug, it.doc_id, it.version = p.slug, p.doc_id, p.version
        if (p.out_root / p.slug).exists():
            it.held.append(f"docs/{p.slug} already exists. Nothing here replaces a document: reconvert it "
                           "by hand with --replace if that is what is meant")
    if it.shape == "prose" and a["verdict"] != "no-text":
        choice = prose_choice(it, p, decision, prose_converter, docling)
        if choice:
            it.converter = f"{choice}.py"
            extra = ["--shape", "prose"] if choice == "convert_manual" else []
    if not it.held:
        it.command = [PY, str(HERE / it.converter), str(pdf), "--title", it.title, "--slug", it.slug, *extra]
        for key, option in (("version", "--version"), ("doc_id", "--doc-id")):
            if decision.get(key):
                it.command += [option, decision[key]]
    return it


def prose_choice(it: Intake, p, decision: dict, prose_converter: str | None, docling: bool) -> str | None:
    """Which converter takes a prose PDF, or None with the reason it waits.

    The converter named for this PDF in --titles; else the one every other
    edition of its manual was converted with, as SKILL.md asks (two editions
    from different converters make compare_versions measure the converters);
    else --prose-converter; else Docling where it is installed, which is what
    pick_extractor.py names for prose."""
    choice = decision.get("converter")
    if not choice and p is not None:
        used = sorted({(m.get("converter") or {}).get("script") or "an unrecorded converter"
                       for folder, m in editions.load_manifests(p.out_root.parent).items()
                       if folder != p.slug and m.get("doc_id") == p.doc_id})
        if len(used) == 1 and used[0] in ("convert_manual.py", "convert_docling.py"):
            choice = used[0].removesuffix(".py")
            it.edition.append(f"converter: {used[0]}, as for this manual's other editions")
        elif used:
            it.edition.append(f"converter: this manual's other editions were converted with {', '.join(used)}, "
                              "so this one follows the default")
    choice = choice or prose_converter or ("convert_docling" if docling else None)
    if choice is None:
        it.held.append("pick_extractor.py names convert_docling.py for prose, and Docling is not "
                       "installed. Install it (pip install docling), or pass --prose-converter "
                       "convert_manual to convert prose with convert_manual.py: pages on every "
                       "chunk, without Docling's TOC-anchored breadcrumbs")
    elif choice == "convert_docling" and not docling:
        it.held.append("convert_docling.py is the converter for it, and Docling is not installed "
                       "(pip install docling)")
        choice = None
    return choice


def edition_clash(it: Intake, planned: list[Intake], slugs: set) -> bool:
    """Hold what editions.plan would refuse had the PDFs waiting beside `it`
    been filed already, as they will be by the time its converter runs: two
    editions of one manual where one has no version to order them by, or the
    same release twice. It sees only what is filed, so without this the plan
    showed both converting and the second converter then stopped, reported
    only as a failure. True when `it` was held for it."""
    held = False
    for other in planned:
        if not it.doc_id or other.doc_id != it.doc_id:
            continue
        if not it.version or not other.version:
            for lacking in (x for x in (it, other) if not x.version):
                lacking.held.append(f"{(other if lacking is it else it).pdf.name}, waiting beside it, is another "
                                    f"edition of {it.doc_id}, and this one has no version to order the two by. "
                                    'Set "version" for it in --titles')
                if lacking is other:
                    other.command = None
                    slugs.discard((other.collection, other.slug))
            held = held or not it.version
        elif editions.same_release(it.version, other.version):
            it.held.append(f"{other.pdf.name}, waiting beside it, is the same release of {it.doc_id} "
                           f"({other.version}). Delete one copy, or set \"doc_id\" or \"version\" for it in "
                           "--titles if they are different manuals or releases")
            held = True
    return held


def plan_intake(collections: list[Path], decisions: dict, prose_converter: str | None,
                docling: bool) -> tuple[list[Intake], list[str]]:
    """Every waiting PDF planned, and the --titles keys that matched none."""
    waiting = [(c, p) for c in collections for p in editions.inbox_pdfs(c)]
    names = Counter(p.name for _c, p in waiting)
    used, slugs, out = set(), set(), []
    for coll, pdf in waiting:
        key = f"{coll.name}/{pdf.name}"
        ambiguous = False
        if key in decisions:
            decision = decisions[key]
            used.add(key)
        else:
            decision = decisions.get(pdf.name, {})
            if pdf.name in decisions:
                used.add(pdf.name)
                ambiguous = names[pdf.name] > 1
        it = plan_pdf(coll, pdf, {} if ambiguous else decision, prose_converter, docling)
        if ambiguous:
            it.held.append(f"--titles names {pdf.name}, which waits in more than one collection: key it as "
                           f'"{key}"')
        clash = edition_clash(it, [i for i in out if i.collection == coll and i.command], slugs)
        if not clash and it.slug and (coll, it.slug) in slugs:
            it.held.append(f"another PDF waiting here would also become docs/{it.slug}")
        # editions.py compares a PDF with those already filed; two copies
        # waiting at once would be two manuals that answer every search alike.
        twin = editions.same_file(pdf, [i.pdf for i in out if i.collection == coll])
        if twin:
            it.held.append(f"it is byte for byte {twin.name}, also waiting here: delete one copy")
        if it.held:
            it.command = None
        elif it.slug:
            slugs.add((coll, it.slug))
        out.append(it)
    return out, sorted(set(decisions) - used)


# ------------------------------------------------------------- the index

def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def recorded_sources(db: Path) -> dict[str, str] | None:
    """{path: sha256} of every file the index at `db` was built from, or None
    where there is no index or it keeps no such record."""
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
        try:
            return dict(con.execute("SELECT path, sha256 FROM sources").fetchall())
        finally:
            con.close()
    except sqlite3.Error:
        return None


def changes_since_index(root: Path, collections: list[Path], db: Path) -> tuple[dict[Path, list[str]], list[str]]:
    """({collection: [slug, ...]} of the documents the index does not hold as
    they are on disk, [what else has changed that it was built from]).

    The same record the server reads to say an index is stale: each of a
    document's build_search_db.DOCUMENT_SOURCES and each collection's
    current_versions.json, by content. With no
    index every document is new to it; with an index that keeps no record,
    none can be told apart, and it is rebuilt."""
    recorded = recorded_sources(db)
    changed: dict[Path, list[str]] = {}
    if recorded is None:
        if db.is_file():
            return {}, [f"{db.name} keeps no record of what it was built from"]
        for coll in collections:
            slugs = [d.name for d in editions.document_dirs(coll / "docs")]
            if slugs:
                changed[coll] = slugs
        return changed, []
    if recorded and not any((root / rel).is_file() for rel in recorded):
        raise ValueError(f"none of the files {db} was built from is under {root}: it is the index of "
                         "another folder. Pass the corpus root it was built from, or --db for this one")
    seen, other = set(), []
    for coll in collections:
        for doc_dir in editions.document_dirs(coll / "docs"):
            for name in build_search_db.DOCUMENT_SOURCES:
                path = doc_dir / name
                if not path.is_file():
                    continue
                rel = path.relative_to(root).as_posix()
                seen.add(rel)
                if recorded.get(rel) != sha256(path) and doc_dir.name not in changed.get(coll, []):
                    changed.setdefault(coll, []).append(doc_dir.name)
        pins = coll / editions.PINS_FILE
        if pins.is_file() and pins.relative_to(root).as_posix() not in recorded:
            other.append(f"{pins.relative_to(root).as_posix()} is new")
    for rel, digest in sorted(recorded.items()):
        if rel in seen:
            continue
        path = root / rel
        if not path.is_file():
            other.append(f"{rel} is gone")
        elif sha256(path) != digest:
            other.append(f"{rel} has changed")
    return changed, other


def candidate_path(db: Path) -> Path:
    return db.with_name(f"{db.stem}.candidate{db.suffix}")


# ----------------------------------------------------------------- steps

def shown(cmd: list[str]) -> str:
    """A command line as it can be typed: `python scripts/x.py ...`."""
    def arg(a: str) -> str:
        return f'"{a}"' if not a or re.search(r'[\s"&|<>^;()$`\']', a) else a
    parts = ["python"]
    for a in cmd[1:]:
        if a.endswith(".py") and Path(a).parent == HERE:
            try:
                a = os.path.relpath(a)
            except ValueError:  # another drive
                pass
        parts.append(arg(a))
    return " ".join(parts)


def run_step(cmd: list[str]) -> tuple[int, list[str]]:
    """Run one script as its own process, its output shown as it comes.
    Returns its exit status and its last lines."""
    print(f"\n$ {shown(cmd)}", flush=True)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", env=env)
    tail: deque[str] = deque(maxlen=20)
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        if line.strip():
            tail.append(line.rstrip())
    return proc.wait(), list(tail)


def script(name: str) -> str:
    return str(HERE / name)


def only(slugs: list[str]) -> list[str]:
    return [a for s in slugs for a in ("--only", s)]


def read_manifest(coll: Path, slug: str) -> dict:
    return json.loads((coll / "docs" / slug / "manifest.json").read_text(encoding="utf-8-sig"))


def is_reference(manifest: dict) -> bool:
    return "attribution" in manifest


def needs_enrich(manifest: dict) -> bool:
    """Prose enrich_chunks.py may not have been over, so a document left by a
    run that stopped ends as one converted in a single run does.

    convert_manual.py writes no breadcrumb on prose and enrich_chunks.py
    writes one on every section, so a section without one means it has not
    run. A converter that writes its own breadcrumbs (Docling) leaves no such
    mark, and enrich_chunks.py still strips its page furniture, so that
    document is always passed again: enrich_chunks.py keeps those
    breadcrumbs, and a second pass over its own output changes nothing."""
    if is_reference(manifest):
        return False
    owned = (manifest.get("converter") or {}).get("owns_breadcrumbs") is True
    return owned or any(not s.get("breadcrumb") for s in manifest.get("sections", []))


class Report:
    """What a run did, for the report at its end."""

    def __init__(self, root: Path, db: Path):
        self.root, self.db = root, db
        self.intakes: list[Intake] = []
        self.converted: list[tuple[Path, str]] = []        # (collection, slug) made by this run
        self.new_docs: dict[Path, list[str]] = {}          # every document the index does not hold
        self.checked: list[dict] = []                      # check_corpus.py's record of each
        self.fails: Counter = Counter()
        self.warns: Counter = Counter()
        self.notes: list[str] = []
        self.smoke = "not run"
        self.eval_line = ""
        self.index = "not rebuilt"
        self.stopped: str | None = None

    def stop(self, why: str) -> None:
        self.stopped = why


def post_process(rep: Report, tessdata: str | None, dry_run: bool) -> list[list[str]]:
    """Step 2 for each collection with new documents: the commands, run
    unless `dry_run`. Stops `rep` at the first that fails.

    A document converted in this run gets every step. One converted earlier
    is enriched only if it is prose that never was, and its figures are
    extracted only if it has none yet: neither is redone on a document that
    was finished, as enrich_chunks.py rewrites in place and a document's
    figures can take an hour."""
    planned = []
    fresh = set(rep.converted)
    # In a dry run a document to be converted has no manifest yet.
    shape_of = {(it.collection, it.slug): it.shape for it in rep.intakes if it.command}

    def prose(coll: Path, slug: str) -> bool:
        if (coll, slug) in fresh:
            return shape_of.get((coll, slug)) == "prose" if dry_run else not is_reference(read_manifest(coll, slug))
        return needs_enrich(read_manifest(coll, slug))

    def has_figures(coll: Path, slug: str) -> bool:
        return (coll / "docs" / slug / "figures.json").is_file()

    for coll, slugs in rep.new_docs.items():
        unfigured = [s for s in slugs if (coll, s) in fresh or not has_figures(coll, s)]
        steps: list = []
        enrich = [s for s in slugs if prose(coll, s)]
        if enrich:
            steps.append([PY, script("enrich_chunks.py"), str(coll), *only(enrich)])
        if unfigured:
            steps.append([PY, script("extract_figures.py"), str(coll), *only(unfigured)])
        steps += ["ocr", [PY, script("build_index.py"), str(coll)]]
        for step in steps:
            if step == "ocr":
                # After extraction, every document whose PDF was found has a
                # figures.json; ocr_figures.py reads only figures without text.
                read = [s for s in slugs if has_figures(coll, s) or (dry_run and s in unfigured)]
                if not read:
                    continue
                step = [PY, script("ocr_figures.py"), str(coll), *only(read)]
                if not tessdata:
                    rep.notes.append(f"OCR of figures skipped in {coll.name}: no Tesseract language data. "
                                     "Install Tesseract and set TESSDATA_PREFIX (or pass --tessdata), then run "
                                     f"{shown(step)}")
                    continue
                step[3:3] = ["--tessdata", tessdata]
            planned.append(step)
            if dry_run:
                continue
            code, tail = run_step(step)
            if code != 0:
                rep.stop(f"{Path(step[1]).name} failed in {coll.name} (exit {code}): {tail[-1] if tail else ''}. "
                         "Fix that and rerun; the documents stay new until the index holds them")
                return planned
    return planned


def check(rep: Report, strict: bool, dry_run: bool) -> list[list[str]]:
    """check_corpus.py --strict on the new documents, read from its JSON."""
    planned = []
    with tempfile.TemporaryDirectory() as tmp:
        for n, (coll, slugs) in enumerate(rep.new_docs.items()):
            out = Path(tmp) / f"check-{n}.json"
            step = [PY, script("check_corpus.py"), str(coll), "--strict", *only(slugs)]
            planned.append(step)
            if dry_run:
                continue
            code, tail = run_step(step + ["--json", str(out)])
            if not out.is_file():
                rep.stop(f"check_corpus.py could not check {coll.name} (exit {code}): {tail[-1] if tail else ''}")
                return planned
            report = json.loads(out.read_text(encoding="utf-8"))
            for doc in report["documents"]:
                doc["collection_dir"] = coll
                rep.checked.append(doc)
                for item in doc["problems"]:
                    (rep.fails if item["severity"] == "FAIL" else rep.warns)[item["check"]] += 1
    if not dry_run and rep.fails:
        rep.stop(f"check_corpus.py found {sum(rep.fails.values())} failing check(s) in the new documents; "
                 "the index was not touched")
    elif not dry_run and strict and rep.warns:
        rep.stop(f"check_corpus.py found {sum(rep.warns.values())} warning(s) and --strict was given; "
                 "the index was not touched")
    return planned


def eval_run(db: Path, questions: Path, out: Path) -> tuple[dict | None, str]:
    """(summary of eval_search.py on `db`, or None, and why not)."""
    code, tail = run_step([PY, script("eval_search.py"), "--db", str(db), "--questions", str(questions),
                           "--json", str(out)])
    if code != 0 or not out.is_file():
        return None, f"eval_search.py could not score {db.name} (exit {code}): {tail[-1] if tail else ''}"
    return json.loads(out.read_text(encoding="utf-8")), ""


def scores(run: dict) -> tuple[float, float]:
    return run["summary"]["all"]["hit@5"], run["summary"]["all"]["mrr"]


def build_and_swap(rep: Report, questions: Path | None, accept_regression: bool,
                   emit_vscode_config: bool) -> None:
    """Step 4: a candidate index, its smoke test and eval, and the swap."""
    root, db = rep.root, rep.db
    cand = candidate_path(db)
    # The candidate's build leaves the server copy at the corpus root alone:
    # that copy serves the live index, not the candidate. It is refreshed only
    # once the candidate has replaced the live index, below, so a run refused
    # or interrupted at any step leaves the copy matching the index beside it.

    def refuse(why: str, keep: bool = True) -> None:
        rep.index = f"kept as it was ({why})" + (f"; the candidate is at {cand}" if keep and cand.is_file() else "")
        rep.stop(why)

    # A candidate an earlier run refused is gone before this one is built, so
    # whatever the report names as the candidate is this run's. If it cannot
    # go (on Windows, a SQLite browser holding it open), nothing is built, and
    # the report names no candidate: the one on disk is the earlier run's.
    try:
        cand.unlink(missing_ok=True)
    except OSError as exc:
        return refuse(f"{cand.name}, left by an earlier run, could not be removed ({exc}); is a SQLite "
                      "browser or a server holding it open? Close it, or delete the file, and rerun. "
                      "Nothing was built and the index was not touched", keep=False)
    code, tail = run_step([PY, script("build_search_db.py"), str(root), "--out", str(cand)])
    if code != 0:
        return refuse(f"build_search_db.py failed (exit {code}): {tail[-1] if tail else ''}")
    code, tail = run_step([PY, script("mcp_smoke_test.py"), "--db", str(cand)])
    rep.smoke = "passed" if code == 0 else "FAILED"
    if code != 0:
        return refuse("the candidate failed the smoke test")

    if questions:
        with tempfile.TemporaryDirectory() as tmp:
            before, why_before = (eval_run(db, questions, Path(tmp) / "current.json") if db.is_file()
                                  else (None, "no index yet"))
            after, why_after = eval_run(cand, questions, Path(tmp) / "candidate.json")
            if after and before:
                (h0, m0), (h1, m1) = scores(before), scores(after)
                n = after["summary"]["all"]["n"]
                rep.eval_line = (f"{questions.name}, {n} questions: hit@5 {h0:.0%} -> {h1:.0%}, "
                                 f"MRR {m0:.3f} -> {m1:.3f}")
                ranks = [{r.get("id") or r.get("question"): r["rank"] for r in run["results"]}
                         for run in (before, after)]
                if ranks[0] != ranks[1]:
                    run_step([PY, script("eval_search.py"), "--compare",
                              str(Path(tmp) / "current.json"), str(Path(tmp) / "candidate.json")])
                dropped = h1 < h0 - 1e-9 or m1 < m0 - 1e-9
                if dropped and not accept_regression:
                    return refuse("the candidate scores lower on the questions; --accept-regression takes it")
                if dropped:
                    rep.eval_line += "  (a drop, taken with --accept-regression)"
            elif after:
                h1, m1 = scores(after)
                rep.eval_line = f"{questions.name}: hit@5 {h1:.0%}, MRR {m1:.3f} on the candidate; current: {why_before}"
                if db.is_file() and not accept_regression:
                    return refuse("the current index could not be scored, so a drop cannot be ruled out; "
                                  "--accept-regression replaces it anyway")
            else:
                rep.eval_line = why_after
                if not accept_regression:
                    return refuse("the candidate could not be scored; --accept-regression replaces the index anyway")

    try:
        os.replace(cand, db)
    except OSError as exc:
        return refuse(f"{db.name} could not be replaced ({exc}); is a server holding it open? Close it "
                      "and rerun")
    rep.index = f"replaced: {db}"
    if emit_vscode_config:
        build_search_db.install_server(root)
        rep.notes.append(f"VS Code config -> {build_search_db.emit_vscode_config(root, db)}")
    elif (root / build_search_db.SERVER_NAME).is_file() and build_search_db.copy_serves(root, db):
        rep.notes.append(f"Server copy -> {build_search_db.install_server(root)}")


# ---------------------------------------------------------------- report

def pct(x) -> str:
    return "-" if x is None else f"{x:.0%}"


def print_plan(rep: Report, unused: list[str], other: list[str]) -> None:
    print(f"Corpus: {rep.root}")
    print(f"Index:  {rep.db}{'' if rep.db.is_file() else ' (none yet)'}")
    if not rep.intakes:
        print("\nNo PDFs are waiting in any new_docs/.")
    for it in rep.intakes:
        print(f"\n{it.label}")
        if it.shape:
            print(f"  shape: {it.shape}" + (f" ({it.why})" if it.why else ""))
        if it.title:
            print(f"  title: {it.title!r}, from {it.title_from}")
        for line in it.edition:
            print(f"  {line}")
        if it.command:
            print(f"  => {shown(it.command)}")
        for why in it.held:
            print(f"  WAITS: {why}")
    for key in unused:
        print(f"\nnote: --titles names {key!r}, which is not waiting in any new_docs/")
    earlier = {coll: [s for s in slugs if (coll, s) not in rep.converted] for coll, slugs in rep.new_docs.items()}
    earlier = {c: s for c, s in earlier.items() if s}
    if earlier:
        print("\nAlready converted, and not in the index as they are on disk:")
        for coll, slugs in earlier.items():
            print(f"  {coll.name}: {', '.join(slugs)}")
    for line in other:
        print(f"Index out of date: {line}")


def print_report(rep: Report) -> None:
    bar = "=" * 78
    held = [it for it in rep.intakes if it.held]
    print(f"\n{bar}\nupdate_corpus: {rep.root}")
    rows = []
    for doc in rep.checked:
        coll, slug = doc["collection_dir"], doc["folder"]
        m = read_manifest(coll, slug)
        conv = (m.get("converter") or {}).get("script", "?").removesuffix(".py")
        conv += " (reference)" if is_reference(m) else " (prose)"
        mark = "" if (coll, slug) in rep.converted else " *"
        mt = doc.get("metrics", {})
        warn = sum(1 for p in doc["problems"] if p["severity"] == "WARN")
        fail = sum(1 for p in doc["problems"] if p["severity"] == "FAIL")
        # Prose owns no chunk by design, so "-". A reference that owns none is
        # the worst case for lookup_entity, so 0%, never "-".
        entity = pct(mt.get("owned") or 0.0) if is_reference(m) else "-"
        rows.append((f"{coll.name}/{slug}{mark}", conv, mt.get("chunks", 0), pct(mt.get("pages")),
                     entity, f"{warn}" + (f" +{fail} FAIL" if fail else "")))
    print(f"Documents added: {len(rep.converted)} converted now"
          + (f", {len(rows) - len(rep.converted)} converted earlier (*)" if len(rows) > len(rep.converted) else ""))
    if rows:
        w = max(len(r[0]) for r in rows)
        print(f"  {'document':{w}}  {'converter':28} {'chunks':>6} {'pages':>6} {'entity':>6} {'warn':>5}")
        for r in rows:
            print(f"  {r[0]:{w}}  {r[1]:28} {r[2]:6d} {r[3]:>6} {r[4]:>6} {r[5]:>5}")
    if rep.checked:
        def names(c: Counter) -> str:
            return f" ({', '.join(f'{k} x{v}' if v > 1 else k for k, v in sorted(c.items()))})" if c else ""
        print(f"Checker: {sum(rep.fails.values())} failing, {sum(rep.warns.values())} warning(s)"
              f"{names(rep.fails)}{names(rep.warns)}")
    print(f"Smoke test: {rep.smoke}")
    if rep.eval_line:
        print(f"Eval: {rep.eval_line}")
    print(f"Index: {rep.index}")
    for note in rep.notes:
        print(f"Note: {note}")
    if held:
        print(f"Still waiting in new_docs/ ({len(held)}), nothing written for them:")
        for it in held:
            print(f"  {it.label}")
            for why in it.held:
                print(f"    - {why}")
    if rep.stopped:
        print(f"STOPPED: {rep.stopped}")
    print(bar)


# ------------------------------------------------------------------ main

def main() -> int:
    utf8_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("corpus", type=Path, help="the corpus root (the folder the search index is built from)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and change nothing")
    ap.add_argument("--titles", type=Path, metavar="FILE",
                    help="JSON of titles, and any other decision a PDF needs, by PDF filename")
    ap.add_argument("--prose-converter", choices=CONVERTER_CHOICES,
                    help="what converts prose (default: convert_docling where Docling is installed; "
                         "without it, a prose PDF waits for this to be said)")
    ap.add_argument("--questions", type=Path, metavar="JSONL",
                    help="score the current index and the candidate with eval_search.py on these")
    ap.add_argument("--accept-regression", action="store_true",
                    help="replace the index even where the candidate scores lower, or cannot be scored")
    ap.add_argument("--strict", action="store_true", help="stop on checker warnings too, not only on FAILs")
    ap.add_argument("--db", type=Path, help=f"the search index (default: <corpus>/{build_search_db.DEFAULT_DB_NAME})")
    ap.add_argument("--tessdata", help="Tesseract language-data folder, for ocr_figures.py")
    ap.add_argument("--emit-vscode-config", action="store_true",
                    help="once the index is replaced, copy the server beside it and write .vscode/mcp.json, "
                         "as build_search_db.py --emit-vscode-config does")
    args = ap.parse_args()

    root = args.corpus.resolve()
    if not root.is_dir():
        print(f"{root} is not a folder", file=sys.stderr)
        return 1
    db = (args.db or root / build_search_db.DEFAULT_DB_NAME).resolve()
    questions = args.questions.resolve() if args.questions else None
    if questions and not questions.is_file():
        print(f"No question file at {questions}", file=sys.stderr)
        return 1
    try:
        decisions = load_decisions(args.titles) if args.titles else {}
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    known = editions.collections_under(root)
    waiting = editions.waiting_collections(root)
    others = [c for c in known + waiting if c != root]
    if root in waiting and others:
        # Whether the others are converted yet or only waiting: the first
        # document at the root makes it the one collection, and hides them.
        print(f"{root / editions.INBOX_DIR} holds PDFs, and {root.name} holds collections. Converted here they "
              f"would make {root.name} one collection, and every tool would stop seeing "
              f"{', '.join(c.name for c in others)}. Move them into a collection's {editions.INBOX_DIR}/.",
              file=sys.stderr)
        return 1
    if (known == [root] and not args.db and not db.is_file()
            and (root.parent / build_search_db.DEFAULT_DB_NAME).is_file()):
        print(f"{root} is one collection, and the index beside it is {root.parent / build_search_db.DEFAULT_DB_NAME}. "
              f"Pass the corpus root, {root.parent}, or --db to build one here.", file=sys.stderr)
        return 1
    collections = sorted(set(known) | set(waiting))
    if not collections:
        print(f"No collection under {root}: expected <collection>/{editions.INBOX_DIR}/*.pdf or "
              "<collection>/docs/", file=sys.stderr)
        return 1

    docling = importlib.util.find_spec("docling") is not None
    tessdata = ocr_figures.find_tessdata(args.tessdata)
    rep = Report(root, db)
    rep.intakes, unused = plan_intake(collections, decisions, args.prose_converter, docling)
    try:
        changed, other = changes_since_index(root, collections, db)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    def new_documents() -> dict[Path, list[str]]:
        out = {coll: list(slugs) for coll, slugs in changed.items()}
        for coll, slug in rep.converted:
            if slug not in out.setdefault(coll, []):
                out[coll].append(slug)
        return {c: s for c, s in sorted(out.items()) if s}

    if args.dry_run:
        rep.converted = [(it.collection, it.slug) for it in rep.intakes if it.command]
        rep.new_docs = new_documents()
        print_plan(rep, unused, other)
        print("\nThen, for the new documents:")
        steps = post_process(rep, tessdata, dry_run=True) + check(rep, args.strict, dry_run=True)
        for step in steps:
            print(f"  {shown(step)}")
        for note in rep.notes:
            print(f"  note: {note}")
        if not rep.converted and not any(editions.document_dirs(c / "docs") for c in collections):
            print("  nothing to index: no document would be converted")
        elif rep.new_docs or other or not db.is_file():
            cand = candidate_path(db)
            print(f"  {shown([PY, script('build_search_db.py'), str(root), '--out', str(cand)])}")
            print(f"  {shown([PY, script('mcp_smoke_test.py'), '--db', str(cand)])}")
            if questions:
                for target in ((db,) if db.is_file() else ()) + (cand,):
                    print(f"  {shown([PY, script('eval_search.py'), '--db', str(target), '--questions', str(questions)])}")
            print(f"  and {cand.name} replaces {db.name} if the smoke test passes"
                  + (" and neither hit@5 nor MRR drops" if questions else ""))
        else:
            print("  nothing: the index already holds every document as it is on disk")
        print("\nDry run: nothing was changed.")
        return 0

    rep.new_docs = new_documents()
    print_plan(rep, unused, other)
    for it in rep.intakes:
        if not it.command:
            continue
        code, tail = run_step(it.command)
        if not (it.collection / "docs" / it.slug / "manifest.json").is_file():
            it.held.append(f"{it.converter} failed (exit {code}): {tail[-1] if tail else 'no output'}")
            continue
        # On disk is what the index will hold, so it is checked whatever the
        # converter's exit status said.
        rep.converted.append((it.collection, it.slug))
        if code != 0:
            rep.notes.append(f"{it.converter} wrote docs/{it.slug} and exited {code}: "
                             f"{tail[-1] if tail else 'no output'}")
    rep.new_docs = new_documents()

    if not any(editions.document_dirs(c / "docs") for c in collections):
        rep.index = "not built: no document has been converted yet"
    elif not rep.new_docs and not other and db.is_file():
        rep.index = "unchanged: it already holds every document as it is on disk"
    else:
        post_process(rep, tessdata, dry_run=False)
        if not rep.stopped:
            check(rep, args.strict, dry_run=False)
        if not rep.stopped:
            build_and_swap(rep, questions, args.accept_regression, args.emit_vscode_config)
    print_report(rep)
    if rep.stopped:
        return 1
    return 2 if any(it.held for it in rep.intakes) else 0


if __name__ == "__main__":
    sys.exit(main())
