"""Note parsing, prediction extraction, and the advisory linter.

`expect:` is the one field in a build corpus that scores itself, so the parse
that finds it is load-bearing for a published statistic: every note that fails
to parse silently *lowers* the measured prediction rate rather than erroring.
These are pure functions over text, which makes them the cheapest place in the
package to be thorough.
"""
from __future__ import annotations

import pytest

from isabelle_watchdog import record as R


# ------------------------------------------------------------------- parsing

def test_inline_and_multiline_forms_agree():
    """`-m 'a: x; b: y'` must parse as the three-line form.

    The one-liner is the common case -- newlines are awkward to type at a
    shell and easy to mangle through one -- so the two spellings existing is
    not optional, and them disagreeing would mean the corpus's note structure
    depended on how the operator typed it.
    """
    inline = R._parse_note("diagnosis: too weak; change: generalise; expect: ok")
    block = R._parse_note("diagnosis: too weak\nchange: generalise\nexpect: ok")
    assert inline == block
    assert inline == {"diagnosis": "too weak", "change": "generalise",
                      "expect": "ok"}


def test_semicolon_in_prose_is_not_a_separator():
    """The split only fires before a recognised key.

    Without the lookahead, `change: swap blast; it was too slow` would lose
    everything after the semicolon into a section that does not exist.
    """
    got = R._parse_note("change: swap blast for auto; it was too slow")
    assert got == {"change": "swap blast for auto; it was too slow"}


def test_a_section_runs_until_the_next_key():
    got = R._parse_note("diagnosis: the induction\n  is too weak\n"
                        "  over the tape index\nexpect: fail")
    assert got["diagnosis"] == "the induction\n  is too weak\n  over the tape index"
    assert got["expect"] == "fail"


def test_free_prose_is_captured_whole():
    """A format that rejects free prose collects nothing."""
    assert R._parse_note("just trying something") == {"notes": "just trying something"}


def test_text_before_the_first_key_is_kept():
    got = R._parse_note("some preamble\nchange: the thing")
    assert got == {"notes": "some preamble", "change": "the thing"}


def test_an_empty_note_parses_to_nothing():
    assert R._parse_note("") is None
    assert R._parse_note("   \n\n  ") is None


@pytest.mark.parametrize("line", [
    "expect: ok",
    "EXPECT: ok",
    "- expect: ok",
    "## expect: ok",
    "  expect:ok",
])
def test_key_spellings_a_human_actually_writes(line):
    """Case, markdown markers and spacing are all tolerated -- the parse is a
    convenience for querying, and a note that fails to parse is data lost for
    no gain."""
    assert (R._parse_note(line) or {}).get("expect") == "ok"


# ---------------------------------------------------------------- prediction

@pytest.mark.parametrize("expect,want", [
    ("ok", "ok"),
    ("fail — the tape index is still free", "fail"),
    ("timeout: this one is slow", "timeout"),
    ("OK", "ok"),
    ("`ok`", "ok"),                       # leading non-word characters skipped
])
def test_a_prediction_is_read_from_the_head_of_expect(expect, want):
    assert R._predicted_outcome({"expect": expect}) == want


@pytest.mark.parametrize("expect", [
    "no timeout this time, so ok",        # the case the anchor exists for
    "probably fine",
    "",
])
def test_a_buried_expectation_scores_as_unpredicted(expect):
    """An unscored note is better than a miscounted one.

    Searching the whole section would read "no timeout this time" as
    predicting a timeout, which is the opposite of what was written -- and a
    wrong entry in a calibration statistic is worse than a missing one.
    """
    assert R._predicted_outcome({"expect": expect}) is None


def test_an_outcome_named_outside_expect_is_not_a_prediction():
    fields = R._parse_note("diagnosis: this fails because the goal is false")
    assert R._predicted_outcome(fields) is None


def test_no_note_predicts_nothing():
    assert R._predicted_outcome(None) is None


# --------------------------------------------------------------------- lint

def test_lint_flags_a_near_miss_key():
    """`expects:` becomes prose, which looks exactly like omitting the
    section -- the failure this check exists to make visible."""
    out = R.lint_note("expects: ok")
    assert any("did you mean" in c and "expect" in c for c in out), out


def test_lint_flags_a_note_with_no_sections():
    out = R.lint_note("tried something")
    assert any("no recognised section" in c for c in out), out


def test_lint_flags_a_missing_prediction():
    out = R.lint_note("change: swap blast for auto")
    assert any("no `expect:`" in c for c in out), out


def test_lint_flags_an_unscoreable_prediction():
    out = R.lint_note("change: x\nexpect: probably fine")
    assert any("cannot be scored" in c for c in out), out


def test_a_well_formed_note_lints_clean():
    assert R.lint_note("diagnosis: X; change: Y; expect: ok — Z") == []


def test_lint_never_raises_on_hostile_input():
    """The linter runs on the way to a build, so an input it cannot parse
    must produce complaints, not an exception -- see `lint_note`: refusing a
    build teaches the operator to route around the recorded entry point, and
    crashing does it faster."""
    for text in ("", ":", "::::", "a" * 10_000, "\x00\n:\n", "expect:\n"):
        assert isinstance(R.lint_note(text), list)
