#!/usr/bin/env python3
"""
isabelle-watchdog — Build wrapper for Isabelle with terse output.

Runs an Isabelle build under three kill conditions (a stalled stdout, a wall
budget, and a tactic looping on one line -- which it names), saves the full
output to $WATCHDOG_LOG_DIR/$LOG_NAME, prints a one-line summary, and records
the attempt to a build-trajectory corpus.

Usage: isabelle-watchdog [--no-record] command [args...]

    isabelle-watchdog isabelle build -d t MySession
    isabelle-watchdog --no-record isabelle build -d t MySession
    isabelle-watchdog -- --oddly-named-command    # -- ends the wrapper's flags

Only *leading* flags are the wrapper's; everything from the first non-flag
word on belongs to the command, so an option meant for `isabelle build` is
never eaten here.  A leading `-word` this wrapper does not know is an error,
not a command name -- use `--` for a program actually named that way.

Options:
    --no-record / --record    Turn trajectory capture off / on for this run,
                              overriding $BUILD_RECORD.  Capture is on by
                              default, and is the reason the watchdog exists;
                              but the supervision is useful alone, so a
                              project that wants only that can say so instead
                              of accumulating records it will never read.
    -V / --version            Print the version and exit.
    -h / --help               This text.

WHERE RECORDS GO.  $WATCHDOG_LOG_DIR if set.  Otherwise the tools look rather
than assume, in this order: a committed `.isabelle-watchdog` at the project
root, whose first non-blank, non-comment line names the log directory relative
to itself; then an existing corpus under a known layout (t/logs,
results/isabelle-logs); and only then a new one at t/logs.  Readers resolve
the same way, so `trajectory` lands where this wrote without being told twice.
A new project adopting this package wants the marker: discovery can only find
a corpus that already exists, so it says nothing about a fresh clone.

    $ cat .isabelle-watchdog
    # the log directory, relative to this file
    results/isabelle-logs

Environment:
    WATCHDOG_LOG_DIR          Where the log and the corpus go.  Unset, it is
                              resolved as above -- and honoured by the readers
                              too, so writer and reader cannot disagree.
    BUILD_RECORD              Trajectory capture on/off (default: on).
                              Accepts 1/yes/true/on and 0/no/false/off;
                              anything else is an error rather than a guess.
    WATCHDOG_TIMEOUT          Kill after N seconds of stalled stdout (default: 20).
    WALL_TIMEOUT              Absolute wall-clock limit (default: 40).  The
                              tight ceiling is the point, not an oversight: a
                              build that hits it is either looping or has got
                              measurably more expensive, and both are signal.
                              Raising it to make a red build go green trades a
                              fast, specific failure for a slow, vague one.
                              Raise it deliberately, per project, when a proof
                              genuinely has irreducible elaboration cost --
                              and keep the raise on the watchdog path, since
                              an un-watchdogged build records no attempt at
                              all, not even the success that closes an
                              episode.
    BATTERY_FACTOR            On a laptop running on battery (macOS via
                              `pmset -g ps`, Linux via /sys/class/power_supply;
                              elsewhere the state is unknown and nothing is
                              scaled) the machine runs ~this-many times slower,
                              so all three budgets are multiplied by this factor
                              (default: 2.0).  This *normalises* the budgets to
                              AC-equivalent time rather than bypassing them — a
                              build that is slow in AC-equivalent terms still
                              trips, so the cost-regression signal stays
                              meaningful, while a battery-throttled-but-fine
                              build no longer spuriously trips the activity
                              timeout.  Set to 1.0 to disable scaling.
                              ASSUMED, not measured: throttling changes what a
                              CPU-second buys, and no accounting can see that
                              after the fact.  Contrast LOAD_FACTOR_MAX.
    LOAD_FACTOR_MAX           Ceiling on the *measured* contention factor
                              (default: 4.0; 1.0 disables the measurement and
                              its `ps` calls).  A build sharing the machine
                              gets fewer CPU-seconds per wall-second, which —
                              unlike throttling — is directly measurable: the
                              watchdog samples its process tree's CPU time and
                              scales all three budgets by 1/duty-cycle, capped
                              here.  Dimensionless, so the same number means
                              the same thing on any machine; nothing is
                              calibrated against the machine it was written on.
                              A tree using no CPU at all is not starved but
                              *stuck*, and gets no extension.
    CPU_SAMPLE_INTERVAL       Seconds between those samples (default: 5.0).
    LOOP_PROGRESS_THRESHOLD   Kill after N consecutive Isabelle
                              `command "X" running for ...s (line Y of theory Z)`
                              warnings on the same (theory, line, command) triple
                              (default: 3).  Surfaces a tactic stuck in a search
                              loop on a single line before the wall timeout
                              fires, and the timeout summary names the line.
    BUILD_PROGRESS_THRESHOLD  Seconds a single command must run before Isabelle
                              emits its `command running for ...s (line Y)`
                              warning (default: 15).  Injected as
                              `-o build_progress_threshold=N` into `isabelle
                              build` invocations.  Isabelle's own default is 20s
                              — the SAME as WATCHDOG_TIMEOUT, so a hang trips the
                              activity timeout (no output for 20s) at the exact
                              moment the line-bearing warning would fire, and the
                              stuck line is lost.  At 15s, with the 2s re-emit
                              (build_progress_delay), the warnings land at
                              15/17/19s — three consecutive (LOOP_PROGRESS_
                              THRESHOLD) just under the 20s activity kill, so the
                              loop is caught and its line named.  Kept close to
                              20 deliberately: a lower value would risk
                              false-tripping a legit single command that runs a
                              few seconds long on one line.
    LOG_NAME                  Log file basename under logs/ (default: last-build.log).
                              Override per-stage so parallel or sequential stages
                              don't clobber each other's output.

Exit codes: 0 = success, 124 = watchdog kill, other = child's code.
"""

