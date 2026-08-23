"""The supervision loop itself, driven by a fake child.

The watchdog does not know it is watching Isabelle.  It watches a pipe and
decides, from the text arriving on it and the clock, whether to let the
process finish or kill its tree -- and which of three diagnoses to record.  A
shell script that prints the right lines at the right times therefore
exercises the real code path, in seconds rather than the minutes a real build
costs, and *without* needing Isabelle installed.

What this cannot check is whether Isabelle still says these things.  That is
`test_isabelle_integration.py`, and the two are complements: this one fails
when the watchdog's logic breaks, that one when Isabelle's output changes.

NOTE: killing a tree ends with `pkill -TERM -f poly` as a safety net for
orphaned Poly/ML processes.  Every timeout test here therefore signals any
process on the machine whose command line contains "poly" -- including an
unrelated interactive Isabelle session.  That is the tool's production
behaviour, not something the tests add.
"""
from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.slow

# Enough output to mark the build as started, so the (shorter) activity budget
# applies rather than the startup grace period.
STARTED = 'echo "Session Probe"; '

# Isabelle's long-running-command warning, verbatim in shape.  The elapsed
# field varies per emission; the (theory, line, command) triple does not, and
# that is what "stuck" means.
def warn(line: int = 12, cmd: str = "by", secs: str = "15.0") -> str:
    return (f'echo \'Probe: command "{cmd}" running for {secs}s '
            f'(line {line} of theory "Probe.Probe_A")\'; ')


# A sibling theory starting: the session progressing somewhere that is not the
# slow command.  Isabelle builds a session's theories in parallel, so this is
# what interleaves with the warnings above on any build bigger than a probe.
def sibling(name: str = "Probe_B") -> str:
    return f'echo "Probe: theory Probe.{name}"; '


# Isabelle's session announcements, verbatim in shape.  The verb carries the
# build graph: the heap is stored exactly when something else in the build
# depends on this session, and that flag picks the word (build_process.scala:95).
def building(name: str) -> str:
    return f'echo "Building {name} ..."; '


def running(name: str) -> str:
    return f'echo "Running {name} ..."; '


# The plan, printed before anything starts and only under `-v` (which
# `build.py` always passes).  Seeing one is what lets an empty session list
# mean "nothing was rebuilt" rather than "the output never said".
def plan(*names: str) -> str:
    return "".join(f'echo "Session Unsorted/{n}"; ' for n in names)


# A warning from a theory in some other session -- the dependency case.
def foreign_warn(session: str, theory: str, line: int = 444) -> str:
    return (f'echo \'{session}: command "by" running for 15.0s '
            f'(line {line} of theory "{session}.{theory}")\'; ')


# Longer than the watchdog's 1-second select() poll, so the timeout checks
# actually get a chance to run between emissions -- see the chatty-child test
# at the foot of this file for what happens when they do not.
QUIET = "sleep 1.2; "


# --------------------------------------------------------------- happy paths

def test_a_green_build_is_recorded_and_its_exit_code_passed_through(watchdog):
    run = watchdog("sh", "-c", STARTED + 'echo "Probe: theory Probe.Probe_A 100%"')
    assert run.code == 0, run
    rec = run.record
    assert rec["outcome"] == "ok"
    assert rec["exit_code"] == 0
    assert rec["timeout_reason"] is None
    assert rec["error_loci"] is None


def test_the_childs_exit_code_survives(watchdog):
    """Not normalised to 1: a caller distinguishing Isabelle's exit codes
    must still be able to."""
    assert watchdog("sh", "-c", STARTED + "exit 3").code == 3


def test_a_failure_records_its_head_and_loci(watchdog):
    run = watchdog("sh", "-c", STARTED + (
        'echo "*** Failed to finish proof:"; '
        'echo "*** At command \\"by\\" (line 9 of \\"thy/Probe_A.thy\\")"; '
        "exit 1"))
    assert run.code == 1, run
    rec = run.record
    assert rec["outcome"] == "fail"
    assert "Failed to finish proof" in rec["error_head"]
    assert rec["error_loci"] == [["thy/Probe_A.thy", "9"]]


def test_the_budgets_in_force_are_recorded(watchdog):
    """Without them, "the proof got slower" and "the clock got tighter"
    produce identical records, and the second reads as a regression in the
    mathematics."""
    run = watchdog("sh", "-c", STARTED, WATCHDOG_TIMEOUT=7, WALL_TIMEOUT=11)
    limits = run.record["limits"]
    assert limits["activity_timeout"] == 7
    assert limits["wall_timeout"] == 11
    assert limits["startup_timeout"] == 27          # activity + 20
    assert limits["battery_factor_applied"] == 1.0


def test_the_log_name_can_be_overridden(watchdog):
    """So sequential stages do not clobber each other's output."""
    run = watchdog("sh", "-c", STARTED, LOG_NAME="stage-two.log")
    assert "Session Probe" in run.log_text("stage-two.log")
    assert run.record["log"] == "stage-two.log"


# ------------------------------------------------------------ the three kills

def test_a_silent_child_trips_the_activity_budget(watchdog):
    run = watchdog("sh", "-c", STARTED + "sleep 30",
                   WATCHDOG_TIMEOUT=2, WALL_TIMEOUT=60)
    assert run.code == 124, run
    rec = run.record
    assert rec["outcome"] == "timeout"
    assert rec["timeout_reason"] == "activity"
    assert "STUCK" in run.out


