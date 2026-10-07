"""Fixtures and helpers shared by the test files in this folder.

Run the suite:   python -m unittest discover -s tests -v
Run one file:    python tests/test_converters.py
Every test alone, each in its own process:   python tests/run_each.py

Plain unittest, no pytest, no fixtures checked in: the PDFs are generated with
PyMuPDF so the ground truth (bookmark TOC, command names, page numbers) is known
exactly rather than asserted against whatever a real manual happens to contain.

What is covered is deliberately the class of thing that has actually broken
here, not a coverage percentage:

  * idempotency -- enrich_chunks must change nothing on a second run. A
    breadcrumb detector keyed on the separator once re-prepended a crumb every
    run, and the title-only fallback reintroduced exactly that risk.
  * no metadata downgrade -- a pass that can only add information must not
    overwrite better information already there, whichever branch it takes.
    This is what protects the page-accurate entity breadcrumbs
    rebuild_reference.py writes.
  * entity attribution -- the command level is found from the TOC, and its
    >=20-entry threshold means a small document silently attributes nothing.
    A chapter that is not a command must end the command before it, or the
    last command in a chapter owns the appendices.
  * the output contract -- build_index.py and build_search_db.py read what the
    converters write, so a manifest field rename breaks retrieval, not a test.
  * the server -- driven over stdio as an editor drives it, including the
    AND->OR fallback on an identifier, which reaches FTS5 as a phrase.
  * figures -- extracted from the PDF without touching what the converter
    wrote, tied to a section only by evidence, and served as images. A figure
    OCR could not read stays unread, so a rerun retries it.
  * optional-dependency gating -- convert_docling.py must fail with
    instructions, not a traceback, when Docling is absent.
  * the contract checker -- check_corpus.py passes the pipeline's own output,
    passes a document written by hand to the contract with warnings counted,
    and catches each defect it claims to when that defect is planted alone.
  * editions -- a PDF dropped in new_docs/ is filed under source/ with its
    release in the manifest, a second release of the same filename does not
    overwrite the first, search answers from one edition per manual, a version
    that is not there is refused rather than approximated, a cover that says
    "and later" answers for later releases and no other does, and a
    comparison reads the whole entry, past the point where a lookup is cut.

Every test passes run on its own. A test that checks a step runs that step
in a folder of its own; a test that only needs a step's output takes it from
WS, which builds it the first time any test in the process asks.
"""
from __future__ import annotations

import atexit
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import pymupdf
except ImportError:
    sys.exit("Tests need pymupdf. Run: pip install -r scripts/requirements.txt")

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
PY = sys.executable

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(SCRIPTS / script), *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


JSON_TYPES = {"string": str, "boolean": bool, "array": list, "object": dict, "null": type(None)}


