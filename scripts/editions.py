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

The version is read from the PDF's first pages, never from its filename:
on one real corpus twelve filenames carried no version at all, and two named
a release their cover did not. Where the cover gives none, or disagrees with the
filename, or only says where support starts ("2023.1 and later") with no
filename to confirm it, `version` is left out and reported, to be set by hand
with --version or `stamp --set`.

The converters import this for the layout and the manifest fields. The
commands below are for a corpus that predates either:

  python scripts/editions.py status  <corpus or collection>
  python scripts/editions.py stamp   <collection> [--dry-run] [--set SLUG=VERSION] [--doc-id SLUG=ID]
  python scripts/editions.py migrate <collection> [--dry-run]

`status` lists every manual with its editions, the pins, and the PDFs still
waiting. `stamp` adds `doc_id` and `version` to manifests that lack them,
keeping the old manifests under .rebuild-backup/. `migrate` moves the PDFs at
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
from collections import Counter
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
# "Software Version 2023.1 and later": the cover names where support starts,
# not which release this document is.
QUALIFIED_RE = re.compile(r"\s+(?:and|or)\s+(?:later|newer|above|higher)\b", re.I)

FILENAME_LETTERED_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]-20\d\d\.\d\d(?:-SP\d+(?:-\d+)?)?)", re.I)
FILENAME_YEAR_RE = re.compile(r"(?<!\d)(20\d\d)[._](\d{1,2})(?!\d)")

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
    """A manifest's `source_pdf` on disk. source/ first; then the collection
    root, where every PDF lived before source/ existed; then new_docs/, where
    one sits while its own conversion is still running."""
    if not name:
        return None
    for folder in (collection / SOURCE_DIR, collection, collection / INBOX_DIR):
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


def filed_pdfs(collection: Path) -> list[Path]:
    """Every PDF the collection has taken in: source/ and, in a collection
    not yet migrated, its root."""
    found = sorted((collection / SOURCE_DIR).glob("*.pdf")) if (collection / SOURCE_DIR).is_dir() else []
    return found + sorted(collection.glob("*.pdf"))


def inbox_pdfs(collection: Path) -> list[Path]:
    return sorted((collection / INBOX_DIR).glob("*.pdf")) if (collection / INBOX_DIR).is_dir() else []


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

    "Y-2026.03-SP2" is (2026, 3, 2, 0) and "2026.2" is (2026, 2, 0, 0): year,
    release within it, service pack. Anything without a year is ordered by
    the numbers in it as written. Only editions of one manual are ever
    compared, so two vendors' schemes never meet.
    """
    v = norm_version(version)
    if not v:
        return None
    m = re.search(r"(20\d\d)\.(\d{1,2})", v)
    if m:
        sp = re.search(r"SP(\d+)(?:-(\d+))?", v)
        return (int(m.group(1)), int(m.group(2)), int(sp.group(1)) if sp else 0,
                int(sp.group(2)) if sp and sp.group(2) else 0)
    numbers = tuple(int(n) for n in re.findall(r"\d+", v))
    return numbers or None


def version_sort(version: str | None) -> str | None:
    """version_key as text that sorts the same way, for the index."""
    key = version_key(version)
    return ".".join(f"{n:06d}" for n in key) if key else None


def filename_version(name: str) -> str | None:
    stem = Path(name).stem
    m = FILENAME_LETTERED_RE.search(stem)
    if m:
        return m.group(1).upper()
    m = FILENAME_YEAR_RE.search(stem)
    return f"{m.group(1)}.{int(m.group(2))}" if m else None


class Cover(NamedTuple):
    version: str | None   # safe to record as this document's release
    found: str | None     # what the first pages say, usable or not
    note: str             # why `version` is empty, or ""


def read_cover(pdf: Path) -> Cover:
    """The release named on a PDF's first pages, when it is unambiguous."""
    try:
        import pymupdf
    except ImportError:
        return Cover(None, None, "PyMuPDF is not installed, so the cover was not read")
    try:
        with pymupdf.open(str(pdf)) as doc:
            text = "\n".join(doc[i].get_text() for i in range(min(COVER_PAGES, doc.page_count)))
    except Exception as exc:  # damaged or encrypted: reported, not fatal
        return Cover(None, None, f"could not be opened ({str(exc)[:80]})")

    lettered = [(m.group(1), m.end()) for m in LETTERED_RE.finditer(text)]
    hits = lettered or [(m.group(1), m.end()) for m in LABELLED_RE.finditer(text)]
    if not hits:
        return Cover(None, None, "no version on the first pages")
    counts = Counter(norm_version(v) for v, _ in hits)
    (best, n), *rest = counts.most_common()
    shown = next(v for v, _ in hits if norm_version(v) == best)
    if rest and rest[0][1] == n:
        return Cover(None, shown, "the first pages name more than one version: "
                     + ", ".join(sorted(counts)))
    named = filename_version(pdf.name)
    if named and norm_version(named) != best:
        return Cover(None, shown, f"the cover says {shown} and the filename says {named}")
    # Unless the filename says the same: then two sources agree on the release.
    if not named and any(QUALIFIED_RE.match(text, end) for v, end in hits if norm_version(v) == best):
        return Cover(None, shown, f'the cover says "{shown} and later", which is where support '
                                  "starts, not this document's release")
    return Cover(shown, shown, "")