def test_startup_gets_a_longer_grace_than_the_activity_budget(watchdog):
    """Loading a heap produces no output for a while, and killing the build
    for that would make the watchdog unusable on any real session.  Until the
    first phase line the budget is activity + 20s."""
    run = watchdog("sh", "-c", 'sleep 4; echo "Session Probe"',
                   WATCHDOG_TIMEOUT=1, WALL_TIMEOUT=60)
    assert run.code == 0, run
    assert run.record["outcome"] == "ok"


def test_a_slow_but_talking_child_trips_the_wall(watchdog):
    """Activity alone cannot catch a build that is merely far too expensive:
    it is producing output the whole time."""
    run = watchdog("sh", "-c", STARTED + "while :; do sleep 1.4; echo tick; done",
                   WATCHDOG_TIMEOUT=30, WALL_TIMEOUT=4)
    assert run.code == 124, run
    assert run.record["timeout_reason"] == "wall"
    assert "TIMEOUT" in run.out


def test_three_warnings_on_one_line_is_a_loop_and_the_line_is_named(watchdog):
    """The kill worth having.

    Three consecutive warnings carrying the same (theory, line, command)
    triple mean a tactic is searching in a loop -- and unlike the other two
    kills, this one knows *where*.  The record must keep the line, because
    "the build hung" and "`by simp` at Probe_A:12 hung" are different amounts
    of help.
    """
    run = watchdog("sh", "-c", STARTED + (warn() + QUIET) * 3 + "sleep 20",
                   WATCHDOG_TIMEOUT=30, WALL_TIMEOUT=60,
                   LOOP_PROGRESS_THRESHOLD=3)
    assert run.code == 124, run
    rec = run.record
    assert rec["timeout_reason"] == "loop_progress"
    assert rec["error_loci"] == [["Probe.Probe_A", "12"]]
    assert "12" in rec["error_head"] and "by" in rec["error_head"]
    # Session-qualified, in the record and on the terminal alike: the theory
    # that hangs is not always one the operator has opened, and the qualifier
    # is what says so (github.com/ott2/isabelle-watchdog#3).
    assert "Probe.Probe_A" in rec["error_head"], rec["error_head"]
    assert 'LOOP  Probe.Probe_A: "by" looping on line 12' in run.out, run.out


def test_warnings_on_different_lines_are_progress_not_a_loop(watchdog):
    """A long build legitimately emits these -- one per slow command.  Only
    *consecutive* warnings on the same triple mean stuck, so the counter has
    to reset when the triple changes, or every slow session is a false loop."""
    body = "".join(warn(line=n) + QUIET for n in (12, 13, 14, 15))
    run = watchdog("sh", "-c", STARTED + body, WATCHDOG_TIMEOUT=30,
                   WALL_TIMEOUT=60, LOOP_PROGRESS_THRESHOLD=3)
    assert run.code == 0, run
    assert run.record["outcome"] == "ok"


def test_a_slow_command_beside_a_building_session_is_not_a_loop(watchdog):
    """The counter counts consecutive *lines*, not consecutive warnings.

    Isabelle builds a session's theories in parallel, so a merely slow command
    emits its warnings while the rest of the session visibly progresses.
    Counting only the warnings ignores everything between them and calls that
    a loop -- which killed a healthy AFP rebuild 73 seconds in, and Isabelle
    discards a session's heap image when rebuilding it, so the whole thing
    restarted from zero (github.com/ott2/isabelle-watchdog#1).

    Three warnings on one line, a threshold of three, and no kill: what
    decides it is the eleven characters of theory line in between.
    """
    body = (warn(secs="15.2") + QUIET + sibling("Probe_B") + sibling("Probe_C")
            + QUIET + warn(secs="17.2") + QUIET + sibling("Probe_D")
            + QUIET + warn(secs="19.2") + QUIET)
    run = watchdog("sh", "-c", STARTED + body, WATCHDOG_TIMEOUT=30,
                   WALL_TIMEOUT=60, LOOP_PROGRESS_THRESHOLD=3)
    assert run.code == 0, run
    assert run.record["outcome"] == "ok"
    assert "LOOP" not in run.out, run.out


def test_a_loop_is_still_caught_once_the_rest_of_the_session_quiesces(watchdog):
    """And the postponement is the whole cost -- the kill is not surrendered.

    The worry about resetting on progress is that a big parallel build would
    never trip the detector and would wait out the wall clock instead.  It
    trips it as soon as the claim becomes true: the other theories finish, the
    warnings stop being interleaved, and three of them land four seconds
    later.  Measured against a real looping `by` beside three theories
    building in parallel, that crossover was at 5.6s of a 75s capture.

    So the detector answers "is this command the only thing left happening",
    which is what a kill needs to know, rather than "has this command been
    slow three times", which it does not.
    """
    body = ((warn() + QUIET + sibling() + QUIET) * 2      # a parallel front...
            + (warn() + QUIET) * 3 + "sleep 20")          # ...that then closes
    run = watchdog("sh", "-c", STARTED + body, WATCHDOG_TIMEOUT=30,
                   WALL_TIMEOUT=60, LOOP_PROGRESS_THRESHOLD=3)
    assert run.code == 124, run
    rec = run.record
    assert rec["timeout_reason"] == "loop_progress"
    assert rec["error_loci"] == [["Probe.Probe_A", "12"]]


