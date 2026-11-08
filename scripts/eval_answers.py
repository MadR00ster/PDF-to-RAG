#!/usr/bin/env python3
"""
Answer a question set end to end, and grade the answers.

eval_search.py asks whether the right section ranks high. This asks what a user
cares about: given only the documentation tools, does an agent answer
correctly, and does it cite where the answer is? Each question goes through
Claude Code's non-interactive mode (`claude -p`) with the corpus's MCP server
and nothing else: no file tools, no other MCP servers, an empty working
folder. A second `claude -p` call, with no tools, grades the answer.

  python scripts/eval_answers.py --db <corpus>/mcp-index.sqlite3 --questions eval/questions.jsonl
  python scripts/eval_answers.py --db ... --questions ... --kind real --limit 10 --json run.json
  python scripts/eval_answers.py --db ... --questions ... --dry-run    # what would run; spends nothing

A question is run if it has a short gold answer, in a `gold` field:

  {"id": "r01", "kind": "real", "question": "torque the bolts evenly",
   "ask": "How tight should the mounting bolts be?",           (optional: what to ask)
   "gold": "Torque them evenly, so the bracket does not warp.",
   "answers": [{"slug": "widget-guide", "file": "sections/0003-mounting.md"}]}

`ask` is the question as a person would put it; without it `question` is used.
triage_log.py asks for both when it turns a logged search into a question.
`answers` are the sections that hold the answer, as eval_search.py reads them.

Each answer gets one verdict:

  correct      says what the gold answer says, contradicts nothing in it, and
               cites at least one of the sections in `answers` (names the
               document and the page or section)
  unsupported  gives an answer that misses or contradicts the gold answer, or
               cites none of those sections
  declined     gives no answer: says the documentation does not cover it, or
               asks a question back

The grader sees the question, the gold answer, the gold sections' documents,
versions, headings and pages, and the answer; it is told to use nothing else.
A call that failed or timed out is an `error`, left out of the rates. Beside
each verdict is whether a gold section was among those the tools returned, from
the server's own call log: it separates an answer that went wrong because the
documentation was not found from one that went wrong with it in hand.

The report ends with the tokens used, answering and grading separately. A
question costs one answering run (several tool calls, so several model turns)
and one grading call; --limit and --dry-run are there for that. Untested here
against a live `claude`; run one question first (--limit 1).

--bare passes through to `claude`; that mode reads only ANTHROPIC_API_KEY and
skips the login a desktop install uses.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import eval_search  # noqa: E402
import mcp_server  # noqa: E402
from _common import utf8_console  # noqa: E402
from triage_log import read_calls  # noqa: E402

SERVER_KEY = "docs"                  # what the MCP config calls the server: its tools are mcp__docs__*
VERDICTS = ("correct", "unsupported", "declined")
ANSWER_NOTE = ("Answer from the documentation your tools give you, and cite the document, its version and the "
               "page. If the documentation does not contain the answer, say so plainly instead of answering "
               "from memory.")
JUDGE_PROMPT = """You are grading an answer to a question about technical documentation. Judge only against the gold answer and the gold sources below. Use no outside knowledge.

Question:
{question}

Gold answer:
{gold}

Gold sources (where the documentation says it):
{sources}

Answer to grade:
{answer}

Give exactly one verdict:
- "declined": the answer gives no answer. It says the documentation does not cover the question, or asks a question back, or refuses.
- "correct": the answer says what the gold answer says (extra correct detail is fine, but nothing in it may contradict the gold answer) AND it cites at least one of the gold sources by naming the document and the page or section.
- "unsupported": it gives an answer, but the answer misses or contradicts the gold answer, or cites none of the gold sources.

