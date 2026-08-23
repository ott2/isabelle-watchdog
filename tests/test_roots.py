"""What this project needs from `isabelle-layout`, and the fragment adapter.

`roots.py` no longer holds a ROOT grammar -- there is one parser now, and it
is `isabelle_layout`'s. So this is not a conformance suite: whether that
parser matches Isabelle is its own repository's problem, checked there against
a corpus it regenerates from a real `isabelle sessions -d`. Duplicating that
here would mean asserting a dependency's behaviour back at it, and failing
whenever it legitimately improved.

What is left is genuinely local, and it is two things.

**A fragment is not a file.** `attempts.py` reads the added and context lines
of a hunk in a ROOT, which is a ROOT with holes in it, and the public entry
point takes a path. Everything about that seam belongs here.

**The names this project acts on.** `build.py` builds what a ROOT declares and
`attempts.py` attributes records to it, so the two must agree about spelling
-- they once did not, and `session "HOL-Analysis"` was built under that name
and attributed to `HOL`. A handful of cases pin the shapes real projects use.
They are a statement of requirements, not a re-derivation of the grammar.
"""
from __future__ import annotations

import pytest

from isabelle_watchdog import roots


# The spellings the two real projects and the AFP actually contain.  If one of
# these ever changes meaning, something here builds or attributes the wrong
# session, so the assertion is about *this* package regardless of where the
# parsing happens.
REQUIRED = [
    ("plain",            "session Probe = HOL +\n",            ["Probe"]),
    ("quoted",           'session "MHComputation" = HOL +\n',  ["MHComputation"]),
    # The defect that forced one parser: `[A-Za-z0-9_']+` stopped inside the
    # quotes, and `HOL-Analysis` is a session that exists.
    ("quoted-hyphen",    'session "HOL-Analysis" = HOL +\n',   ["HOL-Analysis"]),
    ("quoted-dots",      'session "With.Dots-2" = HOL +\n',    ["With.Dots-2"]),
    ("in-subdir",        "session Probe in sub = HOL +\n",     ["Probe"]),
    ("groups",           "session Probe (slow) = HOL +\n",     ["Probe"]),
    ("qualified-parent", 'session Probe = "HOL-Library" +\n',  ["Probe"]),
    ("two",              "session A = HOL +\n\nsession B in sub = A +\n",
                                                               ["A", "B"]),
    ("chapter-first",    "chapter AFP\n\nsession Probe = HOL +\n", ["Probe"]),
    ("commented-out",    "(* session Retired = HOL + *)\nsession Live = HOL +\n",
                                                               ["Live"]),
    ("no-session",       "chapter_definition AFP\n",           []),
]


@pytest.mark.parametrize("label, text, want", REQUIRED,
                         ids=[c[0] for c in REQUIRED])
def test_a_root_file_yields_the_names_this_project_acts_on(tmp_path, label,
                                                           text, want):
    root = tmp_path / "ROOT"
    root.write_text(text)
    assert roots.sessions_in(root) == want


def test_an_unreadable_root_yields_nothing(tmp_path):
    assert roots.sessions_in(tmp_path / "absent") == []


# ------------------------------------------------------- fragments, not files

@pytest.mark.parametrize("label, text, want", REQUIRED,
                         ids=[c[0] for c in REQUIRED])
def test_a_whole_root_read_as_a_fragment_reads_the_same(label, text, want):
    """The seam must not change the answer.  A hunk covering a whole ROOT is
    the case where the two readers are asked the identical question."""
    assert roots.sessions_in_fragment(text.splitlines()) == want


def test_a_comment_inside_the_fragment_hides_what_it_encloses():
    """Better than the line-wise reader this replaced, which saw through it.

    Parsing the hunk as a unit is what buys this: the `(*` is *in* the
    payload, so there is no reason to guess.
    """
    assert roots.sessions_in_fragment(
        ["(*", "session Old = HOL +", "*)", "session Live = HOL +"]) == ["Live"]


def test_a_comment_opened_outside_the_fragment_cannot_be_seen():
    """The limit, and it is a property of diffs rather than of parsing: the
    enclosing `(*` was never captured, so nothing can know it was there.

    A commented-out session therefore still maps its name to a directory.
    That costs an unused entry in an attribution map, against losing the
    mapping entirely -- which is the trade `attempts.py` documents.
    """
    assert roots.sessions_in_fragment(["session Old = HOL +", "*)"]) == ["Old"]


def test_a_nameless_session_stanza_is_not_a_name():
    """`isabelle_layout` calls a `session` keyword with nothing after it
    `<anon>`.  Isabelle rejects that outright, so it is the absence of a name
    and must not reach an attribution map as one.

    Reached more easily than it looks: the tokeniser's identifier class
    excludes `#`, so `# not a session` reduces to the bare keyword, and prose
    landing on a hunk boundary does that without trying.
    """
    assert roots.sessions_in_fragment(["# not a session"]) == []
    assert roots.sessions_in_fragment(["session"]) == []


def test_a_fragment_with_no_declaration_yields_nothing():
    assert roots.sessions_in_fragment(["  theories", "    X", "chapter Foo"]) == []
    assert roots.sessions_in_fragment([]) == []


# --------------------------------------------------- what a session sits on

def test_a_parent_is_read_from_the_declaration(tmp_path):
    """`parents_in` answers "what does this ROOT declare *on top of*", which
    is the edge Isabelle's `store_heap` rule runs over."""
    root = tmp_path / "ROOT"
    root.write_text("session Base = HOL +\n\nsession Upper = Base +\n")
    assert roots.parents_in(root) == {"Base": "HOL", "Upper": "Base"}


def test_a_quoted_parent_keeps_its_punctuation(tmp_path):
    """Same defect as `sessions_in`'s `HOL-Analysis` case, on the other side
    of the `=`: a parent read as `HOL` names a different real session."""
    root = tmp_path / "ROOT"
    root.write_text('session Probe = "HOL-Analysis" +\n')
    assert roots.parents_in(root) == {"Probe": "HOL-Analysis"}


def test_a_session_with_no_parent_is_left_out_rather_than_mapped_to_none(
        tmp_path):
    """Every caller asks "who descends from X".  An entry that can never
    answer that is noise in the lookup, not information."""
    root = tmp_path / "ROOT"
    root.write_text("session Orphan\n")
    assert roots.parents_in(root) == {}


def test_an_unreadable_root_yields_no_parents(tmp_path):
    assert roots.parents_in(tmp_path / "absent" / "ROOT") == {}