def test_a_wall_kill_names_the_line_without_calling_it_a_loop(watchdog):
    """The loop threshold was not reached, but a warning did land.

    Two things at once, and the second is the point.  There is no reason to
    make the operator grep for a culprit the watchdog already saw -- so the
    line is named.  But naming it is all the watchdog has earned here:
    `loop_key` is a *locus* and `loop_count` is the verdict, and the verdict
    was not reached.  Saying "looping on" anyway asserted the conclusion the
    detector had just declined to draw, and a reader acts on it -- one went
    hunting a runaway tactic in a dependency that was merely being
    re-elaborated (github.com/ott2/isabelle-watchdog#4).

    The word belongs to the loop kill above, and only to it.
    """
    run = watchdog("sh", "-c", STARTED + warn() + QUIET + "sleep 20",
                   WATCHDOG_TIMEOUT=30, WALL_TIMEOUT=4,
                   LOOP_PROGRESS_THRESHOLD=99)
    assert run.code == 124, run
    rec = run.record
    assert rec["timeout_reason"] == "wall"
    assert rec["error_loci"] == [["Probe.Probe_A", "12"]]
    assert '(last: "by" at Probe.Probe_A line 12' in run.out, run.out
    assert "loop" not in run.out.lower(), run.out


# --------------------------------------------- where the budget actually went

def test_a_timeout_inside_a_dependency_says_so_rather_than_blaming_the_theory(
        watchdog):
    """The report, end to end.

    A new session with an eight-theory workload timed out at 20s, and the
    summary named line 444 of `Multitape_Alphabet_Enlargement....` -- a
    theory in a *different* session, which Isabelle was re-elaborating from
    source because its heap was out of date.  The operator went looking for a
    looping proof in code they had never written
    (github.com/ott2/isabelle-watchdog#4).

    The budget was never theirs to spend, and nothing in the old summary said
    so.  Now the first note does, and it is derived from Isabelle's own verb
    rather than from any notion of a target the watchdog would have to be
    handed.
    """
    body = (plan("Dep", "Mine") + building("Dep")
            + foreign_warn("Dep", "Other_Peoples_Theory") + QUIET + "sleep 20")
    run = watchdog("sh", "-c", body, WATCHDOG_TIMEOUT=30, WALL_TIMEOUT=4,
                   LOOP_PROGRESS_THRESHOLD=99)
    assert run.code == 124, run
    rec = run.record
    assert rec["timeout_reason"] == "wall"
    assert "rebuilding dependency Dep" in run.out, run.out
    # And the observation survives into the corpus, not just the terminal: a
    # timeout spent on an ancestor and one spent on a proof that got harder
    # are otherwise the same record.  Same argument as `limits`/`contention`.
    assert [(s["name"], s["role"]) for s in rec["sessions"]] == [
        ("Dep", "dependency")]


def test_a_timeout_in_your_own_session_still_names_what_was_rebuilt_first(
        watchdog):
    """The other half of the same budget question.  Here the clock did reach
    your session -- so the theory named is yours and the note does not say
    otherwise -- but it arrived having already spent itself on an ancestor,
    which is still the first thing to know."""
    body = (plan("Dep", "Mine") + building("Dep") + QUIET + running("Mine")
            + foreign_warn("Mine", "Probe_A", 12) + QUIET + "sleep 20")
    run = watchdog("sh", "-c", body, WATCHDOG_TIMEOUT=30, WALL_TIMEOUT=6,
                   LOOP_PROGRESS_THRESHOLD=99)
    assert run.code == 124, run
    assert "rebuilt from source first: Dep" in run.out, run.out
    assert [(s["name"], s["role"]) for s in run.record["sessions"]] == [
        ("Dep", "dependency"), ("Mine", "target")]


def test_a_build_that_rebuilt_nothing_records_that_rather_than_silence(watchdog):
    """`[]` and `null` are different claims.  Everything loaded from heaps is
    a fact worth having -- it is the answer to 'did a rebuild eat this
    budget?' -- and it is only knowable because the plan lines were seen."""
    run = watchdog("sh", "-c", plan("Mine") + 'echo "Mine: theory Mine.T 100%"')
    assert run.code == 0, run
    assert run.record["sessions"] == []


def test_output_that_never_named_a_session_records_null_not_empty(watchdog):
    """The plan lines only appear under `-v`.  `build.py` always passes it; a
    bare `isabelle-watchdog isabelle build ...` need not, and then the honest
    record is that nothing was observed."""
    run = watchdog("sh", "-c", 'echo "Mine: theory Mine.T 100%"')
    assert run.code == 0, run
    assert run.record["sessions"] is None


# The trailing `isabelle build -b` is not run -- `sh -c SCRIPT` puts anything
# after SCRIPT in $0, $1, ... -- but it is the argv the watchdog inspects,
# which is where `-b` is read from.
_AS_A_B_BUILD = ("isabelle", "build", "-b")


def test_forcing_heaps_records_no_role_rather_than_the_wrong_one(watchdog):
    """`-b` sets `store_heap` for every session (build.scala:427 ->
    build_process.scala:1165), so Isabelle says `Building` throughout and its
    verb -- the only thing the role is derived from -- stops discriminating.

    The session the operator asked for was then filed `dependency`, which is
    the misdiagnosis #4 was about, stated in the words of its own fix.  `null`
    is what is actually known."""
    body = plan("Dep", "Mine") + building("Dep") + building("Mine")
    run = watchdog("sh", "-c", body, *_AS_A_B_BUILD)
    assert run.code == 0, run
    assert [(s["name"], s["role"]) for s in run.record["sessions"]] == [
        ("Dep", None), ("Mine", None)]


