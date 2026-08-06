"""What the watchdog reads out of Isabelle's output, and what it prints back.

Everything here is a pure function over text.  That is worth saying, because
it is why they can be tested at all: the supervision loop's judgements are
*derived* from these, so a corpus full of missing error loci and a build that
hangs without naming a line are both, ultimately, a regex not matching a
string.

`test_isabelle_integration.py` checks the other half -- whether Isabelle still
emits the strings these assume.  Neither test replaces the other.
"""
from __future__ import annotations

import pytest

from isabelle_watchdog import watchdog as W


# ------------------------------------------------ the injected progress threshold

def test_the_threshold_lands_immediately_after_build():
    """Right after the subcommand, so a later `--` or a session name cannot
    swallow it."""
    got = W.inject_progress_threshold(["isabelle", "build", "-d", "t", "S"], 15)
    assert got == ["isabelle", "build", "-o", "build_progress_threshold=15",
                   "-d", "t", "S"]


def test_an_absolute_launcher_is_still_recognised():
    """`isabelle` is often reached by full path (`~/.local/bin/isabelle`), and
    matching the whole argv[0] against the literal string would silently skip
    injection there -- costing the stuck line on every hang, with no error."""
    got = W.inject_progress_threshold(["/Applications/Isabelle.app/bin/isabelle",
                                       "build", "S"], 15)
    assert got[2:4] == ["-o", "build_progress_threshold=15"]


@pytest.mark.parametrize("argv", [
    ["isabelle", "console"],                      # not a build
    ["sh", "-c", "echo hi"],                      # not isabelle
    ["isabelle"],                                 # nothing to inject into
    [],
])
def test_anything_that_is_not_an_isabelle_build_is_left_alone(argv):
    assert W.inject_progress_threshold(argv, 15) == argv


@pytest.mark.parametrize("value,want", [
    (15, "15"), (15.0, "15"), (30.0, "30"), (22.5, "22.5"),
])
def test_the_threshold_is_formatted_without_a_stray_decimal(value, want):
    """`%g`, because battery scaling makes this a float.  Isabelle takes
    `build_progress_threshold=30.0` too, but the value is echoed into the log
    header and read back by a human comparing it with the constant."""
    got = W.inject_progress_threshold(["isabelle", "build", "S"], value)
    assert got[3] == f"build_progress_threshold={want}"


# ------------------------------------------------------------------ error loci

AT_COMMAND = ('*** At command "by" (line 1441 of '
              '"~/projects/ndtht/t/ar/AlphabetReduction.thy")')
AT_LEMMA = ('*** At command "lemma" (line 41 of '
            '"~/projects/ndtht/t/ae/Wrap_Convention.thy")')


def test_loci_are_deduped_and_keep_first_appearance_order():
    """The parallel checker elaborates every reachable obligation, so one
    failed build reports the same locus repeatedly and several distinct ones
    -- and the FAIL summary shows only the first error block."""
    loci = W._error_loci([AT_COMMAND, "*** something", AT_LEMMA, AT_COMMAND])
    assert loci == [("~/projects/ndtht/t/ar/AlphabetReduction.thy", "1441"),
                    ("~/projects/ndtht/t/ae/Wrap_Convention.thy", "41")]


def test_the_theory_path_is_kept_whole():
    """Shortening here was fine while the only consumer was a terminal and
    wrong once the loci went into the record: the `t/<session>/` prefix is
    exactly what attribution reads, and 11 base names have lived in more than
    one session directory."""
    (path, _), = W._error_loci([AT_COMMAND])
    assert path.endswith("t/ar/AlphabetReduction.thy")


def test_output_with_no_markers_yields_no_loci():
    assert W._error_loci(["*** Failed to finish proof:", "*** goal (1 subgoal):"]) == []
    assert W._count_error_loci([]) == 0


# ------------------------------------------------------------------ error head

def test_the_head_keeps_the_first_two_error_lines():
    head = W._first_error(["Session Probe", "*** Undefined constant: \"foo\"",
                           "*** At command \"lemma\" (line 4)", "*** third"])
    assert head == 'Undefined constant: "foo" | At command "lemma" (line 4)'


def test_blank_error_lines_do_not_count():
    """`***` on its own is padding in Isabelle's output; letting it fill one
    of the two slots would spend half the head on nothing."""
    assert W._first_error(["*** ", "***", "*** real one"]) == "real one"


