"""The search index and the MCP server built on it."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _support import WS, run  # noqa: E402


class ServerTest(unittest.TestCase):
    """The index builders and the MCP server, on what the converters write."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.tmp = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_08_index_and_search_db_consume_the_output(self):
        self.corpus = WS.fresh(WS.corpus(), "index-build")
        r = run("build_index.py", str(self.corpus))
        self.assertEqual(r.returncode, 0, r.stderr)
        index = json.loads((self.corpus / "docs" / "index.json").read_text(encoding="utf-8"))
        self.assertTrue(index.get("manuals"), "index.json has no manuals")

        db = self.tmp / "index.sqlite3"
        r = run("build_search_db.py", "--root", str(self.corpus), "--out", str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(db.is_file(), "search index not written")

        import sqlite3
        con = sqlite3.connect(db)
        chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        nocrumb = con.execute("SELECT COUNT(*) FROM chunks WHERE breadcrumb = ''").fetchone()[0]
        con.close()
        self.assertGreater(chunks, 0, "no chunks indexed")
        self.assertEqual(nocrumb, 0, "chunks reached the index with no attribution")

    def test_10_mcp_server_passes_its_smoke_test(self):
        """The server, driven over stdio the way an editor drives it.

        The smoke test includes the AND->OR fallback on an identifier. An
        identifier reaches FTS5 as a multi-word phrase, and the fallback used to
        rewrite the spaces inside it as well, turning "set widget option 00"
        into a phrase containing the word "or" that could never match.
        """
        db = WS.index()
        r = run("mcp_smoke_test.py", "--db", str(db))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("SKIP", r.stdout,
                         "the index has no entities, so the identifier probes never ran")


if __name__ == "__main__":
    unittest.main(verbosity=2)
