"""The code-vs-doc classifier, and what counts as an attempt.

These two decide the headline statistic.  Many attempts change only prose and
build green by construction, so counting them inflates the one-shot rate and
flattens the length histogram -- the classifier is the filter that stops a
documentation pass from reading as a proof found first time.

It is a state machine over Isabelle's document cartouches, run against diff
hunks rather than whole files, so it has to *seed* its state from a hunk
header and can be wrong.  That is why it has a resync rule, and why it needs
tests over hunks rather than over files.
"""
from __future__ import annotations

import pytest

from helpers import make_record
from isabelle_watchdog import attempts as A


def hunk(path: str, context: str, lines: list[str]) -> str:
    body = "\n".join(lines)
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,{len(lines)} +1,{len(lines)} @@ {context}\n{body}\n")


# ------------------------------------------------------------ prose vs proof

def test_editing_a_lemma_is_code():
    diff = hunk("thy/A.thy", "begin", [
        " begin",
        '-lemma x: "True" by simp',
        '+lemma x: "True" by auto',
        " end",
    ])
    verdict, _ = A.classify(diff)
    assert verdict == "code"


def test_editing_only_a_document_cartouche_is_prose():
    """`text ‹…›` is prose, not proof.  A build that changed only this is
    green by construction and is not evidence that anything was proved."""
    diff = hunk("thy/A.thy", "", [
        " text \\<open>",
        "-Some prose about the reduction.",
        "+Some longer prose about the reduction.",
        " \\<close>",
    ])
    verdict, per_file = A.classify(diff)
    assert verdict == "doc", per_file


def test_prose_and_proof_in_one_delta_is_code():
    """The overall verdict is an `any`: an attempt that touched a proof is a
    proof attempt whatever else it touched."""
    diff = (hunk("thy/A.thy", "", [" text \\<open>", "-old prose",
                                   "+new prose", " \\<close>"])
            + hunk("thy/B.thy", "begin", [" begin", '-by simp', '+by auto']))
    assert A.classify(diff)[0] == "code"


def test_a_comment_is_not_code():
    diff = hunk("thy/A.thy", "begin", [
        " begin",
        "-(* an old aside *)",
        "+(* a new aside *)",
    ])
    assert A.classify(diff)[0] == "doc"


@pytest.mark.parametrize("path", ["paper.tex", "README.md", "refs.bib",
                                  "thy/document/root.tex"])
def test_files_that_are_prose_by_definition_need_no_projection(path):
    assert A.classify_file(path, ["+anything at all"])[0] == "doc"


def test_an_unrecognised_file_type_counts_as_code():
    """Deliberate: anything the classifier does not understand is treated as
    code, so nothing is hidden from the statistics by accident.  A ROOT edit
    is build-relevant and shows up as code for exactly this reason."""
    assert A.classify_file("thy/ROOT", ["+    Probe_B"])[0] == "code"
    assert A.classify_file("Makefile", ["+WALL_TIMEOUT=90"])[0] == "code"


def test_no_diff_at_all_is_neither():
    assert A.classify("") == ("none", [])


def test_the_classifier_recovers_when_its_seed_was_wrong():
    """A hunk starts mid-file with an unknown cartouche state, guessed from
    git's truncated context line.  An Isabelle command at column 0 cannot
    occur inside a cartouche, so meeting one proves the guess wrong and
    resyncs -- without which a whole file after one bad guess reads as
    prose."""
    diff = hunk("thy/A.thy", "text \\<open>", [
        " some trailing prose",
        "-lemma x: \"True\" by simp",
        "+lemma x: \"True\" by auto",
    ])
    assert A.classify(diff)[0] == "code"


def test_the_class_of_a_record_is_memoised():
    rec = make_record(diff=hunk("thy/A.thy", "begin", ["-by simp", "+by auto"]))
    assert A.rec_class(rec) == "code"
    assert rec["_class"] == "code"