Reply with one line of JSON and nothing else: {{"verdict": "correct" | "unsupported" | "declined", "reason": "<one short sentence>"}}
"""
VERDICT_RE = re.compile(r'"verdict"\s*:\s*"(\w+)"')


def claude_base(command: str, extra: list[str]) -> list[str]:
    """The command that runs Claude Code: `command` found on PATH, then any
    leading arguments (a script, when `command` is an interpreter)."""
    found = shutil.which(command)
    if not found:
        sys.exit(f"Cannot find `{command}`. Install Claude Code, or say where it is with --claude.")
    return [found, *extra]


def mcp_config(path: Path, servers: dict) -> Path:
    path.write_text(json.dumps({"mcpServers": servers}, indent=1), encoding="utf-8")
    return path


def docs_server(server: Path, db: Path, call_log: Path) -> dict:
    """The corpus's server as Claude Code starts it, writing its call log to
    a file of this question's own."""
    return {SERVER_KEY: {"command": sys.executable, "args": [str(server), "--db", str(db)],
                         "env": {mcp_server.CALL_LOG_ENV: str(call_log)}}}


def run_claude(base: list[str], flags: list[str], prompt: str, cwd: Path, timeout: float) -> tuple[dict | None, str]:
    """(the result object `claude -p --output-format json` printed, and what
    went wrong, if anything). The prompt goes in on stdin: it is the question
    verbatim, and a variadic option would otherwise take it as an argument."""
    try:
        proc = subprocess.run([*base, "-p", "--output-format", "json", *flags], input=prompt, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout:.0f} s"
    except OSError as exc:
        return None, f"could not run {base[0]}: {exc}"
    out = proc.stdout.strip()
    try:
        result = json.loads(out)
    except json.JSONDecodeError:
        return None, f"claude printed no JSON (exit {proc.returncode}): {(proc.stderr.strip() or out)[-300:]}"
    if isinstance(result, list):            # a stream of messages: the last result in it
        result = next((x for x in reversed(result) if isinstance(x, dict) and x.get("type") == "result"), None)
    if not isinstance(result, dict):
        return None, "claude printed JSON that is not a result"
    if result.get("is_error"):
        return result, f"claude reported an error: {str(result.get('result'))[:300]}"
    if proc.returncode != 0:
        return result, f"claude exited {proc.returncode}"
    return result, ""


def usage_of(result: dict | None) -> dict:
    u = (result or {}).get("usage") or {}
    return {"input": u.get("input_tokens") or 0, "output": u.get("output_tokens") or 0,
            "cache_read": u.get("cache_read_input_tokens") or 0, "cache_write": u.get("cache_creation_input_tokens") or 0,
            "cost_usd": (result or {}).get("total_cost_usd"), "turns": (result or {}).get("num_turns")}


def sources_of(answers: list[dict]) -> str:
    """The gold sections as a reader would cite them: document, version, heading, pages."""
    lines = []
    for a in answers:
        sql = ("SELECT c.heading, c.page_start, c.page_end, d.title, d.version FROM chunks c "
               "JOIN documents d ON d.slug = c.slug AND d.collection = c.collection "
               "WHERE c.slug = ? AND c.file LIKE ? ESCAPE '\\'")
        params = [a["slug"], "%/" + mcp_server.like_escape(a["file"].lstrip("/"))]
        if a.get("collection"):
            sql += " AND c.collection = ?"
            params.append(a["collection"])
        row = mcp_server.CORPUS.db.execute(sql, params).fetchone()
        if not row:
            lines.append(f"- {a['slug']}, {a['file']}")
            continue
        pages = ""
        if row["page_start"] is not None:
            pages = f", p. {row['page_start']}" if row["page_end"] in (None, row["page_start"]) \
                else f", pp. {row['page_start']}-{row['page_end']}"
        version = f", version {row['version']}" if row["version"] else ""
        lines.append(f"- {row['title']} ({a['slug']}){version}, section \"{row['heading'] or '(untitled)'}\"{pages}")
    return "\n".join(lines)