def test_forcing_heaps_still_records_which_sessions_and_when(watchdog):
    """Only the role is unknowable under `-b`.  Which sessions were elaborated
    and in what order is on the pipe either way, and it is the half a timeout
    audit needs most."""
    body = plan("Dep", "Mine") + building("Dep") + building("Mine")
    run = watchdog("sh", "-c", body, *_AS_A_B_BUILD)
    assert [s["name"] for s in run.record["sessions"]] == ["Dep", "Mine"]
    assert all(s["started_s"] is not None for s in run.record["sessions"])


def test_forcing_heaps_stops_the_summary_blaming_a_dependency(watchdog):
    """The other half, and the one a reader sees.  With every session reading
    `Building`, the note would have told an operator timing out in their own
    session that the budget went on a dependency -- naming the session they
    asked for as one they did not."""
    body = (plan("Dep", "Mine") + building("Dep") + QUIET + building("Mine")
            + foreign_warn("Mine", "Probe_A", 12) + QUIET + "sleep 20")
    run = watchdog("sh", "-c", body, *_AS_A_B_BUILD,
                   WATCHDOG_TIMEOUT=30, WALL_TIMEOUT=6,
                   LOOP_PROGRESS_THRESHOLD=99)
    assert run.code == 124, run
    assert "dependency" not in run.out, run.out
    assert "rebuilt from source first" not in run.out, run.out


# ------------------------------------------------------- reading the pipe itself

def test_a_burst_of_output_before_a_hang_is_read_in_full(watchdog):
    """Every line has to be seen, not just the first of each read.

    This was broken, and broken in the worst place.  `select()` polls the
    pipe while `readline()` read it through a buffered reader, which pulls a
    whole chunk into userspace and returns one line -- after which the pipe
    is empty, select reports not-ready, and the rest of the chunk is stranded
    until more output arrives.  A child that printed four lines and went
    quiet had exactly *one* of them logged.

    So the case it broke was precisely a burst followed by a hang: the error
    block and the progress warnings that name the stuck line arrive together,
    and everything after the first line was invisible to the log, to the
    error head, to the loci and to the loop detector.
    """
    burst = "".join(f'echo "*** At command \\"by\\" (line {n} of \\"A.thy\\")"; '
                    for n in (4, 5, 6, 7))
    run = watchdog("sh", "-c", STARTED + burst + "sleep 20",
                   WATCHDOG_TIMEOUT=30, WALL_TIMEOUT=3)
    assert run.code == 124, run
    log = run.log_text()
    assert all(f"line {n} of" in log for n in (4, 5, 6, 7)), log
    # ...and the timeout summary counts what it read.
    assert "(4 error loci)" in run.out


def test_a_child_that_never_stops_talking_is_still_measured(watchdog):
    """The wall clock has to apply to a build that is producing output.

    It did not: the budget checks lived in the branch taken when `select()`
    *timed out*, so a child emitting more than one line a second kept the
    pipe permanently ready and was never measured against the wall at all.
    `while :; do echo tick; done` ran unbounded under a 3-second budget --
    and "produces output continuously" is what a parallel Isabelle build with
    `-v` looks like.
    """
    run = watchdog("sh", "-c", STARTED + "while :; do echo tick; done",
                   WATCHDOG_TIMEOUT=60, WALL_TIMEOUT=3, timeout=90)
    assert run.code == 124, run
    assert run.record["timeout_reason"] == "wall"


def test_a_final_line_without_a_newline_is_not_dropped(watchdog):
    """`printf` with no trailing newline is how a truncated error block
    arrives, and it is still the error."""
    run = watchdog("sh", "-c", STARTED + 'printf "*** Undefined constant"; exit 1')
    assert run.record["error_head"] == "Undefined constant"


# ---------------------------------------------------------- the injected option

def test_the_progress_threshold_reaches_the_child(watchdog, stub_bin):
    """Injection is invisible until it is missing, and then it costs the
    stuck line on every hang with no error of any kind -- so check the option
    actually arrives on the command line."""
    stub_bin("isabelle", 'echo "Session Probe"; echo "ARGV: $*"')
    run = watchdog("isabelle", "build", "-d", "thy", "Probe",
                   PATH=f"{stub_bin.dir}{os.pathsep}{os.environ['PATH']}")
    assert run.code == 0, run
    assert "ARGV: build -o build_progress_threshold=15 -d thy Probe" in run.log_text()
    assert run.record["command"][:4] == ["isabelle", "build", "-o",
                                         "build_progress_threshold=15"]


# ------------------------------------------------------------------- battery

@pytest.mark.skipif(sys.platform != "darwin", reason="pmset is macOS-only")
def test_on_battery_all_three_budgets_scale(watchdog, stub_bin):
    """Scaling only the *budgets* would leave a battery-slow-but-fine command
    crossing the unscaled loop-warn threshold and being killed as a loop while
    the scaled budgets still had room.  All three move together or none do.

    Note this normalises to AC-equivalent time rather than bypassing the
    budget, so a genuine cost regression still trips.
    """
    stub_bin("pmset", "echo \"Now drawing from 'Battery Power'\"")
    run = watchdog("sh", "-c", STARTED,
                   WATCHDOG_TIMEOUT=5, WALL_TIMEOUT=10,
                   BUILD_PROGRESS_THRESHOLD=15, BATTERY_FACTOR=2.0,
                   PATH=f"{stub_bin.dir}{os.pathsep}{os.environ['PATH']}")
    assert run.code == 0, run
    rec = run.record
    assert rec["power"] == "battery"
    assert rec["limits"] == {
        "activity_timeout": 10, "wall_timeout": 20, "startup_timeout": 30,
        "loop_progress_threshold": 3, "build_progress_threshold": 30.0,
        "battery_factor_applied": 2.0, "load_factor_max": 4.0,
    }
    assert "budgets scaled x2" in run.out