def default_doc_id(slug: str) -> str:
    """The name a manual's editions share: the slug without its release."""
    return VERSION_SUFFIX_RE.sub("", slug) or slug


def default_slug(stem_slug: str, version: str | None) -> str:
    """docs/<slug> for a new edition: the manual's name and its release, so
    two editions of one PDF name never ask for the same folder."""
    return f"{default_doc_id(stem_slug)}-{slugify(version)}" if version else stem_slug


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


def resolve_editions(docs: list[dict], pins: dict[str, str]) -> list[str]:
    """Mark each of one collection's documents `is_latest` / `is_current`.

    `docs` are dicts with slug, doc_id and version; the two flags are added
    in place. Returns what stops the result being trusted: a manual with two
    editions where one has no usable version (which is newer cannot be told),
    two editions with the same version, a pin on a manual or a version that
    is not here. Nothing is guessed around any of them.
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
        match = [e for e in editions if e.get("version") and norm_version(e["version"]) == norm_version(pin)]
        if not match:
            have = ", ".join(str(e.get("version") or "no version") for e in editions)
            problems.append(f"{PINS_FILE} pins {doc_id} to {pin}; its editions here are: {have}")
            continue
        for e in editions:
            e["is_current"] = e is match[0]

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
        sys.exit(f"{pdf.name} is byte for byte the PDF already filed as "
                 f"{twin.relative_to(collection)}. Nothing to convert; delete the copy in {INBOX_DIR}/.")
    taken = {p.name.lower() for p in filed}
    if pdf.name.lower() not in taken:
        return pdf.name
    if not version:
        sys.exit(f"{SOURCE_DIR}/ already holds a different {pdf.name}, and this one's cover gives no "
                 "version to tell them apart. Pass --version.")
    name = f"{pdf.stem}_{re.sub(r'[^0-9A-Za-z.-]+', '-', version).strip('-')}{pdf.suffix}"
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
    source_name: str
    move: bool


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--version", help="this document's release (default: read from the PDF's first pages)")
    ap.add_argument("--doc-id", help="the name every edition of this manual shares "
                                     "(default: the slug without its release)")


def plan(pdf: Path, slug: str | None, out_root: Path | None, version: str | None,
         doc_id: str | None) -> Plan:
    """Decide, before anything is written, what a conversion will be called.

    A PDF in new_docs/ converted into its own collection is moved to source/
    when the conversion ends (see finish), so its final name has to be known
    now: the manifest records it.
    """
    pdf = pdf.resolve()
    collection = collection_of(pdf)
    out_root = (out_root or collection / "docs").resolve()

    if version:
        print(f"version: {version} (given)")
    else:
        cover = read_cover(pdf)
        version = cover.version
        print(f"version: {version} (from the cover)" if version
              else f"version: not recorded -- {cover.note}. Pass --version to set it.")

    move = pdf.parent.name == INBOX_DIR and out_root == (collection / "docs").resolve()
    source_name = source_name_for(collection, pdf, version) if move else pdf.name
    if source_name != pdf.name:
        print(f"{SOURCE_DIR}/ already has a {pdf.name}; this one will be filed as {source_name}")

    slug = slug or default_slug(slugify(pdf.stem), version)
    doc_id = doc_id or default_doc_id(slug)

    siblings, unstamped = [], []
    for folder, m in load_manifests(out_root.parent).items() if out_root.name == "docs" else ():
        if folder == slug:
            continue
        if m.get("doc_id") == doc_id:
            siblings.append(m)
        elif "doc_id" not in m and default_doc_id(folder) == doc_id:
            unstamped.append(folder)
    for m in siblings:
        if version and m.get("version") and norm_version(m["version"]) == norm_version(version):
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
    return Plan(pdf, collection, out_root, slug, doc_id, version, source_name, move)


def manifest_fields(p: Plan) -> dict:
    """What a converter adds to its manifest. `version` is left out, not
    written as null, when the cover did not give one."""
    fields = {"source_pdf": p.source_name, "doc_id": p.doc_id}
    if p.version:
        fields["version"] = p.version
    return fields


def finish(p: Plan) -> None:
    """Move a converted PDF from new_docs/ to source/. Last, so a conversion
    that failed leaves the PDF where it was waiting."""
    if not p.move:
        return
    target = p.collection / SOURCE_DIR / p.source_name
    target.parent.mkdir(exist_ok=True)
    try:
        # A rename, never a copy: a half-moved PDF would be filed twice.
        p.pdf.rename(target)
    except OSError as exc:
        print(f"  !! could not move {INBOX_DIR}/{p.pdf.name} to {SOURCE_DIR}/{p.source_name} ({exc}). "
              "The conversion is complete; move the file by hand.")
        return
    print(f"moved {INBOX_DIR}/{p.pdf.name} -> {SOURCE_DIR}/{p.source_name}")


# ---------------------------------------------------------------- commands

def dump_manifest(manifest: dict) -> str:
    # ensure_ascii, as the converters write it: a non-UTF-8 default locale
    # mangles a literal trademark sign for any reader that forgets encoding=.
    return json.dumps(manifest, indent=2) + "\n"


def with_edition_fields(manifest: dict, doc_id: str | None, version: str | None) -> dict:
    """The manifest with doc_id and version placed after slug, where a person
    reading it looks for what the document is."""
    out = {}
    for key, value in manifest.items():
        if key in ("doc_id", "version"):
            continue
        out[key] = value
        if key == "slug":
            if doc_id:
                out["doc_id"] = doc_id
            if version:
                out["version"] = version
    if "slug" not in manifest:
        if doc_id:
            out["doc_id"] = doc_id
        if version:
            out["version"] = version
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
        doc_id = set_doc_id.get(slug) or m.get("doc_id") or default_doc_id(slug)
        version, note = set_version.get(slug) or m.get("version"), ""
        if not version:
            pdf = find_source_pdf(collection, m.get("source_pdf"))
            if pdf is None:
                note = "source PDF not found, so the cover was not read"
            else:
                cover = read_cover(pdf)
                version, note = cover.version, cover.note
        if not version:
            unset.append(slug)
        new = with_edition_fields(m, doc_id, version)
        differs = new.get("doc_id") != m.get("doc_id") or new.get("version") != m.get("version")
        print(f"  {slug[:42]:42s} {doc_id[:30]:30s} {str(version or '-'):16s} "
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
              "before converting a second.")
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
             for pdf in sorted(collection.glob("*.pdf"))]
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
            print(f"  {d['doc_id'][:32]:32s} {str(d['version'] or '-'):16s} {d['slug'][:42]:42s} "
                  f"{', '.join(marks)}")
        waiting = inbox_pdfs(collection)
        if waiting:
            print(f"  waiting in {INBOX_DIR}/: " + ", ".join(p.name for p in waiting))
        loose = sorted(collection.glob("*.pdf"))
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
                   help="set this document's version (repeatable)")
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
