#!/usr/bin/env python3
"""
Turn what the server was really asked into test questions.

mcp_server.py appends one line per tool call to the file $PDF_TO_RAG_LOG names
(it logs nothing unless that is set). This reads that file, shows each search
with the results it returned, and asks you whether it found the answer. Every
search you mark goes into the question file as kind `real`, which
eval_search.py then scores like any other and reports on its own row: the one
kind not written by someone who had just read the answer.

  python scripts/triage_log.py --log calls.jsonl --questions eval/questions.jsonl --db mcp-index.sqlite3
  python scripts/triage_log.py --log calls.jsonl --summary    # no questions asked: what the log holds
  python scripts/triage_log.py --log calls.jsonl --list       # the searches still to triage

For each search:

  h  hit    the answer is among the results shown; you say which (1, or 1,3)
  m  miss   it is not; you name the right section: a section id from the index
            given with --db, `slug sections/NNN-name.md`, or words to search
            for in that index and pick from
  s  skip   ask again next time
  i  ignore not a question about the documentation; never shown again
  q  quit

A hit or a miss then asks for two optional things, for eval_answers.py: your own
wording of the question (what the agent typed into search_docs is usually
keywords, not what you asked) and a short gold answer.

The question file is only ever appended to. A search asked several times is
shown once. What is done is kept beside the log, in <log>.triage.json, so a
later run shows only what is new. The log names each result by collection, slug
and file, so triaging after the index was rebuilt still finds the sections.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcp_server  # noqa: E402
from _common import utf8_console  # noqa: E402

TOP = 5
FILE_ANSWER_RE = re.compile(r"^(\S+?)(?::|\s+)\s*((?:\S*/)?sections/\S+\.md)$")


def read_calls(path: Path) -> tuple[list[dict], int]:
    """(the calls in a log, and how many lines were not one). A line cut short
    by a server that was stopped mid-write is not a call."""
    calls, bad = [], 0
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            call = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(call, dict) and isinstance(call.get("tool"), str):
            calls.append(call)
        else:
            bad += 1
    return calls, bad


def search_key(call: dict) -> str:
    """What makes two searches the same: the words, and where they looked."""
    a = call.get("args") or {}
    query = " ".join(str(a.get("query", "")).split()).casefold()
    return "|".join([query, str(a.get("collection") or ""), str(a.get("document") or ""), str(a.get("version") or "")])


def group_searches(calls: list[dict]) -> dict[str, dict]:
    """Each distinct search_docs call that ran, by key, in the order the
    searches were first asked: the latest call (its results are the ones to
    judge), and how often it was asked."""
    out: dict[str, dict] = {}
    for call in calls:
        if call["tool"] != "search_docs" or call.get("error"):
            continue
        if not str((call.get("args") or {}).get("query", "")).strip():
            continue
        key = search_key(call)
        seen = out.get(key)
        out[key] = {"call": call, "count": (seen["count"] if seen else 0) + 1}
    return out


# ------------------------------------------------------------------ state

def state_path(log: Path) -> Path:
    return log.with_name(log.name + ".triage.json")


def load_state(log: Path) -> dict:
    try:
        state = json.loads(state_path(log).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"done": {}}
    if not isinstance(state, dict) or not isinstance(state.get("done"), dict):
        sys.exit(f"{state_path(log)} is not a triage record; move it aside to start again")
    return state


def save_state(log: Path, state: dict) -> None:
    path = state_path(log)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------ question file

def question_ids(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    ids = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            q = json.loads(line)
        except json.JSONDecodeError:
            sys.exit(f"{path} has a line that is not JSON; fix it before adding to it:\n  {line[:100]}")
        if isinstance(q, dict) and q.get("id") is not None:
            ids.add(str(q["id"]))
    return ids


def next_id(ids: set[str]) -> str:
    taken = [int(m.group(1)) for i in ids if (m := re.fullmatch(r"r(\d+)", i))]
    return f"r{max(taken, default=0) + 1:02d}"


def append_question(path: Path, question: dict) -> None:
    """Add one line. Whatever is in the file already is left exactly as it is."""
    needs_break = path.is_file() and path.stat().st_size and not path.read_bytes().endswith(b"\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        if needs_break:
            f.write("\n")
        f.write(json.dumps(question, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- the index

def corpus_or_none():
    """The index opened for this run, if --db was given: the server module's
    CORPUS is only an annotation until something assigns it."""
    return getattr(mcp_server, "CORPUS", None)


def describe(ref: list) -> str:
    """A result as one line: where it is, and, with an index, what it is."""
    collection, slug, file = ref
    label = f"{collection or '?'}/{slug} · {file.rsplit('/', 1)[-1]}"
    corpus = corpus_or_none()
    if corpus is None:
        return label
    row = corpus.db.execute(
        "SELECT heading, page_start, page_end FROM chunks WHERE collection = ? AND slug = ? "
        "AND file LIKE ? ESCAPE '\\'", (collection, slug, "%/" + mcp_server.like_escape(file))).fetchone()
    if not row:
        return label + "  (not in this index)"
    pages = ""
    if row["page_start"] is not None:
        pages = f"  p. {row['page_start']}" if row["page_end"] in (None, row["page_start"]) \
            else f"  pp. {row['page_start']}-{row['page_end']}"
    return f"{label}  {row['heading'] or '(untitled)'}{pages}"


def answer_of(ref: list) -> dict:
    """An answer as eval_search.py reads it. Without a collection (a section
    named by hand with no index to say) the key is left out, not left empty."""
    collection, slug, file = ref
    return {"slug": slug, "file": file, **({"collection": collection} if collection else {})}


def same_section(ref: list, answer: list) -> bool:
    return ref[1] == answer[1] and ref[2] == answer[2] and answer[0] in ("", ref[0])


def ref_from_row(row) -> list:
    return [row["collection"], row["slug"], "sections/" + row["file"].rsplit("/", 1)[-1]]


def resolve_named(text: str, ask) -> list | None:
    """The section the user named: an id, `slug file`, or words to search for.
    None if it names nothing the index (or the text alone) can settle."""
    text = text.strip()
    corpus = corpus_or_none()
    if text.isdigit():
        if corpus is None:
            print("  a section id needs --db")
            return None
        row = corpus.db.execute("SELECT collection, slug, file FROM chunks WHERE rowid = ?", (int(text),)).fetchone()
        if not row:
            print(f"  no section {text} in this index")
            return None
        ref = ref_from_row(row)
        print(f"  = {describe(ref)}")
        return ref
    m = FILE_ANSWER_RE.match(text)
    if m:
        slug, file = m.group(1), "sections/" + m.group(2).rsplit("sections/", 1)[-1]
        if corpus is None:
            print("  taken as written; give --db to check it exists")
            return ["", slug, file]
        rows = corpus.db.execute(
            "SELECT collection, slug, file FROM chunks WHERE slug = ? AND file LIKE ? ESCAPE '\\'",
            (slug, "%/" + mcp_server.like_escape(file))).fetchall()
        if len(rows) != 1:
            print(f"  {slug} {file} is {'in more than one collection' if rows else 'not in this index'}")
            return None
        ref = ref_from_row(rows[0])
        print(f"  = {describe(ref)}")
        return ref
    if corpus is None:
        print("  words to search for need --db; or give `slug sections/NNN-name.md`")
        return None
    try:
        rows = mcp_server.search(text, limit=8)
    except ValueError as exc:
        print(f"  {exc}")
        return None
    if not rows:
        print("  nothing matches")
        return None
    refs = [ref_from_row(r) for r in rows]
    for i, ref in enumerate(refs, 1):
        print(f"   {i}. {describe(ref)}")
    pick = ask("  which one? (number, Enter for none): ")
    if pick and pick.strip().isdigit() and 1 <= int(pick) <= len(refs):
        return refs[int(pick) - 1]
    return None


# --------------------------------------------------------------- the dialog

def show(n: int, total: int, group: dict, top: int) -> list[list]:
    call = group["call"]
    args = call.get("args") or {}
    refs = call.get("refs") or []
    again = f" · asked {group['count']} times" if group["count"] > 1 else ""
    client = f" · {call['client']}" if call.get("client") else ""
    print(f"\n[{n}/{total}] {call.get('t', '?')}{client}{again}")
    print(f"  query: {args.get('query')}")
    scope = {k: args[k] for k in ("collection", "document", "version") if args.get(k)}
    if scope:
        print("  scope: " + ", ".join(f"{k}={v}" for k, v in scope.items()))
    if not refs:
        print("  results: none")
    for i, ref in enumerate(refs[:top], 1):
        print(f"  {i}. {describe(ref)}")
    if len(refs) > top:
        print(f"  …and {len(refs) - top} more")
    return refs


def triage(log: Path, questions: Path, top: int, ask) -> int:
    calls, bad = read_calls(log)
    if bad:
        print(f"({bad} line(s) of the log are not calls and were skipped)")
    state = load_state(log)
    groups = {k: g for k, g in group_searches(calls).items() if k not in state["done"]}
    if not groups:
        print("Nothing new to triage.")
        return 0
    ids = question_ids(questions)
    added = 0
    print(f"{len(groups)} search(es) to triage. h = hit, m = miss, s = skip, i = ignore, q = quit.")
    for n, (key, group) in enumerate(groups.items(), 1):
        refs = show(n, len(groups), group, top)
        call = group["call"]
        while True:
            typed = ask("  [h]it  [m]iss  [s]kip  [i]gnore  [q]uit > ")
            choice = "q" if typed is None else typed.strip().lower()[:1]      # end of input quits
            if choice and choice in "hmsiq":
                break
        if choice == "q":
            break
        if choice == "s":
            continue
        if choice == "i":
            state["done"][key] = "ignored"
            save_state(log, state)
            continue

        answers: list[list] = []
        if choice == "h":
            if not refs:
                print("  nothing was returned, so this was a miss")
                choice = "m"
            else:
                while not answers:
                    pick = ask(f"  which result answers it? (1-{min(top, len(refs))}, or 1,3; Enter to go back) > ")
                    if not pick or not pick.strip():
                        break
                    nums = [p.strip() for p in pick.split(",")]
                    if all(p.isdigit() and 1 <= int(p) <= min(top, len(refs)) for p in nums):
                        answers = [refs[int(p) - 1] for p in nums]
        if choice == "m":
            while True:
                named = ask("  the right section (id, `slug sections/NNN-name.md`, or words; Enter when done) > ")
                if not named or not named.strip():
                    break
                ref = resolve_named(named, ask)
                if ref and not any(same_section(ref, a) for a in answers):
                    answers.append(ref)
        if not answers:
            print("  no answer named: left for next time")
            continue

        ask_wording = (ask("  your own wording of the question, for eval_answers.py (Enter to skip) > ") or "").strip()
        gold = (ask("  a short gold answer, for eval_answers.py (Enter to skip) > ") or "").strip()
        rank = next((i for i, r in enumerate(refs, 1) if any(same_section(r, a) for a in answers)), None)
        marked = "hit" if choice == "h" else "miss"
        question = {
            "id": next_id(ids),
            "kind": "real",
            "question": str((call.get("args") or {}).get("query")).strip(),
            "answers": [answer_of(a) for a in answers],
        }
        if ask_wording:
            question["ask"] = ask_wording
        if gold:
            question["gold"] = gold
        question["note"] = (f"from the call log, {call.get('t', '?')}: marked {marked}; the search returned the answer "
                            + (f"at rank {rank}" if rank else "nowhere in its results"))
        append_question(questions, question)
        ids.add(question["id"])
        state["done"][key] = f"{marked}: {question['id']}"
        save_state(log, state)
        added += 1
        print(f"  added {question['id']} to {questions.name}")
    print(f"\n{added} question(s) added to {questions}")
    return 0


# ---------------------------------------------------------------- summaries

def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[int(p * (len(ordered) - 1))]


def summarize(calls: list[dict], bad: int) -> None:
    if not calls:
        print("The log holds no calls.")
        return
    times = sorted(c["t"] for c in calls if isinstance(c.get("t"), str))
    print(f"{len(calls)} call(s)" + (f", {times[0]} to {times[-1]}" if times else "")
          + (f"; {bad} unreadable line(s)" if bad else ""))
    clients = Counter(c.get("client") or "(not said)" for c in calls)
    print("clients: " + ", ".join(f"{k} {v}" for k, v in clients.most_common()))
    print(f"\n{'tool':18} {'calls':>6} {'errors':>6} {'median ms':>10} {'p95 ms':>8} {'chars/call':>11}")
    for tool, n in Counter(c["tool"] for c in calls).most_common():
        mine = [c for c in calls if c["tool"] == tool]
        ms = [c["ms"] for c in mine if isinstance(c.get("ms"), (int, float))]
        chars = [c["chars"] for c in mine if isinstance(c.get("chars"), (int, float))]
        print(f"{tool:18} {n:>6} {sum(1 for c in mine if c.get('error')):>6} "
              f"{statistics.median(ms) if ms else 0:>10.0f} {percentile(ms, 0.95) if ms else 0:>8.0f} "
              f"{statistics.mean(chars) if chars else 0:>11.0f}")
    searches = [c for c in calls if c["tool"] == "search_docs" and not c.get("error")]
    empty = [c for c in searches if not c.get("sections")]
    print(f"\nsearches: {len(searches)} ({len(group_searches(calls))} distinct); "
          f"{len(empty)} returned nothing")
    seen = set()
    for c in empty:
        q = str((c.get("args") or {}).get("query"))
        if q not in seen and len(seen) < 20:
            seen.add(q)
            print(f"  no results: {q}")


def main() -> int:
    utf8_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", type=Path, required=True, help="the call log mcp_server.py wrote ($PDF_TO_RAG_LOG)")
    ap.add_argument("--questions", type=Path, help="the question file to add to (needed to triage)")
    ap.add_argument("--db", type=Path, help="the index, to show headings and pages, and to name a section by id or words")
    ap.add_argument("--top", type=int, default=TOP, help=f"results to show for each search (default {TOP})")
    ap.add_argument("--list", action="store_true", help="show the searches still to triage and stop")
    ap.add_argument("--summary", action="store_true", help="count the calls in the log and stop")
    args = ap.parse_args()

    if not args.log.is_file():
        print(f"No call log at {args.log}. The server writes one when PDF_TO_RAG_LOG names a file.", file=sys.stderr)
        return 1
    if args.summary:
        calls, bad = read_calls(args.log)
        summarize(calls, bad)
        return 0
    if not (args.list or args.questions):
        ap.error("--questions is required unless --list or --summary is given")
    if args.db:
        mcp_server.CORPUS = mcp_server.Corpus(args.db.resolve())
        if not mcp_server.CORPUS.available:
            print(f"No index at {args.db}", file=sys.stderr)
            return 1
    if args.list:
        calls, _ = read_calls(args.log)
        done = load_state(args.log)["done"]
        todo = {k: g for k, g in group_searches(calls).items() if k not in done}
        for n, group in enumerate(todo.values(), 1):
            show(n, len(todo), group, args.top)
        print(f"\n{len(todo)} search(es) to triage")
        return 0

    def ask(prompt: str) -> str | None:
        try:
            return input(prompt)
        except EOFError:
            return None
    return triage(args.log, args.questions, args.top, ask)


if __name__ == "__main__":
    sys.exit(main())