def parse_verdict(text: str) -> tuple[str | None, str]:
    """(verdict, reason) from the grader's reply: one JSON object, though a
    model may wrap it in a sentence or a code fence."""
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(text[start:end + 1])
            verdict = str(obj.get("verdict", "")).strip().lower()
            if verdict in VERDICTS:
                return verdict, str(obj.get("reason", "")).strip()
        except (json.JSONDecodeError, AttributeError):
            pass
    m = VERDICT_RE.search(text)
    if m and m.group(1).lower() in VERDICTS:
        return m.group(1).lower(), ""
    return None, f"the grader's reply was not a verdict: {text[:200]!r}"


def returned_gold(calls: list[dict], answers: list[dict]) -> bool:
    """Whether any section the tools returned is one that holds the answer."""
    for call in calls:
        if call.get("error"):
            continue
        for collection, slug, file in call.get("refs") or []:
            if any(a["slug"] == slug and a["file"].lstrip("/") == file and a.get("collection") in (None, collection)
                   for a in answers):
                return True
    return False


def select(questions: list[dict], ids: set[str], kinds: set[str], limit: int | None) -> tuple[list[dict], int]:
    """(the questions to run, and how many were left out for having no gold answer)."""
    chosen, no_gold = [], 0
    for q in questions:
        if ids and str(q.get("id")) not in ids:
            continue
        if kinds and q.get("kind") not in kinds:
            continue
        if not str(q.get("gold") or "").strip():
            no_gold += 1
            continue
        chosen.append(q)
    return (chosen[:limit] if limit else chosen), no_gold


def evaluate(q: dict, ctx: dict, work: Path) -> dict:
    asked = str(q.get("ask") or q["question"]).strip()
    rec = {"id": q.get("id"), "kind": q.get("kind", "-"), "asked": asked, "gold": q["gold"], "verdict": None,
           "reason": "", "answer": "", "returned_gold": None, "tools": {}, "answer_usage": {}, "judge_usage": {},
           "seconds": 0.0, "error": ""}
    started = time.perf_counter()
    tag = re.sub(r"\W+", "_", str(q.get("id") or len(ctx["records"])))
    call_log = work / f"calls-{tag}.jsonl"
    cfg = mcp_config(work / f"mcp-{tag}.json", docs_server(ctx["server"], ctx["db"], call_log))
    cwd = work / "cwd"
    cwd.mkdir(exist_ok=True)
    flags = ["--mcp-config", str(cfg), "--strict-mcp-config", "--tools", "", "--allowedTools", f"mcp__{SERVER_KEY}",
             "--no-session-persistence", "--append-system-prompt", ANSWER_NOTE, *ctx["common"]]
    if ctx["budget"]:
        flags += ["--max-budget-usd", str(ctx["budget"])]
    result, err = run_claude(ctx["base"], flags, asked, cwd, ctx["timeout"])
    rec["answer_usage"] = usage_of(result)
    calls = read_calls(call_log)[0] if call_log.is_file() else []
    rec["tools"] = dict(Counter(c["tool"] for c in calls))
    rec["returned_gold"] = returned_gold(calls, q["answers"])
    if err or result is None:
        rec.update(verdict="error", error=err or "no result")
        rec["seconds"] = round(time.perf_counter() - started, 1)
        return rec
    rec["answer"] = str(result.get("result") or "").strip()

    prompt = JUDGE_PROMPT.format(question=asked, gold=q["gold"], sources=sources_of(q["answers"]),
                                 answer=rec["answer"] or "(the answer was empty)")
    empty = mcp_config(work / "mcp-none.json", {})
    judged, err = run_claude(ctx["base"], ["--mcp-config", str(empty), "--strict-mcp-config", "--tools", "",
                                           "--no-session-persistence", *ctx["judge_flags"]],
                             prompt, cwd, ctx["timeout"])
    rec["judge_usage"] = usage_of(judged)
    if err or judged is None:
        rec.update(verdict="error", error=f"grading failed: {err}")
    else:
        verdict, reason = parse_verdict(str(judged.get("result") or ""))
        if verdict:
            rec.update(verdict=verdict, reason=reason)
        else:
            rec.update(verdict="error", error=reason)
    rec["seconds"] = round(time.perf_counter() - started, 1)
    return rec


