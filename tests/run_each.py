#!/usr/bin/env python3
"""Run every test alone, in a process of its own.

A test that passes only after another one has run is a test of the order,
not of the code. `python -m unittest discover -s tests` shares one process,
and with it whatever earlier tests left behind; this does not.

  python tests/run_each.py
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def ids(suite):
    for t in suite:
        yield from ids(t) if isinstance(t, unittest.TestSuite) else [t.id()]


def main() -> int:
    tests = list(ids(unittest.defaultTestLoader.discover(str(HERE))))
    failed = []
    for i, test in enumerate(tests, 1):
        r = subprocess.run([sys.executable, "-m", "unittest", test], cwd=HERE,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(f"[{i}/{len(tests)}] {'ok  ' if r.returncode == 0 else 'FAIL'} {test}", flush=True)
        if r.returncode:
            failed.append((test, r.stdout + r.stderr))
    for test, out in failed:
        print(f"\n===== {test}\n{out[-3000:]}")
    print(f"\n{len(tests) - len(failed)} of {len(tests)} tests pass alone.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
