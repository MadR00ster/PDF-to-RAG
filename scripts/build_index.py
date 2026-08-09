#!/usr/bin/env python3
"""
Regenerate a vendor folder's docs/index.json and docs/README.md from the
docs/<slug>/manifest.json files actually on disk, plus that folder's
superseded.json (hand-maintained list of {"file": <old pdf>, "superseded_by":
<slug>}).

Run this after convert_manual.py adds or replaces a manual, so the index
never drifts out of sync with what's actually converted.

Usage:
  python scripts/build_index.py "Synopsys Manual"
  python scripts/build_index.py "Tessent Manual"
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


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
        f"Converted from the {vendor_label} EDA tool PDFs in the parent folder. Each manual "
        "has its own folder with a full markdown dump plus per-section files for "
        "finer-grained retrieval.",
        "",
        "## Manuals",
        "",
        "| Manual | Slug | Pages | Sections | Full doc |",
        "|---|---|---|---|---|",
    ]
    for m in manuals:
        lines.append(
            f"| {m['title']} | `{m['slug']}` | {m['page_count']} | {m['section_count']} "
            f"| [{m['full_md']}]({m['full_md']}) |"
        )
    lines += [
        "",
        "## Folder layout",
        "",
        "```",
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
            "The original PDFs remain in the parent folder if you need to look up "
            "version-specific behavior later.",
        ]
    (docs_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def report_orphans(vendor_dir: Path, manuals: list[dict], superseded: list[dict]) -> None:
    accounted_for = {m["source_pdf"] for m in manuals} | {s["file"] for s in superseded}
    orphans = [p.name for p in sorted(vendor_dir.glob("*.pdf")) if p.name not in accounted_for]
    if orphans:
        print("PDFs at the root with no docs/ folder and no superseded.json entry:")
        for o in orphans:
            print(f"  - {o}")
        print("Convert them, add a superseded.json entry, or ignore if intentional.")
    print(
        f"{len(manuals)} converted manuals, {len(superseded)} superseded, "
        f"{len(orphans)} unaccounted-for PDF(s) at root."
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

    write_index_json(docs_dir, manuals, superseded)
    write_readme(vendor_dir, docs_dir, manuals, superseded)
    report_orphans(vendor_dir, manuals, superseded)


if __name__ == "__main__":
    main()
