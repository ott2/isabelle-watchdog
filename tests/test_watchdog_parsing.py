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
    """Session-qualified, `Probe.Probe_A` and not `Probe_A`.

    This string is the record's `error_head`, read long after the build by
    someone who cannot ask which session it was.  A stale dependency heap
    silently adds its parent sessions to the plan, so the stuck theory need
    not be one the operator has ever opened -- and then the qualifier is the
    only thing that says so (github.com/ott2/isabelle-watchdog#3).
    """
    head = W._timeout_head("loop_progress", ("Probe.Probe_A", "12", "by"),
                           "19.9", 40)
    assert head == 'loop_progress: "by" line 12 of Probe.Probe_A (19.9s)'


def test_a_bare_wall_timeout_has_no_locus_to_name():
    """The case the wall budget exists to cover: nothing ever reported a
    line, so the record says so rather than inventing one."""
    assert W._timeout_head("wall", None, "", 40) == "wall timeout (40s wall)"


def test_the_log_line_leads_with_the_stuck_command(tmp_path):
    """A hang's first question is 'where', and the log path is printed first
    so `make build | head -3` still captures it."""
    line = W._log_line(tmp_path / "b.log", [], ("Probe.Probe_A", "12", "by"))
    assert line.endswith("(stuck at Probe.Probe_A line 12)")


def test_a_wall_kill_says_where_the_build_was_not_that_it_was_stuck(tmp_path):
    """"Stuck" is a claim, and only two of the three kills earn it.

    A loop kill and an activity kill both mean nothing else was happening.  A
    wall kill means only that the clock ran out -- the command may have been
    working perfectly, in a dependency Isabelle was re-elaborating.  Saying
    `stuck at` there is a verdict on a theory that was fine, and it is the
    phrase the report quoted (github.com/ott2/isabelle-watchdog#4).
    """
    key = ("Probe.Probe_A", "12", "by")
    assert W._log_line(tmp_path / "b.log", [], key, "wall").endswith(
        "(last at Probe.Probe_A line 12)")
    for reason in ("loop_progress", "activity"):
        assert W._log_line(tmp_path / "b.log", [], key, reason).endswith(
            "(stuck at Probe.Probe_A line 12)")


# ------------------------------------------------- where the budget actually went

def test_isabelles_own_verb_says_which_sessions_are_dependencies():
    """`Building` vs `Running` is not a turn of phrase: Isabelle stores a
    session's heap exactly when something else in the build depends on it
    (build_process.scala:95), and prints the verb from that flag.  So the
    dependency/target split is Isabelle's answer, read off the pipe, rather
    than ours re-derived from an argv the watchdog may not even have."""
    m = W.SESSION_BUILD_RE.match("Building HOL-Library ...")
    assert m.groups() == ("Building", "HOL-Library")
    m = W.SESSION_BUILD_RE.match("Running FSM_Tests ...")
    assert m.groups() == ("Running", "FSM_Tests")
    # `build_log_verbose`/NUMA add a parenthetical before the `...`
    m = W.SESSION_BUILD_RE.match("Building MTTM (started 0:00:03 on node 1) ...")
    assert m.groups() == ("Building", "MTTM")


@pytest.mark.parametrize("line", [
    "Running out of ideas",                       # no trailing `...`
    "Probe: theory Probe.Building 100%",          # not at the start
    "Session AFP/MTTM",                           # the plan, not a start
])
def test_a_line_that_merely_starts_with_the_word_is_not_a_session_start(line):
    assert W.SESSION_BUILD_RE.match(line) is None


def test_the_session_comes_from_the_qualifier_and_is_never_guessed():
    """#3 made the qualifier unconditional for display; this is what it buys
    beyond display.  An unqualified name cannot answer 'which session', so
    the honest answer is nothing at all -- a guessed session in a diagnosis
    is worse than an absent one."""
    assert W._stuck_session(("FSM_Tests.Util", "1650", "by")) == "FSM_Tests"
    assert W._stuck_session(None, "FSM_Tests.Util") == "FSM_Tests"
    assert W._stuck_session(None, "Util") == ""
    assert W._stuck_session(None, "") == ""


def test_a_timeout_inside_a_dependency_says_so_first():
    """The report's case: a 20s budget spent re-elaborating an ancestor, and
    a summary naming line 444 of a theory the operator does not own.  What
    they could not reconstruct is that a second session was involved at all,
    so that is what the note says (github.com/ott2/isabelle-watchdog#4)."""
    built = [("Multitape_Alphabet_Enlargement", "dependency", 0.4),
             ("Nondeterministic_Time_Hierarchy", "target", 55.1)]
    assert W._budget_note(built, "Multitape_Alphabet_Enlargement") == (
        "the budget went on rebuilding dependency "
        "Multitape_Alphabet_Enlargement, not on the session you asked for")


def test_a_timeout_in_your_own_session_still_names_what_came_first():
    """Not the report's case, but the same budget: the clock reached your
    session having already spent itself elsewhere, and the summary should
    not leave you to work that out from the log."""
    built = [("MTTM", "dependency", 0.4), ("Mine", "target", 30.0)]
    assert W._budget_note(built, "Mine") == "rebuilt from source first: MTTM"


@pytest.mark.parametrize("built,stuck", [
    ([], "Mine"),                              # nothing observed -- say nothing
    ([("Mine", "target", 0.2)], "Mine"),       # one session, and it is yours
    ([("Mine", "target", 0.2)], ""),           # no qualifier to reason from
])
def test_nothing_is_added_when_there_is_nothing_to_add(built, stuck):
    """A note that fires on an ordinary single-session timeout is noise, and
    noise beside a kill is what stops the useful notes being read."""
    assert W._budget_note(built, stuck) == ""


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