@pytest.mark.skipif(sys.platform != "darwin", reason="pmset is macOS-only")
def test_on_ac_nothing_is_scaled(watchdog, stub_bin):
    stub_bin("pmset", "echo \"Now drawing from 'AC Power'\"")
    run = watchdog("sh", "-c", STARTED, WATCHDOG_TIMEOUT=5, BATTERY_FACTOR=2.0,
                   PATH=f"{stub_bin.dir}{os.pathsep}{os.environ['PATH']}")
    rec = run.record
    assert rec["power"] == "ac"
    assert rec["battery_factor"] == 1.0
    assert rec["limits"]["activity_timeout"] == 5


@pytest.mark.skipif(sys.platform != "darwin", reason="pmset is macOS-only")
def test_an_undetectable_power_state_scales_nothing(watchdog, stub_bin):
    """A failing `pmset` must read as "unknown", not as "on battery" -- a
    wrongly-doubled budget hides exactly the regression the tight wall clock
    exists to catch."""
    stub_bin("pmset", "exit 1")
    run = watchdog("sh", "-c", STARTED, WATCHDOG_TIMEOUT=5, BATTERY_FACTOR=2.0,
                   PATH=f"{stub_bin.dir}{os.pathsep}{os.environ['PATH']}")
    assert run.record["power"] == "unknown"
    assert run.record["limits"]["activity_timeout"] == 5


# ----------------------------------------------------------------- contention
#
# Battery is *assumed* from a machine state; contention is *measured* from the
# build.  These drive the measurement with a fake `ps`, which is the only way
# to hold a duty cycle still -- a real one on a `sleep` reports no CPU, and a
# real one on a spinner reports whatever the machine happens to be doing while
# the suite runs.

def cpu_stub(stub_bin, cs_per_second: int):
    """A fake `ps` reporting `cs_per_second` centiseconds of tree CPU for every
    second the supervised child has been alive -- so it presents a duty cycle
    of `cs_per_second / 100`, whatever the sampler's own timing does.

    Pure `sh`, and that is a requirement rather than a style: this stands in
    for `ps` inside the supervisor's `select()` loop, which is the loop whose
    timing these tests are about.  A version that shelled out to `python3` to
    read a real clock cost 0.34 s per call on a Mac with a security agent in
    the exec path -- a hundred times the real thing -- and blocked the read
    loop for that long each sample, inflating the runs it was measuring.  A
    fixture must not perturb the quantity under test.

    **It reports against a clock, not a call counter**, and that is the whole
    of why these tests hold still.  Counting calls makes the presented duty
    `cs_per_call x calls-per-second`, and calls-per-second is not 1: the
    sampler pays two `pgrep`s and this stub per sample, and on the machine
    above each exec costs 150-300 ms, so a nominal 1 s interval measures 2.3 s
    and every duty here came out at 0.43x its nominal value.  That error is
    systematic in the machine's exec latency rather than an occasional skipped
    sample, and it is unbounded as that latency grows: it put a nominal duty
    of 0.10 at 0.043, under STALL_DUTY, so the *starved* build a test had set
    up was measured as a *stalled* one and the extension it was asserting was
    never applied.  Anchoring to the child's own elapsed time makes the
    sampler's cost change *when* samples land, not what they say.

    The real `ps` is reached by absolute path -- PATH now starts with this
    stub -- and `-o etime=` is the field that answers "how long has this been
    alive".  What is left is its whole-second resolution: both ends of a
    window are floored independently, so a window of span `t` reads within
    `1/t` of nominal, in *either* direction.  `t` is a sample interval or two
    (2-3 s in practice), which puts the residue around +/-40%, and it does not
    grow with the machine -- that is the difference that matters.  Measured
    here: nominal 0.20 reads 0.227, 0.50 reads 0.478, 3.0 reads 2.83.

    Two rules follow for adding a case.  Leave a duty a factor of two clear of
    any threshold it must stay one side of (STALL_DUTY 0.05, RUNNING_DUTY
    0.9); and assert an exact `load_factor_applied` only where `min(cap,
    1/duty)` saturates, since an unsaturated factor inherits the +/-40% and
    can only be given a band.
    """
    stub_bin("ps", f"""
PS=/bin/ps
[ -x "$PS" ] || PS=/usr/bin/ps
for a in "$@"; do pids=$a; done          # the pid list is the last argument
set -- $($PS -o etime= -p "${{pids%%,*}}")   # splitting strips ps's padding
et=${{1#*-}}                                 # drop a DD- prefix if one appears
secs=0
IFS=:
for f in $et; do f=${{f#0}}; secs=$((secs * 60 + ${{f:-0}})); done
cs=$((secs * {cs_per_second}))
printf '  %d:%02d.%02d\\n' $((cs / 6000)) $((cs / 100 % 60)) $((cs % 100))
""")


def with_stub(stub_bin, **env):
    env["PATH"] = f"{stub_bin.dir}{os.pathsep}{os.environ['PATH']}"
    env.setdefault("CPU_SAMPLE_INTERVAL", 1.0)
    return env


