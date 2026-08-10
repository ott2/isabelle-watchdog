"""What a ROOT declares, and the contract with `isabelle-layout`.

`roots.py` is a 15-line reader of session *names*, deliberately not an import
of the real parser: this package declares no runtime dependencies because it
runs beside a build, and anything it imports is something that can break one
(an unsupervised, unrecorded build is what the watchdog exists to prevent).

That buys independence and costs divergence, so agreement is pinned here
instead.  `isabelle-layout` ships its conformance corpus as **package data**
for precisely this arrangement -- a consumer that cannot import it at runtime
can still prove it agrees -- and this module consumes that artefact rather
than a copy of it.

**The copy is what went wrong before.** These cases used to be eight literals
transcribed into this file, produced by diffing against
`isabelle_query.common.parse_root_sessions` -- one parser checked against
another, with no Isabelle anywhere near it.  Four of the eight were ROOTs
`isabelle build` *refuses*:

    session "Probe (AFP)" = HOL +      error: session ... (line 1)
    session With.Dots-2 = HOL +        error: keyword ... (line 1)
    session A = HOL +                  *** Duplicate use of directory
    session B = A +
    session A = HOL +                  error: bad input (line 2)
    (* oops

So the table asserted things about Isabelle that Isabelle does not do, and the
docstring it justified told a story -- a session *built* as `Probe (AFP)` and
attributed as `Probe` -- that could not have happened, because that ROOT does
not build.  The underlying defect was real and the illustration was not; see
`roots.py`, where it now uses the spelling Isabelle accepts.

The corpus keeps the distinction the transcription lost.  A case Isabelle
`accepts` has a ground truth and any parser that disagrees is wrong.  A case
it `rejects` has none, and exists only so that readers agree on malformed
input rather than each inventing a different silent renaming.  Both are
checked, for different reasons and with different force.

If Isabelle's ROOT syntax moves, `isabelle-layout` regenerates that corpus
against the new release and this is what fails.
"""
from __future__ import annotations

import pytest

from isabelle_watchdog import roots

# Scoped to the checks that need it, rather than `importorskip` at module
# level: the comment, fragment and line-structure tests below are about *this*
# parser and need no reference at all, and skipping them along with the corpus
# would quietly halve what a contributor without the sibling package runs.
try:
    import isabelle_layout as layout
    from isabelle_layout import conformance
    CASES = conformance.cases()
except ImportError:                      # pragma: no cover - environment
    layout = conformance = None
    CASES = []

needs_layout = pytest.mark.skipif(
    layout is None,
    reason="needs isabelle-layout: `pip install ../isabelle-layout`, or "
           "`.[conformance]` once it is published.  Without it the ROOT name "
           "grammar is checked against nothing, which is the state this "
           "module exists to end")

ACCEPTED = [c for c in CASES if c["isabelle"] == "accepts"]
REJECTED = [c for c in CASES if c["isabelle"] != "accepts"]


def ids(cases):
    return [c["id"] for c in cases]


# ------------------------------------------------------------- conformance

@needs_layout
@pytest.mark.parametrize("case", ACCEPTED, ids=ids(ACCEPTED))
def test_a_root_isabelle_accepts_is_read_the_way_isabelle_reads_it(case):
    """Ground truth, so a disagreement here is a defect and not a difference.

    `why` on each case says what it would catch; it is printed on failure
    because a bare "expected [X] got [Y]" over a two-line ROOT is not enough
    to tell whether the fix is in the parser or in the expectation.
    """
    want = conformance.session_names(case)
    got = roots.sessions_in_text(case["root"])
    assert got == want, f"{case['id']}: {case['why']}\n{case['root']!r}"


@needs_layout
@pytest.mark.parametrize("case", REJECTED, ids=ids(REJECTED))
def test_a_root_isabelle_refuses_is_read_the_way_the_reference_reads_it(case):
    """No ground truth -- `isabelle build` would not accept this at all.

    Checked anyway, and this is the weaker claim of the two: the corpus
    records what `isabelle-layout` does, so agreeing means the two readers
    fail the same way rather than silently producing two different names for
    input neither can build.  A change here is a decision, not a bug.
    """
    want = conformance.session_names(case)
    got = roots.sessions_in_text(case["root"])
    assert got == want, f"{case['id']}: {case['why']}\n{case['root']!r}"


@needs_layout
def test_the_corpus_is_checked_against_a_real_isabelle():
    """The corpus's value is its provenance, so a consumer should assert it
    has some.  A fixture that stopped being regenerated would otherwise go on
    passing forever while Isabelle moved underneath it."""
    meta = conformance.load()
    assert meta["verified_against"].startswith("Isabelle")
    assert ACCEPTED and REJECTED, "both kinds of case must be present"


@needs_layout
def test_the_two_parsers_agree_directly_not_only_via_the_corpus(tmp_path):
    """The corpus is a fixed set; this is the live comparison.

    They are different checks: the corpus survives `isabelle-layout` being
    wrong (its cases carry Isabelle's own verdict), and this one survives the
    corpus being incomplete.  Neither subsumes the other.

    Through the file-taking public entry point, which is the one a consumer
    would call -- `sessions_in` is this package's equivalent, and the pair is
    what has to agree.
    """
    for case in CASES:
        root = tmp_path / case["id"] / "ROOT"
        root.parent.mkdir(parents=True, exist_ok=True)
        root.write_text(case["root"])
        assert roots.sessions_in(root) == \
            [s.name for s in layout.parse_root_sessions(root)], case["id"]


# ------------------------------------------------------------------ comments

def test_a_nested_comment_closes_at_the_outer_end():
    """Isabelle's block comments nest.  A non-greedy `.*?` closes at the first
    `*)` and re-exposes the tail of the outer comment, which would resurrect
    exactly the declaration that was commented out."""
    text = "(* outer (* inner *)\nsession Ghost = HOL +\n*)\nsession Live = HOL +\n"
    assert roots.sessions_in_text(text) == ["Live"]


def test_an_unterminated_comment_swallows_the_rest():
    """Isabelle rejects this outright (`error: bad input`), so there is no
    behaviour to conform to -- only a choice.  Swallowing to end of file is
    the same choice `isabelle-layout` makes, and it beats the alternative:
    guessing otherwise would mean a typo'd `(*` yields a session list nobody
    can build, silently."""
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


@needs_layout
def test_the_two_readers_share_one_name_grammar():
    """The property that was broken.  Whole-file and line-wise reading differ
    on *comments* by design; differing on what a session is *called* meant
    building one name and attributing another."""
    for case in CASES:
        named = [n for n in
                 (roots.session_in_line(l) for l in case["root"].splitlines())
                 if n]
        # Every name the whole-file reader keeps is spelt the same way by the
        # line reader; the line reader may additionally see commented ones.
        assert set(roots.sessions_in_text(case["root"])) <= set(named), case["id"]


def test_an_unreadable_root_yields_nothing(tmp_path):
    assert roots.sessions_in(tmp_path / "absent") == []