def pct(n: int, d: int) -> str:
    return f"{n / d:.0%}" if d else "-"


def total_tokens(u: dict) -> int:
    return u.get("input", 0) + u.get("output", 0) + u.get("cache_read", 0) + u.get("cache_write", 0)


def report(records: list[dict], total: int, no_gold: int, name: str) -> None:
    print(f"\n{name}: {len(records)} question(s) run, of {total} ({no_gold} without a gold answer)\n")
    groups: dict[str, list[dict]] = {}
    for r in records:
        groups.setdefault(r["kind"], []).append(r)
    rows = [*sorted(groups.items()), ("all", records)]
    print(f"{'kind':12} {'n':>3} {'correct':>12} {'unsupported':>14} {'declined':>12} {'error':>6} {'gold returned':>15}")
    for kind, items in rows:
        graded = [r for r in items if r["verdict"] != "error"]
        counts = Counter(r["verdict"] for r in graded)
        cells = [f"{counts[v]:>3} {pct(counts[v], len(graded)):>4}" for v in VERDICTS]
        got = sum(1 for r in items if r["returned_gold"])
        print(f"{kind:12} {len(items):>3} {cells[0]:>12} {cells[1]:>14} {cells[2]:>12} "
              f"{len(items) - len(graded):>6} {f'{got} of {len(items)}':>15}")
    print("\nPercentages are of the answers that were graded; `gold returned` is whether a section that holds "
          "the answer came back from any tool call.")

    answering = {k: sum(r["answer_usage"].get(k, 0) for r in records) for k in ("input", "output", "cache_read", "cache_write")}
    grading = {k: sum(r["judge_usage"].get(k, 0) for r in records) for k in ("input", "output", "cache_read", "cache_write")}
    print("\nTokens" + (f" ({len(records)} questions)" if records else ""))
    for label, u in (("answering", answering), ("grading", grading)):
        print(f"  {label:10} in {u['input']:,} · cache read {u['cache_read']:,} · cache write {u['cache_write']:,} · "
              f"out {u['output']:,}  = {total_tokens(u):,}")
    if records:
        print(f"  per question, answering: {total_tokens(answering) // len(records):,} on average; "
              f"{sum((r['answer_usage'].get('turns') or 0) for r in records) / len(records):.1f} turns, "
              f"{sum(sum(r['tools'].values()) for r in records) / len(records):.1f} tool calls")
    costs = [r["answer_usage"].get("cost_usd") for r in records] + [r["judge_usage"].get("cost_usd") for r in records]
    if any(c is not None for c in costs):
        print(f"  cost reported by claude: ${sum(c for c in costs if c):.4f}")

    bad = [r for r in records if r["verdict"] != "correct"]
    if bad:
        print("\nNot correct:")
        for r in bad:
            print(f"\n[{r['id']}] {r['verdict']}" + ("" if r["returned_gold"] else "  (gold section never returned)")
                  + f"\n  asked:  {r['asked']}\n  gold:   {r['gold']}")
            if r["error"]:
                print(f"  error:  {r['error']}")
            else:
                print(f"  answer: {' '.join(r['answer'].split())[:240]}")
                if r["reason"]:
                    print(f"  why:    {r['reason']}")