def test_power_state_is_unknown_on_an_unsupported_platform(monkeypatch):
    """Guessing would be worse than not scaling: a wrongly-applied factor
    doubles every budget and hides a real regression."""
    monkeypatch.setattr(W.sys, "platform", "sunos5")
    assert W.on_battery() is None


# ------------------------------------------------------------- power, on Linux

def supplies(root, **kinds):
    """Build a fake /sys/class/power_supply."""
    for name, (typ, online) in kinds.items():
        d = root / name
        d.mkdir(parents=True)
        (d / "type").write_text(typ + "\n")
        if online is not None:
            (d / "online").write_text(str(online) + "\n")
    return root


def test_linux_mains_offline_is_battery(tmp_path):
    supplies(tmp_path, AC=("Mains", 0), BAT0=("Battery", None))
    assert W._on_battery_linux(tmp_path) is True


def test_linux_mains_online_is_ac(tmp_path):
    supplies(tmp_path, AC=("Mains", 1), BAT0=("Battery", None))
    assert W._on_battery_linux(tmp_path) is False


def test_a_desktop_with_no_mains_supply_is_unknown(tmp_path):
    """"No battery" and "on battery" must not be confused, and a machine that
    cannot run on battery needs no scaling either way."""
    supplies(tmp_path)
    assert W._on_battery_linux(tmp_path) is None


def test_no_sysfs_at_all_is_unknown(tmp_path):
    assert W._on_battery_linux(tmp_path / "nope") is None


# --------------------------------------------------------------- contention
#
# The measurement that load average could not provide: a dimensionless duty
# cycle, so a threshold expressed in it is not fitted to one machine.

@pytest.mark.parametrize("field, secs", [
    ("  0:01.22", 1.22),          # macOS
    ("00:00:01", 1.0),            # Linux
    ("12:34.00", 754.0),
    ("01:00:00", 3600.0),
    ("2-03:00:00", 183600.0),     # Linux, days
    ("", None),
    ("garbage", None),
])
def test_ps_time_is_read_in_both_platform_formats(field, secs):
    """One parser for both deliberately: a watchdog that silently measured
    nothing on Linux would simply never see contention there."""
    assert W._parse_ps_time(field) == secs


def test_a_duty_cycle_needs_two_samples_far_enough_apart():
    assert W.duty_cycle([]) is None
    assert W.duty_cycle([(0.0, 0.0)]) is None
    assert W.duty_cycle([(0.0, 0.0), (0.5, 0.5)], min_span=2.0) is None


def test_a_duty_cycle_is_cpu_seconds_per_wall_second():
    """Dimensionless by construction -- 1.0 is a whole core on any machine,
    which is the whole reason this is measurable rather than calibrated."""
    assert W.duty_cycle([(0.0, 0.0), (10.0, 10.0)]) == 1.0
    assert W.duty_cycle([(0.0, 0.0), (10.0, 2.5)]) == 0.25
    assert W.duty_cycle([(0.0, 5.0), (10.0, 45.0)]) == 4.0    # a parallel build


def test_a_duty_cycle_is_measured_over_the_window_it_is_given():
    """The window matters because the question differs by kill condition: a
    build that ran flat out for a minute and then hung has a healthy whole-run
    duty cycle and a dead recent one."""
    ran_then_hung = [(0.0, 0.0), (30.0, 30.0), (40.0, 30.0)]
    assert W.duty_cycle(ran_then_hung) == 0.75                # whole run: fine
    assert W.duty_cycle(ran_then_hung[1:]) == 0.0             # recent: stopped


@pytest.mark.parametrize("duty, verdict, factor", [
    (None, "unknown", 1.0),
    (0.0, "stalled", 1.0),
    (0.01, "stalled", 1.0),
    (0.5, "starved", 2.0),
    (0.25, "starved", 4.0),
    (0.1, "starved", 4.0),        # capped
    (0.95, "running", 1.0),       # a healthy single-threaded build, jitter and all
    (1.0, "running", 1.0),
    (3.9, "running", 1.0),        # a healthy -j4 build
])
def test_the_three_regimes(duty, verdict, factor):
    assert W.contention(duty, cap=4.0) == (verdict, factor)


def test_a_stalled_tree_earns_no_extension():
    """The case that makes this safe.

    1/duty for a hung process is unbounded, so a naive factor would hand a
    deadlock four times its deadline -- the opposite of the point.  No CPU is
    not slowness, it is absence of work, and no amount of extra time fixes it.
    """
    _, factor = W.contention(0.0, cap=4.0)
    assert factor == 1.0


def test_a_build_running_flat_out_keeps_its_budget():
    """The property the cost-regression signal rests on: a proof that got
    genuinely more expensive burns CPU at full rate, so it is never mistaken
    for a starved one and still trips its budget on time."""
    assert W.contention(1.0, cap=4.0)[1] == 1.0


def test_a_whole_core_is_not_measured_as_exactly_one():
    """Nothing is scheduled for 100.0% of wall time, and the sampler lands
    where a 1-second poll allows.  A strict boundary would label every healthy
    single-threaded build `starved` -- harmless in budget terms, and a
    systematically wrong label on most of the corpus."""
    assert W.contention(0.96, cap=4.0) == ("running", 1.0)


def test_the_cap_is_honoured():
    """A budget that stretches without limit is not a budget."""
    assert W.contention(0.1, cap=2.0)[1] == 2.0    # 1/duty would be 10
    assert W.contention(0.5, cap=1.0)[1] == 1.0    # cap 1.0 disables entirely
