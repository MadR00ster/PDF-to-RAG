"""The server over HTTP: the same answers as over stdio, behind a bearer token,
one database connection to a thread."""
from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _support import PY, SCRIPTS, WS, load_script  # noqa: E402

TOKEN = "t0ken-" + "a1b2c3d4" * 4              # 38 characters; the server wants 16 or more
OTHER = "second-" + "e5f6a7b8" * 4


class Server:
    """mcp_server.py --http, started on a port the system picks."""

    def __init__(self, db: Path, *args: str, env: dict | None = None, host: str = "127.0.0.1"):
        base = {k: v for k, v in os.environ.items() if k not in ("PDF_TO_RAG_TOKEN", "PDF_TO_RAG_LOG")}
        self.proc = subprocess.Popen(
            [PY, str(SCRIPTS / "mcp_server.py"), "--db", str(db), "--http", f"{host}:0", *args],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", env={**base, **(env or {})})
        self.log: list[str] = []
        self.port: int | None = None
        ready = threading.Event()

        def drain() -> None:                 # the access log must never fill the pipe and stall the server
            for line in self.proc.stderr:
                self.log.append(line)
                found = re.search(r"listening on http://\S+:(\d+)/mcp", line)
                if found:
                    self.port = int(found.group(1))
                    ready.set()
            ready.set()
        threading.Thread(target=drain, daemon=True).start()
        if not ready.wait(60) or self.port is None:
            self.stop()
            raise AssertionError("the server did not start:\n" + "".join(self.log))

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def stop(self) -> None:
        self.proc.kill()
        self.proc.wait(10)
        for pipe in (self.proc.stdout, self.proc.stderr):
            pipe.close()

    def request(self, method: str, path: str = "/mcp", body=None, token: str | None = TOKEN,
                headers: dict | None = None) -> tuple[int, dict, object]:
        """(status, response headers, JSON body or None)."""
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode("utf-8")
        head = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if token:
            head["Authorization"] = f"Bearer {token}"
        head.update(headers or {})
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=head, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                status, got, raw = r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as exc:
            status, got, raw = exc.code, dict(exc.headers), exc.read()
            exc.close()
        return status, got, (json.loads(raw) if raw else None)

    def rpc(self, method: str, params: dict | None = None, **kw):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params is not None else {})}
        return self.request("POST", body=body, **kw)


class HttpTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.tmp = Path(cls._tmp.name)
        cls.db = WS.index()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def start(self, *args: str, **kw) -> Server:
        server = Server(self.db, *args, **kw)
        self.addCleanup(server.stop)
        return server

    def test_59_the_transport_answers_as_stdio_does(self):
        server = self.start(env={"PDF_TO_RAG_TOKEN": TOKEN})
        status, headers, body = server.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertEqual(body["id"], 1)
        self.assertTrue(body["result"]["serverInfo"]["name"])

        # A notification is acknowledged, with nothing to say.
        status, _, body = server.request("POST", body={"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((status, body), (202, None))
        status, _, body = server.rpc("tools/list")
        self.assertIn("search_docs", {t["name"] for t in body["result"]["tools"]})
        status, _, body = server.rpc("tools/call", {"name": "search_docs", "arguments": {"query": "torque the bolts"}})
        self.assertEqual(status, 200)
        self.assertIn("result(s) for", body["result"]["content"][0]["text"])
        status, _, body = server.rpc("tools/call", {"name": "get_section", "arguments": {}})
        self.assertTrue(body["result"]["isError"], "a tool's error should be a result, as over stdio")

        # What it refuses, as over stdio, and nothing takes the server down.
        status, _, body = server.request("POST", body=b"{not json")
        self.assertEqual((status, body["error"]["code"]), (400, -32700))
        status, _, body = server.request("POST", body=b"[1, 2]")
        self.assertEqual((status, body["error"]["code"]), (400, -32600))
        status, _, body = server.request("POST", body={"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": [1]})
        self.assertEqual((status, body["error"]["code"]), (200, -32602))
        status, _, body = server.rpc("no/such/method")
        self.assertEqual((status, body["error"]["code"]), (200, -32601))
        status, _, body = server.request("POST", body=b"{}", headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _, body = server.request("POST", body={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                         headers={"MCP-Protocol-Version": "1999-01-01"})
        self.assertEqual(status, 400)
        self.assertEqual(server.rpc("ping")[2]["result"], {})

        # There is no event stream and no session to end.
        status, headers, _ = server.request("GET")
        self.assertEqual((status, headers["Allow"]), (405, "POST"))
        self.assertEqual(server.request("DELETE")[0], 405)
        self.assertEqual(server.request("GET", "/nope")[0], 404)
        self.assertEqual(server.request("POST", "/nope", body=b"{}")[0], 404)

        # A body past the limit is refused unread.
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
        conn.putrequest("POST", "/mcp")
        for key, value in (("Content-Type", "application/json"), ("Content-Length", str(50_000_000)),
                           ("Authorization", f"Bearer {TOKEN}")):
            conn.putheader(key, value)
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)
        conn.close()

    def test_59i_initialize_is_answered_whatever_version_header_it_carries(self):
        """The version is negotiated in initialize's reply, so a client that
        names one this server does not know still gets that reply (and the
        version the server speaks), where every later call is refused."""
        server = self.start(env={"PDF_TO_RAG_TOKEN": TOKEN})
        status, _, body = server.request(
            "POST", body={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "1999-01-01", "capabilities": {}}},
            headers={"MCP-Protocol-Version": "1999-01-01"})
        self.assertEqual(status, 200)
        self.assertIn(body["result"]["protocolVersion"], load_script("mcp_server").KNOWN_PROTOCOLS)
        status, _, _ = server.request("POST", body={"jsonrpc": "2.0", "id": 2, "method": "ping"},
                                      headers={"MCP-Protocol-Version": "1999-01-01"})
        self.assertEqual(status, 400)

    def test_59j_a_logged_call_names_its_token_without_giving_it_away(self):
        """Over HTTP the call log can tell whose call it was: each line carries
        a short hash of the bearer token it came with, never the token."""
        import hashlib
        tokens = self.tmp / "tokens59j.txt"
        tokens.write_text(OTHER + chr(10), encoding="utf-8")
        log = self.tmp / "tokens59j.jsonl"
        server = self.start("--token-file", str(tokens), env={"PDF_TO_RAG_TOKEN": TOKEN, "PDF_TO_RAG_LOG": str(log)})
        ask = {"name": "search_docs", "arguments": {"query": "torque the bolts"}}
        for token in (TOKEN, OTHER, TOKEN):
            self.assertEqual(server.rpc("tools/call", ask, token=token)[0], 200)
        lines = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()]
        expect = lambda token: hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]  # noqa: E731
        self.assertEqual([ln["token_id"] for ln in lines], [expect(TOKEN), expect(OTHER), expect(TOKEN)])
        self.assertNotEqual(expect(TOKEN), expect(OTHER))
        text = log.read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(OTHER, text)

        # With no token required there is nobody to name.
        open_log = self.tmp / "open59j.jsonl"
        open_server = self.start(env={"PDF_TO_RAG_LOG": str(open_log)})
        self.assertEqual(open_server.rpc("tools/call", ask, token=None)[0], 200)
        (line,) = [json.loads(ln) for ln in open_log.read_text(encoding="utf-8").splitlines()]
        self.assertNotIn("token_id", line)

    def test_59b_health_reports_the_index(self):
        server = self.start()
        status, _, body = server.request("GET", "/health", token=None)
        self.assertEqual(status, 200)
        self.assertIn(body["status"], ("ok", "degraded"))
        self.assertEqual(body["documents"], 4)
        self.assertGreater(body["chunks"], 0)
        self.assertEqual(os.path.realpath(body["index"]), os.path.realpath(self.db))
        self.assertTrue(body["built_at"])

        gone = Server(self.tmp / "no-index.sqlite3", host="127.0.0.1")
        try:
            status, _, body = gone.request("GET", "/health", token=None)
            self.assertEqual((status, body["status"]), (503, "no index"))
        finally:
            gone.stop()

    def test_59c_a_token_is_required_when_one_is_set(self):
        tokens = self.tmp / "tokens.txt"
        tokens.write_text(f"# one a person\n\n{OTHER}\n", encoding="utf-8")
        server = self.start("--token-file", str(tokens), env={"PDF_TO_RAG_TOKEN": TOKEN})
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        status, headers, _ = server.request("POST", body=ping, token=None)
        self.assertEqual(status, 401)
        self.assertIn("Bearer", headers["WWW-Authenticate"])
        self.assertEqual(server.request("POST", body=ping, token="wrong-" * 8)[0], 401)
        self.assertEqual(server.request("POST", body=ping, token=TOKEN[:-1])[0], 401)
        self.assertEqual(server.request("POST", body=ping, token=None, headers={"Authorization": TOKEN})[0], 401)
        self.assertEqual(server.request("GET", "/health", token=None)[0], 401, "/health was open")
        self.assertEqual(server.request("GET", "/health", token="wrong-" * 8)[0], 401)
        for good in (TOKEN, OTHER):
            self.assertEqual(server.request("POST", body=ping, token=good)[0], 200)
        self.assertEqual(server.request("POST", body=ping, token=None, headers={"Authorization": f"bearer {TOKEN}"})[0],
                         200)
        self.assertEqual(server.request("GET", "/health")[0], 200)
        self.assertNotIn(TOKEN, "".join(server.log), "a token reached the log")
        self.assertNotIn(OTHER, "".join(server.log))

        # The smoke test drives it end to end over HTTP, the way a deployment is checked.
        r = subprocess.run([PY, str(SCRIPTS / "mcp_smoke_test.py"), "--db", str(self.db), "--url", server.url,
                            "--token", OTHER], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("All checks passed", r.stdout)
        r = subprocess.run([PY, str(SCRIPTS / "mcp_smoke_test.py"), "--db", str(self.db), "--url", server.url,
                            "--token", "wrong-" * 8], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertNotEqual(r.returncode, 0, "the smoke test passed against a server that refused it")

    def test_59d_an_unsafe_start_is_refused(self):
        def start(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
            base = {k: v for k, v in os.environ.items() if k != "PDF_TO_RAG_TOKEN"}
            return subprocess.run([PY, str(SCRIPTS / "mcp_server.py"), "--db", str(self.db), *args],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  env={**base, **(env or {})}, stdin=subprocess.DEVNULL, timeout=60)
        for spec in ("0.0.0.0:0", ":0", "192.0.2.1:0", "docs.example.com:0"):
            r = start("--http", spec)
            self.assertEqual(r.returncode, 2, f"{spec}: {r.stdout}{r.stderr}")
            self.assertIn("Refusing to listen", r.stderr)
        r = start("--http", "127.0.0.1:0", env={"PDF_TO_RAG_TOKEN": "short"})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("shorter than 16", r.stderr)
        r = start("--http", "nonsense")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("HOST:PORT", r.stderr)
        r = start("--http", "127.0.0.1:0", "--token-file", str(self.tmp / "missing.txt"))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)

        # With a token, any address will do.
        server = Server(self.db, env={"PDF_TO_RAG_TOKEN": TOKEN}, host="0.0.0.0")
        self.addCleanup(server.stop)
        self.assertEqual(server.rpc("ping")[0], 200)
        self.assertEqual(server.request("POST", body=b"{}", token=None)[0], 401)

    def test_59e_a_browser_page_is_not_let_in(self):
        """A page in a browser can reach a loopback server. Clients that are not
        browsers send no Origin, so the ones that do are refused unless named."""
        server = self.start("--allow-origin", "http://localhost:6274")
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        self.assertEqual(server.request("POST", body=ping, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(server.request("GET", "/health", headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(server.request("POST", body=ping, headers={"Origin": "http://localhost:6274"})[0], 200)
        self.assertEqual(server.request("POST", body=ping)[0], 200)

    def test_59f_every_thread_has_a_connection_of_its_own(self):
        server_module = load_script("mcp_server")
        corpus = server_module.Corpus(self.db)
        seen: dict[int, int] = {}
        errors: list[str] = []
        ready = threading.Barrier(8)

        def work(n: int) -> None:
            try:
                ready.wait(30)                       # all eight at once
                seen[n] = id(corpus.db)
                for _ in range(20):
                    count = corpus.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                    assert count > 0
                assert id(corpus.db) == seen[n], "a thread's connection changed"
            except Exception as exc:               # noqa: BLE001 -- reported below
                errors.append(f"{n}: {exc!r}")
        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(len(set(seen.values())), 8, "threads shared a connection")
        mine = corpus.db
        self.assertIs(corpus.db, mine, "one thread was given a second connection")
        self.addCleanup(mine.close)

        # And over HTTP, many requests at once all get the same right answer.
        server = self.start()
        answers: list[str] = []
        failures: list[str] = []

        def ask() -> None:
            for _ in range(5):
                status, _, body = server.rpc("tools/call", {"name": "search_docs", "arguments": {"query": "torque the bolts"}})
                (answers if status == 200 else failures).append(body["result"]["content"][0]["text"] if status == 200 else str(status))
        threads = [threading.Thread(target=ask) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        self.assertEqual(failures, [])
        self.assertEqual(len(answers), 50)
        self.assertEqual(len(set(answers)), 1, "concurrent requests got different answers")

    def test_59h_the_call_logs_state_is_each_threads_own(self):
        """Over HTTP every request is a thread. The sections a call has
        returned, and the client that said hello, belong to that thread: one
        call finishing must not take another's sections, clear them, or put
        its own client's name on another's line."""
        server = load_script("mcp_server")
        server.CORPUS = server.Corpus(self.db)
        self.addCleanup(lambda: server.CORPUS._db and server.CORPUS._db.close())
        log = self.tmp / "threads.jsonl"
        server.CALL_LOG = log
        row = lambda n: {"id": n, "collection": "c", "slug": "s", "file": f"docs/s/sections/{n:04d}-x.md"}  # noqa: E731
        text = [{"type": "text", "text": "answer"}]
        a_started, b_finished = threading.Event(), threading.Event()
        failures: list[str] = []

        def a() -> None:                  # a call that is still running while b's finishes
            try:
                server.handle_request("initialize", {"protocolVersion": "2025-06-18",
                                                     "clientInfo": {"name": "client-a"}})
                server.note_returned(row(1))
                a_started.set()
                b_finished.wait(30)
                server.record_call("search_docs", {"query": "a"}, text, False, 0.0)
            except Exception as exc:       # noqa: BLE001
                failures.append(f"a: {exc!r}")

        def b() -> None:
            try:
                a_started.wait(30)
                server.note_returned(row(2))
                server.record_call("search_docs", {"query": "b"}, text, False, 0.0)
                b_finished.set()
            except Exception as exc:       # noqa: BLE001
                failures.append(f"b: {exc!r}")
        threads = [threading.Thread(target=a), threading.Thread(target=b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(failures, [])
        lines = {json.loads(x)["args"]["query"]: json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()}
        self.assertEqual(lines["b"]["sections"], [2], "a call was given another call's sections")
        self.assertEqual(lines["a"]["sections"], [1], "a call lost its sections to another call finishing")
        self.assertEqual(lines["a"]["client"], "client-a")
        self.assertNotIn("client", lines["b"], "a call was given the name of a client that never made it")

    def test_59g_client_settings_are_printed_without_the_token(self):
        r = subprocess.run([PY, str(SCRIPTS / "mcp_server.py"), "--print-client-config", "https://docs.example.com/mcp",
                            "--name", "eda-docs"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=self.tmp, env={k: v for k, v in os.environ.items() if k != "PDF_TO_RAG_DB"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)         # no index is needed to print them
        parts = re.split(r"^# .*$", r.stdout, flags=re.M)
        vscode, cursor = (json.loads(p) for p in parts[1:3])
        server = vscode["servers"]["eda-docs"]
        self.assertEqual((server["type"], server["url"]), ("http", "https://docs.example.com/mcp"))
        self.assertEqual(server["headers"]["Authorization"], "Bearer ${input:pdf-to-rag-token}")
        asked = vscode["inputs"][0]
        self.assertEqual((asked["id"], asked["type"], asked["password"]), ("pdf-to-rag-token", "promptString", True))
        entry = cursor["mcpServers"]["eda-docs"]
        self.assertEqual(entry["url"], "https://docs.example.com/mcp")
        self.assertEqual(entry["headers"]["Authorization"], "Bearer ${env:PDF_TO_RAG_TOKEN}")
        self.assertIn(".vscode/mcp.json", r.stdout)
        self.assertIn(".cursor/mcp.json", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
