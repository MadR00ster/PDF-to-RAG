#!/usr/bin/env python3
"""
Sample sections of a converted corpus to write search-test questions from.

A retrieval test set is questions paired with the section that answers them.
Writing each question while reading its section keeps the answer honest; the
sample decides which sections, so the test covers the corpus rather than the
chapters someone happened to open. It is stratified -- every document gets a
share, larger ones more but only by the square root of their size, so one
6,000-section command reference cannot become the whole test -- and it leaves
out front matter, legal boilerplate and stubs too small to answer anything.

Where a manual is converted in several editions, only the one search answers
from is sampled. eval_search.py searches the way a user does, without naming
a version, so a question about a section of any other edition could only ever
score as a miss.

Prints each sampled section's slug, file, heading and opening text for whoever
writes the questions. With --figures it samples only sections that have
figures, and names each figure's image so it can be looked at. Sections
already answering a question in --exclude are not sampled again.

Usage:
  python scripts/sample_sections.py --root <corpus> --n 60 --seed 7
  python scripts/sample_sections.py --root <corpus> --n 30 --figures --exclude eval/questions.jsonl

See "Measuring retrieval" in SKILL.md for how to write the questions, and
eval_search.py for scoring them.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import editions  # noqa: E402
from _common import utf8_console  # noqa: E402
from build_search_db import clean_heading, find_collections, is_noise  # noqa: E402

utf8_console()

BOILERPLATE_RE = re.compile(
    r"copyright|trademark|legal|licen[cs]e|third.party|end.user|disclaimer|"
    r"revision history|contacting|customer support|about this", re.I)


def answered(path: Path | None) -> set[tuple[str | None, str, str]]:
    """(collection, slug, file) of every section some question already points
    at; the collection is None where the answer does not name one, and then
    stands for every collection. An answer given as a quote names no file and
    excludes nothing."""
    used = set()
    if path and path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("//"):
                used.update((a.get("collection"), a["slug"], a["file"])
                            for a in json.loads(line).get("answers", []) if "slug" in a and "file" in a)
    return used


def pools(root: Path, min_chars: int, used: set, figures_only: bool) -> dict:
    """(collection, slug) -> (doc_dir, [(section, its figures)]) of sections
    worth asking about. Keyed by both: two collections may each have a
    document of one slug."""
    out = {}
    for key, _display, docs in find_collections(root):
        # A document is its folder, as it is to the index build.
        manifests = {d / "manifest.json": json.loads((d / "manifest.json").read_text(encoding="utf-8-sig"))
                     for d in editions.document_dirs(docs)}
        for mp, m in manifests.items():
            m["slug"] = mp.parent.name
        entries = [{"slug": m["slug"], "doc_id": m.get("doc_id") or m["slug"], "version": m.get("version"),
                    "version_and_later": m.get("version_and_later") is True} for m in manifests.values()]
        try:
            problems = editions.resolve_editions(entries, editions.load_pins(docs.parent))
        except ValueError as exc:
            problems = [str(exc)]
        if problems:
            sys.exit("Which edition of each manual is current cannot be told, so there is nothing sound to "
                     "sample:\n  " + "\n  ".join(problems))
        current = {e["slug"] for e in entries if e["is_current"]}
        for mp, manifest in manifests.items():
            if manifest["slug"] not in current:
                continue
            figures: dict[str, list] = {}
            fig_path = mp.parent / "figures.json"
            if fig_path.is_file():
                for f in json.loads(fig_path.read_text(encoding="utf-8-sig")).get("figures", []):
                    if f.get("section"):
                        figures.setdefault(f["section"], []).append(f)
            pool = []
            for s in manifest.get("sections", []):
                if {(key, manifest["slug"], s["file"]), (None, manifest["slug"], s["file"])} & used:
                    continue
                if BOILERPLATE_RE.search(clean_heading(s.get("heading") or "")):
                    continue
                # Read only once the heading has let it through.
                try:
                    size = len((mp.parent / s["file"]).read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
                if size < min_chars:
                    continue
                if figures_only and s["file"] not in figures:
                    continue
                pool.append((s, figures.get(s["file"], [])))
            if pool:
                out[(key, manifest["slug"])] = (mp.parent, pool)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, help="corpus root, as given to build_search_db.py")
    ap.add_argument("--n", type=int, default=60, help="sections to sample (default 60)")
    ap.add_argument("--seed", type=int, default=7, help="random seed, so a sample can be reproduced")
    ap.add_argument("--min-chars", type=int, default=1200, help="skip sections shorter than this")
    ap.add_argument("--chars", type=int, default=2000, help="opening characters to print per section")
    ap.add_argument("--figures", action="store_true", help="only sections that have figures")
    ap.add_argument("--exclude", type=Path, help="questions.jsonl whose answer sections to skip")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    docs = pools(Path(args.root).resolve(), args.min_chars, answered(args.exclude), args.figures)
    if not docs:
        print("Nothing to sample -- check --root, and for --figures that extract_figures.py has run.",
              file=sys.stderr)
        return 1
    weight = {doc: math.sqrt(len(pool)) for doc, (_, pool) in docs.items()}
    total = sum(weight.values())
    # The collection is named only where the slug alone is ambiguous: in the
    # whole corpus, not only among the documents that had sections to sample.
    every = [d.name for _key, _display, coll in find_collections(Path(args.root).resolve())
             for d in editions.document_dirs(coll)]
    shared = {slug for slug in every if every.count(slug) > 1}

    n = 0
    for doc in sorted(docs):
        key, slug = doc
        doc_dir, pool = docs[doc]
        quota = max(1, round(args.n * weight[doc] / total))
        rng.shuffle(pool)
        taken = 0
        for section, figures in pool:
            if taken >= quota:
                break
            body = (doc_dir / section["file"]).read_text(encoding="utf-8", errors="replace")
            heading = clean_heading(section.get("heading") or "")
            if is_noise(heading, body):
                continue  # contents pages and figure lists answer nothing
            taken += 1
            n += 1
            print(f"=== [{n}] {slug} · {section['file']}" + (f" · collection {key}" if slug in shared else ""))
            print(f"heading: {heading}")
            for f in figures:
                print(f"figure: {f.get('caption') or '(no caption)'} -> {(doc_dir / f['file']).as_posix()}")
            print("---")
            print(body[:args.chars].rstrip() + ("\n[...]" if len(body) > args.chars else ""))
            print()
    print(f"{n} section(s) from {len(docs)} document(s), seed {args.seed}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