import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from . import corpus  # resolve_log_dir — one answer to "where do records go"
from . import guard  # run_guarded — capture must never break the build

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Contention measurement (see the `contention` section below).  A duty cycle
# under this is not slowness, it is absence of work: extending a budget for a
# process getting no CPU would stretch a deadlock's deadline fourfold.
STALL_DUTY = 0.05
# At or above this, treat the tree as having a whole core -- not a strict 1.0.
# Nothing is scheduled for 100.0% of wall time (page faults, I/O, and the
# sampler's own jitter: samples land where the 1-second select() poll allows),
# so a healthy single-threaded build measures ~0.96 and a strict boundary
# would label every one of them `starved` and hand it a few percent of budget
# it does not need.  Being wrong here is cheap; being wrong *systematically*
# puts a misleading label on most of the corpus.
RUNNING_DUTY = 0.9
# Seconds between tree-CPU samples.  The duty cycle is measured over a window
# of three of them rather than over one interval, because `ps` reports whole
# seconds on Linux and a short window quantises the ratio into uselessness.
# Derived rather than separately configurable: two knobs that must stay in
# proportion are one knob and a mistake waiting to happen.
CPU_SAMPLE_INTERVAL = 5.0
CPU_WINDOW_SAMPLES = 3

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

TIMING_RE = re.compile(
    r"^Timing\s+(\S+)\s+\((\d+)\s+threads?,\s+"
    r"([\d.]+)s\s+elapsed.*?([\d.]+)s\s+cpu"
)
TOTAL_ELAPSED_RE = re.compile(r"^(\d+):(\d\d):(\d\d) elapsed time")
THEORY_DONE_RE = re.compile(r":\s+theory\s+\S+\s+100%")
ERROR_RE = re.compile(r"^\*\*\*\s+(.*)")
# Each Isabelle error block closes with a location marker of the form
#     *** At command "by" (line 3721 of "~/.../AlphabetEnlargement_Setup.thy")
# The parallel checker elaborates every reachable obligation, so a single
# failed build can carry several of these — but the FAIL/timeout summary
# only shows the first error block.  Counting these markers (deduped by
# theory+line) tells the reader how many distinct loci are already in the
# log, replacing the discover-one-locus-per-rebuild tax.
AT_COMMAND_RE = re.compile(
    r'^\*\*\*\s+At command\s+"[^"]*"\s+\(line\s+(\d+)\s+of\s+"([^"]+)"\)'
)
THEORY_PROGRESS_RE = re.compile(r"^(\S+):\s+theory\s+(\S+?)(?:\s+(\d+)%)?")
BUILD_PHASE_RE = re.compile(r"^(Session |Running )")

# Isabelle's long-running-command warning, emitted periodically while
# a single command (typically `by ...` or `apply ...`) is still
# searching for a proof.  Lines look like:
#
#     NDTHT: command "by" running for 25.674s (line 1488 of theory "NDTHT.AlphabetReduction")
#
# Consecutive matches on the same (theory, line, command) triple
# are the definitive signature of a single tactic stuck in a
# search loop; the elapsed-time field changes per emission but
# the triple stays constant.
LOOP_RE = re.compile(
    r'^\S+:\s+command\s+"(\S+)"\s+running\s+for\s+([\d.]+)s\s+'
    r'\(line\s+(\d+)\s+of\s+theory\s+"([^"]+)"\)'
)


def strip_ansi(line: str) -> str:
    return ANSI_RE.sub("", line)


def inject_progress_threshold(args: list[str], threshold: float) -> list[str]:
    """Insert `-o build_progress_threshold=<threshold>` right after the
    `build` subcommand of an `isabelle build` invocation.

    Isabelle emits its `command "X" running for Ns (line Y of theory Z)`
    warning — the only output that names the stuck line — once a single
    command has run for `build_progress_threshold` seconds (Isabelle
    default 20s).  That default coincides with the activity timeout, so a
    hang trips the bare activity kill before the line-bearing warning
    fires.  Lowering the threshold lands the warning early enough that
    loop_key (hence the stuck line) is captured.  No-op for non-build
    commands."""
    if len(args) >= 2 and Path(args[0]).name == "isabelle" and args[1] == "build":
        opt = f"build_progress_threshold={threshold:g}"
        return args[:2] + ["-o", opt] + args[2:]
    return args


# ---------------------------------------------------------------------------
# Process tree management
# ---------------------------------------------------------------------------

def get_descendants(pid: int) -> list[int]:
    """Get all descendant PIDs via pgrep -P (recursive)."""
    result = []
    try:
        out = subprocess.check_output(
            ["pgrep", "-P", str(pid)], stderr=subprocess.DEVNULL, text=True
        )
        for line in out.strip().split("\n"):
            if line.strip().isdigit():
                child = int(line.strip())
                result.append(child)
                result.extend(get_descendants(child))
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return result


# ---------------------------------------------------------------------------
# Contention: how much CPU is this build actually getting?
# ---------------------------------------------------------------------------
#
# Battery and load look like the same problem and are not, which is why one
# scalar cannot cover both:
#
#   - Battery/thermal changes how much work a CPU-second *buys*.  The process
#     still gets its CPU-second per wall-second; it accomplishes less in it.
#     No accounting can see that after the fact, so it needs a factor assumed
#     in advance -- which is exactly what BATTERY_FACTOR is.
#   - Load changes how many CPU-seconds you *get* per wall-second.  A
#     descheduled process accrues no CPU time at all.  That is directly
#     measurable, on this build, at the moment the question is asked.
#
# So load needs no prediction and no benchmark, and the measurement it needs
# is dimensionless: the **duty cycle**, CPU-seconds per wall-second of the
# process tree.  1.0 means a whole core's worth -- running flat out -- on any
# machine, fast or slow, and a threshold expressed in it is not fitted to the
# machine it was written on.  That matters more than the cost: a factor
# calibrated here would be wrong everywhere else.
#
# Load average was the obvious alternative and does not work.  It is a 60 s
# damped average, so its time constant is longer than the whole wall budget it
# would govern -- measured on an 8-core Mac, a workload that was already 1.27x
# slower after 5 s still read as idle -- and on a heterogeneous CPU (4
# performance + 4 efficiency cores here) the scheduler migrating a thread
# between core types swamps what signal remains.  It is free to read and not
# worth reading.

def _parse_ps_time(field: str) -> "float | None":
    """Seconds from `ps -o time=`: `[[DD-]HH:]MM:SS[.ss]`.

    Both formats in one parser deliberately -- macOS prints `0:01.22` and
    Linux `00:00:01`, and a watchdog that silently measured nothing on one of
    them would simply never see contention there.
    """
    field = field.strip()
    if not field:
        return None
    days = 0.0
    if "-" in field:
        d, _, field = field.partition("-")
        days = float(d)
    try:
        parts = [float(p) for p in field.split(":")]
    except ValueError:
        return None
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return days * 86400 + secs