def schema_errors(value, schema: dict, path: str = "$") -> list[str]:
    """What is wrong with `value` under a JSON Schema, for the keywords
    scripts/manifest.schema.json uses: type, required, properties, items, enum,
    const, minimum, minLength, pattern and additionalProperties (true). Anything else in a
    schema is documentation here."""
    errors = []
    if "type" in schema:
        for name in ([schema["type"]] if isinstance(schema["type"], str) else schema["type"]):
            if name == "integer":
                ok = isinstance(value, int) and not isinstance(value, bool)
            elif name == "number":
                ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            else:
                ok = isinstance(value, JSON_TYPES[name]) and not (name != "boolean" and isinstance(value, bool))
            if ok:
                break
        else:
            return [f"{path}: {value!r} is not {schema['type']}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: {value!r} is not {schema['const']!r}")
    if "minimum" in schema and isinstance(value, (int, float)) and value < schema["minimum"]:
        errors.append(f"{path}: {value} is below {schema['minimum']}")
    if "minLength" in schema and isinstance(value, str) and len(value) < schema["minLength"]:
        errors.append(f"{path}: {value!r} is shorter than {schema['minLength']}")
    if "pattern" in schema and isinstance(value, str) and not re.search(schema["pattern"], value):
        errors.append(f"{path}: {value!r} does not match {schema['pattern']}")
    if isinstance(value, dict):
        errors += [f"{path}: lacks {name!r}" for name in schema.get("required", []) if name not in value]
        for name, sub in schema.get("properties", {}).items():
            if name in value:
                errors += schema_errors(value[name], sub, f"{path}.{name}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors += schema_errors(item, schema["items"], f"{path}[{i}]")
    return errors


def write_pdf(path: Path, pages: list[list[tuple[str, int]]], toc: list) -> None:
    """Build a PDF from [(text, fontsize), ...] per page, plus a bookmark TOC.

    Font size carries the heading signal, because that is what pymupdf4llm
    infers structure from -- the fixture has to exercise the same weakness the
    real documents do.
    """
    doc = pymupdf.open()
    for blocks in pages:
        page = doc.new_page()
        y = 72
        for text, size in blocks:
            page.insert_text((72, y), text, fontsize=size)
            y += size + 10
        page.insert_text((72, 760), "Feedback", fontsize=8)   # page furniture
    doc.set_toc(toc)
    doc.save(str(path))
    doc.close()


def prose_fixture(path: Path) -> None:
    body = [("This section explains the widget and how to seat it correctly.", 11),
            ("Torque the bolts evenly to avoid warping the bracket.", 11)]
    pages, toc = [], []
    chapters = [("Installation", ["Unpacking", "Mounting"]),
                ("Calibration", ["First Run", "Drift"])]
    page_no = 1
    for chap, sections in chapters:
        pages.append([(chap, 22)] + body)
        toc.append([1, chap, page_no])
        page_no += 1
        for sec in sections:
            pages.append([(sec, 16)] + body)
            toc.append([2, sec, page_no])
            page_no += 1
    write_pdf(path, pages, toc)


def inverted_fixture(path: Path) -> None:
    """A preface set larger than the chapter after it, as real manuals do.
    The chapter heading comes out as ### inside the preface's chunk, so a walk
    by heading level keeps the preface as the parent of the whole chapter."""
    pages, toc = [], []
    for title, size, level in [("About This Guide", 22, 1), ("Normalized Formats", 14, 1),
                               ("Data Loading", 16, 2), ("Parametric Data", 16, 2)]:
        pages.append([(title, size),
                      (f"This part explains {title.lower()} and how its pages are laid out.", 11)])
        toc.append([level, title, len(pages)])
    write_pdf(path, pages, toc)


def command_page(name: str) -> list[tuple[str, int]]:
    return [(name, 20),
            ("SYNTAX", 14), (f"{name} -value <int>", 11),
            ("ARGUMENTS", 14), ("-value  the value to set", 11)]


def reference_fixture(path: Path, n_commands: int) -> None:
    """A command dictionary: one entry per command, listed at TOC level 1."""
    pages, toc = [], []
    for i in range(n_commands):
        name = f"set_widget_option_{i:02d}"
        pages.append(command_page(name))
        toc.append([1, name, len(pages)])
    write_pdf(path, pages, toc)


def tcl_reference_fixture(path: Path, n_commands: int) -> None:
    """A command dictionary whose entries are plain lowercase words, the way
    Tcl command names are: nothing in a title says "command" to the level
    picker, though pick_extractor sees a reference."""
    pages, toc = [], []
    for i in range(n_commands):
        name = "verb" + chr(97 + i // 26) + chr(97 + i % 26)
        # Enough text that pick_extractor sees a text layer (100 characters a page).
        pages.append(command_page(name) + [("Sets the widget option of the same name.", 11)])
        toc.append([1, name, len(pages)])
    write_pdf(path, pages, toc)


def mixed_fixture(path: Path) -> None:
    """A prose manual with a section of commands: 20 entries over 150 pages, 0.13
    a page, between what pick_extractor calls prose and what it calls a reference."""
    pages, toc = [], []
    for n in range(150):
        entry = n >= 30 and n % 6 == 0
        name = f"set_gadget_option_{n:03d}" if entry else f"Chapter {n} Overview"
        pages.append([(name, 20), (f"This page of the guide describes how part {n} of the gadget works in use.", 11)])
        if n < 30 or entry:
            toc.append([1, name, n + 1])
    write_pdf(path, pages, toc)


def nested_reference_fixture(path: Path, n_commands: int) -> None:
    """Commands at TOC level 2 under a chapter, then chapters that are not
    commands -- the tshell-ref shape, with an appendix and a licence after the
    last entry. Neither belongs to any command."""
    pages = [[("Command Reference", 22), ("This chapter lists every widget command.", 11)]]
    toc = [[1, "Command Reference", 1]]
    for i in range(n_commands):
        name = f"set_widget_option_{i:02d}"
        pages.append(command_page(name))
        toc.append([2, name, len(pages)])
    trailing = [("Appendix A Troubleshooting", "If the widget will not power on, check the fuse."),
                ("End-User License Agreement", "You may not redistribute this software.")]
    for chapter, text in trailing:
        pages.append([(chapter, 22), (text, 11)])
        toc.append([1, chapter, len(pages)])
    write_pdf(path, pages, toc)


def figure_fixture(path: Path) -> None:
    """Two captioned figures of the two kinds real manuals mix: a vector block
    diagram whose labels are real text (Synopsys draws its figures) and a
    raster image whose words are only pixels (Tessent embeds them)."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Wiring", fontsize=20)
    page.insert_text((72, 104), "The widget connects to the controller through the harness shown below.", fontsize=11)
    page.insert_text((72, 124), "Seat every connector before applying power to the assembly.", fontsize=11)
    page.insert_text((72, 160), "Figure 1. Widget Wiring Diagram", fontsize=10)
    shape = page.new_shape()
    for i in range(3):
        shape.draw_rect(pymupdf.Rect(90 + i * 160, 200, 200 + i * 160, 270))
    for i in range(2):
        x = 200 + i * 160
        shape.draw_line((x, 235), (x + 50, 235))
        shape.draw_polyline([(x + 42, 230), (x + 50, 235), (x + 42, 240)])
    shape.draw_circle((305, 320), 22)
    shape.draw_line((305, 270), (305, 298))
    shape.finish(color=(0, 0, 0), width=1.2)
    shape.commit()
    for i, name in enumerate(("CPU", "BUS", "WIDGET")):
        page.insert_text((105 + i * 160, 240), name, fontsize=10)
    page.insert_text((292, 324), "PSU", fontsize=9)
    page.insert_text((72, 380), "After wiring, continue with calibration as the next chapter describes.", fontsize=11)

    page = doc.new_page()
    page.insert_text((72, 72), "Front Panel", fontsize=20)
    page.insert_text((72, 104), "The front panel carries the status lamps and the reset switch.", fontsize=11)
    art = pymupdf.open()
    canvas = art.new_page(width=360, height=200)
    canvas.draw_rect(canvas.rect, color=None, fill=(0.88, 0.91, 0.94))
    for i, rgb in enumerate([(0.8, 0.15, 0.15), (0.15, 0.6, 0.25), (0.95, 0.8, 0.15)]):
        canvas.draw_rect(pymupdf.Rect(40 + i * 100, 30, 100 + i * 100, 90), color=None, fill=rgb)
    canvas.insert_text((60, 150), "RESET LAMP", fontsize=30)
    page.insert_image(pymupdf.Rect(110, 130, 470, 330), pixmap=canvas.get_pixmap(dpi=144))
    page.insert_text((72, 350), "Figure 2. Front Panel Lamps", fontsize=10)
    page.insert_text((72, 390), "Press reset for two seconds to clear a fault.", fontsize=11)
    doc.set_toc([[1, "Wiring", 1], [1, "Front Panel", 2]])
    doc.save(str(path))
    doc.close()


def handmade_document(collection: Path, slug: str = "hand") -> None:
    """A document written the way any converter might write one, with no
    script from this repo involved, so check_corpus.py is tested on the
    contract alone. Six pages with their own vocabulary, one chunk per page,
    two chapters' worth of TOC nesting per pair."""
    toc = [[1, "Alpha", 1], [2, "Alpha Setup", 2], [1, "Beta", 3],
           [2, "Beta Tuning", 4], [1, "Gamma", 5], [2, "Gamma Limits", 6]]
    doc_dir = collection / "docs" / slug
    (doc_dir / "sections").mkdir(parents=True)
    pdf = pymupdf.open()
    sections, texts, chapter = [], [], None
    for n, (level, heading, page_no) in enumerate(toc, start=1):
        vocab = [f"{heading.split()[0].lower()}{n}term{chr(97 + i % 26)}{chr(97 + i * 7 % 26)}"
                 for i in range(36)]
        lines = [" ".join(vocab[i:i + 6]) for i in range(0, len(vocab), 6)]
        page = pdf.new_page()
        page.insert_text((72, 72), heading, fontsize=18)
        for i, line in enumerate(lines):
            page.insert_text((72, 110 + i * 18), line, fontsize=11)
        chapter = heading if level == 1 else chapter
        crumb = " › ".join(["Hand Guide", chapter] + ([heading] if level == 2 else []))
        text = f"*{crumb}*\n\n## {heading}\n\n" + "\n".join(lines) + "\n"
        name = f"sections/{n:03d}-{heading.lower().replace(' ', '-')}.md"
        (doc_dir / name).write_text(text, encoding="utf-8")
        texts.append(text)
        sections.append({"file": name, "heading": heading, "level": level, "chars": len(text),
                         "page_start": page_no, "page_end": page_no, "breadcrumb": crumb})
    pdf.set_toc(toc)
    pdf.save(str(collection / "hand.pdf"))
    pdf.close()
    full = "\n\n".join(texts)
    (doc_dir / "full.md").write_text(full, encoding="utf-8")
    manifest = {"source_pdf": "hand.pdf", "title": "Hand Guide", "slug": slug, "page_count": 6,
                "toc": [{"level": l, "title": t, "page": p} for l, t, p in toc],
                "sections": sections, "full_md_chars": len(full)}
    (doc_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def words(text: str) -> str:
    """Lowercase alphanumerics, so markdown emphasis cannot hide a caption."""
    return " ".join(re.findall(r"[0-9a-z]+", text.lower()))


def gadget_fixture(path: Path, version: str, sections: list[str]) -> None:
    """One release of a prose manual, its version printed on the cover the
    way a vendor prints it and nowhere in the filename."""
    pages = [[("Gadget Guide", 24), (f"Software Version {version}", 11)]]
    toc = [[1, "Gadget Guide", 1]]
    for sec in sections:
        pages.append([(sec, 18), (f"This section covers {sec.lower()} for the gadget in release {version}.", 11),
                      ("Keep the gadget dry and check the seals before every use.", 11)])
        toc.append([1, sec, len(pages)])
    write_pdf(path, pages, toc)


LONG_ENTRY = "set_widget_option_05"
LONG_LINES = 600      # at ~75 characters a line, well past the server's 40,000-character cut


def edition_reference_fixture(path: Path, numbers: list[int], last_line: str) -> None:
    """One release of a command reference. One entry runs to twenty pages, and
    `last_line` is its final line: the only place two releases' text differs."""
    pages, toc = [], []
    for i in numbers:
        name = f"set_widget_option_{i:02d}"
        toc.append([1, name, len(pages) + 1])
        if name != LONG_ENTRY:
            pages.append(command_page(name))
            continue
        lines = [f"Setting {n:03d} of the long option is described on this line in full detail."
                 for n in range(LONG_LINES)]
        lines[-1] = last_line
        for start in range(0, LONG_LINES, 30):
            pages.append(([(name, 20)] if start == 0 else []) + [(l, 11) for l in lines[start:start + 30]])
    write_pdf(path, pages, toc)


def load_script(name: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Workspace:
    """Fixture PDFs and what the pipeline makes of them, built the first time
    a test asks and shared by every test in the process after that.

    A test that checks a step runs that step itself, in a folder of its own.
    A test that only needs a step's output takes it from here. A test that
    changes what it gets copies it first, with fresh(): a shared corpus one
    test damaged would make the next test's result depend on the order.
    """

    PDFS = {
        "prose": ("widget-guide.pdf", prose_fixture),
        "ref": ("widget-commands.pdf", lambda p: reference_fixture(p, 25)),    # clears the >=20 command threshold
        "tiny": ("tiny-commands.pdf", lambda p: reference_fixture(p, 8)),      # deliberately below it
        "nested": ("nested-commands.pdf", lambda p: nested_reference_fixture(p, 22)),
        "tcl": ("tcl-commands.pdf", lambda p: tcl_reference_fixture(p, 30)),   # no underscore: the picker declines
    }

    def __init__(self) -> None:
        self._dir: tempfile.TemporaryDirectory | None = None
        self._built: dict = {}
        self._copies = 0

    @property
    def tmp(self) -> Path:
        if self._dir is None:
            # ignore_cleanup_errors: on Windows an SQLite handle still open
            # at exit would otherwise fail the cleanup, and the run with it.
            self._dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
            atexit.register(self._dir.cleanup)
        return Path(self._dir.name)

    def _once(self, key: str, build):
        if key not in self._built:
            self._built[key] = build()
        return self._built[key]

    @staticmethod
    def step(script: str, *args: str) -> subprocess.CompletedProcess:
        r = run(script, *args)
        if r.returncode != 0:
            raise AssertionError(f"{script} {' '.join(args)} failed while building a shared fixture:\n"
                                 f"{r.stdout}{r.stderr}")
        return r

    def pdf(self, name: str) -> Path:
        """One of the four fixture PDFs, under the name tests have always used."""
        def make() -> Path:
            filename, write = self.PDFS[name]
            path = self.tmp / "pdfs" / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            write(path)
            return path
        return self._once(f"pdf:{name}", make)

    def fresh(self, src: Path, name: str) -> Path:
        """A copy of `src` for one test to change."""
        self._copies += 1
        dest = self.tmp / f"{name}-{self._copies}"
        shutil.copytree(src, dest, symlinks=True)
        return dest

    def collection(self, name: str, *pdfs: str) -> Path:
        """A new collection, docs/ empty, holding copies of the named PDFs."""
        self._copies += 1
        coll = self.tmp / f"{name}-{self._copies}"
        (coll / "docs").mkdir(parents=True)
        for pdf in pdfs:
            shutil.copy(self.pdf(pdf), coll / self.PDFS[pdf][0])
        return coll

    def corpus(self) -> Path:
        """The four fixtures converted into one collection: prose (enriched)
        and three references, with index.json. Read it; copy it to change it."""
        def make() -> Path:
            corpus = self.tmp / "Corpus"
            (corpus / "docs").mkdir(parents=True)
            for name in self.PDFS:
                shutil.copy(self.pdf(name), corpus / self.PDFS[name][0])
            self.step("convert_manual.py", str(corpus / "widget-guide.pdf"),
                      "--title", "Widget Guide", "--slug", "prose")
            self.step("enrich_chunks.py", str(corpus))
            for pdf, title, slug in (("widget-commands.pdf", "Widget Commands", "ref"),
                                     ("tiny-commands.pdf", "Tiny", "tiny"),
                                     ("nested-commands.pdf", "Widget Commands", "nested")):
                self.step("rebuild_reference.py", str(corpus / pdf), "--title", title, "--slug", slug)
            self.step("build_index.py", str(corpus))
            return corpus
        return self._once("corpus", make)

    def index(self) -> Path:
        """The search index of corpus()."""
        def make() -> Path:
            db = self.tmp / "index.sqlite3"
            self.step("build_search_db.py", "--root", str(self.corpus()), "--out", str(db))
            return db
        return self._once("index", make)

    def fig_corpus(self) -> Path:
        """The figure fixture converted, with its figures extracted."""
        def make() -> Path:
            corpus = self.tmp / "FigCorpus"
            (corpus / "docs").mkdir(parents=True)
            figure_fixture(corpus / "widget-figures.pdf")
            self.step("convert_manual.py", str(corpus / "widget-figures.pdf"),
                      "--title", "Widget Figures", "--slug", "figs")
            self.step("extract_figures.py", str(corpus))
            return corpus
        return self._once("fig_corpus", make)

    def hand_corpus(self) -> Path:
        """handmade_document() with its index.json: the checker's control."""
        def make() -> Path:
            corpus = self.tmp / "HandCorpus"
            handmade_document(corpus)
            self.step("build_index.py", str(corpus))
            return corpus
        return self._once("hand_corpus", make)

    def gadgets(self) -> tuple[Path, Path]:
        """(root, collection): two releases of one prose manual, filed from
        new_docs/ the way test_20 files them, enriched and listed in
        index.json. Copy the root with fresh() before changing anything."""
        def make() -> tuple[Path, Path]:
            root = self.tmp / "Root"
            coll = root / "Gadgets"
            for folder in ("new_docs", "source", "docs"):
                (coll / folder).mkdir(parents=True)
            for version, sections in (("2025.1", ["Unpacking", "Mounting", "Legacy Mode"]),
                                      ("2026.1", ["Unpacking", "Mounting", "Cloud Sync"])):
                gadget_fixture(coll / "new_docs" / "gadget.pdf", version, sections)
                self.step("convert_manual.py", str(coll / "new_docs" / "gadget.pdf"), "--title", "Gadget Guide")
            self.step("enrich_chunks.py", str(coll))
            self.step("build_index.py", str(coll))
            return root, coll
        return self._once("gadgets", make)


WS = Workspace()
