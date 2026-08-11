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
    assert "LOOP" in run.out


def test_warnings_on_different_lines_are_progress_not_a_loop(watchdog):
    """A long build legitimately emits these -- one per slow command.  Only
    *consecutive* warnings on the same triple mean stuck, so the counter has
    to reset when the triple changes, or every slow session is a false loop."""
    body = "".join(warn(line=n) + QUIET for n in (12, 13, 14, 15))
    run = watchdog("sh", "-c", STARTED + body, WATCHDOG_TIMEOUT=30,
                   WALL_TIMEOUT=60, LOOP_PROGRESS_THRESHOLD=3)
    assert run.code == 0, run
    assert run.record["outcome"] == "ok"


def test_a_wall_kill_still_names_a_line_it_happens_to_know(watchdog):
    """The loop threshold was not reached, but a warning did land.  There is
    no reason to make the operator grep for a culprit the watchdog already
    saw."""
    run = watchdog("sh", "-c", STARTED + warn() + QUIET + "sleep 20",
                   WATCHDOG_TIMEOUT=30, WALL_TIMEOUT=4,
                   LOOP_PROGRESS_THRESHOLD=99)
    assert run.code == 124, run
    rec = run.record
    assert rec["timeout_reason"] == "wall"
    assert rec["error_loci"] == [["Probe.Probe_A", "12"]]
    assert "looping on Probe_A line 12" in run.out


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

def cpu_stub(stub_bin, tmp_path, cs_per_call: int):
    """A fake `ps` whose reported tree CPU advances `cs_per_call` centiseconds
    per sample.  With CPU_SAMPLE_INTERVAL=1 that is the duty cycle x 100.

    Pure `sh`, and that is a requirement rather than a style: this stands in
    for `ps` inside the supervisor's `select()` loop, which is the loop whose
    timing these tests are about.  A version that shelled out to `python3` to
    read a real clock cost 0.34 s per call on a Mac with a security agent in
    the exec path -- a hundred times the real thing -- and blocked the read
    loop for that long each sample, inflating the runs it was measuring.  A
    fixture must not perturb the quantity under test.

    The cost of staying cheap is that duty is `cs_per_call x calls-per-second`
    rather than a fraction of the clock, and calls-per-second is only
    approximately one: the sampler fires on its own timer from a loop polling
    with a 1 s timeout, so a poll landing a hair early skips a sample and the
    next window spans two seconds with one call in it, halving the measured
    duty.

    Rather than chase determinism there, each test below is made robust to a
    skipped sample -- which is a fair demand, since a real machine can deliver
    an irregular sample too.  Two ways, and which one applies is worth
    knowing when adding a case:

      - where a *verdict* is asserted, the value has headroom, so halving it
        does not cross a threshold;
      - where a *factor* is asserted, `min(cap, 1/duty)` already saturates,
        so halving the duty changes nothing.
    """
    counter = tmp_path / "ps-calls"
    stub_bin("ps", f"""
n=$(cat {counter} 2>/dev/null || echo 0)
n=$((n + 1))
echo $n > {counter}
cs=$((n * {cs_per_call}))
printf '  %d:%02d.%02d\\n' $((cs / 6000)) $((cs / 100 % 60)) $((cs % 100))
""")


def with_stub(stub_bin, **env):
    env["PATH"] = f"{stub_bin.dir}{os.pathsep}{os.environ['PATH']}"
    env.setdefault("CPU_SAMPLE_INTERVAL", 1.0)
    return env


def test_a_starved_build_is_given_back_the_time_it_did_not_get(watchdog, stub_bin,
                                                               tmp_path):
    """A build getting a quarter of a core has had a quarter of the budget it
    was charged for, so 1/duty restores what it would have had uncontended.

    Measured, not assumed -- which is what makes the rule portable: 0.25 of a
    core means the same thing on any machine, so nothing here is calibrated
    against the one it was written on.
    """
    cpu_stub(stub_bin, tmp_path, 25)                      # duty 0.25 -> factor 4
    run = watchdog("sh", "-c", STARTED + "sleep 5", WATCHDOG_TIMEOUT=30,
                   **with_stub(stub_bin, WALL_TIMEOUT=3, LOAD_FACTOR_MAX=4.0))
    assert run.code == 0, run                             # 3s x 4 = 12s > 5s
    c = run.record["contention"]
    assert c["verdict"] == "starved"
    # A band, not `== 4.0`: the factor is `1/duty` over a *measured* duty, so
    # it lands near 4 and not on it.  The old exact assertion held only
    # because the fixture's duty came out slightly *low* and `min(cap, …)`
    # clamped the result back to the cap -- reading as precision when it was
    # saturation.  An accurate fixture gives 3.9-something, which is the
    # honest answer.
    assert 3.5 <= c["load_factor_applied"] <= 4.0


def test_a_build_running_flat_out_still_trips_its_budget(watchdog, stub_bin,
                                                         tmp_path):
    """The property the whole design turns on.

    A proof that got genuinely more expensive burns CPU at full rate, so it is
    never mistaken for a starved one -- scaling by an *estimated* load factor
    would have made those two indistinguishable and quietly destroyed the
    cost-regression signal the tight wall budget exists to carry.
    """
    # Three cores' worth, not exactly one: a parallel build is the ordinary
    # instance of this, and the headroom means a skipped sample still reads
    # `running` rather than flipping to `starved` and sparing the build --
    # which is how this test used to fail about a third of the time.
    cpu_stub(stub_bin, tmp_path, 300)                     # duty 3.0 -> factor 1
    run = watchdog("sh", "-c", STARTED + "sleep 20", WATCHDOG_TIMEOUT=30,
                   **with_stub(stub_bin, WALL_TIMEOUT=3, LOAD_FACTOR_MAX=4.0))
    assert run.code == 124, run
    rec = run.record
    assert rec["timeout_reason"] == "wall"
    assert rec["contention"]["verdict"] == "running"
    assert rec["contention"]["load_factor_applied"] == 1.0


