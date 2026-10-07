#!/usr/bin/env python3
"""
Carry a question file's answers across a reconversion.

Section files are named by position, so converting a document again renames
files and moves text between them; an eval's `answers` then point at files that
are gone, and every question reads as a miss. This finds where each answer's
text went, by comparing the old section (kept in `.rebuild-backup/<slug>/` when
a conversion replaces one) with the sections now in `docs/<slug>/`:

  python scripts/remap_answers.py --root my-corpus --questions eval/questions.jsonl

For every answer whose file is gone from `docs/<slug>/` but kept in the backup,
the old text is cut into word 5-grams and each current section is scored by the
share of them it holds. A section holding at least 0.6 of the old one, and 0.2
more than any other, is the answer (1:1). Failing that, the sections holding
0.3 or more that together hold 0.8 of it are (1:n: the text was split), and the
question's note says so. Anything else is reported with its three nearest
sections and left alone: a wrong answer is worse than a missing one, because
it makes a miss look like a hit.

The result goes to `--out` (default `<questions>.remapped.jsonl`), never over
the input. Every other field and line is kept; lines that need no change keep
their text, written out as UTF-8 with LF line endings and no BOM. Exit status 1 if any answer was unresolved.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import editions  # noqa: E402
from _common import utf8_console  # noqa: E402
from build_search_db import find_collections  # noqa: E402

SHINGLE = 5
SAME = 0.6          # share of the old text a section must hold to be the answer
MARGIN = 0.2        # ...and by how much it must beat the next
PART = 0.3          # share a section must hold to be one part of a split
WHOLE = 0.8         # share the parts of a split must hold together
TOKEN_RE = re.compile(r"[0-9a-z]+")
CRUMB_RE = re.compile(r"^\*[^\n]*\*[ \t]*$")


def shingles(text: str) -> set[tuple[str, ...]]:
    """Word 5-grams of a section, without its breadcrumb line: reconverting
    can rewrite that, and it is not what the answer says."""
    lines = text.lstrip().split("\n", 1)
    if CRUMB_RE.match(lines[0]):
        text = lines[1] if len(lines) > 1 else ""
    tokens = TOKEN_RE.findall(text.lower())
    if not tokens:
        return set()
    if len(tokens) < SHINGLE:
        return {tuple(tokens)}
    return {tuple(tokens[i:i + SHINGLE]) for i in range(len(tokens) - SHINGLE + 1)}


class Documents:
    """Where each (collection, slug) lives, and its current sections' shingles."""

    def __init__(self, root: Path):
        self.docs = {}
        for key, _display, docs in find_collections(root):
            for d in editions.document_dirs(docs):
                self.docs[(key, d.name)] = d
        self._sections: dict[tuple, dict[str, set]] = {}

    def candidates(self, slug: str, collection: str | None) -> list[tuple[str, str]]:
        return [k for k in self.docs if k[1] == slug and collection in (None, k[0])]

    def sections(self, key: tuple[str, str]) -> dict[str, set]:
        if key not in self._sections:
            folder = self.docs[key]
            out = {}
            for f in sorted((folder / "sections").glob("*.md")):
                out[f"sections/{f.name}"] = shingles(f.read_text(encoding="utf-8", errors="replace"))
            self._sections[key] = out
        return self._sections[key]

    def old_text(self, key: tuple[str, str], file: str) -> str | None:
        path = self.docs[key].parent.parent / editions.BACKUP_DIR / key[1] / file
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None


def remap(old: set, sections: dict[str, set]) -> tuple[str, list[str], list[tuple[str, float]]]:
    """("1:1" | "1:n" | "none", the new files, every section's score best first)."""
    scores = sorted(((name, len(old & sh) / len(old)) for name, sh in sections.items()), key=lambda kv: (-kv[1], kv[0]))
    if scores and scores[0][1] >= SAME and (len(scores) == 1 or scores[0][1] - scores[1][1] >= MARGIN):
        return "1:1", [scores[0][0]], scores
    parts = [name for name, score in scores if score >= PART]
    covered = set().union(*(old & sections[name] for name in parts)) if parts else set()
    if parts and len(covered) / len(old) >= WHOLE:
        return "1:n", sorted(parts), scores
    return "none", [], scores


def main() -> int:
    utf8_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, type=Path, help="the corpus folder")
    ap.add_argument("--questions", required=True, type=Path, help="questions.jsonl, as eval_search.py reads it")
    ap.add_argument("--out", type=Path, help="where to write the remapped file (default: beside the input)")
    args = ap.parse_args()

    out_path = args.out or args.questions.with_name(args.questions.stem + ".remapped.jsonl")
    if out_path.resolve() == args.questions.resolve():
        sys.exit("--out is the questions file; this never overwrites its input")
    corpus = Documents(args.root.resolve())
    counts = {"unchanged": 0, "1:1": 0, "1:n": 0, "unresolved": 0}
    lines_out = []
    for n, line in enumerate(args.questions.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip() or line.strip().startswith("//"):
            lines_out.append(line)
            continue
        q = json.loads(line)
        answers, changed, notes = [], False, []
        for a in q.get("answers", []):
            file = a.get("file")
            keys = corpus.candidates(a.get("slug"), a.get("collection")) if file else []
            if not keys or any((corpus.docs[k] / file).is_file() for k in keys):
                counts["unchanged"] += 1
                answers.append(a)
                continue
            old = next((t for t in (corpus.old_text(k, file) for k in keys) if t is not None), None)
            key = next((k for k in keys if corpus.old_text(k, file) is not None), None)
            old_set = shingles(old) if old is not None else set()
            if not old_set:
                print(f"{q.get('id') or n}: {a['slug']} {file}: gone, and no backup of it to compare with")
                counts["unresolved"] += 1
                answers.append(a)
                continue
            how, files, scores = remap(old_set, corpus.sections(key))
            if how == "none":
                print(f"{q.get('id') or n}: {a['slug']} {file}: no section holds it; nearest:")
                for name, score in scores[:3]:
                    print(f"    {score:.2f}  {name}")
                counts["unresolved"] += 1
                answers.append(a)
                continue
            counts[how] += 1
            changed = True
            answers.extend(x for x in ({**a, "file": f} for f in files) if x not in answers)
            if how == "1:n":
                notes.append(f"remapped from {file} to {len(files)} sections")
        if changed:
            q["answers"] = answers
            if notes:
                q["note"] = "; ".join(filter(None, [q.get("note"), *notes]))
            line = json.dumps(q, ensure_ascii=False)
        lines_out.append(line)
    out_path.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
    print(f"{counts['unchanged']} unchanged, {counts['1:1']} remapped 1:1, {counts['1:n']} split 1:n, "
          f"{counts['unresolved']} unresolved -> {out_path}")
    return 1 if counts["unresolved"] else 0


if __name__ == "__main__":
    sys.exit(main())