def test_a_clean_build_has_no_head():
    assert W._first_error(["Session Probe", "Finished Probe"]) == ""


# --------------------------------------------------------------- session names

@pytest.mark.parametrize("argv,want", [
    (["isabelle", "build", "-d", "t", "-o", "x=1", "-v", "S1", "S2"], ["S1", "S2"]),
    (["isabelle", "build", "S"], ["S"]),
    (["isabelle", "build", "-j", "4"], []),
    (["isabelle", "console"], []),                # no `build` at all
])
def test_session_names_skip_flags_and_their_values(argv, want):
    """These names drive `isabelle build_log -H Error`, which is the
    authoritative error source -- the console stream elides long messages.
    Mistaking `4` for a session name would query a session that does not
    exist and silently fall back to the elided text."""
    assert W._session_names(argv) == want


# ------------------------------------------------------------- the loop warning

def test_the_loop_warning_parses_into_a_triple():
    """The one line that names the stuck command.  Its elapsed field changes
    on every emission and the triple does not, which is what makes
    'consecutive matches' mean 'stuck' rather than 'still working'."""
    m = W.LOOP_RE.match('Probe: command "by" running for 25.674s '
                        '(line 1488 of theory "Probe.AlphabetReduction")')
    assert m is not None
    assert m.groups() == ("by", "25.674", "1488", "Probe.AlphabetReduction")


def test_a_progress_line_for_a_different_command_does_not_match_the_loop_shape():
    assert W.LOOP_RE.match("Probe: theory Probe.Probe_A 50%") is None


# ------------------------------------------------------------------- summaries

def test_a_timeout_head_names_the_line_when_one_is_known():
    head = W._timeout_head("loop_progress", ("Probe.Probe_A", "12", "by"),
                           "19.9", 40)
    assert head == 'loop_progress: "by" line 12 of Probe_A (19.9s)'


def test_a_bare_wall_timeout_has_no_locus_to_name():
    """The case the wall budget exists to cover: nothing ever reported a
    line, so the record says so rather than inventing one."""
    assert W._timeout_head("wall", None, "", 40) == "wall timeout (40s wall)"


def test_the_log_line_leads_with_the_stuck_command(tmp_path):
    """A hang's first question is 'where', and the log path is printed first
    so `make build | head -3` still captures it."""
    line = W._log_line(tmp_path / "b.log", [], ("Probe.Probe_A", "12", "by"))
    assert line.endswith("(stuck at Probe_A line 12)")


@pytest.mark.parametrize("lines,tail", [
    ([], ""),
    ([AT_COMMAND], " (1 error locus)"),
    ([AT_COMMAND, AT_LEMMA], " (2 error loci)"),
])
def test_the_log_line_counts_loci_the_summary_did_not_show(tmp_path, lines, tail):
    assert W._log_line(tmp_path / "b.log", lines) == f"log: {tmp_path / 'b.log'}{tail}"


def test_the_fail_summary_prints_the_log_path_before_the_error(capsys, tmp_path):
    """So that `head -N` on a long error block still yields the log path."""
    W._print_summary_fail([AT_COMMAND, "*** Failed to finish proof:"],
                          tmp_path / "b.log")
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("log: ")
    assert out[1].startswith("FAIL  AlphabetReduction.thy:1441")


def test_the_fail_summary_still_says_something_with_no_error_lines(capsys, tmp_path):
    W._print_summary_fail(["some unstructured output"], tmp_path / "b.log")
    assert "FAIL  some unstructured output" in capsys.readouterr().out


def test_success_prints_no_log_path(capsys, tmp_path):
    """Printing it after `OK` invites the eye to look for something to do."""
    W._print_summary_ok(["Probe: theory Probe.Probe_A 100%"], tmp_path / "b.log")
    out = capsys.readouterr().out
    assert out.startswith("OK")
    assert "log:" not in out


# ------------------------------------------------------------------- odds and ends

def test_ansi_escapes_are_stripped_before_matching():
    """Isabelle colours its progress output, and an escape sequence in front
    of `***` would defeat every pattern above."""
    assert W.strip_ansi("\x1b[32m*** At command\x1b[0m") == "*** At command"


def test_power_state_is_unknown_off_macos(monkeypatch):
    """`pmset` is macOS-only, and guessing would be worse than not scaling:
    a wrongly-applied factor doubles every budget and hides a real
    regression."""
    monkeypatch.setattr(W.sys, "platform", "linux")
    assert W.on_battery() is None
