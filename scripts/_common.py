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
import sys

# The separator of a chunk's breadcrumb: a single right-pointing angle quote.
# check_corpus.py keeps its own copy, as it imports nothing from the scripts;
# a test holds the two together.
BREADCRUMB_SEP = " \u203a "

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


def utf8_console() -> None:
    """Make stdout and stderr UTF-8, replacing what cannot be encoded. These
    documents are full of characters like the trademark sign and the right
    angle quote that a Windows console's legacy code page (cp1252, cp950, ...)
    cannot encode, and a report that prints one died halfway through."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def slugify(text: str, maxlen: int | None = None, default: str = "", lower_first: bool = True) -> str:
    """Lowercase ASCII letters and digits, runs of anything else becoming one
    hyphen, none at either end. The callers differ in what they do to the text
    first, in how long a slug may be, and in what an empty one becomes, and
    keep those.

    `lower_first=False` substitutes before lowercasing, as the collection key
    always has: a character whose lowercase is ASCII (the Kelvin sign) is then
    a separator, where lowercasing first makes it a letter."""
    if lower_first:
        s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    else:
        s = re.sub(r"[^0-9A-Za-z]+", "-", text).strip("-").lower()
    if maxlen is not None:
        s = s[:maxlen].strip("-")
    return s or default


def strip_emphasis(line: str) -> str:
    """A line without its heading marks and the bold, italic or code marks
    around the whole of it."""
    s = line.strip()
    s = re.sub(r"^#{1,6}\s*", "", s).strip()
    for _ in range(3):
        s2 = re.sub(r"^(\*\*|__|\*|_|`)(.*?)\1$", r"\2", s.strip())
        if s2 == s:
            break
        s = s2
    return s.strip()
