"""Code two scripts share, standard library only.

pick_extractor.py has to stay fast and rebuild_reference.py has to import
pymupdf4llm, which starts its layout model; neither may import the other, so
what both need lives here and imports nothing heavy.

`pick_command_level` here and `pick_extractor.looks_like_entry` are two tests
for entries on purpose. They answer different questions -- is this document a
reference at all, and from which TOC level to attribute -- and each was
measured as it stands: pick_extractor's on 38 manuals, this one on manuals that
already convert correctly. The `" -"` clause below is looser than anything in
pick_extractor, and tightening either to match the other moves measured
results. Merge them only after re-measuring both.
"""
from __future__ import annotations

import re

IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*(\s+-[a-z0-9_]+)*$", re.I)
# An error/warning message code ("ADES-002", "CMD-082"): a reference entry too.
MESSAGE_CODE_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}-\d{2,5}$")

# Fewer titles shaped like commands than this and no level is picked: the
# evidence is too thin to attribute a chunk to an entry.
MIN_COMMANDS = 20


def pick_command_level(toc: list) -> int | None:
    """The TOC level whose titles look most like command names.

    Necessary because the level differs per manual -- tshell-ref keeps
    commands at L3, syn2 at L1 (with SYNTAX/ARGUMENTS/DESCRIPTION at L2).
    Picking "the most populous level" gets syn2 wrong.
    """
    best, best_n = None, 0
    by_level: dict[int, list[str]] = {}
    for lvl, title, _page in toc:
        by_level.setdefault(lvl, []).append((title or "").strip())
    for lvl, titles in by_level.items():
        n = sum(
            1
            for t in titles
            # Parenthesised as Python already groups it. The bare `or " -" in t`
            # predates this change; tightening it would move which level is
            # picked for manuals that already convert correctly.
            if (IDENTIFIER_RE.match(t) and "_" in t) or " -" in t or MESSAGE_CODE_RE.match(t)
        )
        if n > best_n:
            best, best_n = lvl, n
    return best if best_n >= MIN_COMMANDS else None
