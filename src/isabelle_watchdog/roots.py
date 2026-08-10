"""roots.py — reading what a ROOT declares, including when it is not a file.

The grammar is `isabelle_layout`'s, entirely. This module holds no regex, no
comment stripper and no notion of what a session name may contain; it exists
because one of the two callers here does not have a ROOT *file* to hand, and
`isabelle_layout.parse_root_sessions` takes a path.

  - `build.py` has a real ROOT and asks which sessions it declares, to derive
    what to build. `sessions_in` is a one-line adapter for it.
  - `attempts.py` has **fragments of a captured diff** — the added and context
    lines of a hunk in a ROOT — and asks which session names they mention, to
    map a name to the directory it was declared in. `sessions_in_fragment`
    is for that.

Both go through the same parser, which is the point. A private copy here used
to spell names differently from `build.py`'s, and `session "HOL-Analysis"` was
built under that name while the record was attributed to `HOL` — a different
real session, with nothing downstream able to tell.

**Why a dependency now, when there deliberately was none.** The old rule said
the watchdog runs beside a build so anything it imports can break one, and it
was formed against `isabelle_query.common` — a module inside an 11k-line
querying CLI, with that tool's release cadence and its userbase's constraints
attached. `isabelle-layout` is that parser extracted for exactly this reason:
it declares no dependencies of its own, so the transitive tree stays empty,
and it is the parser rather than a tool that happens to contain one. The rule
was a proxy for the weight, and the weight is gone.

Nor does the import land mid-build. Both callers reach it *before* `isabelle
build` is spawned (deriving the session) or long *after* (reading a corpus),
so a failure here is in the class `build.py` already treats as configuration —
loud, before anything runs, with the fix in the message — rather than the
class `guard.py` swallows.

**The fragment bridge.** `sessions_in_fragment` writes the fragment to a
temporary ROOT and parses that, because the public entry point takes a path.
Sixty hunks across both real corpora, so the cost is nothing; it is a bridge
rather than a design, and a text-taking entry point upstream would remove it.

Parsing the fragment as a *unit* rather than line by line is strictly better
than what it replaces: a `(* … *)` wholly inside the hunk now correctly hides
what it encloses, where the line-wise reader saw through it. What neither can
do is see an enclosing `(*` that was never in the payload — so a commented-out
session may still map a name to a directory. That costs an unused entry in an
attribution map, against losing the mapping entirely.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from isabelle_layout import parse_root_sessions

# `isabelle_layout` names a `session` stanza that has no name `<anon>` --
# a bare `session` keyword with nothing after it, which Isabelle rejects
# (`error: bad input`).  It is the *absence* of a name, so it is dropped
# here rather than treated as one.
#
# This is not hypothetical for the fragment reader: the tokeniser's
# identifier class excludes `#`, so a line like `# not a session` reduces to
# the keyword alone, and prose reaching a hunk boundary does that easily.
# Letting it through would put an `<anon> -> some/directory` entry into an
# attribution map, where a name that cannot be built has no business.
_ANON = "<anon>"


def _named(sessions) -> list[str]:
    return [s.name for s in sessions if s.name != _ANON]


def sessions_in(root_file: Path) -> list[str]:
    """Sessions a ROOT file declares, in file order.  Unreadable → none."""
    try:
        return _named(parse_root_sessions(Path(root_file)))
    except OSError:
        return []


def sessions_in_fragment(lines: list[str]) -> list[str]:
    """Sessions named in a fragment of a ROOT — a captured diff hunk's worth.

    `lines` are ROOT source lines with their diff prefixes already stripped.
    """
    text = "\n".join(lines)
    if "session" not in text:            # the overwhelming majority of hunks
        return []
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "ROOT"
        root.write_text(text)
        return _named(parse_root_sessions(root))