def test_a_stalled_tree_earns_no_extension(watchdog, stub_bin, tmp_path):
    """1/duty is unbounded as duty goes to zero, so the naive rule would hand
    a deadlock four times its deadline.  No CPU is not slowness -- it is the
    absence of work, and more time does not fix it.

    It also sharpens the diagnosis: "no output" alone could be a build working
    quietly, and "no output and no CPU" could not.
    """
    cpu_stub(stub_bin, tmp_path, 0)                       # duty 0 -> stalled
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


def test_the_cap_bounds_what_starvation_can_buy(watchdog, stub_bin, tmp_path):
    """A budget that stretches without limit is not a budget."""
    cpu_stub(stub_bin, tmp_path, 10)                      # duty 0.1 -> 1/duty=10
    run = watchdog("sh", "-c", STARTED + "sleep 20", WATCHDOG_TIMEOUT=30,
                   **with_stub(stub_bin, WALL_TIMEOUT=3, LOAD_FACTOR_MAX=2.0))
    assert run.code == 124, run                           # 3s x 2 = 6s, not 30s
    assert run.record["contention"]["load_factor_applied"] == 2.0


def test_the_measurement_can_be_switched_off(watchdog, stub_bin, tmp_path):
    """LOAD_FACTOR_MAX=1.0 skips the sampling entirely, `ps` calls included --
    the setting to reach for on a machine where this misbehaves."""
    cpu_stub(stub_bin, tmp_path, 25)
    run = watchdog("sh", "-c", STARTED + "sleep 20", WATCHDOG_TIMEOUT=30,
                   **with_stub(stub_bin, WALL_TIMEOUT=3, LOAD_FACTOR_MAX=1.0))
    assert run.code == 124, run                           # killed on the nose
    c = run.record["contention"]
    assert c["verdict"] == "unknown" and c["cpu_time_s"] is None


def test_an_unreadable_tree_changes_nothing(watchdog, stub_bin, tmp_path):
    """No `ps`, a platform without one, or a race with the tree exiting.
    Unmeasured contention has to behave exactly as it did before any of this
    existed, or a portability gap becomes a behaviour change."""
    stub_bin("ps", "exit 1")
    run = watchdog("sh", "-c", STARTED + "sleep 20", WATCHDOG_TIMEOUT=30,
                   **with_stub(stub_bin, WALL_TIMEOUT=3, LOAD_FACTOR_MAX=4.0))
    assert run.code == 124, run
    assert run.record["contention"]["verdict"] == "unknown"


def test_the_observations_are_recorded_not_just_the_verdict(watchdog, stub_bin,
                                                            tmp_path):
    """A record keeping only the applied factor could never answer "was that
    timeout a hard proof or a busy laptop" once the policy above it changed.
    Duty cycle and CPU seconds stay meaningful whatever the policy becomes.

    The band is wide because this test is about the fields being *stored*, and
    a skipped sample halves the stub's duty (see `cpu_stub`).  What the number
    means exactly is settled by the pure `duty_cycle` tests, which compute it
    from synthetic samples and can assert 0.25 and 4.0 on the nose.
    """
    cpu_stub(stub_bin, tmp_path, 50)                      # ~half a core
    run = watchdog("sh", "-c", STARTED + "sleep 4", WATCHDOG_TIMEOUT=30,
                   **with_stub(stub_bin, WALL_TIMEOUT=6, LOAD_FACTOR_MAX=4.0))
    c = run.record["contention"]
    assert 0.2 <= c["duty_cycle"] <= 0.6
    assert c["verdict"] == "starved"               # the band stays inside one
    assert c["cpu_time_s"] > 0
    assert run.record["limits"]["load_factor_max"] == 4.0


def test_a_run_too_short_to_sample_claims_no_cpu_time(watchdog, stub_bin,
                                                      tmp_path):
    """The pair has to be consistent, because a reader will trust the number.

    One sample is taken at spawn, as the baseline the first duty cycle is
    measured against.  It says what the tree had used a millisecond into the
    run -- nothing about the run.  Reporting it as `cpu_time_s` gave a 3.4 s
    build `0.02` beside a correctly-null `duty_cycle`: two fields describing
    the same run, one of which could not be true.  Null is the honest answer
    when nothing was measured, and it is the one `duty_cycle` already gives.
    """
    cpu_stub(stub_bin, tmp_path, 50)
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

def test_a_broken_recorder_does_not_change_the_exit_code(watchdog, tmp_path, logs):
    """Run from somewhere that is not a git repository, so the recorder
    cannot do its job at all.

    The build must still report its own result.  This is the contract the
    whole `guard` module exists for, and the one property of this package
    that a refactor must never quietly lose: instrumentation is allowed to
    fail, and a failure in it is not allowed to be mistaken for a failure of
    the build.
    """
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    run = watchdog("sh", "-c", STARTED + "exit 7", cwd=outside)
    assert run.code == 7, run
    assert run.records == []                       # nothing captured...
    assert "build-record: skipped" in run.out      # ...and it says so


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
