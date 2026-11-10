#!/usr/bin/env python3
"""Run the test files side by side, one process per file.

`python -m unittest discover -s tests` runs every file in one process, one test
after another. The files share nothing (`WS` in `_support.py` is a workspace
per process), so this runs several at the same time. It checks what the serial
run checks; it does not replace it for CI or for `run_each.py`, which is about
order.

Two at a time is the default because that is what was measured to help. On the
Windows machine it was timed on (32 logical cores), the suite took 506 s
serially and 436 s with two workers; with four it took 696 s and with ten 751 s,
because the conversions that fill most tests slow each other down when many run
at once. `test_converters.py` alone takes about 330 s, so no width gets below
that. Time it on your own machine before raising `-j`.

  python tests/run_parallel.py            # two files at a time
  python tests/run_parallel.py -j 4       # at most four at once
  python tests/run_parallel.py test_http  # only the named files
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run_file(name: str) -> tuple[str, int, float, str]:
    started = time.perf_counter()
    r = subprocess.run([sys.executable, "-m", "unittest", name], cwd=HERE,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return name, r.returncode, time.perf_counter() - started, r.stdout + r.stderr


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="*", help="test files to run (default: every test_*.py)")
    ap.add_argument("-j", "--jobs", type=int, default=2, help="files at once (default 2)")
    args = ap.parse_args()

    names = [Path(f).stem for f in args.files] or sorted(p.stem for p in HERE.glob("test_*.py"))
    missing = [n for n in names if not (HERE / f"{n}.py").is_file()]
    if missing:
        print("No such test file: " + ", ".join(missing), file=sys.stderr)
        return 2
    # The biggest files first, so the slowest does not start last.
    names.sort(key=lambda n: -(HERE / f"{n}.py").stat().st_size)

    started = time.perf_counter()
    failed: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for future in as_completed([pool.submit(run_file, n) for n in names]):
            name, code, seconds, output = future.result()
            print(f"{'ok  ' if code == 0 else 'FAIL'} {name:22} {seconds:6.0f}s", flush=True)
            if code:
                failed.append((name, output))
    for name, output in failed:
        print(f"\n===== {name}\n{output[-4000:]}")
    print(f"\n{len(names) - len(failed)} of {len(names)} test files pass; {time.perf_counter() - started:.0f}s wall time.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
