"""roots.py — what an Isabelle ROOT declares.

Two callers with genuinely different inputs, which is why this is one module
and not one function:

  - `build.py` has a **whole ROOT file** and must know which sessions exist,
    because it derives what to build from that. It can and must ignore
    commented-out declarations.
  - `attempts.py` has **fragments of a captured diff** — added and context
    lines from a hunk, with no guarantee that an enclosing `(*` was ever in
    the payload. It cannot strip comments it cannot see, so it matches line
    by line and accepts that a commented-out session may map a name to a
    directory. That costs an unused entry in an attribution map; refusing to
    match at all would cost the mapping entirely.

What they must share is the **name grammar**, and they did not. `attempts.py`
matched `"?([A-Za-z0-9_']+)"?`, which stops at the first character outside
that class *inside* the quotes:

    session "HOL-Analysis"    -> `HOL-Analysis`, but `HOL` in attempts.py
    session "With.Dots-2"     -> `With.Dots-2`, but `With` in attempts.py

Both are ROOTs Isabelle accepts, and both truncate to a name that is either
a different real session or nothing at all. Two spellings of "what is a
session called" inside one package is the failure this repo documents
everywhere else in a different guise: `build.py` would build `HOL-Analysis`
while `attempts.py` attributed the record to a session named `HOL`, and
nothing downstream could tell.

(An earlier version of this note used `session "Probe (AFP)"` and a bare
`session With.Dots-2` as the examples. Neither is a ROOT `isabelle build`
will accept — checked against Isabelle2025-2 — so no such build could have
happened. The defect was real; those illustrations were not. See the head of
`tests/test_roots.py`, which is where the mistake came from.)

**Why not import `isabelle-layout`**, which does all of this properly with a
real tokenizer? Because this package has no runtime dependencies on purpose:
it runs beside a build, and anything it imports is something that can break
one — an unsupervised, unrecorded build is the exact failure the watchdog
exists to prevent. The same call was made for `run_guarded`, six lines in
`guard.py`. What is needed here is "the names in this file", not a session
graph with parent resolution and import classification.

The cost of that decision is divergence, and the answer to divergence is
agreement pinned by tests rather than by a shared import. `isabelle-layout`
ships a conformance corpus as package data for exactly this arrangement — a
consumer that cannot import it at runtime can still prove it agrees — and
`tests/test_roots.py` checks this file against that artefact rather than
against a transcription of it. If Isabelle's syntax moves, the corpus is
regenerated against the new release and those tests are what fail.
"""

from __future__ import annotations

import re
from pathlib import Path

# A session name: quoted, or bare.
#
# Quoted is the case that matters.  Isabelle accepts `.` in a bare name but
# not `-` or `'` (Isabelle2025-2), so `HOL-Analysis` and `With.Dots-2` must be
# written `"HOL-Analysis"` and `"With.Dots-2"` — and `[^"]+` is what keeps
# them whole.  Anything up to the closing quote is the name, including spaces,
# even though Isabelle rejects those: over-accepting here costs an entry in an
# attribution map for a ROOT nobody can build, and under-accepting silently
# renames a session that builds fine.
#
# The bare alternative admits `-` and `'` for the same reason — they only
# occur in input a build would refuse, and reading such a line as a *different*
# session is worse than reading it as an unbuildable one.
SESSION_DECL = re.compile(r'^\s*session\s+(?:"([^"]+)"|([A-Za-z0-9_\'.\-]+))')

# Isabelle's block comments nest, and its cartouches (`\<open> … \<close>`)
# carry free text — a `description \<open>…\<close>` spanning lines can hold
# anything, including the word `session` at the start of one.
_COMMENT_TOKENS = re.compile(r"\(\*|\*\)|\\<open>|\\<close>")


def session_in_line(line: str) -> str | None:
    """The session a single line declares, ignoring any comment context.

    For callers holding fragments rather than files. A commented-out
    declaration matches, because deciding otherwise needs the enclosing text.
    """
    m = SESSION_DECL.match(line)
    return (m.group(1) or m.group(2)) if m else None


def strip_comments(text: str) -> str:
    """Blank out `(* … *)` and `\\<open> … \\<close>` spans, keeping newlines.

    Newlines survive so line-oriented matching downstream still sees the
    file's shape; the content inside a comment becomes spaces, so a
    declaration inside one cannot match. Nesting is counted rather than
    matched non-greedily, because Isabelle's comments nest and a non-greedy
    `.*?` closes at the first `*)`, re-exposing the tail of an outer comment.
    """
    out, depth, start = list(text), 0, 0
    for m in _COMMENT_TOKENS.finditer(text):
        if m.group(0) in ("(*", "\\<open>"):
            if depth == 0:
                start = m.start()
            depth += 1
        elif depth:
            depth -= 1
            if depth == 0:
                _blank(out, start, m.end())
    # An unterminated span runs to end of file, which is what Isabelle does.
    if depth:
        _blank(out, start, len(out))
    return "".join(out)


def _blank(chars: list[str], start: int, end: int) -> None:
    """Spaces, but keep newlines: line-oriented matching downstream still has
    to see the file's shape."""
    for i in range(start, end):
        if chars[i] != "\n":
            chars[i] = " "


def sessions_in_text(text: str) -> list[str]:
    """Sessions a whole ROOT declares, in file order, comments excluded."""
    return [s for line in strip_comments(text).splitlines()
            if (s := session_in_line(line))]


def sessions_in(root_file: Path) -> list[str]:
    """Sessions a ROOT file declares.  Unreadable file → none."""
    try:
        return sessions_in_text(root_file.read_text(errors="replace"))
    except OSError:
        return []
