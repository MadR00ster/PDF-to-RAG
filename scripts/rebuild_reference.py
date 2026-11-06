#!/usr/bin/env python3
"""
Page-accurate conversion for command-dictionary manuals (syn2, tshell-ref).

Why this exists, separately from a prose conversion: in a command reference
the chunker splits one command's entry into several chunks (Description,
Arguments, Usage, Examples), and those chunks do not say which command they
belong to. Retrieving a bare "Arguments" chunk is not merely unhelpful -- a
model will confidently attach those flags to whatever command the user
asked about. Measured on the current corpus, ~2,000 chunks are orphaned this
way.

Recovering the owner from chunk text alone does not work: only 82.5% of
commands are ever anchored, and every miss silently inherits the *previous*
command (verified: a `tessent -shell` chunk labelled `tessent -diagserver`).

So the conversion goes through pages instead, which is exact:

  1. Extract with page_chunks=True, so every page's text is known separately.
  2. Concatenate into full.md, recording each page's character span.
  3. Split full.md into one region per command, from the PDF's own TOC, which
     lists every command with its page (1,319 in tshell-ref, 1,334 in syn2 --
     complete and authoritative). A chunk belongs to the last command whose
     page <= the chunk's first page, unless a shallower TOC entry (a chapter,
     an appendix, the licence) starts in between -- then it belongs to none.
  4. Chunk each region with the same rules as convert_manual.py, and read each
     chunk's page range off the offsets it was cut at.

The code is convert_manual.convert_reference; `convert_manual.py --shape
reference` runs the same thing, and this script keeps the command line it has
always had. Output matches convert_manual.py's layout, plus per-section
`page_start`, `page_end`, `command`, and `breadcrumb`.

Usage:
  python scripts/rebuild_reference.py "Synopsys Manual/syn2.pdf" \
      --title "Synthesis Tool Commands" --slug syn2 --replace
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import convert_manual as cm  # noqa: E402
import enrich_chunks as ec  # noqa: E402,F401
from _common import MIN_COMMANDS, IDENTIFIER_RE, MESSAGE_CODE_RE, pick_command_level  # noqa: E402,F401

editions = cm.editions


def main() -> None:
    cm.main(shape="reference", description=__doc__)


if __name__ == "__main__":
    main()
