#!/usr/bin/env python3
"""
Score search against a test set: for each question, where does the section
that answers it rank?

  python scripts/eval_search.py --db <corpus>/mcp-index.sqlite3 --questions eval/questions.jsonl
  python scripts/eval_search.py --db ... --questions ... --misses        # what came back instead
  python scripts/eval_search.py --db ... --questions ... --json run.json # keep a run to compare
  python scripts/eval_search.py --compare before.json after.json         # what moved between two runs

Runs the server's own search() in-process with its defaults -- the ranking an
agent's first search_docs call gets, stopwords, fallback, front-matter demotion
and per-document cap included -- so a ranking change is measured as it ships.

questions.jsonl holds one object per line:

  {"id": "q001", "kind": "concept",
   "question": "How do I keep scan chains from crossing clock domains?",
   "answers": [{"slug": "dftug-v-2023-12-sp1", "file": "sections/092-....md"}],
   "note": "optional: why that section answers it"}

`answers` lists every section that answers the question, and a hit on any of
them counts. An answer may instead name a phrase, which survives a
reconversion that renames the files:
  {"doc_id": "dftug", "version": "2023.12", "quote": "a short exact phrase"}
It stands for the sections of that edition whose text contains the phrase,
whitespace aside (the current edition when `version` is left out). A phrase in
none, or in more than three, is reported like a stale answer. After a
reconversion that left answers by file behind, remap_answers.py finds them. Where two collections each have a document of that slug, add
`"collection"` (the collection's key, as list_documents shows it) to say which. When a miss turns out to be another section that answers just as
well, add it there: a test that marks right answers wrong under-reports, and
the misses are where to look.

`kind` is free-form and results are broken down by it. The ones this skill
uses, matching SKILL.md's "Measuring retrieval": identifier (names a command,
option, rule or message), concept (natural wording, sharing some of the
answer's terms), paraphrase (deliberately avoids the answer's own wording --
the gap an embedding index would close), figure (answered by what a figure
shows), and real (asked by an actual user -- the most valuable, and the only
kind not written by someone who had just read the answer).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcp_server  # noqa: E402
from _common import utf8_console  # noqa: E402

KS = (1, 3, 5, 10)
LIMIT = 10  # search_docs' default page of results
MAX_QUOTE_SECTIONS = 3


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
               and a.get("collection") in (None, row["collection"])
               for a in answers)


def resolve_quotes(questions: list[dict]) -> list[str]:
    """Replace each answer given as a quote with the sections that hold it, and
    return those that hold it nowhere, or in too many sections to be one answer."""
    stale = []
    for q in questions:
        resolved = []
        for a in q["answers"]:
            if "quote" not in a:
                resolved.append(a)
                continue
            quote = " ".join(str(a["quote"]).split())
            try:
                doc = mcp_server.resolve_document(a.get("doc_id"), a.get("version"), a.get("collection"))
            except ValueError as exc:
                stale.append(f"{q.get('id', '(no id)')}: {exc}")
                continue
            rows = mcp_server.CORPUS.db.execute(
                "SELECT file, body FROM chunks WHERE slug = ? AND collection = ? ORDER BY ord",
                (doc["slug"], doc["collection"])).fetchall()
            hits = [r["file"] for r in rows if quote in " ".join(r["body"].split())]
            if not 1 <= len(hits) <= MAX_QUOTE_SECTIONS:
                stale.append(f"{q.get('id', '(no id)')}: {quote[:50]!r} is in {len(hits)} sections of {doc['slug']}")
                continue
            resolved.extend({"slug": doc["slug"], "collection": doc["collection"],
                             "file": "sections/" + h.rsplit("/", 1)[-1]} for h in hits)
        q["answers"] = resolved
    return stale


def missing_answers(questions: list[dict]) -> list[str]:
    """Answers the index does not contain: a question gone stale after a
    reconversion renamed its section would otherwise just look like a miss."""
    out = []
    for q in questions:
        for a in q["answers"]:
            sql = "SELECT 1 FROM chunks WHERE slug = ? AND file LIKE ? ESCAPE '\\'"
            params = [a["slug"], "%/" + mcp_server.like_escape(a["file"].lstrip("/"))]
            if a.get("collection"):
                sql += " AND collection = ?"
                params.append(a["collection"])
            hit = mcp_server.CORPUS.db.execute(sql, params).fetchone()
            if not hit:
                out.append(f"{q.get('id', '(no id)')}: {a['slug']} {a['file']}")
    return out


def score(items: list[dict]) -> dict:
    n = len(items)
    ranks = [x["rank"] for x in items]
    return {
        "n": n,
        **{f"hit@{k}": sum(1 for r in ranks if r and r <= k) / n for k in KS},
        "mrr": sum(1 / r for r in ranks if r) / n,
    }


def git_commit() -> str | None:
    """The short hash of the code this run used, so a saved run can be traced to it."""
    try:
        out = subprocess.run(["git", "-C", str(Path(__file__).resolve().parent.parent), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def rank_text(rank) -> str:
    return "-" if rank is None else str(rank)


def compare(path_a: Path, path_b: Path) -> None:
    """Print what changed between two saved runs: their totals by kind, and
    every question whose rank differs. The totals say whether a change helped;
    the questions say why, and one question is a point or two."""
    runs = [json.loads(p.read_text(encoding="utf-8-sig")) for p in (path_a, path_b)]
    for label, path, run in zip("AB", (path_a, path_b), runs):
        meta = run.get("meta") or {}
        figures = "on" if meta.get("figure_text") == "1" else "off" if "figure_text" in meta else "?"
        print(f"{label}: {path.name}  built {meta.get('built_at', '?')}  "
              f"code {run.get('git_commit') or '?'}  figure text {figures}")
    a, b = runs
    print(f"\n{'kind':12} {'n':>4} {'hit@1 A':>8} {'hit@1 B':>8} {'hit@5 A':>8} {'hit@5 B':>8} {'MRR A':>7} {'MRR B':>7}")
    for kind in sorted(set(a["summary"]) | set(b["summary"]), key=lambda k: (k == "all", k)):
        sa, sb = a["summary"].get(kind), b["summary"].get(kind)
        n = f"{sa['n']}" if sa and sb and sa["n"] == sb["n"] else f"{sa['n'] if sa else 0}/{sb['n'] if sb else 0}"

        def cell(s, key, fmt):
            return format(s[key], fmt) if s else "-"
        print(f"{kind:12} {n:>4} {cell(sa, 'hit@1', '.0%'):>8} {cell(sb, 'hit@1', '.0%'):>8} "
              f"{cell(sa, 'hit@5', '.0%'):>8} {cell(sb, 'hit@5', '.0%'):>8} "
              f"{cell(sa, 'mrr', '.3f'):>7} {cell(sb, 'mrr', '.3f'):>7}")

    # A question without an id is known by its text, so two of them do not
    # collapse into one key.
    def qkey(r):
        return r["id"] if r.get("id") is not None else r.get("question")
    by_a = {qkey(r): r for r in a["results"]}
    by_b = {qkey(r): r for r in b["results"]}
    moved = sorted((r for i, r in by_b.items() if i in by_a and by_a[i]["rank"] != r["rank"]),
                   key=lambda r: (r["kind"], str(qkey(r))))
    print(f"\nRank changes (rank 11+ shown as -): {len(moved)}")
    for r in moved:
        question = r.get("question") or by_a[qkey(r)].get("question") or ""
        print(f"  [{r.get('id')}] {r['kind']:12} {rank_text(by_a[qkey(r)]['rank']):>2} -> "
              f"{rank_text(r['rank']):<2}  {question}")
    for label, only in (("A", sorted(set(by_a) - set(by_b), key=str)), ("B", sorted(set(by_b) - set(by_a), key=str))):
        if only:
            print(f"Only in {label}: {', '.join(map(str, only))}")


def main() -> int:
    utf8_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="index built by build_search_db.py")
    ap.add_argument("--questions", type=Path, help="questions.jsonl")
    ap.add_argument("--misses", action="store_true", help="print each question missing the top 5")
    ap.add_argument("--json", type=Path, help="write per-question results here")
    ap.add_argument("--compare", nargs=2, type=Path, metavar=("A.json", "B.json"),
                    help="compare two runs saved with --json; needs no index")
    args = ap.parse_args()

    if args.compare:
        compare(*args.compare)
        return 0
    if not args.db or not args.questions:
        ap.error("--db and --questions are required unless --compare is given")

    mcp_server.CORPUS = mcp_server.Corpus(Path(args.db).resolve())
    if not mcp_server.CORPUS.available:
        print(f"No index at {mcp_server.CORPUS.db_path}", file=sys.stderr)
        return 1
    questions = load_questions(args.questions)
    stale = resolve_quotes(questions) + missing_answers(questions)
    if stale:
        print(f"!! {len(stale)} answer section(s) are not in this index -- fix these first:")
        for s in stale[:20]:
            print(f"   {s}")
        return 2

    results, errors = [], []
    for q in questions:
        try:
            rows = mcp_server.search(q["question"], limit=LIMIT)
        except ValueError as exc:
            # A query the index cannot run is not a miss. Scored as one, a
            # search that is broken for every question reads as a search that
            # ranks badly, and this tool exists to tell those apart.
            errors.append(f"{q.get('id')}: {exc}")
            rows = []
        rank = next((i for i, row in enumerate(rows, 1) if is_answer(row, q["answers"])), None)
        results.append({
            "id": q.get("id"),
            "kind": q.get("kind", "-"),
            "question": q["question"],
            "rank": rank,
            "top": [f"{row['slug']} · {row['file'].rsplit('/', 1)[-1]}" for row in rows[:3]],
        })

    groups: dict[str, list] = {}
    for r in results:
        groups.setdefault(r["kind"], []).append(r)
    summary = {kind: score(items) for kind, items in sorted(groups.items())}
    summary["all"] = score(results)

    if errors:
        print(f"!! {len(errors)} question(s) could not be searched at all. Until these run, the "
              "scores below measure nothing about ranking:")
        for e in errors[:10]:
            print(f"   {e}")
        print()

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
        args.json.write_text(json.dumps({"db": str(args.db), "git_commit": git_commit(),
                                         "meta": meta, "summary": summary,
                                         "results": results, "errors": errors},
                                        indent=2) + "\n", encoding="utf-8")
    return 3 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