def test_keeping_code_deltas_is_the_default_view():
    code = make_record(diff=hunk("thy/A.thy", "begin", ["-by simp", "+by auto"]))
    doc = make_record(diff=hunk("thy/A.thy", "", [" text \\<open>", "-a", "+b",
                                                  " \\<close>"]))
    assert A.keep([code, doc], include_all=False) == [code]
    assert A.keep([code, doc], include_all=True) == [code, doc]


# ------------------------------------------------------- what counts as work

CODE = hunk("thy/A.thy", "begin", ["-by simp", "+by auto"])


def test_a_failure_is_an_attempt_even_with_no_captured_diff():
    """Something was built and it did not compile.

    Before the 2026-07-27 capture fix, 124 failing builds recorded an empty
    diff because the theory being authored was untracked; counting only
    captured deltas scored 23 multi-attempt searches as one-shot.
    """
    assert A.is_attempt(make_record(outcome="fail", diff=""), None) is True


def test_a_green_that_closed_a_failing_run_is_an_attempt():
    """It is the repair, and it is an attempt for the same reason -- even
    when its diff was lost."""
    prev = make_record(outcome="fail")
    assert A.is_attempt(make_record(outcome="ok", diff=""), prev) is True


def test_a_green_after_a_green_with_nothing_changed_is_a_rebuild():
    """And that, and only that, is not an attempt."""
    prev = make_record(outcome="ok")
    assert A.is_attempt(make_record(outcome="ok", diff=""), prev) is False


def test_episode_length_counts_attempts_not_records():
    ep = [make_record(outcome="fail", diff=CODE),
          make_record(outcome="fail", diff=CODE),
          make_record(outcome="ok", diff=CODE)]
    assert A.attempt_length(ep) == 3


def test_a_lone_no_op_rebuild_has_no_length():
    """None, not 1: a rebuild of an unchanged tree entering the histogram as
    a one-shot success is precisely the bookkeeping that inflates the
    headline rate."""
    assert A.attempt_length([make_record(outcome="ok", diff="")]) is None


# ---------------------------------------------------------- proof-bearing runs

@pytest.fixture
def attributed():
    """A corpus-level attribution, installed the way `fit_attribution` would."""
    return A.use_attribution(A.Attribution(dirs={"thy"}, targets={}, aliases={}))


def test_a_theory_edit_makes_an_episode_proof_bearing(attributed):
    assert A.proof_bearing([make_record(outcome="ok", diff=CODE)]) is True


def test_registering_a_theory_in_a_root_does_not(attributed):
    """A bare ROOT edit is `code` by the treat-the-unknown-as-code rule and
    builds green by construction.  Without this filter it enters the
    histogram as a one-shot success that had no proof to get wrong."""
    root = hunk("thy/ROOT", "", ["+    Probe_B"])
    assert A.proof_bearing([make_record(outcome="ok", diff=root)]) is False


def test_a_timeout_is_proof_bearing_whatever_it_touched(attributed):
    """Build furniture cannot time out: registering a theory in a ROOT does
    not take 40 seconds, and a build the watchdog had to kill was
    demonstrably deep in elaboration.  It is also the one kind of evidence
    that cannot reintroduce the bias being guarded against, a timeout being
    by definition not a green."""
    ep = [make_record(outcome="timeout", diff="", timeout_reason="wall")]
    assert A.proof_bearing(ep) is True


def test_an_error_locus_is_proof_bearing_with_no_diff_at_all(attributed):
    """The case the whole ladder exists for: a failure that named a line, and
    a capture that saw nothing."""
    ep = [make_record(outcome="fail", diff="",
                      error_loci=[["thy/A.thy", "12"]])]
    assert A.proof_bearing(ep) is True


def test_a_view_that_needs_attribution_without_fitting_fails_loudly():
    """Rather than labelling everything from an empty map, which is how the
    whole 43sp corpus came to be filed under 'tooling'."""
    with pytest.raises(RuntimeError, match="has not been fitted"):
        A.proof_bearing([make_record(outcome="ok", diff="")])
