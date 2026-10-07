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

# A title that looks like a command / API entry rather than a prose heading:
# pick_extractor's test for whether a document is a reference at all.
ENTRY_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]*$")

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


def looks_like_entry(title: str) -> bool:
    t = (title or "").strip()
    if MESSAGE_CODE_RE.match(t):
        return True
    if not t or " " in t and not t.split()[0].endswith(("_",)):
        # Allow "tessent -shell" style two-token entries, reject prose.
        parts = t.split()
        if not (len(parts) == 2 and parts[1].startswith("-")):
            return "_" in t and ENTRY_IDENTIFIER_RE.match(t.split()[0] or "") is not None
    return bool(ENTRY_IDENTIFIER_RE.match(t)) and ("_" in t or t.islower())


# A dictionary is substantially *made of* entries, so they recur every few
# pages. Without this, a prose manual with one appendix listing 22 option names
# at L6 was classified as a reference -- 22 entries across 1,648 pages.
MIN_ENTRIES_PER_PAGE = 0.10


def detect_shape(toc, pages: int) -> tuple[str, str, int | None]:
    """Reference/dictionary if some TOC level is mostly identifier-like titles.

    Keyed on a level rather than the whole TOC because a dictionary keeps its
    entries at one depth (tshell-ref at L3, syn2 at L1) under prose chapter
    headings that would otherwise dilute the signal. Density then separates a
    real dictionary from a prose manual that happens to list some identifiers.

    Returns (shape, why, level): the level judged to hold the entries, or None
    where no level did.
    """
    by_level: dict[int, list[str]] = {}
    for lvl, title, _page in toc:
        by_level.setdefault(lvl, []).append(title or "")
    best = None
    for lvl, titles in sorted(by_level.items()):
        n = sum(1 for t in titles if looks_like_entry(t))
        if n >= 20 and n >= 0.3 * len(titles):
            if best is None or n > best[1]:
                best = (lvl, n, len(titles))
    if not best:
        return "prose", "no level is dominated by identifier-like titles", None

    lvl, n, total = best
    per_page = n / max(pages, 1)
    if per_page < MIN_ENTRIES_PER_PAGE:
        return "prose", (f"L{lvl} has {n} identifier-like titles but only "
                         f"{per_page:.3f}/page -- a list inside a prose manual"), None
    if per_page < 0.15:
        return "mixed", f"L{lvl}: {n}/{total} titles look like entries, {per_page:.2f}/page", lvl
    return "reference", f"L{lvl}: {n}/{total} titles look like entries, {per_page:.2f}/page", lvl
