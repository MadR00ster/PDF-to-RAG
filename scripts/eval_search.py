#!/usr/bin/env python3
"""
Score search against a test set: for each question, where does the section
that answers it rank?

  python scripts/eval_search.py --db <corpus>/mcp-index.sqlite3 --questions eval/questions.jsonl
  python scripts/eval_search.py --db ... --questions ... --misses        # what came back instead
  python scripts/eval_search.py --db ... --questions ... --json run.json # keep a run to compare

Runs the server's own search() in-process with its defaults -- the ranking an
agent's first search_docs call gets, stopwords, fallback, front-matter demotion
and per-document cap included -- so a ranking change is measured as it ships.

questions.jsonl holds one object per line:

  {"id": "q001", "kind": "concept",
   "question": "How do I keep scan chains from crossing clock domains?",
   "answers": [{"slug": "dftug-v-2023-12-sp1", "file": "sections/092-....md"}],
   "note": "optional: why that section answers it"}

`answers` lists every section that answers the question, and a hit on any of
them counts. When a miss turns out to be another section that answers just as
well, add it there: a test that marks right answers wrong under-reports, and
the misses are where to look.

`kind` is free-form and results are broken down by it. The ones this skill
uses: identifier (names a command, option or message), concept (paraphrased,
avoiding the answer's own wording), figure (answered by what a figure shows),
and real (asked by an actual user -- the most valuable, and the only kind not
written by someone who had just read the answer).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcp_server  # noqa: E402

KS = (1, 3, 5, 10)
LIMIT = 10  # search_docs' default page of results


def load_questions(path: Path) -> list[dict]:
    questions = []
    for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        q = json.loads(line)
        if not q.get("question") or not q.get("answers"):
            sys.exit(f"{path}:{n}: every question needs `question` and `answers`")
        questions.append(q)
    return questions


def is_answer(row, answers: list[dict]) -> bool:
    return any(row["slug"] == a["slug"] and row["file"].endswith("/" + a["file"].lstrip("/"))
               for a in answers)


def missing_answers(questions: list[dict]) -> list[str]:
    """Answers the index does not contain: a question gone stale after a
    reconversion renamed its section would otherwise just look like a miss."""
    out = []
    for q in questions:
        for a in q["answers"]:
            hit = mcp_server.CORPUS.db.execute(
                "SELECT 1 FROM chunks WHERE slug = ? AND file LIKE ?",
                (a["slug"], "%/" + a["file"].lstrip("/")),
            ).fetchone()
            if not hit:
                out.append(f"{q['id']}: {a['slug']} {a['file']}")
    return out


def score(items: list[dict]) -> dict:
    n = len(items)
    ranks = [x["rank"] for x in items]
    return {
        "n": n,
        **{f"hit@{k}": sum(1 for r in ranks if r and r <= k) / n for k in KS},
        "mrr": sum(1 / r for r in ranks if r) / n,
    }


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="index built by build_search_db.py")
    ap.add_argument("--questions", required=True, type=Path, help="questions.jsonl")
    ap.add_argument("--misses", action="store_true", help="print each question missing the top 5")
    ap.add_argument("--json", type=Path, help="write per-question results here")
    args = ap.parse_args()

    mcp_server.CORPUS = mcp_server.Corpus(Path(args.db).resolve())
    if not mcp_server.CORPUS.available:
        print(f"No index at {mcp_server.CORPUS.db_path}", file=sys.stderr)
        return 1
    questions = load_questions(args.questions)
    stale = missing_answers(questions)
    if stale:
        print(f"!! {len(stale)} answer section(s) are not in this index -- fix these first:")
        for s in stale[:20]:
            print(f"   {s}")
        return 2

    results = []
    for q in questions:
        try:
            rows = mcp_server.search(q["question"], limit=LIMIT)
        except ValueError:
            rows = []
        rank = next((i for i, row in enumerate(rows, 1) if is_answer(row, q["answers"])), None)
        results.append({
            "id": q.get("id"),
            "kind": q.get("kind", "-"),
            "rank": rank,
            "top": [f"{row['slug']} · {row['file'].rsplit('/', 1)[-1]}" for row in rows[:3]],
        })

    groups: dict[str, list] = {}
    for r in results:
        groups.setdefault(r["kind"], []).append(r)
    summary = {kind: score(items) for kind, items in sorted(groups.items())}
    summary["all"] = score(results)

    meta = dict(mcp_server.CORPUS.db.execute("SELECT key, value FROM meta").fetchall())
    print(f"{args.questions.name} against {Path(args.db).name} "
          f"(figure text {'on' if meta.get('figure_text', '0') == '1' else 'off'})\n")
    print(f"{'kind':12} {'n':>4} " + " ".join(f"{'hit@' + str(k):>7}" for k in KS) + f" {'MRR':>6}")
    for kind, s in summary.items():
        print(f"{kind:12} {s['n']:>4} " + " ".join(f"{s[f'hit@{k}']:>7.0%}" for k in KS) + f" {s['mrr']:>6.3f}")

    if args.misses:
        print("\nMissed the top 5:")
        for q, r in zip(questions, results):
            if r["rank"] and r["rank"] <= 5:
                continue
            print(f"\n[{r['id']}] ({r['kind']}) {q['question']}")
            print(f"   wanted: " + "; ".join(f"{a['slug']} · {a['file']}" for a in q["answers"]))
            print(f"   rank:   {r['rank'] or '-'}")
            for t in r["top"]:
                print(f"   got:    {t}")

    if args.json:
        args.json.write_text(json.dumps({"db": str(args.db), "meta": meta, "summary": summary,
                                         "results": results}, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
