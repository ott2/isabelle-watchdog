"""What a ROOT declares, and the contract with `isabelle-query`.

The cases below were produced by diffing this package against
`isabelle_query.common.parse_root_sessions` — a real tokenizing parser, in a
package this one deliberately does not import (see `roots.py` for why: no
runtime dependencies, because the watchdog runs beside a build).

That decision buys independence and costs divergence, so this table *is* the
agreement. It found two live disagreements when it was first written: a
quoted session name truncated at its first space, and a bare one truncated at
its first `.` or `-` — meaning a session built as `Probe (AFP)` was recorded
against a session named `Probe`, with nothing downstream able to tell.

If Isabelle's ROOT syntax moves, this is what should fail.
"""
from __future__ import annotations

import pytest

from isabelle_watchdog import roots


# (source, what isabelle-query's parser returns).  Verified against it, not
# invented: `python /tmp/cmp_root.py` in the session that added this module.
CONFORMANCE = [
    ("plain",
     "session Probe = HOL +\n  theories\n    X\n",                ["Probe"]),
    ("quoted, with spaces and parens",
     'session "Probe (AFP)" = HOL +\n',                           ["Probe (AFP)"]),
    ("dotted and hyphenated",
     "session With.Dots-2 = HOL +\n",                             ["With.Dots-2"]),
    ("an `in` clause",
     "session Probe in sub = HOL +\n",                            ["Probe"]),
    ("commented out on one line",
     "(* session Retired = HOL + *)\nsession Live = HOL +\n",     ["Live"]),
    ("commented out across lines",
     "(*\nsession Old = HOL +\n*)\nsession Live = HOL +\n",       ["Live"]),
    ("the word `session` inside a description cartouche",
     "session Probe = HOL +\n"
     "  description \\<open>session Ghost = X\\<close>\n",        ["Probe"]),
    ("two real declarations",
     "session A = HOL +\n\nsession B = A +\n",                    ["A", "B"]),
]


@pytest.mark.parametrize("label, text, want",
                         CONFORMANCE, ids=[c[0] for c in CONFORMANCE])
def test_agrees_with_isabelle_query(label, text, want):
    assert roots.sessions_in_text(text) == want


# ------------------------------------------------------------------ comments

def test_a_nested_comment_closes_at_the_outer_end():
    """Isabelle's block comments nest.  A non-greedy `.*?` closes at the first
    `*)` and re-exposes the tail of the outer comment, which would resurrect
    exactly the declaration that was commented out."""
    text = "(* outer (* inner *)\nsession Ghost = HOL +\n*)\nsession Live = HOL +\n"
    assert roots.sessions_in_text(text) == ["Live"]


def test_an_unterminated_comment_swallows_the_rest():
    """What Isabelle does.  Guessing otherwise would mean a typo'd `(*` yields
    a session list nobody can build."""
    assert roots.sessions_in_text("session A = HOL +\n(* oops\nsession B = A +\n") \
        == ["A"]


def test_blanking_keeps_the_line_structure():
    """Content becomes spaces, newlines survive: matching downstream is
    line-oriented and has to see the file's shape."""
    text = "session A = HOL +\n(*\nx\n*)\nsession B = A +\n"
    assert roots.strip_comments(text).count("\n") == text.count("\n")


# ----------------------------------------------------- fragments versus files

def test_a_line_is_matched_without_its_context():
    """`attempts.py` reads added and context lines out of a captured diff
    hunk, where an enclosing `(*` may never have been in the payload.  It
    cannot strip what it cannot see."""
    assert roots.session_in_line("session Probe = HOL +") == "Probe"
    assert roots.session_in_line("  theories") is None
    assert roots.session_in_line("chapter Foo") is None


def test_the_two_readers_share_one_name_grammar():
    """The property that was broken.  Whole-file and line-wise reading differ
    on *comments* by design; differing on what a session is *called* meant
    building one name and attributing another."""
    for _label, text, _want in CONFORMANCE:
        lines = [roots.session_in_line(l) for l in text.splitlines()]
        named = [n for n in lines if n]
        # Every name the whole-file reader keeps is spelt the same way by the
        # line reader; the line reader may additionally see commented ones.
        assert set(roots.sessions_in_text(text)) <= set(named)


def test_an_unreadable_root_yields_nothing(tmp_path):
    assert roots.sessions_in(tmp_path / "absent") == []