def test_a_starved_build_is_given_back_the_time_it_did_not_get(watchdog, stub_bin):
    """A build getting a quarter of a core has had a quarter of the budget it
    was charged for, so 1/duty restores what it would have had uncontended.

    Measured, not assumed -- which is what makes the rule portable: 0.25 of a
    core means the same thing on any machine, so nothing here is calibrated
    against the one it was written on.
    """
    cpu_stub(stub_bin, 25)                      # duty 0.25 -> factor ~4
    run = watchdog("sh", "-c", STARTED + "sleep 5", WATCHDOG_TIMEOUT=30,
                   **with_stub(stub_bin, WALL_TIMEOUT=3, LOAD_FACTOR_MAX=4.0))
    # The claim, and the one assertion here that carries no measurement error:
    # unextended, a 3 s budget kills this at 3 s.  It lived, so it was given
    # the time back.
    assert run.code == 0, run                             # 3s x ~4 = ~12s > 5s
    c = run.record["contention"]
    assert c["verdict"] == "starved"
    # A band, not `== 4.0`, and a wide one.  This is the only test in the
    # group asserting an *unsaturated* factor, so it is the only one exposed
    # to the fixture's whole-second `etime` resolution -- +/-40% on the duty,
    # hence 1/duty anywhere from 2.8 to over the cap (see `cpu_stub`).  The
    # exact arithmetic is settled by the pure `contention` tests, which take a
    # duty as an argument and can assert 4.0 on the nose.
    assert 2.7 <= c["load_factor_applied"] <= 4.0


def test_a_build_running_flat_out_still_trips_its_budget(watchdog, stub_bin):
    """The property the whole design turns on.

    A proof that got genuinely more expensive burns CPU at full rate, so it is
    never mistaken for a starved one -- scaling by an *estimated* load factor
    would have made those two indistinguishable and quietly destroyed the
    cost-regression signal the tight wall budget exists to carry.
    """
    # Three cores' worth, not exactly one: a parallel build is the ordinary
    # instance of this, and the headroom keeps the measurement clear of
    # RUNNING_DUTY, so the build is never spared by a low reading -- which is
    # how this test used to fail about a third of the time.
    cpu_stub(stub_bin, 300)                     # duty 3.0 -> factor 1
    run = watchdog("sh", "-c", STARTED + "sleep 20", WATCHDOG_TIMEOUT=30,
                   **with_stub(stub_bin, WALL_TIMEOUT=3, LOAD_FACTOR_MAX=4.0))
    assert run.code == 124, run
    rec = run.record
    assert rec["timeout_reason"] == "wall"
    assert rec["contention"]["verdict"] == "running"
    assert rec["contention"]["load_factor_applied"] == 1.0


def test_a_stalled_tree_earns_no_extension(watchdog, stub_bin):
    """1/duty is unbounded as duty goes to zero, so the naive rule would hand
    a deadlock four times its deadline.  No CPU is not slowness -- it is the
    absence of work, and more time does not fix it.

    It also sharpens the diagnosis: "no output" alone could be a build working
    quietly, and "no output and no CPU" could not.
    """
    cpu_stub(stub_bin, 0)                       # duty 0 -> stalled
    run = watchdog("sh", "-c", STARTED + "sleep 20", WATCHDOG_TIMEOUT=30,
                   **with_stub(stub_bin, WALL_TIMEOUT=3, LOAD_FACTOR_MAX=4.0))
    assert run.code == 124, run
    c = run.record["contention"]
    assert c["verdict"] == "stalled"
    assert c["load_factor_applied"] == 1.0
    # And it says so: "timed out" is a report, "timed out without using the
    # CPU" is a diagnosis -- and this is the one verdict where nothing was
    # scaled, so no other part of the output would hint at it.
    assert "used no CPU" in run.out, run


def test_the_cap_bounds_what_starvation_can_buy(watchdog, stub_bin):
    """A budget that stretches without limit is not a budget.

    0.2 rather than a more dramatic 0.02, because the duty has to stay a
    factor of two clear of STALL_DUTY: below that floor nothing is owed at
    all, the factor is 1.0, and the cap this is about never comes into it.
    That is precisely how this test used to fail -- it asked for 0.1 from a
    fixture delivering 0.43x of nominal, got 0.043, and read `stalled`.
    """
    cpu_stub(stub_bin, 20)                      # duty 0.2 -> 1/duty=5
    run = watchdog("sh", "-c", STARTED + "sleep 20", WATCHDOG_TIMEOUT=30,
                   **with_stub(stub_bin, WALL_TIMEOUT=3, LOAD_FACTOR_MAX=2.0))
    assert run.code == 124, run                           # 3s x 2 = 6s, not 15s
    # Exact, and it stays exact under the fixture's +/-40%: `min(2.0, 1/duty)`
    # saturates for every duty below 0.5, and this one cannot reach that.
    c = run.record["contention"]
    assert c["load_factor_applied"] == 2.0, c


def test_the_measurement_can_be_switched_off(watchdog, stub_bin):
    """LOAD_FACTOR_MAX=1.0 skips the sampling entirely, `ps` calls included --
    the setting to reach for on a machine where this misbehaves."""
    cpu_stub(stub_bin, 25)
    run = watchdog("sh", "-c", STARTED + "sleep 20", WATCHDOG_TIMEOUT=30,
                   **with_stub(stub_bin, WALL_TIMEOUT=3, LOAD_FACTOR_MAX=1.0))
    assert run.code == 124, run                           # killed on the nose
    c = run.record["contention"]
    assert c["verdict"] == "unknown" and c["cpu_time_s"] is None


