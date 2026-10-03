#!/usr/bin/env python3
"""
Regenerate a vendor folder's docs/index.json and docs/README.md from the
docs/<slug>/manifest.json files actually on disk, plus that folder's
superseded.json (hand-maintained list of {"file": <old pdf>, "superseded_by":
<slug>}) for editions kept as PDFs only.

Run this after convert_manual.py adds or replaces a manual, so the index
never drifts out of sync with what's actually converted.

Each manual is listed with the `doc_id` and `version` from its manifest and
whether it is the edition search answers from -- the newest of its doc_id
unless current_versions.json pins another (see editions.py).

Usage:
  python scripts/build_index.py "Synopsys Manual"
  python scripts/build_index.py "Tessent Manual"
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import editions  # noqa: E402


def load_manuals(docs_dir: Path) -> list[dict]:
    manuals = []
    for manifest_path in sorted(docs_dir.glob("*/manifest.json")):
        slug = manifest_path.parent.name
        # Ignore scratch/backup dirs so a rebuild left-over is never listed
        # as a real manual.
        if slug.endswith((".old", ".new")) or slug.startswith((".", "_")):
            continue
        m = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        manuals.append(
            {
                "slug": slug,
                "doc_id": m.get("doc_id") or slug,
                "version": m.get("version"),
                "title": m["title"],
                "source_pdf": m["source_pdf"],
                "page_count": m["page_count"],
                "section_count": len(m["sections"]),
                "full_md": f"{slug}/full.md",
                "sections_dir": f"{slug}/sections/",
            }
        )
    manuals.sort(key=lambda x: x["slug"])
    return manuals


def mark_current(vendor_dir: Path, manuals: list[dict]) -> list[str]:
    """Add `current` to each manual; returns what makes that undecidable."""
    try:
        pins = editions.load_pins(vendor_dir)
    except ValueError as exc:
        pins, problems = {}, [str(exc)]
    else:
        problems = []
    problems += editions.resolve_editions(manuals, pins)
    for m in manuals:
        m["current"] = bool(m.pop("is_current"))
        m.pop("is_latest")
    return problems


def load_superseded(vendor_dir: Path) -> list[dict]:
    p = vendor_dir / "superseded.json"
    return json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else []


def load_vendor_label(vendor_dir: Path) -> str:
    """Prose name for this vendor, used in docs/README.md's intro line.
    Optional vendor.json {"label": "..."} overrides the default, which is
    just the folder name minus a trailing " Manual" (e.g. "Tessent" ->
    "Siemens Tessent")."""
    p = vendor_dir / "vendor.json"
    if p.exists():
        label = json.loads(p.read_text(encoding="utf-8-sig")).get("label")
        if label:
            return label
    return vendor_dir.name.replace(" Manual", "")


def write_index_json(docs_dir: Path, manuals: list[dict], superseded: list[dict]) -> None:
    data = {"manuals": manuals, "skipped_older_versions": superseded}
    # ensure_ascii=True and no trailing newline. Matches Tessent's existing
    # index.json byte-for-byte; Synopsys's currently stores those same
    # characters literally, so it gets one cosmetic rewrite to escapes on
    # first regeneration (identical data -- json.load returns the same
    # strings either way). Escapes are the safer default here because this
    # machine's Python defaults to a non-UTF-8 locale (cp950), where a
    # literal "™" breaks any reader that forgets encoding="utf-8".
    (docs_dir / "index.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_readme(vendor_dir: Path, docs_dir: Path, manuals: list[dict], superseded: list[dict]) -> None:
    vendor_label = load_vendor_label(vendor_dir)
    lines = [
        f"# {vendor_dir.name} Docs (Markdown, RAG-ready)",
        "",
        f"Converted from the {vendor_label} EDA tool PDFs in `../{editions.SOURCE_DIR}/`. Each manual "
        "has its own folder with a full markdown dump plus per-section files for "
        "finer-grained retrieval.",
        "",
        "## Manuals",
        "",
        "| Manual | Version | Slug | Pages | Sections | Full doc |",
        "|---|---|---|---|---|---|",
    ]
    for m in manuals:
        version = (m["version"] or "-") + ("" if m["current"] else " (not current)")
        lines.append(
            f"| {m['title']} | {version} | `{m['slug']}` | {m['page_count']} | {m['section_count']} "
            f"| [{m['full_md']}]({m['full_md']}) |"
        )
    if not all(m["current"] for m in manuals):
        lines += [
            "",
            "Search answers from one edition of each manual: the newest, unless "
            f"`{editions.PINS_FILE}` pins another. The others are read only when asked for by "
            "version.",
        ]
    lines += [
        "",
        "## Folder layout",
        "",
        "```",
        "source/              <- the PDFs these were converted from",
        "new_docs/            <- PDFs waiting to be converted",
        "docs/",
        "  index.json          <- machine-readable index of all manuals",
        "  <manual-slug>/",
        "    manifest.json     <- title, page count, PDF TOC, section list",
        "    full.md           <- entire manual as one markdown file",
        "    sections/",
        "      001-*.md ...    <- split on headings, one chunk per file",
        "```",
    ]
    if superseded:
        lines += [
            "",
            "## Older versions skipped",
            "",
            "These PDFs were superseded by a newer version already covered above and were "
            "not converted, to avoid duplicate/conflicting chunks in a RAG index:",
            "",
        ]
        lines += [f"- `{s['file']}` -> see `{s['superseded_by']}`" for s in superseded]
        lines += [
            "",
            "The original PDFs remain in `source/` if you need to look up "
            "version-specific behavior later, or to convert one as an edition of its own.",
        ]
    (docs_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def report_orphans(vendor_dir: Path, manuals: list[dict], superseded: list[dict]) -> None:
    accounted_for = {m["source_pdf"] for m in manuals} | {s["file"] for s in superseded}
    orphans = [p for p in editions.filed_pdfs(vendor_dir) if p.name not in accounted_for]
    if orphans:
        print("PDFs with no docs/ folder and no superseded.json entry:")
        for o in orphans:
            print(f"  - {o.relative_to(vendor_dir)}")
        print("Convert them, add a superseded.json entry, or ignore if intentional.")
    waiting = editions.inbox_pdfs(vendor_dir)
    if waiting:
        print(f"Waiting in {editions.INBOX_DIR}/: " + ", ".join(p.name for p in waiting))
    print(
        f"{len(manuals)} converted manuals, {len(superseded)} superseded, "
        f"{len(orphans)} unaccounted-for PDF(s), {len(waiting)} waiting."
    )


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: python build_index.py <vendor-folder>")
    vendor_dir = Path(sys.argv[1]).resolve()
    docs_dir = vendor_dir / "docs"
    if not docs_dir.exists():
        sys.exit(f"No docs/ folder under {vendor_dir}")

    manuals = load_manuals(docs_dir)
    superseded = load_superseded(vendor_dir)
    problems = mark_current(vendor_dir, manuals)

    write_index_json(docs_dir, manuals, superseded)
    write_readme(vendor_dir, docs_dir, manuals, superseded)
    report_orphans(vendor_dir, manuals, superseded)
    if problems:
        print("\nEditions that could not be ordered -- build_search_db.py stops on these:")
        for p in problems:
            print(f"  !! {p}")
        sys.exit(1)


if __name__ == "__main__":
    main()
