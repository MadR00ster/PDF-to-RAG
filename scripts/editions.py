#!/usr/bin/env python3
"""
Editions: where a collection keeps its PDFs, and which release each document is.

One collection on disk:

    <collection>/
      new_docs/               PDFs waiting to be converted -- new ones go here
      source/                 PDFs that have been converted, or set aside in
                              superseded.json
      docs/<slug>/            the converted output, one folder per edition
      current_versions.json   optional {"<doc_id>": "<version>"} pins

A manual that exists in several releases is several documents, one per
edition, sharing a `doc_id` and differing in `version`. Both live in each
manifest. Search answers from one edition per manual -- the newest, unless
current_versions.json pins another -- and from any other only when asked for
it by name, so two editions never compete for the same question.

`version` is the tool release the document applies to, as its cover prints
it, and `version_and_later` records a cover that says "2023.1 and later": the
vendor has not reissued the manual since, and says it still holds. Such an
edition answers for every release from its own up to the next edition's. One
without that claim answers for its own release only; whether it holds for the
next is not something the document says.

The version is read from the PDF's first pages, never from its filename: on
one real corpus twelve filenames carried no version at all, and two named a
later release than the cover, which said "and later". A filename that names
an earlier release than the cover, or a different one where the cover makes
no such claim, is a contradiction: `version` is then left out and reported,
as it is when the cover gives none, to be set by hand with --version or
`stamp --set` (`2023.1+` means "2023.1 and later").

The converters import this for the layout and the manifest fields. The
commands below are for a corpus that predates either:

  python scripts/editions.py status  <corpus or collection>
  python scripts/editions.py stamp   <collection> [--dry-run] [--set SLUG=VERSION] [--doc-id SLUG=ID]
  python scripts/editions.py migrate <collection> [--dry-run]

`status` lists every manual with its editions, the pins, and the PDFs still
waiting. `stamp` adds `doc_id`, `version` and `version_and_later` to manifests
that lack them, keeping the old manifests under .rebuild-backup/. `migrate` moves the PDFs at
a collection's root into source/, or into new_docs/ when nothing accounts for
them.

Standard library only at import; reading a cover needs PyMuPDF.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import NamedTuple

SOURCE_DIR = "source"
INBOX_DIR = "new_docs"
PINS_FILE = "current_versions.json"
BACKUP_DIR = ".rebuild-backup"

# How far into a PDF the release is looked for: the cover and the copyright
# page behind it. Further in, a version string is as likely to be about
# another product.
COVER_PAGES = 3

# "Y-2026.03-SP2": a letter, a year and a month are distinctive enough alone.
LETTERED_RE = re.compile(r"(?<![\w-])([A-Z]-20\d\d\.\d\d(?:-SP\d+(?:-\d+)?)?)(?!\.?\w)")
# "Software Version 2026.2", "Version 4.1", "Release 2024.09". A bare number
# is not taken: "IEEE 1149.1" is on more covers than a version is.
LABELLED_RE = re.compile(
    r"\b(?:Software\s+Version|Version|Release)[:\s]+v?(\d+(?:\.\d+)+(?:-[A-Za-z]+\d+)*)(?!\.?\w)", re.I)
# "Document Version 1.3", "File Format Version 2.0": a version, and not the
# product's. Only on the same line: a title ending in one of these words above
# "Version 4.1" is the usual cover.
NOT_THE_PRODUCT_RE = re.compile(
    r"\b(?:document|doc|file|format|schema|standard|specification|spec|protocol|template)\.?[ \t]+$", re.I)
# "Software Version 2023.1 and later": the document applies from that release
# on, and says so.
QUALIFIED_RE = re.compile(r"\s+(?:and|or)\s+(?:later|newer|above|higher)\b", re.I)

FILENAME_LETTERED_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]-20\d\d\.\d\d(?:-SP\d+(?:-\d+)?)?)", re.I)
FILENAME_YEAR_RE = re.compile(r"(?<!\d)(20\d\d)[._](\d{1,2})((?:[._]\d+)*)(-SP\d+(?:-\d+)?)?(?!\d)", re.I)

# What a slug ends in when it was named after a release: "-2026-2",
# "-y-2026-03-sp2". Dropping it gives the name the editions share.
VERSION_SUFFIX_RE = re.compile(r"-(?:[a-z]-)?20\d\d-\d{1,2}(?:-sp\d+(?:-\d+)?)?$")


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# ------------------------------------------------------------------ layout

def collection_of(pdf: Path) -> Path:
    """The collection a PDF belongs to: its folder, or the one above when it
    sits in new_docs/ or source/."""
    parent = pdf.resolve().parent
    return parent.parent if parent.name in (SOURCE_DIR, INBOX_DIR) else parent


def find_source_pdf(collection: Path, name: str | None) -> Path | None:
    """A manifest's `source_pdf` on disk: source/, or the collection root,
    where every PDF lived before source/ existed.

    Not new_docs/. A file there has not been converted, and vendors reuse
    filenames, so one with the right name is as likely the next release as
    this document's PDF. A source that is missing is reported as missing; it
    is not replaced by whatever is waiting under the same name.
    """
    if not name:
        return None
    for folder in (collection / SOURCE_DIR, collection):
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


def pdfs_in(folder: Path) -> list[Path]:
    """The PDFs directly in a folder, whatever the case of their suffix. A
    glob for *.pdf does not see PTUG.PDF on a case-sensitive filesystem, and
    a filed PDF that is not seen is a name that looks free to take."""
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")


def filed_pdfs(collection: Path) -> list[Path]:
    """Every PDF the collection has taken in: source/ and, in a collection
    not yet migrated, its root."""
    return pdfs_in(collection / SOURCE_DIR) + pdfs_in(collection)


def inbox_pdfs(collection: Path) -> list[Path]:
    return pdfs_in(collection / INBOX_DIR)


def find_collections(root: Path) -> list[Path]:
    """`root` itself when it holds docs/, otherwise each subfolder that does."""
    if (root / "docs").is_dir():
        return [root]
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "docs").is_dir())


def is_document_dir(path: Path) -> bool:
    name = path.name
    return (path / "manifest.json").is_file() and not (
        name.endswith((".old", ".new")) or name.startswith((".", "_")))


def load_manifests(collection: Path) -> dict[str, dict]:
    """{folder name: manifest} for every document in the collection."""
    out = {}
    docs = collection / "docs"
    for d in sorted(docs.iterdir()) if docs.is_dir() else []:
        if not is_document_dir(d):
            continue
        try:
            out[d.name] = json.loads((d / "manifest.json").read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
    return out


# ---------------------------------------------------------------- versions

def norm_version(version: str | None) -> str:
    """A version as people type it, comparable: "v2025_2" is "2025.2"."""
    v = re.sub(r"\s+", "", str(version or "")).upper().replace("_", ".")
    return v[1:] if re.match(r"V\d", v) else v


def version_key(version: str | None) -> tuple[int, ...] | None:
    """Numbers to order editions by, or None when the version has none.

    Where there is a year: year, release within it, any further dotted
    components, then -1 and the service pack. "2026.2" is (2026, 2, -1, 0, 0),
    "Y-2026.03-SP2" is (2026, 3, -1, 2, 0) and "2026.1.1" is
    (2026, 1, 1, -1, 0, 0). Every component counts: with the third dropped,
    2026.1.1 and 2026.1.2 were one release, so the second was refused as a
    duplicate and a request for one was answered from the other. The -1
    keeps a release ahead of its own patches and service packs apart from
    them. Anything without a year is ordered by the numbers in it as
    written. Only editions of one manual are ever compared, so two vendors'
    schemes never meet. mcp_server.version_sort and
    check_corpus.version_numbers do the same thing and must keep doing it.
    """
    v = norm_version(version)
    if not v:
        return None
    m = re.search(r"(20\d\d)\.(\d{1,2})((?:\.\d+)*)", v)
    if m:
        sp = re.search(r"SP(\d+)(?:-(\d+))?", v)
        further = tuple(int(n) for n in m.group(3).split(".") if n)
        return (int(m.group(1)), int(m.group(2)), *further, -1, int(sp.group(1)) if sp else 0,
                int(sp.group(2)) if sp and sp.group(2) else 0)
    numbers = tuple(int(n) for n in re.findall(r"\d+", v))
    return numbers or None


def version_sort(version: str | None) -> str | None:
    """version_key as text that sorts the same way, for the index."""
    key = version_key(version)
    return ".".join(f"{n:06d}" for n in key) if key else None


def same_release(a: str | None, b: str | None) -> bool:
    """Whether two version strings name one release: "2026.3" and "Y-2026.03"
    do. Judged by the numbers editions are ordered by, which is also how two
    editions are found to share a version, so a release cannot be both the
    same as an edition and absent from the manual. Versions with no number in
    them are the same only as written."""
    ka, kb = version_key(a), version_key(b)
    if ka is None or kb is None:
        return bool(norm_version(a)) and norm_version(a) == norm_version(b)
    return ka == kb


def filename_version(name: str) -> str | None:
    stem = Path(name).stem
    m = FILENAME_LETTERED_RE.search(stem)
    if m:
        return m.group(1).upper()
    m = FILENAME_YEAR_RE.search(stem)
    if not m:
        return None
    # All of it: `guide_2026.1-SP2.pdf` read as 2026.1 contradicts a cover
    # that says 2026.1-SP2, and the version is then left out for no reason.
    further = "".join(f".{int(n)}" for n in re.findall(r"\d+", m.group(3)))
    return f"{m.group(1)}.{int(m.group(2))}{further}{(m.group(4) or '').upper()}"


def _dotted(version: str) -> tuple[tuple[int, ...], tuple | None] | None:
    """A year-style version as its dotted numbers and its service pack."""
    v = norm_version(version)
    m = re.search(r"(20\d\d)\.(\d{1,2})((?:\.\d+)*)", v)
    if not m:
        return None
    sp = re.search(r"SP(\d+)(?:-(\d+))?", v)
    dotted = (int(m.group(1)), int(m.group(2)), *(int(n) for n in m.group(3).split(".") if n))
    return dotted, (sp.groups() if sp else None)


def filename_agrees(named: str, shown: str) -> bool:
    """Whether a filename's version is the cover's, or only says more or less
    of it. Digits after the release in a filename are a revision counter, a
    sequence number or a date at least as often as part of the version
    (`ug_2026_2_001.pdf`), and a filename may leave a service pack out or
    carry one the unchanged cover does not. So the two agree when one's
    numbers begin with the other's and no two service packs differ. A
    different year or release is a contradiction."""
    if same_release(named, shown):
        return True
    a, b = _dotted(named), _dotted(shown)
    if not a or not b:
        return False
    n = min(len(a[0]), len(b[0]))
    return a[0][:n] == b[0][:n] and (a[1] == b[1] or not a[1] or not b[1])


class Cover(NamedTuple):
    version: str | None   # the release the document applies to, when that is unambiguous
    later: bool           # the cover says "and later"
    found: str | None     # what the first pages say, usable or not
    note: str             # why `version` is empty, or ""


def display_version(version: str | None, later: bool = False) -> str:
    return f"{version} and later" if version and later else str(version or "-")


def split_version(text: str | None) -> tuple[str | None, bool]:
    """A version as typed on a command line: "2023.1+" or "2023.1 and later"
    is 2023.1 with the and-later claim."""
    text = (text or "").strip()
    m = re.fullmatch(r"(.+?)\s*(?:\+|\band\s+later)", text, re.I)
    return (m.group(1).strip(), True) if m else (text or None, False)


def read_cover(pdf: Path) -> Cover:
    """The tool release a PDF's first pages say it applies to, when they say
    one thing."""
    try:
        import pymupdf
    except ImportError:
        return Cover(None, False, None, "PyMuPDF is not installed, so the cover was not read")
    try:
        with pymupdf.open(str(pdf)) as doc:
            text = "\n".join(doc[i].get_text() for i in range(min(COVER_PAGES, doc.page_count)))
    except Exception as exc:  # damaged or encrypted: reported, not fatal
        return Cover(None, False, None, f"could not be opened ({str(exc)[:80]})")

    # Both ways a release is printed, together and in reading order. Taking
    # the lettered form where there was one let "Y-2026.03" on a cover that
    # also says "Software Version 2025.1" pass as unambiguous.
    hits = sorted((m.start(), m.group(1), m.end())
                  for pattern in (LETTERED_RE, LABELLED_RE) for m in pattern.finditer(text)
                  if not (pattern is LABELLED_RE
                          and NOT_THE_PRODUCT_RE.search(text, max(0, m.start() - 24), m.start())))
    if not hits:
        return Cover(None, False, None, "no version on the first pages")
    shown = hits[0][1]
    others = sorted({v for _start, v, _end in hits if not same_release(v, shown)})
    if others:
        # Not the one printed most often: a running header repeats, and how
        # often a version appears says nothing about which the manual covers.
        return Cover(None, False, shown, "the first pages name more than one version: "
                     + ", ".join([shown] + others))
    later = any(QUALIFIED_RE.match(text, end) for _start, _v, end in hits)
    named = filename_version(pdf.name)
    if named and not filename_agrees(named, shown):
        # A manual the vendor ships unchanged keeps its cover and takes each
        # new release's filename: "2023.1 and later" inside x_2025_2.pdf. That
        # agrees with the cover. A filename naming an earlier release, or a
        # different one where the cover claims nothing, does not.
        covered = later and (version_key(named) or ()) > (version_key(shown) or ())
        if not covered:
            return Cover(None, later, shown, f"the cover says {display_version(shown, later)} "
                                             f"and the filename says {named}")
    return Cover(shown, later, shown, "")


def default_doc_id(slug: str, version: str | None = None) -> str:
    """The name a manual's editions share: the slug without its release.

    The document's own version where the slug ends in it, whatever its shape:
    `guide-4-1` at 4.1 is `guide`, or 4.1 and 4.2 of one manual would be two
    unrelated manuals that both answer every search. Otherwise a year-style
    ending, which is what a slug named after a filename carries. Nothing else
    is taken off: a number that is not this document's version is part of the
    manual's name.
    """
    tail = f"-{slugify(version)}" if version and slugify(version) else ""
    if tail and slug.endswith(tail) and len(slug) > len(tail):
        return slug[: -len(tail)]
    return VERSION_SUFFIX_RE.sub("", slug) or slug


def default_slug(stem_slug: str, version: str | None) -> str:
    """docs/<slug> for a new edition: the manual's name and its release, so
    two editions of one PDF name never ask for the same folder."""
    return f"{default_doc_id(stem_slug, version)}-{slugify(version)}" if version else stem_slug


# -------------------------------------------------------------------- pins

def load_pins(collection: Path) -> dict[str, str]:
    """current_versions.json: {"<doc_id>": "<version>"}. Raises ValueError on
    a file that is there and not that shape -- a pin that is silently ignored
    answers from an edition nobody chose."""
    path = collection / PINS_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} is not readable JSON: {exc}") from exc
    if not isinstance(data, dict) or not all(isinstance(v, str) and v.strip() for v in data.values()):
        raise ValueError(f'{path} must be an object of "<doc_id>": "<version>" pairs')
    return {str(k): v.strip() for k, v in data.items()}


def applies_to(editions: list[dict], release: str | None) -> dict | None:
    """The edition of one manual that documents tool release `release`.

    The one for exactly that release. Failing that, the nearest earlier one,
    and only if it says "and later": that is the document's own claim to
    cover what follows it, up to the next edition. An earlier edition that
    makes no such claim is not offered -- that it still holds would be our
    guess, not the vendor's statement.
    """
    for e in editions:
        if e.get("version") and same_release(e["version"], release):
            return e
    key = version_key(release)
    earlier = [e for e in editions if key and (version_key(e.get("version")) or key) < key]
    if not earlier:
        return None
    nearest = max(earlier, key=lambda e: version_key(e["version"]))
    return nearest if nearest.get("version_and_later") else None


def resolve_editions(docs: list[dict], pins: dict[str, str]) -> list[str]:
    """Mark each of one collection's documents `is_latest` / `is_current`.

    `docs` are dicts with slug, doc_id, version and version_and_later; the two
    flags are added in place. A pin names the tool release in use, and selects
    the edition that applies to it (see applies_to). Returns what stops the
    result being trusted: a manual with two editions where one has no usable
    version (which is newer cannot be told), two editions with the same
    version, a pin on a manual that is not here or a release none of its
    editions covers. Nothing is guessed around any of them.
    """
    problems: list[str] = []
    lines: dict[str, list[dict]] = {}
    for d in docs:
        d["is_latest"] = d["is_current"] = True
        lines.setdefault(d["doc_id"], []).append(d)

    for doc_id, editions in sorted(lines.items()):
        if len(editions) > 1:
            unordered = [e["slug"] for e in editions if version_key(e.get("version")) is None]
            if unordered:
                problems.append(f"{doc_id}: {len(editions)} editions, and {', '.join(unordered)} "
                                "has no version to order them by")
                continue
            seen: dict[tuple, str] = {}
            clash = False
            for e in editions:
                key = version_key(e["version"])
                if key in seen:
                    problems.append(f"{doc_id}: {seen[key]} and {e['slug']} are both version {e['version']}")
                    clash = True
                seen[key] = e["slug"]
            if clash:
                continue
            latest = max(editions, key=lambda e: version_key(e["version"]))
            for e in editions:
                e["is_latest"] = e["is_current"] = e is latest
        pin = pins.get(doc_id)
        if pin is None:
            continue
        match = applies_to(editions, pin)
        if match is None:
            have = ", ".join(display_version(e.get("version"), bool(e.get("version_and_later")))
                             if e.get("version") else "no version" for e in editions)
            problems.append(f"{PINS_FILE} pins {doc_id} to {pin}; its editions here are: {have}")
            continue
        for e in editions:
            e["is_current"] = e is match

    for doc_id in sorted(set(pins) - set(lines)):
        problems.append(f"{PINS_FILE} pins {doc_id}, which is not a doc_id in this collection")
    return problems


# ------------------------------------------------------------------ intake

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def same_file(pdf: Path, others: list[Path]) -> Path | None:
    """One of `others` holding exactly the bytes of `pdf`, under any name."""
    size, digest = pdf.stat().st_size, None
    for other in others:
        try:
            if other.resolve() == pdf.resolve() or other.stat().st_size != size:
                continue
        except OSError:
            continue
        digest = digest or _sha256(pdf)
        if _sha256(other) == digest:
            return other
    return None


def versioned_name(name: str, version: str) -> str:
    """`ptug.pdf` at Y-2027.03 as `ptug_Y-2027.03.pdf`."""
    path = Path(name)
    return f"{path.stem}_{re.sub(r'[^0-9A-Za-z.-]+', '-', version).strip('-')}{path.suffix}"


def free_source_name(collection: Path, name: str, version: str | None) -> str:
    """A name no filed PDF has: `name` with the version on the end, or
    failing that with a number."""
    taken = {p.name.lower() for p in filed_pdfs(collection)}
    path = Path(name)
    candidates = ([versioned_name(name, version)] if version else []) + [
        f"{path.stem}_{n}{path.suffix}" for n in range(2, 1000)]
    return next(c for c in candidates if c.lower() not in taken)


def source_name_for(collection: Path, pdf: Path, version: str | None) -> str:
    """The name a PDF from new_docs/ takes in source/.

    Its own, unless a different PDF already has it: vendors reuse a filename
    from one release to the next. Then the version goes on the end. Exits
    rather than choose when that cannot be done -- the same file filed twice,
    or the same name with no version to tell the two apart.
    """
    filed = filed_pdfs(collection)
    twin = same_file(pdf, filed)
    if twin:
        where = twin.relative_to(collection)
        converted = any(m.get("source_pdf") == twin.name for m in load_manifests(collection).values())
        sys.exit(f"{pdf.name} is byte for byte the PDF already filed as {where}. "
                 + ("Nothing to convert; " if converted else
                    f"That one has not been converted: run the converter on {where} instead, and ")
                 + f"delete the copy in {INBOX_DIR}/.")
    taken = {p.name.lower() for p in filed}
    if pdf.name.lower() not in taken:
        return pdf.name
    if not version:
        sys.exit(f"{SOURCE_DIR}/ already holds a different {pdf.name}, and this one's cover gives no "
                 "version to tell them apart. Pass --version.")
    name = versioned_name(pdf.name, version)
    if name.lower() in taken:
        sys.exit(f"{SOURCE_DIR}/ already holds both {pdf.name} and {name}, and neither is this file. "
                 "Two different PDFs claim the same name and version; rename one by hand.")
    return name


class Plan(NamedTuple):
    pdf: Path
    collection: Path
    out_root: Path
    slug: str
    doc_id: str
    version: str | None
    later: bool
    source_name: str
    move: bool


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--version", help="the tool release this document applies to; end it with + for "
                                      '"and later", e.g. 2023.1+ (default: read from the PDF\'s first pages)')
    ap.add_argument("--doc-id", help="the name every edition of this manual shares "
                                     "(default: the slug without its release)")


def plan(pdf: Path, slug: str | None, out_root: Path | None, version: str | None,
         doc_id: str | None, later: bool = False) -> Plan:
    """Decide, before anything is written, what a conversion will be called.

    A PDF in new_docs/ converted into its own collection is moved to source/
    when the conversion ends (see finish), so its final name has to be known
    now: the manifest records it.
    """
    pdf = pdf.resolve()
    collection = collection_of(pdf)
    out_root = (out_root or collection / "docs").resolve()

    if version:
        version, typed_later = split_version(version)
        later = later or typed_later
        print(f"version: {display_version(version, later)} (given)")
    else:
        cover = read_cover(pdf)
        version, later = cover.version, bool(cover.version) and cover.later
        print(f"version: {display_version(version, later)} (from the cover)" if version
              else f"version: not recorded -- {cover.note}. Pass --version to set it.")

    move = pdf.parent.name == INBOX_DIR and out_root == (collection / "docs").resolve()
    source_name = source_name_for(collection, pdf, version) if move else pdf.name
    if source_name != pdf.name:
        print(f"{SOURCE_DIR}/ already has a {pdf.name}; this one will be filed as {source_name}")

    # A filename with no ASCII letter or digit in it slugs to nothing, and an
    # empty slug is docs/ itself.
    slug = slug or default_slug(slugify(pdf.stem) or "document", version)
    doc_id = doc_id or default_doc_id(slug, version)

    siblings, unstamped = [], []
    for folder, m in load_manifests(out_root.parent).items() if out_root.name == "docs" else ():
        if folder == slug:
            continue
        if m.get("doc_id") == doc_id:
            siblings.append(m)
        elif "doc_id" not in m and default_doc_id(folder, m.get("version")) == doc_id:
            unstamped.append(folder)
    for m in siblings:
        if version and m.get("version") and same_release(m["version"], version):
            sys.exit(f"{doc_id} {version} is already converted as docs/{m.get('slug')}. "
                     "Pass --doc-id if this is a different manual, or --version if the release is wrong.")
    if siblings:
        others = ", ".join(sorted(str(m.get("version") or m.get("slug")) for m in siblings))
        print(f"doc_id: {doc_id} -- an edition of a manual already here ({others})")
        if not version:
            sys.exit(f"{doc_id} already has an edition here, so this one needs a version to be "
                     "ordered against it. Pass --version.")
    else:
        print(f"doc_id: {doc_id} -- a manual not converted here before")
    if unstamped:
        print(f"  !! {', '.join(unstamped)} looks like another edition of {doc_id} but its manifest has "
              f"no doc_id, so search will treat the two as unrelated manuals. Run: "
              f'python scripts/editions.py stamp "{out_root.parent}"')
    return Plan(pdf, collection, out_root, slug, doc_id, version, later, source_name, move)


def finish(p: Plan) -> None:
    """Move a converted PDF from new_docs/ to source/. Last, so a conversion
    that failed leaves the PDF where it was waiting."""
    if not p.move:
        return
    target = p.collection / SOURCE_DIR / p.source_name
    target.parent.mkdir(exist_ok=True)
    if target.exists():
        # The name was free when the conversion started, and the manifest was
        # written with it. Another PDF has been filed under it since. Left
        # as it is, this edition's manifest names that PDF -- every tool
        # looks in source/ first -- so its figures and page images would come
        # from a different document; and rename() would replace the file
        # without a word on POSIX. So take a name nothing has, and put it in
        # the manifest before the move.
        name = free_source_name(p.collection, p.source_name, p.version)
        manifest_path = p.out_root / p.slug / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        manifest["source_pdf"] = name
        manifest_path.write_text(dump_manifest(manifest), encoding="utf-8")
        print(f"  !! {SOURCE_DIR}/{p.source_name} was taken while this conversion ran. Nothing was "
              f"overwritten: this PDF is filed as {name}, and the manifest now says so.")
        target = target.with_name(name)
    try:
        # A rename, never a copy: a half-moved PDF would be filed twice.
        p.pdf.rename(target)
    except OSError as exc:
        print(f"  !! could not move {INBOX_DIR}/{p.pdf.name} to {SOURCE_DIR}/{target.name} ({exc}). "
              "The conversion is complete and its manifest names that file; move it by hand.")
        return
    print(f"moved {INBOX_DIR}/{p.pdf.name} -> {SOURCE_DIR}/{target.name}")


# ---------------------------------------------------------------- commands

def dump_manifest(manifest: dict) -> str:
    # ensure_ascii, as the converters write it: a non-UTF-8 default locale
    # mangles a literal trademark sign for any reader that forgets encoding=.
    return json.dumps(manifest, indent=2) + "\n"


def with_edition_fields(manifest: dict, doc_id: str | None, version: str | None,
                        later: bool = False) -> dict:
    """The manifest with doc_id and version placed after slug, where a person
    reading it looks for what the document is. `version` is left out, not
    written as null, when the cover gave none; `version_and_later` appears
    only when the cover makes that claim."""
    fields = {}
    if doc_id:
        fields["doc_id"] = doc_id
    if version:
        fields["version"] = version
        if later:
            fields["version_and_later"] = True
    out = {}
    for key, value in manifest.items():
        if key in ("doc_id", "version", "version_and_later"):
            continue
        out[key] = value
        if key == "slug":
            out.update(fields)
    if "slug" not in manifest:
        out.update(fields)
    return out


def parse_pairs(pairs: list[str], what: str) -> dict[str, str]:
    out = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip() or not value.strip():
            sys.exit(f"{what} takes SLUG=VALUE, not {pair!r}")
        out[key.strip()] = value.strip()
    return out


def cmd_stamp(args) -> int:
    collection = args.path.resolve()
    manifests = load_manifests(collection)
    if not manifests:
        sys.exit(f"No documents under {collection / 'docs'}")
    set_version = parse_pairs(args.set, "--set")
    set_doc_id = parse_pairs(args.doc_id, "--doc-id")
    for slug in sorted((set(set_version) | set(set_doc_id)) - set(manifests)):
        sys.exit(f"{slug} is not a document in {collection.name}")

    backup = collection / BACKUP_DIR / f"manifests-{time.strftime('%Y-%m-%d')}-pre-editions"
    changed, unset = 0, []
    print(f"{collection.name} ({'DRY RUN -- nothing written' if args.dry_run else 'writing'})")
    print(f"  {'document':42s} {'doc_id':30s} {'version':16s} note")
    for slug, m in manifests.items():
        note = ""
        if slug in set_version:
            version, later = split_version(set_version[slug])
        else:
            version, later = m.get("version"), m.get("version_and_later") is True
            pdf = find_source_pdf(collection, m.get("source_pdf"))
            cover = read_cover(pdf) if pdf else None
            if not version and cover:
                version, later, note = cover.version, bool(cover.version) and cover.later, cover.note
            elif not version:
                note = "source PDF not found, so the cover was not read"
            elif cover and cover.later and same_release(cover.version, version):
                later = True          # stamped before "and later" was recorded
        if not version:
            unset.append(slug)
        # After the version: a slug ending in this document's own version
        # loses it, whatever the version looks like.
        doc_id = set_doc_id.get(slug) or m.get("doc_id") or default_doc_id(slug, version)
        new = with_edition_fields(m, doc_id, version, later)
        differs = any(new.get(k) != m.get(k) for k in ("doc_id", "version", "version_and_later"))
        print(f"  {slug[:42]:42s} {doc_id[:30]:30s} {display_version(version, later):16s} "
              f"{'' if differs else '(unchanged) '}{note}")
        if differs and not args.dry_run:
            path = collection / "docs" / slug / "manifest.json"
            backup.mkdir(parents=True, exist_ok=True)
            kept = backup / f"{slug}.manifest.json"
            if not kept.exists():
                shutil.copy2(path, kept)
            path.write_text(dump_manifest(new), encoding="utf-8")
        changed += differs
    print(f"\n{changed} manifest(s) {'would change' if args.dry_run else 'changed'}"
          + (f"; originals kept in {backup.relative_to(collection)}" if changed and not args.dry_run else ""))
    if unset:
        print(f"{len(unset)} left without a version: {', '.join(unset)}\n"
              "  That is fine for a manual with one edition. Set one with --set SLUG=VERSION "
              "(VERSION+ for \"and later\") before converting a second.")
    print("Next: build_index.py, then build_search_db.py.")
    return 0


def cmd_migrate(args) -> int:
    collection = args.path.resolve()
    if not (collection / "docs").is_dir():
        sys.exit(f"No docs/ under {collection}")
    manifests = load_manifests(collection)
    accounted = {str(m.get("source_pdf")) for m in manifests.values()}
    superseded = collection / "superseded.json"
    if superseded.is_file():
        try:
            accounted |= {str(e.get("file")) for e in json.loads(superseded.read_text(encoding="utf-8-sig"))
                          if isinstance(e, dict)}
        except (OSError, json.JSONDecodeError) as exc:
            sys.exit(f"{superseded} is not readable JSON ({exc}); fix it first, or PDFs it accounts "
                     f"for would be sent back to {INBOX_DIR}/.")

    moves = [(pdf, collection / (SOURCE_DIR if pdf.name in accounted else INBOX_DIR) / pdf.name)
             for pdf in pdfs_in(collection)]
    print(f"{collection.name} ({'DRY RUN -- nothing moved' if args.dry_run else 'moving'})")
    blocked = [target for _pdf, target in moves if target.exists()]
    if blocked:
        sys.exit("These already exist, so nothing was moved: "
                 + ", ".join(str(t.relative_to(collection)) for t in blocked))
    for pdf, target in moves:
        print(f"  {pdf.name} -> {target.parent.name}/")
        if not args.dry_run:
            target.parent.mkdir(exist_ok=True)
            shutil.move(str(pdf), str(target))
    if not args.dry_run:
        for folder in (SOURCE_DIR, INBOX_DIR):
            (collection / folder).mkdir(exist_ok=True)
    to_source = sum(1 for _p, t in moves if t.parent.name == SOURCE_DIR)
    print(f"\n{to_source} PDF(s) to {SOURCE_DIR}/, {len(moves) - to_source} to {INBOX_DIR}/ "
          "(neither converted nor in superseded.json).")
    return 0


def cmd_status(args) -> int:
    root = args.path.resolve()
    collections = find_collections(root)
    if not collections:
        sys.exit(f"No docs/ under {root}")
    worst = 0
    for collection in collections:
        manifests = load_manifests(collection)
        try:
            pins = load_pins(collection)
        except ValueError as exc:
            print(f"!! {exc}")
            pins, worst = {}, 1
        docs = [{"slug": slug, "doc_id": m.get("doc_id") or slug, "version": m.get("version"),
                 "version_and_later": m.get("version_and_later") is True,
                 "stamped": "doc_id" in m, "title": m.get("title") or slug}
                for slug, m in manifests.items()]
        problems = resolve_editions(docs, pins)
        print(f"{collection.name}: {len(docs)} document(s), "
              f"{len({d['doc_id'] for d in docs})} manual(s)")
        for d in sorted(docs, key=lambda d: (d["doc_id"], version_key(d["version"]) or ())):
            marks = [m for m, on in (("current", d["is_current"]),
                                     ("pinned", d["is_current"] and d["doc_id"] in pins),
                                     ("latest", d["is_latest"] and not d["is_current"]),
                                     ("no doc_id in manifest", not d["stamped"])) if on]
            print(f"  {d['doc_id'][:32]:32s} {display_version(d['version'], d['version_and_later']):16s} "
                  f"{d['slug'][:42]:42s} {', '.join(marks)}")
        waiting = inbox_pdfs(collection)
        if waiting:
            print(f"  waiting in {INBOX_DIR}/: " + ", ".join(p.name for p in waiting))
        loose = pdfs_in(collection)
        if loose:
            print(f"  {len(loose)} PDF(s) at the collection root, not in {SOURCE_DIR}/ -- "
                  f"see `editions.py migrate`")
        for p in problems:
            print(f"  !! {p}")
            worst = 1
        print()
    return worst


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    s = sub.add_parser("status", help="manuals, editions, pins and waiting PDFs")
    s.add_argument("path", type=Path, help="corpus root or one collection")
    s.set_defaults(run=cmd_status)

    s = sub.add_parser("stamp", help="add doc_id and version to manifests that lack them")
    s.add_argument("path", type=Path, help="one collection")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--set", action="append", default=[], metavar="SLUG=VERSION",
                   help="set this document's version; VERSION+ for \"and later\" (repeatable)")
    s.add_argument("--doc-id", action="append", default=[], metavar="SLUG=ID",
                   help="set this document's doc_id (repeatable)")
    s.set_defaults(run=cmd_stamp)

    s = sub.add_parser("migrate", help=f"move root PDFs into {SOURCE_DIR}/ or {INBOX_DIR}/")
    s.add_argument("path", type=Path, help="one collection")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(run=cmd_migrate)

    args = ap.parse_args()
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