def test_an_unreadable_tree_changes_nothing(watchdog, stub_bin):
    """No `ps`, a platform without one, or a race with the tree exiting.
    Unmeasured contention has to behave exactly as it did before any of this
    existed, or a portability gap becomes a behaviour change."""
    stub_bin("ps", "exit 1")
    run = watchdog("sh", "-c", STARTED + "sleep 20", WATCHDOG_TIMEOUT=30,
                   **with_stub(stub_bin, WALL_TIMEOUT=3, LOAD_FACTOR_MAX=4.0))
    assert run.code == 124, run
    assert run.record["contention"]["verdict"] == "unknown"


def test_the_observations_are_recorded_not_just_the_verdict(watchdog, stub_bin):
    """A record keeping only the applied factor could never answer "was that
    timeout a hard proof or a busy laptop" once the policy above it changed.
    Duty cycle and CPU seconds stay meaningful whatever the policy becomes.

    The band is wide because this test is about the fields being *stored*, and
    the stub's duty carries the whole-second `etime` residue in both
    directions (see `cpu_stub`).  What the number means exactly is settled by
    the pure `duty_cycle` tests, which compute it from synthetic samples and
    can assert 0.25 and 4.0 on the nose.
    """
    cpu_stub(stub_bin, 50)                      # ~half a core
    run = watchdog("sh", "-c", STARTED + "sleep 4", WATCHDOG_TIMEOUT=30,
                   **with_stub(stub_bin, WALL_TIMEOUT=6, LOAD_FACTOR_MAX=4.0))
    c = run.record["contention"]
    assert 0.25 <= c["duty_cycle"] <= 0.75
    assert c["verdict"] == "starved"               # the band stays inside one
    assert c["cpu_time_s"] > 0
    assert run.record["limits"]["load_factor_max"] == 4.0


def test_a_run_too_short_to_sample_claims_no_cpu_time(watchdog, stub_bin):
    """The pair has to be consistent, because a reader will trust the number.

    One sample is taken at spawn, as the baseline the first duty cycle is
    measured against.  It says what the tree had used a millisecond into the
    run -- nothing about the run.  Reporting it as `cpu_time_s` gave a 3.4 s
    build `0.02` beside a correctly-null `duty_cycle`: two fields describing
    the same run, one of which could not be true.  Null is the honest answer
    when nothing was measured, and it is the one `duty_cycle` already gives.
    """
    cpu_stub(stub_bin, 50)
    # Sampling every 30 s, so only the spawn baseline is ever taken.
    run = watchdog("sh", "-c", STARTED + "sleep 1", WATCHDOG_TIMEOUT=30,
                   **with_stub(stub_bin, WALL_TIMEOUT=10, LOAD_FACTOR_MAX=4.0,
                               CPU_SAMPLE_INTERVAL=30.0))
    assert run.code == 0, run
    c = run.record["contention"]
    assert c["duty_cycle"] is None
    assert c["cpu_time_s"] is None, "the spawn baseline reached the record"
    assert c["verdict"] == "unknown"


# --------------------------------------------------- capture never costs a build

def test_a_recorder_that_cannot_run_does_not_change_the_exit_code(
        watchdog, tmp_path, logs):
    """Run from somewhere that is not a git repository, so the recorder
    cannot do its job at all.

    The build must still report its own result.  This is the contract the
    whole `guard` module exists for, and the one property of this package
    that a refactor must never quietly lose: instrumentation is allowed to
    fail, and a failure in it is not allowed to be mistaken for a failure of
    the build.

    It asserted `build-record: skipped` and now asserts the precondition
    message, because this case stopped being a breakage: "there is no
    repository here" is something the operator can act on, so it says what
    and how rather than quoting a `CalledProcessError`.  The contract under
    test is unchanged -- exit 7 is still exit 7.
    """
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    run = watchdog("sh", "-c", STARTED + "exit 7", cwd=outside)
    assert run.code == 7, run
    assert run.records == []                       # nothing captured...
    assert "build-record: NOT recorded" in run.out          # ...and it says so
    assert "is not a git repository" in run.out             # ...and why
    assert "git init" in run.out                            # ...and what to do


# ------------------------------------------------------- where the records land

def test_the_watchdog_finds_the_project_corpus_unaided(watchdog, repo):
    """The 43sp incident, end to end and through both writers.

    With `WATCHDOG_LOG_DIR` unset -- a build run outside the Makefile that
    owns it -- the watchdog and the recorder each have to decide where the
    records go, and they used to decide by falling back to a built-in default
    rather than by looking.  In a project whose corpus is the *other* known
    layout that mints a second one: new instance id, empty history, and every
    record in it perfectly valid, so nothing downstream reports a problem.
    """
    (repo.root / "results/isabelle-logs").mkdir(parents=True)
    (repo.root / "results/isabelle-logs/builds.jsonl").write_text("")
    run = watchdog("sh", "-c", 'echo "Session Probe"', WATCHDOG_LOG_DIR=None)
    assert run.code == 0, run

    corpus = repo.root / "results/isabelle-logs/builds.jsonl"
    assert corpus.read_text().strip(), f"nothing was appended.\n{run}"
    # Not merely "the right one won" -- the wrong one must not come into
    # existence, because a stray empty corpus is what a later reader trips on.
    assert not (repo.root / "t/logs/builds.jsonl").exists()


def test_the_watchdog_log_lands_in_the_project_not_the_install(watchdog, repo):
    """`last-build.log` was placed relative to `__file__`.

    That named the project only while this script lived inside it; installed,
    it names site-packages.  The recorder's copy of the bug was fixed during
    consolidation and this one was missed -- it misplaces a log file rather
    than a payload, so nothing errors and no record ever looks wrong.
    """
    run = watchdog("sh", "-c", 'echo "Session Probe"', WATCHDOG_LOG_DIR=None)
    assert run.code == 0, run
    assert (repo.root / "t/logs/last-build.log").exists(), run