def main() -> int:
    utf8_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, required=True, help="index built by build_search_db.py")
    ap.add_argument("--questions", type=Path, required=True, help="questions.jsonl; those with a `gold` answer are run")
    ap.add_argument("--ids", help="comma-separated question ids to run")
    ap.add_argument("--kind", action="append", default=[], help="run only this kind (repeatable)")
    ap.add_argument("--limit", type=int, help="run at most this many questions")
    ap.add_argument("--server", type=Path, help="mcp_server.py to serve with (default: the copy beside the index, "
                                                "else this checkout's)")
    ap.add_argument("--claude", default="claude", help="the Claude Code command (default: claude)")
    ap.add_argument("--claude-arg", action="append", default=[], help="an argument to put before -p (repeatable)")
    ap.add_argument("--model", help="model for answering")
    ap.add_argument("--judge-model", help="model for grading (default: the same as --model)")
    ap.add_argument("--max-budget-usd", type=float, dest="budget", help="spending limit for each answering run")
    ap.add_argument("--timeout", type=float, default=300, help="seconds allowed each claude call (default 300)")
    ap.add_argument("--bare", action="store_true", help="pass --bare to claude (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--json", type=Path, help="write the per-question results here")
    ap.add_argument("--dry-run", action="store_true", help="list what would run and spend nothing")
    args = ap.parse_args()

    db = args.db.resolve()
    mcp_server.CORPUS = mcp_server.Corpus(db)
    if not mcp_server.CORPUS.available:
        print(f"No index at {db}", file=sys.stderr)
        return 1
    questions = eval_search.load_questions(args.questions)
    chosen, no_gold = select(questions, {i.strip() for i in (args.ids or "").split(",") if i.strip()},
                             set(args.kind), args.limit)
    if not chosen:
        print(f"No question to run: {no_gold} had no `gold` answer. triage_log.py asks for one, or add "
              '"gold": "a short answer" to a question.', file=sys.stderr)
        return 1
    stale = eval_search.resolve_quotes(chosen) + eval_search.missing_answers(chosen)
    if stale:
        print(f"!! {len(stale)} answer section(s) are not in this index -- fix these first:")
        for s in stale[:20]:
            print(f"   {s}")
        return 2

    print(f"{len(chosen)} question(s) to run ({no_gold} left out for having no gold answer): "
          f"{len(chosen)} answering run(s) and {len(chosen)} grading call(s).")
    if args.dry_run:
        for q in chosen:
            print(f"  [{q.get('id')}] ({q.get('kind', '-')}) {q.get('ask') or q['question']}\n"
                  f"      gold: {q['gold']}")
        return 0

    server = (args.server or (db.parent / "mcp_server.py" if (db.parent / "mcp_server.py").is_file()
                              else HERE / "mcp_server.py")).resolve()
    common = (["--model", args.model] if args.model else []) + (["--bare"] if args.bare else [])
    judge_model = args.judge_model or args.model
    ctx = {"base": claude_base(args.claude, args.claude_arg), "server": server, "db": db, "budget": args.budget,
           "timeout": args.timeout, "common": common, "records": [],
           "judge_flags": (["--model", judge_model] if judge_model else []) + (["--bare"] if args.bare else [])}
    records = ctx["records"]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        for n, q in enumerate(chosen, 1):
            rec = evaluate(q, ctx, Path(tmp))
            records.append(rec)
            print(f"[{n}/{len(chosen)}] {rec['id']}: {rec['verdict']}"
                  + (f" -- {rec['error']}" if rec["error"] else "")
                  + f"  ({total_tokens(rec['answer_usage']) + total_tokens(rec['judge_usage']):,} tokens, "
                    f"{rec['seconds']:.0f} s)", flush=True)

    report(records, len(questions), no_gold, args.questions.name)
    if args.json:
        args.json.write_text(json.dumps({
            "db": str(db), "git_commit": eval_search.git_commit(), "questions": str(args.questions),
            "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model": args.model, "judge_model": judge_model, "results": records}, indent=2,
            ensure_ascii=False) + "\n", encoding="utf-8")
    return 3 if any(r["verdict"] == "error" for r in records) else 0


if __name__ == "__main__":
    sys.exit(main())