def tree_cpu_seconds(pid: int) -> "float | None":
    """CPU seconds used by the process tree, or None if it cannot be read.

    One `ps` over the whole tree -- 2-5 ms, sampled every few seconds, so a
    fraction of a percent of a build.  `/proc` would be faster on Linux and is
    deliberately not used: the saving is milliseconds, and the platform branch
    would leave half of this untested on whichever machine the suite ran on.

    None on any failure (no `ps`, Windows, a race with the tree exiting), and
    the caller then applies no adjustment -- unmeasured contention behaves
    exactly as it did before this existed.
    """
    pids = [pid] + get_descendants(pid)
    try:
        out = subprocess.check_output(
            ["ps", "-o", "time=", "-p", ",".join(str(p) for p in pids)],
            stderr=subprocess.DEVNULL, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    total = 0.0
    seen = False
    for line in out.splitlines():
        secs = _parse_ps_time(line)
        if secs is not None:
            total += secs
            seen = True
    return total if seen else None


def duty_cycle(samples: "list[tuple[float, float]]",
               min_span: float = 2.0) -> "float | None":
    """CPU-seconds per wall-second over the samples held, or None if too few.

    Measured over a *window* rather than the whole run, because the question
    differs by kill condition: the wall budget asks "did this build get its 40
    seconds of CPU", but the activity budget asks "is it working *now*" -- and
    a build that ran flat out for a minute and then hung has a healthy
    whole-run duty cycle and a dead one.  The caller prunes the window.
    """
    if len(samples) < 2:
        return None
    (t0, c0), (t1, c1) = samples[0], samples[-1]
    span = t1 - t0
    if span < min_span:
        return None
    return max(0.0, (c1 - c0) / span)


def contention(duty: "float | None", cap: float) -> "tuple[str, float]":
    """`(verdict, factor)` from a measured duty cycle.  Pure.

    Three regimes, and the middle one is the only one that earns more time:

      - **stalled** -- essentially no CPU.  Either hung or starved to nothing,
        and no extension helps either; extending here would turn a 40 s budget
        into a 160 s one for a deadlock, which is the opposite of the point.
        This *sharpens* the activity kill, which previously could not tell a
        hang from a build that was working quietly.
      - **starved** -- progressing, but on less than a core.  The build has
        had `duty` of the machine, so the budget it has actually consumed is
        `duty x` the wall clock; giving back `1/duty` restores what it would
        have had uncontended.  Capped, because an uncapped factor is not a
        budget.
      - **running** -- a whole core or more.  Not starved, so nothing is owed.
        This is the case that preserves the cost-regression signal: a proof
        that got genuinely more expensive burns CPU at full rate and still
        trips its budget on time.

    A parallel build (`-j4`) that is starved to one core's worth reads as
    `running` and gets no extension.  That under-compensates, deliberately:
    the failure it avoids is a build with real parallelism being handed 4x its
    budget, and erring toward killing keeps the budget meaningful.
    """
    if duty is None:
        return "unknown", 1.0
    if duty < STALL_DUTY:
        return "stalled", 1.0
    if duty >= RUNNING_DUTY:
        return "running", 1.0
    return "starved", min(cap, 1.0 / duty)


def kill_tree(pid: int) -> None:
    """SIGTERM, wait, SIGKILL the process tree, then pkill poly."""
    all_pids = [pid] + get_descendants(pid)

    for p in all_pids:
        try:
            os.kill(p, signal.SIGTERM)
        except ProcessLookupError:
            pass

    time.sleep(2)

    for p in all_pids:
        try:
            os.kill(p, signal.SIGKILL)
        except ProcessLookupError:
            pass

    # Safety net for orphaned poly processes
    subprocess.run(
        ["pkill", "-TERM", "-f", "poly"],
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# Power source (battery vs AC)
# ---------------------------------------------------------------------------

POWER_SUPPLY = Path("/sys/class/power_supply")


def _on_battery_linux(root: Path | None = None) -> "bool | None":
    """Linux power state from sysfs.  `root` is injectable, since the only
    other way to test this is to own a laptop and unplug it.

    An `*/online` of 0 on every mains supply means battery; any 1 means AC.
    A desktop has no `BAT*` and often no `AC*` either, and answers None --
    correctly, since "no battery" and "on battery" must not be confused, and
    a machine that cannot be on battery needs no scaling anyway.
    """
    root = root or POWER_SUPPLY
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return None
    mains = []
    for e in entries:
        try:
            if (e / "type").read_text().strip() == "Mains":
                mains.append((e / "online").read_text().strip())
        except OSError:
            continue
    if not mains:
        return None
    return not any(v == "1" for v in mains)


def on_battery() -> "bool | None":
    """True on battery, False on AC, None if undetermined.

    None on any platform or error this cannot answer for, so the caller
    treats power state as unknown and applies no scaling.  Unknown has always
    been a supported answer here, which is why running elsewhere was never
    broken -- only unscaled.

    Two implementations, and neither is a dependency: macOS shells out to
    `pmset -g ps` (`Now drawing from 'Battery Power'` / `'AC Power'`), Linux
    reads sysfs, which is a file read and needs no subprocess at all.
    """
    if sys.platform.startswith("linux"):
        return _on_battery_linux()
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.check_output(
            ["pmset", "-g", "ps"],
            stderr=subprocess.DEVNULL, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if "Battery Power" in out:
        return True
    if "AC Power" in out:
        return False
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

class UsageError(Exception):
    """A flag this wrapper does not know, before anything has run."""


def _parse_leading_flags(argv: list[str]) -> tuple[list[str], bool | None, str]:
    """Split the watchdog's own flags off the front of the command.

    Only *leading* flags are ours, the way `env`, `nice` and `timeout` do it:
    everything from the first non-flag word onwards belongs to the child, and
    `--` ends the split explicitly for a command whose own name starts with a
    dash.  Anything else would mean this wrapper silently eating an option
    meant for `isabelle build`.

    **An unrecognised leading `-word` is an error, not a command.**  It used
    to fall through to the child, and the result was worse than a bad message:
    `isabelle-watchdog -V` supervised a program called `-V`, so it resolved a
    log directory, *created a corpus* and recorded the failure as an attempt.
    That is the failure this project documents as the hard one to notice --
    "appending to the wrong file is loud; creating the wrong file is silent
    and looks exactly like a first build" -- reachable by mistyping a flag.
    `--version` was quieter still: the command is wrapped in `stdbuf`, so it
    ran `stdbuf --version` and cheerfully reported *stdbuf's* version.

    A program whose own name begins with a dash is what `--` is for, and this
    docstring already said so.

    Returns `(command, record_override, action)`, where `action` is `""`,
    `"help"` or `"version"`.
    """
    record: bool | None = None
    while argv:
        if argv[0] in ("-h", "--help"):
            return [], record, "help"
        if argv[0] in ("-V", "--version"):
            return [], record, "version"
        if argv[0] == "--no-record":
            record = False
        elif argv[0] == "--record":
            record = True
        elif argv[0] == "--":
            return argv[1:], record, ""
        elif argv[0].startswith("-"):
            raise UsageError(
                f"unrecognised option {argv[0]!r}.  Watchdog options are "
                "-h/--help, -V/--version, --record, --no-record; everything "
                "after the first non-option word is the command.  To supervise "
                f"a program actually named {argv[0]!r}, put `--` first.")
        else:
            break
        argv = argv[1:]
    return argv, record, ""


def main() -> int:
    # --- Parse args ---
    try:
        args, record_override, action = _parse_leading_flags(sys.argv[1:])
    except UsageError as exc:
        print(f"isabelle-watchdog: {exc}", file=sys.stderr)
        return 2
    if action:
        # To stdout, exit 0.  `--help` used to be taken as the command to
        # supervise -- the one entry point named after the package, and asking
        # it for help ran it.
        print(__doc__.strip() if action == "help"
              else f"isabelle-watchdog {__version__}")
        return 0
    if not args:
        print(__doc__.strip(), file=sys.stderr)
        return 1

    if record_override is not None:
        os.environ[guard.ENV_RECORD] = "1" if record_override else "0"
    try:
        recording = guard.capture_enabled()
    except ValueError as exc:
        print(f"watchdog: {exc}", file=sys.stderr)
        return 2

    activity_timeout = int(os.environ.get("WATCHDOG_TIMEOUT", "20"))
    wall_timeout = int(os.environ.get("WALL_TIMEOUT", "40"))
    # N consecutive `command "X" running for ...s (line Y...)` warnings
    # on the same (theory, line, command) triple = a tactic in a
    # search loop on a single line.  Threshold 3 kills ~4s after
    # Isabelle's first warning (which itself fires at ~20s of elapsed
    # tactic time), well under the 40s wall budget, with a more
    # informative LOOP-on-line message than the bare wall timeout.
    loop_progress_threshold = int(os.environ.get("LOOP_PROGRESS_THRESHOLD", "3"))
    build_progress_threshold = float(os.environ.get("BUILD_PROGRESS_THRESHOLD", "15"))
    # Ceiling on the *measured* contention factor.  A budget that stretches
    # without limit is not a budget, and the tight wall timeout is doing
    # deliberate work in this design.  1.0 disables the measurement entirely,
    # including its `ps` calls -- the setting to reach for if this ever
    # misbehaves on a machine nobody here has.
    load_factor_max = float(os.environ.get("LOAD_FACTOR_MAX", "4.0"))
    cpu_sample_interval = float(os.environ.get("CPU_SAMPLE_INTERVAL",
                                               CPU_SAMPLE_INTERVAL))
    cpu_window = cpu_sample_interval * CPU_WINDOW_SAMPLES

    # Battery throttling: a laptop on battery runs ~BATTERY_FACTOR times
    # slower, so scale the time budgets to keep them in AC-equivalent
    # units (see the module docstring).  Detection failure / non-macOS =>
    # no scaling.  Done BEFORE inject_progress_threshold so the
    # loop-detection warning threshold is scaled too: otherwise a
    # battery-slow-but-fine command crosses the unscaled 15s threshold,
    # emits its consecutive same-line warnings, and is spuriously
    # loop-killed while the (scaled) activity/wall budgets still had ample
    # room.
    battery = on_battery()
    battery_factor = float(os.environ.get("BATTERY_FACTOR", "2.0"))
    power = "battery" if battery else ("ac" if battery is False else "unknown")
    # Factor actually applied (1.0 = no scaling).  Also handed to the
    # build record so the captured elapsed time can be normalised to
    # AC-equivalent seconds (elapsed_s / applied_factor).
    applied_factor = battery_factor if (battery and battery_factor != 1.0) else 1.0
    if applied_factor != 1.0:
        activity_timeout = int(activity_timeout * applied_factor)
        wall_timeout = int(wall_timeout * applied_factor)
        build_progress_threshold = build_progress_threshold * applied_factor
        print(f"watchdog: on battery — budgets scaled x{applied_factor:g} "
              f"(activity {activity_timeout}s, wall {wall_timeout}s, "
              f"loop-warn {build_progress_threshold:g}s)")

    # Land Isabelle's line-bearing "running for Ns" warning before the
    # activity timeout (see inject_progress_threshold / the docstring).
    args = inject_progress_threshold(args, build_progress_threshold)

    startup_timeout = activity_timeout + 20

    # The budgets actually in force, recorded with the attempt.  Without them
    # an outcome change is ambiguous in exactly the case that matters: "the
    # proof got slower" and "the clock got tighter" produce identical records,
    # so a Makefile edit that halves WALL_TIMEOUT looks like a regression in
    # the theory.  These are the EFFECTIVE values, after battery scaling;
    # divide by battery_factor_applied to recover what was configured.
    limits = {
        "activity_timeout": activity_timeout,
        "wall_timeout": wall_timeout,
        "startup_timeout": startup_timeout,
        "loop_progress_threshold": loop_progress_threshold,
        "build_progress_threshold": build_progress_threshold,
        "battery_factor_applied": applied_factor,
        # The *ceiling* on the contention factor, not the factor: this one is
        # measured during the run and can vary, so it belongs with the
        # observations rather than with the budgets in force.  What the
        # budgets say here is still exactly what they said before contention
        # existed -- divide by `battery_factor_applied` for the configured
        # values, then multiply by `contention.load_factor_applied` for what
        # was actually enforced at the kill.
        "load_factor_max": load_factor_max,
    }

    # --- Log file setup ---
    # Was `Path(__file__).parent.parent / "t" / "logs"` -- the tool's own
    # directory, which named the right thing only while this script lived
    # inside the project it supervised.  Installed, it names site-packages.
    # The recorder's copy of this bug was fixed during consolidation and this
    # one was missed, because it only misplaces a log file: nothing errors,
    # and no record ever looks wrong, so there is nothing to notice.
    try:
        log_dir = corpus.resolve_log_dir(recording=recording)
    except corpus.CorpusError as exc:
        # Before the build, not during it: this is configuration, not capture,
        # so it fails fast and completely rather than being swallowed by the
        # guard.  A run that cannot say where its records go is one whose
        # records would have to be guessed at afterwards.
        print(f"watchdog: {exc}", file=sys.stderr)
        return 2
    # Both writers must land in the same directory, and the recorder resolves
    # independently (it is imported, not invoked).  Publishing the answer here
    # means one resolution per run rather than two that agree by construction.
    os.environ["WATCHDOG_LOG_DIR"] = str(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / os.environ.get("LOG_NAME", "last-build.log")

    # --- Start subprocess ---
    cmd = args
    if shutil.which("stdbuf"):
        cmd = ["stdbuf", "-oL"] + cmd

    # bufsize=0: the read loop below polls the pipe with select() and reads it
    # with os.read(), so nothing may sit in a userspace buffer where select()
    # cannot see it.  See the read loop for what that cost.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )

    # --- Collection state ---
    lines: list[str] = []  # all stripped lines
    last_activity = time.monotonic()
    wall_start = time.monotonic()
    build_started = False
    timeout_reason = ""  # "", "activity", "wall", "loop_progress"
    last_progress_theory = ""
    last_progress_pct = ""
    # Stuck-command tracking: (theory, line, command, count, last_elapsed).
    # Incremented on each consecutive LOOP_RE match with the same
    # (theory, line, command) triple; reset when the triple changes.
    loop_key: tuple[str, str, str] | None = None
    loop_count = 0
    loop_elapsed = ""

    # Contention state.  `load_factor` multiplies all three budgets, exactly as
    # BATTERY_FACTOR does -- but measured from this build rather than assumed
    # about the machine, and re-measured as the machine changes under it.
    # Samples are (monotonic, tree cpu seconds); the first is taken at spawn so
    # the first duty cycle is available a window later.
    cpu_samples: list[tuple[float, float]] = []
    load_factor = 1.0
    duty: float | None = None
    # The run's CPU seconds, and deliberately NOT `cpu_samples[-1]`: the
    # sample below is taken at *spawn*, as a baseline for the first duty
    # cycle, and is a claim about the machine a millisecond before the build
    # rather than about the build.  Reading the list's tail reported that
    # baseline for any run too short to sample again -- a 3.4 s build filed
    # `cpu_time_s: 0.02` beside a correctly-null `duty_cycle`, a pair that
    # cannot both be true.  Set only from an in-loop sample, so the baseline
    # cannot reach the record at all.
    cpu_total: float | None = None
    contention_verdict = "unknown"
    if load_factor_max > 1.0:
        c0 = tree_cpu_seconds(proc.pid)
        if c0 is not None:
            cpu_samples.append((wall_start, c0))
    next_cpu_sample = wall_start + cpu_sample_interval

    # Write log header
    with open(log_path, "w") as log_f:
        log_f.write(f"=== {datetime.now():%Y-%m-%d %H:%M:%S}  {' '.join(args)}\n")

        def consume(raw: bytes) -> None:
            """Everything one line of the child's output can tell us."""
            nonlocal build_started, last_progress_theory, last_progress_pct
            nonlocal loop_key, loop_count, loop_elapsed
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                text = raw.decode("latin-1")
            stripped = strip_ansi(text.rstrip("\n\r"))

            # Log everything
            log_f.write(stripped + "\n")
            log_f.flush()
            lines.append(stripped)

            # Phase detection
            if BUILD_PHASE_RE.match(stripped):
                build_started = True

            # Track latest in-progress theory for STUCK message
            m = THEORY_PROGRESS_RE.match(stripped)
            if m and m.group(3) and m.group(3) != "100":
                last_progress_theory = m.group(2)
                last_progress_pct = m.group(3)

            # Long-running-command (loop-on-line) detection.
            # Isabelle's per-command warning fires every ~2s
            # while a tactic is still searching.  N+ consecutive
            # matches on the same (theory, line, command) triple
            # = the tactic is in a search loop on that line; we
            # surface the line in the timeout summary so the
            # culprit doesn't have to be grepped out of the log.
            mloop = LOOP_RE.match(stripped)
            if mloop:
                command, elapsed, lineno, theory = mloop.groups()
                key = (theory, lineno, command)
                if key == loop_key:
                    loop_count += 1
                else:
                    loop_key = key
                    loop_count = 1
                loop_elapsed = elapsed

        # --- Read loop with select ---
        #
        # Read the pipe RAW (os.read on the fd) and split lines here, rather
        # than calling `proc.stdout.readline()`.  A buffered reader pulls a
        # whole chunk into userspace and hands back one line; select() then
        # sees an empty pipe and reports not-ready, so the rest of that chunk
        # is stranded until more output arrives.  A child that printed four
        # lines and went quiet had exactly one of them logged, and the other
        # three were never matched against anything — no error head, no loci,
        # no loop warning.  The case where that mattered most was the one it
        # broke: a burst of output followed by a hang.
        fd = proc.stdout.fileno()
        pending = b""
        while True:
            ready, _, _ = select.select([fd], [], [], 1.0)

            if ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break  # EOF
                pending += chunk
                *complete, pending = pending.split(b"\n")
                for raw in complete:
                    consume(raw)
                last_activity = time.monotonic()

            # Budgets are checked on EVERY pass, not only when the pipe went
            # quiet.  A child that keeps talking kept select() permanently
            # ready, so the branch holding these checks never ran and the wall
            # clock was never enforced at all — `while :; do echo tick; done`
            # ran unbounded under a 3-second budget.  The activity check is
            # unaffected by moving: `last_activity` was just reset above.
            now = time.monotonic()

            # How much of a CPU is this build actually getting?  Sampled on a
            # timer rather than at the moment a budget trips, because the
            # answer has to be about the recent past: a build that ran flat
            # out and then hung looks healthy measured over the whole run.
            # One `ps` per interval, so a fraction of a percent.
            if load_factor_max > 1.0 and now >= next_cpu_sample:
                next_cpu_sample = now + cpu_sample_interval
                cpu_now = tree_cpu_seconds(proc.pid)
                if cpu_now is not None:
                    cpu_samples.append((now, cpu_now))
                    # Cumulative for the tree since it spawned, so the latest
                    # reading is the run's total regardless of what the window
                    # below prunes.
                    cpu_total = cpu_now
                    cpu_samples = [s for s in cpu_samples
                                   if now - s[0] <= cpu_window] or cpu_samples[-1:]
                    # 0.9 of the interval: samples land where the 1-second
                    # select() poll allows, not on the tick, and rejecting a
                    # 0.98-second span as too short would drop every other
                    # measurement.
                    duty = duty_cycle(cpu_samples, cpu_sample_interval * 0.9)
                    contention_verdict, load_factor = contention(duty,
                                                                 load_factor_max)

            # Wall-clock
            if now - wall_start >= wall_timeout * load_factor:
                timeout_reason = "wall"
                break

            # Loop-on-line: a single tactic emitted N+ consecutive
            # progress warnings on the same line — it's searching
            # in a loop, not making progress.
            #
            # Scaled too, for the reason the battery factor scales it: the
            # warnings fire on Isabelle's *wall* clock, so a command starved
            # to a quarter of a core crosses the 15 s threshold having used
            # under 4 s of CPU, and three of those would loop-kill a command
            # that was merely waiting its turn.
            if loop_count >= loop_progress_threshold * load_factor:
                timeout_reason = "loop_progress"
                break

            # Activity
            idle = now - last_activity
            limit = activity_timeout if build_started else startup_timeout
            if idle >= limit * load_factor:
                timeout_reason = "activity"
                break

        # A final line with no trailing newline is still a line.
        if pending:
            consume(pending)

        log_f.write(f"=== finished {datetime.now():%Y-%m-%d %H:%M:%S}"
                    f"  timeout={timeout_reason or 'none'}\n")

    # --- Determine outcome, capture the attempt, then summarise ---
    elapsed_s = time.monotonic() - wall_start

    # The reported error is sourced from the build database, not the elided
    # console stream (see _fetch_db_error): err_lines drives the FAIL summary
    # and the attempt record, falling back to the console scrape only when the
    # database fetch yields nothing.
    db_error = ""
    err_lines = lines
    if timeout_reason:
        kill_tree(proc.pid)
        proc.wait()
        exit_code = 124
        outcome = "timeout"
        error_head = _timeout_head(timeout_reason, loop_key,
                                   loop_elapsed, wall_timeout)
        # The stuck command's theory is session-qualified
        # (`Alphabet_Enlargement.EncodingWrap_WF`), so the record keeps
        # what the head throws away: `_timeout_head` prints only the base
        # name, and the qualifier is the session.  A bare wall timeout has
        # no loop_key and so no locus — that is the case the build target
        # exists to cover.
        error_loci = ([[loop_key[0], loop_key[1]]] if loop_key else [])
    else:
        proc.wait()
        exit_code = proc.returncode
        outcome = "ok" if exit_code == 0 else "fail"
        if outcome == "fail":
            db_error = guard.run_guarded(
                "db-error", lambda: _fetch_db_error(args)) or ""
            if db_error:
                guard.run_guarded(
                    "db-error-log",
                    lambda: _append_full_error(log_path, db_error))
                err_lines = db_error.splitlines()
        error_head = "" if exit_code == 0 else _first_error(err_lines)
        # `_first_error` keeps the first two `***` lines, which for a failed
        # proof are truncated goal text — 210 records in the corpus name no
        # file at all because the `At command` marker came later.  The loci
        # are already extracted for the FAIL summary; record them too.
        error_loci = ([[thy, ln] for thy, ln in _error_loci(err_lines)]
                      if exit_code else [])

    # Trajectory capture (record.py): a builds.jsonl record carrying this
    # attempt's incremental source diff.  Guarded so it never affects the
    # build's exit code.
    #
    # Skipped entirely when capture is off, rather than entered and
    # short-circuited: `record` resolves the project, the log directory and
    # the pending note at *import* time, and a project that declined capture
    # should not be paying for -- or failing on -- any of that.
    if recording:
        # The observations, not a verdict derived from them.  A record that
        # kept only `load_factor_applied` could never answer "was that
        # timeout a hard proof or a busy laptop" once the policy changed;
        # duty cycle and CPU seconds stay meaningful whatever this file
        # decides to do with them next.
        contention_rec = {
            "cpu_time_s": (round(cpu_total, 2) if cpu_total is not None
                           else None),
            "duty_cycle": (round(duty, 3) if duty is not None else None),
            "verdict": contention_verdict,
            "load_factor_applied": round(load_factor, 2),
        }
        _record_attempt(args, outcome, exit_code, timeout_reason,
                        elapsed_s, error_head, power, applied_factor,
                        error_loci, limits, contention_rec)

    if outcome == "timeout":
        _print_summary_timeout(timeout_reason, lines, wall_timeout,
                               activity_timeout,
                               last_progress_theory, last_progress_pct,
                               loop_key, loop_count, loop_elapsed,
                               log_path, battery, contention_verdict, duty)
    elif outcome == "ok":
        _print_summary_ok(lines, log_path)
    else:
        _print_summary_fail(err_lines, log_path)

    return exit_code


# ---------------------------------------------------------------------------
# Attempt capture (trajectory axis — see record.py)
# ---------------------------------------------------------------------------

def _first_error(lines: list[str]) -> str:
    """First one or two non-empty `***` error lines, joined — the error
    head folded into the attempt record (logging-design.md §12.3.2)."""
    heads: list[str] = []
    for l in lines:
        m = ERROR_RE.match(l)
        if m and m.group(1).strip():
            heads.append(m.group(1).strip())
            if len(heads) >= 2:
                break
    return " | ".join(heads)


# isabelle-build flags that consume the following token as their value;
# everything else after `build` that is not a flag is a session name.
_BUILD_VALUE_FLAGS = {"-d", "-o", "-j", "-D", "-x", "-X", "-B", "-R",
                      "-A", "-P", "-N", "-Z", "-n_jobs"}


def _session_names(args: list[str]) -> list[str]:
    """Positional session names in an `isabelle build ...` command line."""
    try:
        i = args.index("build") + 1
    except ValueError:
        return []
    out: list[str] = []
    while i < len(args):
        a = args[i]
        if a in _BUILD_VALUE_FLAGS:
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            out.append(a)
            i += 1
    return out


def _fetch_db_error(args: list[str]) -> str:
    """The full, un-elided build error from isabelle's build database.

    Isabelle's build *console* elides long error messages -- the lone
    `...` line that swallows the `*** Failed to ...` verb and the head of
    the goal -- so the streamed output the watchdog captures is not a
    reliable error source.  The complete text survives in the build
    database; `isabelle build_log -H Error <session>` returns it verbatim.
    This is the authoritative source the FAIL summary, attempt record, and
    log should report from -- the live stream is for monitoring (progress,
    stuck/loop detection, timing), the database is for the error.  Returns
    "" when there is nothing to fetch.  Best-effort: callers wrap this so a
    fetch failure falls back to the (elided) console scrape and never
    affects the build's exit code."""
    sessions = _session_names(args)
    if not sessions:
        return ""
    iso = args[0] if args else "isabelle"
    chunks: list[str] = []
    for s in sessions:
        r = subprocess.run([iso, "build_log", "-H", "Error", s],
                           capture_output=True, text=True, timeout=60)
        body = (r.stdout or "").strip()
        if body and "***" in body:
            chunks.append(body)
    return "\n\n".join(chunks)


def _append_full_error(log_path: Path, db_error: str) -> None:
    """Append the un-elided database error to the log under a banner."""
    with open(log_path, "a") as f:
        f.write("\n=== full error (isabelle build_log -H Error) ===\n")
        f.write(db_error + "\n")


def _timeout_head(reason: str, loop_key: tuple[str, str, str] | None,
                  loop_elapsed: str, wall_timeout: int) -> str:
    """One-line timeout description for the attempt record."""
    if loop_key is not None:
        theory, lineno, cmd = loop_key
        return (f'{reason}: "{cmd}" line {lineno} of '
                f'{theory.split(".")[-1]} ({loop_elapsed}s)')
    return f"{reason} timeout ({wall_timeout}s wall)"


def _record_attempt(args: list[str], outcome: str, exit_code: int,
                    timeout_reason: str, elapsed_s: float,
                    error_head: str, power: str = "unknown",
                    battery_factor: float = 1.0,
                    error_loci: "list[list[str]] | None" = None,
                    limits: "dict | None" = None,
                    contention_rec: "dict | None" = None) -> None:
    """Hand the attempt to the recorder under the shared best-effort guard,
    which here additionally covers the `import` itself (a guard inside
    `record` cannot catch its own import failure), so a missing or broken
    capture module can never cost a build.

    The import stays inside the guard, and inside the function, for that
    reason -- hoisting it to the top of the module would put it outside the
    guard's reach and make an unimportable recorder fatal to every build."""
    def go() -> None:
        from . import record as recorder
        recorder.record(
            argv=args, outcome=outcome, exit_code=exit_code,
            timeout_reason=timeout_reason, elapsed_s=elapsed_s,
            error_head=error_head, power=power, battery_factor=battery_factor,
            log_name=os.environ.get("LOG_NAME", "last-build.log"),
            error_loci=error_loci or [], limits=limits,
            contention=contention_rec,
        )
    guard.run_guarded("build-record", go, lost=guard.ATTEMPT_LOST)


# ---------------------------------------------------------------------------
# Summary formatters
# ---------------------------------------------------------------------------

def _count_error_loci(lines: list[str]) -> int:
    """Number of distinct error loci in the log — each Isabelle error
    block closes with a `*** At command "X" (line N of "T")` marker.
    Deduped by (theory, line) so a locus reported twice counts once."""
    return len(_error_loci(lines))


def _error_loci(lines: list[str]) -> list[tuple[str, str]]:
    """Ordered, deduped `(theory, line)` error loci from the
    `*** At command "X" (line N of "T")` markers.  These are the jump
    targets a reader wants *first* — the failing command's line beats
    the goal-display context, which is often the same shape every
    iteration of a multi-site fix.  Order = first appearance in the log.

    The theory is kept **whole**, path and all, and shortened at the point
    of display.  It used to be shortened here, which was fine while the
    only consumer was the terminal and wrong once the loci went into the
    attempt record: the `t/<sess>/` prefix is exactly what attribution
    reads, and a bare base name cannot supply it — 11 base names have
    lived in more than one session directory (logging-design.md §13.2.1).
    """
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for l in lines:
        m = AT_COMMAND_RE.match(l)
        if m:
            lineno, theory = m.groups()
            key = (theory, lineno)
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def _stuck_locus(loop_key: "tuple[str, str, str] | None") -> str:
    """`<short-theory> line <N>` for the stuck command, or "" if unknown.
    Derived from the last `command running for ...s (line N of theory T)`
    warning the watchdog saw before the kill."""
    if loop_key is None:
        return ""
    theory, lineno, _cmd = loop_key
    return f"{theory.split('.')[-1]} line {lineno}"


def _log_line(log_path: Path, lines: list[str],
              loop_key: "tuple[str, str, str] | None" = None) -> str:
    """The `log: <path>` summary line.  When the build was killed with a
    known stuck command, name it (`stuck at <theory> line <N>`) — this is
    the jump target a reader wants first on a hang.  Otherwise annotate
    with the distinct error-locus count when the parallel checker
    surfaced more than the one error the FAIL/timeout block displays.
    Singular/plural so the line reads naturally; no annotation when there
    is nothing to point at."""
    stuck = _stuck_locus(loop_key)
    if stuck:
        return f"log: {log_path} (stuck at {stuck})"
    n = _count_error_loci(lines)
    if n == 1:
        return f"log: {log_path} (1 error locus)"
    if n > 1:
        return f"log: {log_path} ({n} error loci)"
    return f"log: {log_path}"


def _print_summary_ok(lines: list[str], log_path: Path) -> None:
    n_theories = sum(1 for l in lines if THEORY_DONE_RE.search(l))
    elapsed = cpu = ""
    for l in lines:
        m = TIMING_RE.match(l)
        if m:
            elapsed = f"{float(m.group(3)):.0f}s elapsed"
            cpu = f"{float(m.group(4)):.0f}s cpu"
    # Fallback: parse total elapsed from "H:MM:SS elapsed time" line
    if not elapsed:
        for l in lines:
            m = TOTAL_ELAPSED_RE.match(l)
            if m:
                secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                elapsed = f"{secs}s elapsed"
    parts = ["OK"]
    if n_theories:
        parts.append(f"{n_theories} theories")
    if elapsed:
        parts.append(elapsed)
    if cpu:
        parts.append(cpu)
    # No `log:` line on success — the log path matters only for
    # diagnosis, and printing it after `OK` invites the eye to look
    # for something to do.
    print("  ".join(parts))


def _print_summary_fail(lines: list[str], log_path: Path) -> None:
    """Print FAIL summary including the first error block.

    The first line is `log: <path>` so a `make build | head -N`
    invocation always captures the log path even when the error
    block is long.  Then `FAIL <first *** line>` followed by up to
    FAIL_BLOCK_LINES - 1 additional contiguous `***` lines,
    capturing the goal display / location info that follows.
    Together this saves a follow-up grep on `t/logs/last-build.log`
    for the typical diagnose-the-failure workflow.
    """
    FAIL_BLOCK_LINES = 6  # total *** lines including the first
    block: list[str] = []
    in_block = False
    for l in lines:
        m = ERROR_RE.match(l)
        if m:
            if not in_block:
                in_block = True
            content = m.group(1).rstrip()
            # Skip blank `*** ` lines — they waste the head -N budget
            # without adding signal.  The error block's structure
            # (location / cause / goal display) is readable without
            # them.
            if not content:
                continue
            block.append(content)
            if len(block) >= FAIL_BLOCK_LINES:
                break
        elif in_block:
            # First non-*** line after entering the block; stop.
            break

    def _trunc(s: str, n: int = 100) -> str:
        return s if len(s) <= n else s[: n - 3] + "..."

    print(_log_line(log_path, lines))
    loci = _error_loci(lines)
    if loci:
        # Line numbers first — the jump targets — then the goal-display
        # context (indented).  Priority order per the diagnose workflow:
        # you want `file:line` before re-reading the same goal shape.
        print("FAIL  " + ", ".join(f"{thy.rsplit('/', 1)[-1]}:{ln}"
                                   for thy, ln in loci))
        for cont in block:
            print(f"      {_trunc(cont.lstrip())}")
    elif block:
        print(f"FAIL  {_trunc(block[0])}")
        for cont in block[1:]:
            # Strip the leading whitespace that `***   ...` has, but
            # keep meaningful indentation (goal body, etc.).
            print(f"      {_trunc(cont.lstrip())}")
    else:
        # No *** line found; show last non-empty line
        for l in reversed(lines):
            if l.strip():
                print(f"FAIL  {_trunc(l.strip())}")
                break
        else:
            print("FAIL  (no output)")


def _print_summary_timeout(
    reason: str,
    lines: list[str],
    wall_timeout: int,
    activity_timeout: int,
    progress_theory: str,
    progress_pct: str,
    loop_key: tuple[str, str, str] | None,
    loop_count: int,
    loop_elapsed: str,
    log_path: Path,
    battery: "bool | None" = None,
    contention_verdict: str = "unknown",
    duty: "float | None" = None,
) -> None:
    # log: first so `head -N` captures it even if the diagnostic
    # line ends up wrapped.  Name the stuck command's line when we have
    # it (any reason — activity/loop/wall); otherwise a timeout can still
    # carry already-elaborated error loci (the checker failed some
    # obligations before the slow one tripped the budget), so annotate the
    # count.
    print(_log_line(log_path, lines, loop_key))
    # The budgets were already scaled for both of these (see main); they are
    # still worth saying on a timeout, because they change what the operator
    # should do about it.  `stalled` is the one that earns its line: it turns
    # "the build timed out" into "the build timed out without using the CPU",
    # which is a diagnosis rather than a report -- and it is the case where
    # nothing was scaled, so nothing else would hint at it.
    notes = []
    if battery:
        notes.append("on battery — likely slowness, not a hang")
    if contention_verdict == "starved" and duty:
        notes.append(f"machine contended — this build got {duty:.2f} of a "
                     f"core, and its budgets were scaled to match")
    elif contention_verdict == "stalled":
        notes.append("used no CPU — a hang, not a busy machine")
    batt = f"  ({'; '.join(notes)})" if notes else ""
    if reason == "loop_progress" and loop_key is not None:
        theory, lineno, cmd = loop_key
        short_theory = theory.split(".")[-1]
        print(f'LOOP  {short_theory}: "{cmd}" looping on line {lineno} '
              f'({loop_count}x same line, last {loop_elapsed}s elapsed)')
    elif reason == "wall":
        # Even on a bare wall timeout, surface the looping line if
        # we have one — Isabelle's per-command warnings make the
        # culprit obvious, no reason to make the user grep for it.
        if loop_key is not None:
            theory, lineno, cmd = loop_key
            short_theory = theory.split(".")[-1]
            print(f"TIMEOUT  {wall_timeout}s wall clock exceeded "
                  f'(looping on {short_theory} line {lineno} — '
                  f'"{cmd}" running for {loop_elapsed}s){batt}')
        else:
            print(f"TIMEOUT  {wall_timeout}s wall clock exceeded{batt}")
    elif reason == "activity":
        # If a `running for Ns` warning landed before the silence (e.g.
        # the command crossed build_progress_threshold, emitted one
        # warning, then went quiet), name its line — the activity kill is
        # otherwise the one timeout path with no locus.
        loc = ""
        if loop_key is not None:
            theory, lineno, cmd = loop_key
            loc = (f' (last: "{cmd}" at {theory.split(".")[-1]} '
                   f'line {lineno}, {loop_elapsed}s)')
        if progress_theory:
            short = progress_theory.split(".")[-1]  # drop session prefix
            print(f"STUCK  {short} {progress_pct}%  "
                  f"no output for {activity_timeout}s{loc}{batt}")
        else:
            print(f"STUCK  no output for {activity_timeout}s{loc}{batt}")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Ignore SIGINT in parent — let the child handle it, we clean up after
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    sys.exit(main())
