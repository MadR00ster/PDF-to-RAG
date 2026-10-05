#!/usr/bin/env python3
"""
End-to-end check of mcp_server.py against a built index.

Spawns the server exactly the way an editor does (stdio, newline-delimited
JSON-RPC) and drives a real handshake plus every tool. Nothing is hardcoded
about the corpus: the probe terms come out of the index itself, so this works
on any corpus the skill produces.

  python scripts/mcp_smoke_test.py --db <corpus>/mcp-index.sqlite3
  python scripts/mcp_smoke_test.py --db ... -v     # print each tool's output
  python scripts/mcp_smoke_test.py --db ... --server <corpus>/mcp_server.py
                                                   # the copy beside an index

Exit code is 0 only if every check passed, so it can gate a rebuild.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "mcp_server.py"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

failures: list[str] = []


class Client:
    def __init__(self, db: Path, server: Path = SERVER):
        self.proc = subprocess.Popen(
            [sys.executable, str(server), "--db", str(db)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
        )
        self.next_id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"server closed the connection\n{self.proc.stderr.read()}")
        return json.loads(line)

    def notify(self, method: str) -> None:
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def close(self) -> None:
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def probes(db: Path) -> dict:
    """Pull real terms out of the index so the checks suit this corpus."""
    con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out = {"entity": None, "collection": None, "figure": None, "editions": False, "latest": None}
    if "doc_id" in {r["name"] for r in con.execute("PRAGMA table_info(documents)")}:
        out["editions"] = True
        # The newest edition of a manual that has several, by its own slug. A
        # doc_id would mean the current edition, which a pin may make the
        # earliest -- nothing to compare it with, in a healthy corpus -- and
        # two collections may share one.
        row = con.execute(
            "SELECT slug FROM documents WHERE is_latest = 1 AND (collection, doc_id) IN"
            " (SELECT collection, doc_id FROM documents GROUP BY collection, doc_id HAVING COUNT(*) > 1)"
            " LIMIT 1").fetchone()
        out["latest"] = row["slug"] if row else None
    # Probe terms come from current editions, because that is what a search
    # without a document reads: a heading that exists only in an edition a
    # pin has set aside would fail a corpus with nothing wrong with it.
    current = " AND slug IN (SELECT slug FROM documents WHERE is_current = 1)" if out["editions"] else ""
    row = con.execute("SELECT slug, title FROM documents WHERE 1 = 1" + current
                      + " ORDER BY section_count DESC LIMIT 1").fetchone()
    out["document"], out["title_word"] = row["slug"], (row["title"].split() or ["the"])[0]
    row = con.execute("SELECT name FROM entities WHERE 1 = 1" + current + " LIMIT 1").fetchone()
    if row:
        out["entity"] = row["name"]
    row = con.execute("SELECT DISTINCT collection FROM documents LIMIT 1").fetchone()
    if row:
        out["collection"] = row["collection"]
    row = con.execute("SELECT heading FROM chunks WHERE length(heading) > 12" + current + " LIMIT 1").fetchone()
    out["phrase"] = row["heading"] if row else out["title_word"]
    try:
        row = con.execute("SELECT id FROM figures ORDER BY id LIMIT 1").fetchone()
        out["figure"] = row["id"] if row else None
    except sqlite3.OperationalError:
        pass  # an index built before figures existed
    con.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--server", type=Path, default=SERVER,
                    help="the server to drive (default: the one beside this script)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    db = Path(args.db).resolve()
    if not db.is_file():
        print(f"No index at {db} — build it first.", file=sys.stderr)
        return 1
    p = probes(db)
    print(f"Testing {args.server} against {db.name}\n")

    client = Client(db, args.server)

    def tool(name: str, tool_args: dict, expect: str, expect_error: bool = False) -> None:
        resp = client.call("tools/call", {"name": name, "arguments": tool_args})
        result = resp.get("result") or {}
        body = "\n".join(c.get("text", "") for c in result.get("content", []))
        ok = result.get("isError", False) == expect_error and expect.lower() in body.lower()
        check(f"{name}({json.dumps(tool_args, ensure_ascii=False)[:70]})", ok, body[:160])
        if args.verbose and body:
            print("\n" + "\n".join("      " + l for l in body.splitlines()[:40]) + "\n")

    def image_tool(name: str, tool_args: dict) -> None:
        """An image tool must return exactly one PNG, not just text about one."""
        resp = client.call("tools/call", {"name": name, "arguments": tool_args})
        result = resp.get("result") or {}
        content = result.get("content", [])
        text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        if result.get("isError") and "needs PyMuPDF" in text:
            print(f"  n/a   {name} (PyMuPDF is not installed for the server)")
            return
        images = [c for c in content if c.get("type") == "image"]
        ok = (not result.get("isError") and len(images) == 1
              and images[0].get("mimeType") == "image/png" and len(images[0].get("data", "")) > 100)
        check(f"{name}({json.dumps(tool_args)[:70]})", ok, text[:160])
        if args.verbose and text:
            print("\n" + "\n".join("      " + l for l in text.splitlines()[:10]) + "\n")

    try:
        resp = client.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": "smoke", "version": "1"}})
        info = (resp.get("result") or {}).get("serverInfo", {})
        check("initialize", bool(info.get("name")), json.dumps(resp)[:160])
        client.notify("notifications/initialized")
        check("ping", client.call("ping").get("result") == {})

        names = {t["name"] for t in (client.call("tools/list").get("result") or {}).get("tools", [])}
        check("tools/list", {"search_docs", "get_section", "lookup_entity", "list_documents", "get_toc",
                             "compare_versions", "get_figure", "get_page_image"} <= names,
              f"got {sorted(names)}")

        print("\nTools:")
        tool("list_documents", {}, "document(s) in the corpus")
        if p["collection"]:
            tool("list_documents", {"collection": p["collection"]}, "document(s)")
        tool("search_docs", {"query": p["title_word"], "limit": 3}, "result(s) for")
        tool("search_docs", {"query": f'"{p["phrase"]}"', "limit": 2}, "result(s) for")
        tool("search_docs", {"query": p["title_word"], "document": p["document"], "limit": 2}, "result(s) for")
        tool("get_toc", {"document": p["document"], "max_level": 1}, "pages")
        tool("get_section", {"section_id": 1}, "file:")
        tool("get_section", {"section_id": 1, "context": 1}, "file:")
        if p["entity"]:
            tool("lookup_entity", {"name": p["entity"]}, p["entity"])
            tool("lookup_entity", {"name": p["entity"] + "zzq"}, "did you mean", expect_error=False)
        else:
            print("  SKIP  lookup_entity (this corpus has no reference documents)")
        if p["figure"]:
            image_tool("get_figure", {"figure_id": p["figure"]})
        else:
            print("  n/a   get_figure (this index has no figures)")
        image_tool("get_page_image", {"document": p["document"], "page": 1})

        print("\nEditions:")
        if not p["editions"]:
            print("  n/a   this index was built before editions were recorded")
        else:
            # A version is per manual, and one that is not here is refused
            # with the list of those that are -- never answered from another.
            tool("search_docs", {"query": p["title_word"], "version": "1.0"}, "needs `document`", expect_error=True)
            tool("search_docs", {"query": p["title_word"], "document": p["document"], "version": "0.0.0.1"},
                 "nothing was substituted", expect_error=True)
            if p["latest"]:
                tool("list_documents", {}, "not current")
                # Its own slug in the answer: a comparison that ran. Not the
                # wording of a finished one -- a prose manual with no bookmark
                # outline has no headings to compare, and nothing wrong with it.
                tool("compare_versions", {"document": p["latest"]}, f"({p['latest']})")
                tool("get_toc", {"document": p["latest"], "max_level": 1}, "pages")
            else:
                tool("compare_versions", {"document": p["document"]}, "only one edition", expect_error=True)

        print("\nError handling:")
        tool("search_docs", {"query": "((("}, "no searchable words", expect_error=True)
        tool("get_toc", {"document": "does-not-exist"}, "unknown document slug", expect_error=True)
        tool("get_section", {}, "section_id", expect_error=True)
        tool("get_figure", {"figure_id": 999999999}, "figure", expect_error=True)
        tool("get_page_image", {"document": p["document"], "page": 0}, "page", expect_error=True)
        tool("search_docs", {"query": "x", "collection": "nope-not-real"}, "unknown collection", expect_error=True)
        tool("search_docs", {"query": "zzzqqxyw", "limit": 3}, "no matches")
        # One impossible word must not sink an otherwise good question -- nor
        # one built around a phrase or an identifier. Both reach FTS5 as a
        # multi-word phrase, which the OR fallback has to keep whole.
        tool("search_docs", {"query": f"{p['title_word']} zzzqqxyw", "limit": 3}, "result(s) for")
        tool("search_docs", {"query": f'"{p["phrase"]}" zzzqqxyw', "limit": 3}, "result(s) for")
        if p["entity"]:
            tool("search_docs", {"query": f"{p['entity']} zzzqqxyw", "limit": 3}, "result(s) for")
    finally:
        client.close()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