def test_creating_a_corpus_says_so(watchdog, repo):
    """The floor under the two tiers above.

    Resolution can only see layouts it knows and markers that were committed,
    so a project keeping its records somewhere else entirely still gets a
    fresh corpus minted.  One line naming the file is what turns that from
    silent into obvious -- "creating" where the operator expected "appending"
    is the whole of the bug.
    """
    first = watchdog("sh", "-c", 'echo "Session Probe"', WATCHDOG_LOG_DIR=None)
    assert "creating a new corpus" in first.out, first
    # Once per corpus, not once per build: a line every time would be noise,
    # and noise is not read.
    second = watchdog("sh", "-c", 'echo "Session Probe"', WATCHDOG_LOG_DIR=None)
    assert "creating a new corpus" not in second.out, second


# ------------------------------------------------------ capture, and turning it off

def test_capture_can_be_turned_off_without_losing_supervision(watchdog, logs):
    """The supervision is useful on its own -- killing a looping tactic and
    naming its line needs no dataset -- so a project that wants only that must
    be able to say so, rather than accumulating records it will never read."""
    run = watchdog("--no-record", "sh", "-c", STARTED + "exit 3")
    assert run.code == 3, run                       # ...it still supervised
    assert run.records == []                        # ...and recorded nothing
    assert (logs / "last-build.log").exists(), run  # ...but still logged


@pytest.mark.parametrize("off", ["0", "no", "false", "off"])
def test_the_environment_turns_capture_off_too(watchdog, off):
    """A flag suits one call; the variable suits a Makefile, which is how a
    project that never wants capture would express it."""
    run = watchdog("sh", "-c", 'echo "Session Probe"', BUILD_RECORD=off)
    assert run.code == 0, run
    assert run.records == []


def test_the_flag_beats_the_environment(watchdog):
    """Said on the command line, about this one call."""
    run = watchdog("--record", "sh", "-c", 'echo "Session Probe"',
                   BUILD_RECORD="0")
    assert run.records, run


def test_a_meaningless_setting_stops_before_the_build(watchdog, logs):
    run = watchdog("sh", "-c", 'echo "Session Probe"', BUILD_RECORD="fasle")
    assert run.code == 2, run
    assert "neither on nor off" in run.out
    assert not (logs / "last-build.log").exists()   # the build never started


def test_capture_off_does_not_resolve_a_corpus_at_all(watchdog, repo):
    """Two corpora make a *recording* writer refuse, because guessing would
    split a dataset.  With nothing being written there is no dataset to
    protect, and refusing to start a build over it would be the tail wagging
    the dog."""
    for layout in ("t/logs", "results/isabelle-logs"):
        (repo.root / layout).mkdir(parents=True)
        (repo.root / layout / "builds.jsonl").write_text("")
    run = watchdog("--no-record", "sh", "-c", 'echo "Session Probe"',
                   WATCHDOG_LOG_DIR=None)
    assert run.code == 0, run


# --------------------------------------------------------- the wrapper's own flags

def test_asking_for_help_does_not_run_help(watchdog):
    """`--help` was taken as the command to supervise: the one entry point
    named after the package, and asking it for help ran it."""
    run = watchdog("--help")
    assert run.code == 0, run
    assert "isabelle-watchdog" in run.out
    # The two things a project adopting this has to know are the marker and
    # the switch, so both have to be in the text it is pointed at.
    assert ".isabelle-watchdog" in run.out and "--no-record" in run.out


def test_only_leading_flags_belong_to_the_wrapper(watchdog, logs):
    """`env`, `nice` and `timeout` all draw the line here, and so must this:
    a flag after the command is the command's, or this wrapper silently eats
    options meant for `isabelle build`."""
    run = watchdog("sh", "-c", 'echo "got: $1"', "sh", "--no-record")
    assert run.code == 0, run
    assert "got: --no-record" in run.log_text()     # passed through, not eaten
    assert run.records, run                         # ...so capture stayed on


def test_a_double_dash_ends_the_wrappers_flags(watchdog):
    run = watchdog("--", "sh", "-c", 'echo "Session Probe"')
    assert run.code == 0, run
    assert run.records, run


def test_the_version_is_this_packages_not_the_wrappers_stdbufs(watchdog):
    """`--version` used to reach `stdbuf`, which the command is wrapped in, so
    it answered confidently with the wrong program's version."""
    for flag in ("-V", "--version"):
        run = watchdog(flag)
        assert run.code == 0, run
        assert "isabelle-watchdog" in run.out
        assert "stdbuf" not in run.out


def test_an_unknown_flag_does_not_become_the_command(watchdog, logs):
    """The bug behind the missing `--version`, and much the worse half.

    An unrecognised leading `-word` fell through to the child, so
    `isabelle-watchdog -V` supervised a program named `-V`: it resolved a log
    directory, **created a corpus**, and recorded the failure as an attempt.
    This project's own note on that failure class is that appending to the
    wrong file is loud while creating one is silent and looks exactly like a
    first build -- and a mistyped flag was enough to do it.

    So: an error, before anything runs, naming `--` for the case where a
    program really is called that.
    """
    run = watchdog("-Q", "sh", "-c", "true")
    assert run.code == 2, run
    assert "unrecognised option" in run.out and "'-Q'" in run.out
    assert "--" in run.out                         # says how to mean it
    assert not run.records, "a usage error minted a corpus"
    assert not (logs / "builds.jsonl").exists()
