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

`isabelle-build` is the usual front door: it derives the session from the
project's ROOT files, carries a note into the record, and then runs this.
Capture happens *here* either way, so calling this directly records the
attempt too -- with no note, and with the session spelled out by hand.  Reach
for it when the argv is not one this project declares, or is not `isabelle
build` at all.

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
                              with no other output between them (default: 3).
                              Surfaces a tactic stuck in a search loop on a
                              single line before the wall timeout fires, and the
                              timeout summary names the line.  Any other line
                              resets the count, so a slow command in a session
                              whose other theories are still building is not a
                              loop.
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

# Isabelle announces every session it elaborates from source, and the verb it
# chooses is a fact about the build graph rather than a turn of phrase:
#
#     Building HOL-Library ...     <- heap stored: something depends on it
#     Running FSM_Tests ...        <- heap not stored: a leaf of the plan
#
# because `store_heap(name) = is_pure(name) || exists(_.ancestors.contains(name))`
# (Isabelle2025-2 src/Pure/Build/build_process.scala:95, printed at :1218).
# `Building` is therefore *Isabelle's own* statement that the session is a
# dependency of something else in this build — the question issue #4 asks,
# answered from the pipe rather than from a target the watchdog would have to
# be told.  `_session_names` below parses the argv for session names, but only
# to enrich an error message, where being wrong costs nothing; a diagnosis
# printed beside a kill is held to a higher bar than that.
#
# The trailing `...` is required because it is what separates these from any
# other line starting with the word: the name may be followed by a
# `(started 0:00:03 on node 1)` parenthetical under `build_log_verbose`/NUMA.
SESSION_BUILD_RE = re.compile(r"^(Building|Running)\s+(\S+)\b.*\.\.\.\s*$")
# `Session <chapter>/<name>` is the build *plan* — every session involved,
# heap-loaded ones included — and it is printed before any of them starts.
# Seeing one is what lets an empty session list mean "nothing was rebuilt"
# rather than "the output never said"; see `sessions_built`.
SESSION_PLAN_RE = re.compile(r"^Session\s+\S+/\S+")
# `Building ` belongs here for the same reason `Running ` does: it is Isabelle
# saying a session has started.  Its absence only ever granted an unearned
# `startup_timeout + 20`, and only on output too terse to carry the `Session `
# lines, which is why nothing noticed.
BUILD_PHASE_RE = re.compile(r"^(Session |Building |Running )")

# Isabelle's long-running-command warning, emitted periodically while
# a single command (typically `by ...` or `apply ...`) is still
# searching for a proof.  Lines look like:
#
#     NDTHT: command "by" running for 25.674s (line 1488 of theory "NDTHT.AlphabetReduction")
#
# Matches on the same (theory, line, command) triple with nothing else
# on the pipe between them are the signature of a single tactic stuck in
# a search loop; the elapsed-time field changes per emission but the
# triple stays constant.  The "nothing else between them" half is what
# separates a loop from a merely slow command in a session whose other
# theories are still building — see the detector in `supervise`.
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


def tree_cpu_by_pid(pid: int) -> "dict[int, float] | None":
    """CPU seconds per *live* process in the tree, or None if unreadable.

    One `ps` over the whole tree -- 2-5 ms, sampled every few seconds, so a
    fraction of a percent of a build.  `/proc` would be faster on Linux and is
    deliberately not used: the saving is milliseconds, and the platform branch
    would leave half of this untested on whichever machine the suite ran on.

    None on any failure (no `ps`, Windows, a race with the tree exiting), and
    the caller then applies no adjustment -- unmeasured contention behaves
    exactly as it did before this existed.

    **Per pid, not a total**, because the total this used to return was not
    monotonic and the whole measurement differentiates it -- see
    `accumulate_tree_cpu`.
    """
    pids = [pid] + get_descendants(pid)
    try:
        out = subprocess.check_output(
            ["ps", "-o", "pid=,time=", "-p", ",".join(str(p) for p in pids)],
            stderr=subprocess.DEVNULL, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    live: dict[int, float] = {}
    for line in out.splitlines():
        head, _, rest = line.strip().partition(" ")
        if not head.isdigit():
            continue
        secs = _parse_ps_time(rest)
        if secs is not None:
            live[int(head)] = secs
    return live or None


def accumulate_tree_cpu(seen: "dict[int, float]",
                        sample: "dict[int, float]") -> float:
    """Fold a per-pid sample into `seen`; return the tree's CPU total.

    **The total has to be monotonic, and summing the live tree is not.**  `ps`
    reports the processes that exist *now*, so when one exits its accumulated
    CPU leaves the sum -- and Isabelle finishing a session's `poly` worker is
    exactly that event, several times per build.  The duty cycle differentiates
    this quantity, so a departure presents as a negative delta; clamped at zero
    it presented as `stalled`, the most confident verdict the policy has, on
    builds that had used 35.9 s of CPU in 40.6 s of wall clock
    (github.com/ott2/isabelle-watchdog#6).  A measurement artefact was being
    laundered into a diagnosis, and the record and the message then disagreed
    with each other in the same JSON object.

    Remembering a departed process is not a workaround for the artefact, it is
    the correct accounting: the CPU it used was really used, by this build.
    What the caller then holds is the build's cumulative CPU, which is what
    `cpu_time_s` always claimed to be.

    `>` rather than plain assignment covers pid reuse, which over a 40 s window
    is vanishingly unlikely and would otherwise reintroduce the same negative
    step.  It over-counts until the new process passes the old one's total, and
    that direction is the safe one: over-counting reads as `running` and grants
    no extension, where under-counting is the false `stalled` above.
    """
    for p, secs in sample.items():
        if secs > seen.get(p, 0.0):
            seen[p] = secs
    return sum(seen.values())


def duty_cycle(samples: "list[tuple[float, float]]",
               min_span: float = 2.0) -> "float | None":
    """CPU-seconds per wall-second over the samples held, or None if too few.

    Measured over a *window* rather than the whole run, because the question
    differs by kill condition: the wall budget asks "did this build get its 40
    seconds of CPU", but the activity budget asks "is it working *now*" -- and
    a build that ran flat out for a minute and then hung has a healthy
    whole-run duty cycle and a dead one.  The caller prunes the window.

    A *falling* total is None rather than zero.  `accumulate_tree_cpu` makes
    that unreachable, and this is the belt to its braces: the reading would be
    a broken measurement, and the one thing it must not do is arrive as a
    verdict.  None reads as `unknown`, which grants no extension either -- so
    the conservative behaviour is kept without the false diagnosis beside it.
    An *equal* total is a genuine flat window and still measures 0.0.
    """
    if len(samples) < 2:
        return None
    (t0, c0), (t1, c1) = samples[0], samples[-1]
    span = t1 - t0
    if span < min_span or c1 < c0:
        return None
    return (c1 - c0) / span


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


def orphaned_pids() -> "set[int]":
    """Pids this user owns whose parent has gone -- reparented to init.

    One `ps`.  There are a couple of hundred of these on an idle desktop
    (XPC services, agents), which is the measurement that decides the shape of
    `orphans_under`: "orphaned" alone is nowhere near a filter.
    """
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid=,ppid=,uid="],
            stderr=subprocess.DEVNULL, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return set()
    me = os.getuid()
    found: set[int] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            pid, ppid, uid = (int(x) for x in parts)
        except ValueError:
            continue
        if ppid == 1 and uid == me:
            found.add(pid)
    return found


def process_cwds(pids: "list[int]") -> "dict[int, str]":
    """Working directory per pid, where it can be read.

    `/proc/<pid>/cwd` on Linux is a readlink and costs no subprocess at all;
    everywhere else it takes one `lsof` over the whole list, because per-pid
    calls would be a hundred execs on a machine where each costs 100 ms.
    """
    if not pids:
        return {}
    if sys.platform.startswith("linux"):
        found: dict[int, str] = {}
        for p in pids:
            try:
                found[p] = os.readlink(f"/proc/{p}/cwd")
            except OSError:
                pass
        return found
    try:
        out = subprocess.check_output(
            ["lsof", "-a", "-d", "cwd", "-Fn", "-p",
             ",".join(str(p) for p in pids)],
            stderr=subprocess.DEVNULL, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return {}
    # `-F` is lsof's machine-readable mode: one field per line, tagged by its
    # first character, `p` opening each process's block.
    found = {}
    cur: "int | None" = None
    for line in out.splitlines():
        tag, rest = line[:1], line[1:]
        if tag == "p":
            cur = int(rest) if rest.isdigit() else None
        elif tag == "n" and cur is not None:
            found.setdefault(cur, rest)
    return found


def orphans_under(root: Path, ignore: "set[int]") -> "list[int]":
    """Processes of ours that lost their parent *during this build* and are
    working inside `root`.

    This is the scoped replacement for `pkill -TERM -f poly`, and it exists
    because the two nets in `kill_tree` cannot reach a process that has both
    left our group and lost its parent -- which is exactly Isabelle's ML
    outliving a JVM that died first, the case that leaks when a build is
    automated and nobody is watching the process table.

    **Working directory rather than command line.**  A pattern match asks "is
    this program called poly", which is true of every Isabelle build on the
    machine; a cwd asks "is this working inside the tree we were pointed at",
    which is true of ours and false of theirs.  It is also what identified the
    process that exposed the leak in the first place.

    Two filters, because neither is enough alone:

      - **`ignore`** is the orphan set sampled at spawn, so a process that was
        already parentless before this build started is never a candidate.
        Without it, an operator who ran from `$HOME` would sweep every orphan
        under their home directory -- a blast radius no better than the
        pattern this replaces, arrived at from the other direction.
      - **`root`** is where the operator was standing.  Isabelle runs its ML
        with the session's theory directory as cwd, which is inside it.

    Anything unreadable is skipped rather than guessed at: an orphan whose cwd
    cannot be resolved is one we cannot claim.
    """
    fresh = sorted(orphaned_pids() - ignore - {os.getpid(), os.getppid()})
    if not fresh:
        return []
    try:
        root = root.resolve()
    except OSError:
        return []
    out: list[int] = []
    for pid, cwd in process_cwds(fresh).items():
        try:
            if Path(cwd).resolve().is_relative_to(root):
                out.append(pid)
        except (OSError, ValueError):
            continue
    return out


def leads_own_group(pid: int) -> bool:
    """Is `pid` its own process-group leader — i.e. was it spawned with
    `start_new_session=True`, so that signalling its group signals only it and
    its descendants?

    Checked rather than assumed, because being wrong here is not a failed kill
    but a catastrophic one: `os.killpg` on a pid that is *not* a group leader
    signals whatever group it happens to be in, which for a child spawned the
    ordinary way is **ours** -- the watchdog, and the shell that ran it.
    """
    try:
        return os.getpgid(pid) == pid
    except (ProcessLookupError, PermissionError, OSError):
        return False


def signal_group(pid: int, sig: int) -> bool:
    """Send `sig` to `pid`'s process group.  False if that is not safe or the
    group has already gone."""
    if not leads_own_group(pid):
        return False
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def kill_tree(pid: int, root: "Path | None" = None,
              orphans_at_spawn: "set[int] | None" = None) -> None:
    """SIGTERM, wait, SIGKILL -- this build's descendants, and its group;
    then sweep what the kill orphaned inside `root`.

    **Neither a pattern match on the process table.**  This used to end with
    `pkill -TERM -f poly` as a safety net for orphaned Poly/ML, and the net
    worked: what it also did was signal every process on the machine whose
    command line contained "poly".  A profiling run of this project's own test
    suite killed three builds of an unrelated project mid-proof -- recorded as
    `fail` with no Isabelle error, at a duty cycle over a whole core -- and
    cost their operator two further attempts diagnosing the interference as a
    fault in their own theories.  One machine hosting several Isabelle
    projects is the ordinary case, not a corner.

    **Both nets, because they miss different escapees.**  Doing the group
    instead of the walk was tried, in 0.6.1, and leaked a looping Poly/ML that
    was still burning a core five minutes after the build was killed.
    Isabelle's process launcher calls `setsid()` on every bash process it
    starts -- `contrib/bash_process-*/bash_process.c`, whose opening comment
    is literally *"Bash process with separate process group id"* -- so its ML
    is never in our group in the first place.  What still binds it is
    **parentage**, through the JVM, which the walk follows and the group does
    not.  Conversely a process orphaned into our group is invisible to the
    walk and caught by the group.  Neither is a superset of the other:

        setsid'd, parent alive    -> walk finds it, group does not
        orphaned, never setsid'd  -> group finds it, walk does not

    **Enumerate before signalling anything.**  `get_descendants` follows
    parent links, and the first kill starts breaking them -- so a walk
    interleaved with the killing would lose the tail of its own tree.

    The case neither net reaches is a process that has *both* left the group
    and lost its parent -- Isabelle's ML outliving a JVM that died first, and
    also anything orphaned by this very kill in the moment between the walk's
    enumeration and the signals.  `pkill -f poly` covered that by signalling
    every Isabelle build on the machine.  `orphans_under` covers it by asking
    where a process is working rather than what it is called, and only about
    orphans that appeared since this build started -- so it runs last, in the
    position `pkill` used to hold, and for the same reason: this kill is what
    creates most of them.  Passing no `root` skips the sweep entirely.
    """
    all_pids = [pid] + get_descendants(pid)

    for p in all_pids:
        try:
            os.kill(p, signal.SIGTERM)
        except ProcessLookupError:
            pass
    signal_group(pid, signal.SIGTERM)

    time.sleep(2)

    for p in all_pids:
        try:
            os.kill(p, signal.SIGKILL)
        except ProcessLookupError:
            pass
    signal_group(pid, signal.SIGKILL)

    if root is None:
        return
    # Let the reparenting the SIGKILL above just caused actually land: a child
    # becomes init's only once its parent has been reaped, which is not
    # instant.  Sweeping first would find nothing and report success.
    time.sleep(0.5)
    # SIGTERM, not SIGKILL -- what `pkill -TERM` used, and enough for Poly/ML,
    # which then gets to remove its own temporary files.
    for p in orphans_under(root, orphans_at_spawn or set()):
        try:
            os.kill(p, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


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
    # Two things `kill_tree` needs, and both have to be taken *before* the
    # child exists (see there).  The orphan set is the baseline the final
    # sweep subtracts, so that only processes parentless since this build
    # began are candidates; `sweep_root` is where the operator is standing,
    # which is what Isabelle's ML works inside and another project's does not.
    orphans_at_spawn = orphaned_pids()
    try:
        sweep_root: "Path | None" = Path.cwd()
    except OSError:
        sweep_root = None

    # start_new_session: the child leads its own process group, so `kill_tree`
    # can signal the group as well as walking the descendants.  The two catch
    # different escapees and neither is a superset of the other -- see there.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        start_new_session=True,
    )

    # A new session has no controlling terminal, so Ctrl-C no longer reaches
    # the child on its own -- the terminal signals the *foreground* group,
    # which is now this process's, not the child's.  Forward it, so the
    # keystroke does what it always did.
    #
    # SIGINT rather than SIGTERM, and no exit here: this reproduces exactly
    # what the terminal used to deliver, and then the ordinary path takes
    # over.  The child dies, the pipe reaches EOF, the read loop ends, and the
    # attempt is recorded with whatever the child exited as.  Killing the
    # interpreter from the handler instead would lose the record -- an
    # abandoned build is still an attempt, and the note written for it is the
    # part that cannot be reconstructed.
    def _forward_sigint(_sig, _frame) -> None:
        signal_group(proc.pid, signal.SIGINT)

    try:
        signal.signal(signal.SIGINT, _forward_sigint)
    except ValueError:
        # Not the main thread -- nothing installed a handler before either.
        pass

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
    # (theory, line, command) triple; reset when the triple changes, and
    # reset by *any* other output — see `consume` for why that second one is
    # the whole of the condition rather than a refinement of it.
    loop_key: tuple[str, str, str] | None = None
    loop_count = 0
    loop_elapsed = ""
    # Sessions Isabelle elaborated from source, in start order, as
    # (name, role, seconds after spawn).  A wall budget is set with one
    # session in mind, and Isabelle silently re-elaborates every out-of-date
    # ancestor before reaching it -- so a timeout can be spent entirely on
    # other people's proofs, and nothing else in the record would say so.
    # `plan_seen` separates "nothing was rebuilt" from "the output never
    # said": the plan lines only appear under `-v`, which `build.py` always
    # passes and a bare `isabelle-watchdog` invocation may not.
    sessions_built: list[tuple[str, str | None, float]] = []
    plan_seen = False
    # `-b` makes every session store a heap, so Isabelle's verb is `Building`
    # throughout and stops discriminating -- `store_heap =
    # build_context.store_heap || state.sessions.store_heap(name)`
    # (build_process.scala:1165), where the left disjunct overrides the very
    # rule the role is read from.  The role is then *unknown*, and recording
    # it as "dependency" filed the session the operator asked for as one they
    # did not: issue #4's misdiagnosis, restated in the words of its own fix.
    # A null role costs a reader nothing it could otherwise have had; a
    # confident wrong one is the failure this file opens by describing.
    roles_meaningful = not stores_all_heaps(args)

    # Contention state.  `load_factor` multiplies all three budgets, exactly as
    # BATTERY_FACTOR does -- but measured from this build rather than assumed
    # about the machine, and re-measured as the machine changes under it.
    # Samples are (monotonic, tree cpu seconds); the first is taken at spawn so
    # the first duty cycle is available a window later.
    cpu_samples: list[tuple[float, float]] = []
    # Every pid this tree has ever held, with the most CPU it was seen to have
    # used.  The sum is the build's cumulative CPU and it only ever rises; a
    # `poly` worker that finishes keeps the seconds it spent (see
    # `accumulate_tree_cpu`).
    cpu_seen: dict[int, float] = {}
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
        s0 = tree_cpu_by_pid(proc.pid)
        if s0 is not None:
            cpu_samples.append((wall_start, accumulate_tree_cpu(cpu_seen, s0)))
    next_cpu_sample = wall_start + cpu_sample_interval

    # Write log header
    with open(log_path, "w") as log_f:
        log_f.write(f"=== {datetime.now():%Y-%m-%d %H:%M:%S}  {' '.join(args)}\n")

        def consume(raw: bytes) -> None:
            """Everything one line of the child's output can tell us."""
            nonlocal build_started, last_progress_theory, last_progress_pct
            nonlocal loop_key, loop_count, loop_elapsed, plan_seen
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
            if SESSION_PLAN_RE.match(stripped):
                plan_seen = True

            # Which sessions are being elaborated from source, and which of
            # them Isabelle calls dependencies.  Once each: the announcement
            # is printed at the moment a session starts, so its position in
            # this list is also the order the budget was spent in.
            msess = SESSION_BUILD_RE.match(stripped)
            if msess and not any(s[0] == msess.group(2) for s in sessions_built):
                sessions_built.append((
                    msess.group(2),
                    ("dependency" if msess.group(1) == "Building" else "target")
                    if roles_meaningful else None,
                    round(time.monotonic() - wall_start, 1)))

            # Track latest in-progress theory for STUCK message
            m = THEORY_PROGRESS_RE.match(stripped)
            if m and m.group(3) and m.group(3) != "100":
                last_progress_theory = m.group(2)
                last_progress_pct = m.group(3)

            # Long-running-command (loop-on-line) detection.
            # Isabelle's per-command warning fires every ~2s while a tactic is
            # still searching.  N+ consecutive matches on the same
            # (theory, line, command) triple, *with nothing else on the pipe
            # between them*, mean the build has stopped doing anything except
            # spinning on that line; we surface it in the timeout summary so
            # the culprit doesn't have to be grepped out of the log.
            #
            # "Consecutive" has to mean consecutive among ALL output, not
            # among warnings.  Isabelle builds theories in parallel, so a
            # merely slow command interleaves with the rest of the session
            # making visible progress:
            #
            #     FSM_Tests: command "by" running for 15.261s (line 1650 ...)
            #     FSM_Tests: theory FSM_Tests.Adaptive_Test_Case
            #     FSM_Tests: theory FSM_Tests.Helper_Algorithms
            #     FSM_Tests: command "by" running for 17.277s (line 1650 ...)
            #
            # Counting only the warnings called that a loop and killed a
            # healthy rebuild 73s in — and Isabelle discards a session's heap
            # image when rebuilding it, so the next attempt restarts from
            # zero (github.com/ott2/isabelle-watchdog#1).
            #
            # ANY other line resets, rather than an enumerated set of
            # "progress" lines, because the two failures are not symmetric: a
            # missed loop kill costs the gap between the loop budget and the
            # wall budget, and `_summary` names the line on a wall kill
            # anyway, while a false one destroys a partial build.
            #
            # That postpones the kill on a parallel build rather than
            # surrendering it, and postpones it to the moment its claim
            # becomes true.  Measured against a real looping `by` beside three
            # theories building in parallel: the others finished at 5.6s, and
            # the warnings then ran uninterrupted every 2s to the end of a 75s
            # capture -- so three still land 4s after the interleaving stops.
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
            else:
                # The count goes, the key stays: a wall or activity kill still
                # names the last line Isabelle complained about, which is the
                # one thing those two diagnoses otherwise cannot supply.
                loop_count = 0

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
                sample = tree_cpu_by_pid(proc.pid)
                if sample is not None:
                    cpu_now = accumulate_tree_cpu(cpu_seen, sample)
                    cpu_samples.append((now, cpu_now))
                    # Cumulative for the tree since it spawned, so the latest
                    # reading is the run's total regardless of what the window
                    # below prunes.  That sentence was false until #6 -- the
                    # sum was over the *live* tree, so it fell whenever a
                    # session's worker exited.
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
        kill_tree(proc.pid, sweep_root, orphans_at_spawn)
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

    # What the machine gave this build: the observations, not a verdict
    # derived from them.  A record that kept only `load_factor_applied` could
    # never answer "was that timeout a hard proof or a busy laptop" once the
    # policy changed; duty cycle and CPU seconds stay meaningful whatever this
    # file decides to do with them next.
    #
    # Built whether or not anything is recorded, because the timeout summary
    # reads from it too.  That summary used to take `contention_verdict` and
    # `duty` as separate arguments and print its own gloss on them, which is
    # how the message came to say "used no CPU" beside a `cpu_time_s` of 35.88
    # in the same run (#6).  One dict, quoted by both, cannot disagree with
    # itself.
    contention_rec = {
        "cpu_time_s": (round(cpu_total, 2) if cpu_total is not None else None),
        "duty_cycle": (round(duty, 3) if duty is not None else None),
        "verdict": contention_verdict,
        "load_factor_applied": round(load_factor, 2),
    }

    # Trajectory capture (record.py): a builds.jsonl record carrying this
    # attempt's incremental source diff.  Guarded so it never affects the
    # build's exit code.
    #
    # Skipped entirely when capture is off, rather than entered and
    # short-circuited: `record` resolves the project, the log directory and
    # the pending note at *import* time, and a project that declined capture
    # should not be paying for -- or failing on -- any of that.
    if recording:
        # Which sessions this build actually elaborated, and which of them
        # Isabelle called dependencies.  Same argument as `limits` and
        # `contention` above, for the third confound in the same family: a
        # timeout spent re-elaborating an out-of-date ancestor and one spent
        # on a proof that got harder are otherwise identical records, and
        # `audits/timeouts.py` — whose whole question is "is this timeout
        # load or genuine proof failure?" — has no way to derive the
        # difference.  `[]` means nothing was rebuilt, `null` means the
        # output never said (see `plan_seen`), and the two are not the same
        # claim.  A `null` *role* is the third distinction of that shape:
        # the session was elaborated, but under `-b` Isabelle's verb cannot
        # say whose it was (see `roles_meaningful`).
        sessions_rec = ([{"name": n, "role": r, "started_s": t}
                         for n, r, t in sessions_built]
                        if (sessions_built or plan_seen) else None)
        _record_attempt(args, outcome, exit_code, timeout_reason,
                        elapsed_s, error_head, power, applied_factor,
                        error_loci, limits, contention_rec, sessions_rec)

    if outcome == "timeout":
        _print_summary_timeout(timeout_reason, lines, wall_timeout,
                               activity_timeout,
                               last_progress_theory, last_progress_pct,
                               loop_key, loop_count, loop_elapsed,
                               log_path, battery, contention_rec, elapsed_s,
                               cpu_window, sessions_built)
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
    head folded into the attempt record (logging-design.md §12.3)."""
    heads: list[str] = []
    for l in lines:
        m = ERROR_RE.match(l)
        if m and m.group(1).strip():
            heads.append(m.group(1).strip())
            if len(heads) >= 2:
                break
    return " | ".join(heads)


# `isabelle build`'s own option spelling, transcribed from Isabelle2025-2
# src/Pure/Build/build.scala (the getopts table) and split by arity, because
# the two arities behave differently in every position.  One table, because
# this file asks two questions of the same command line -- which sessions were
# named, and whether `-b` was passed -- and two tables of one grammar is the
# defect this package already paid for once in its ROOT parsers.
#
# The list it replaces was hand-written and wrong in both directions: it filed
# `-R` and `-N` (both boolean) as value-taking, so `-R` swallowed whatever
# followed it, and it omitted `-g` and `-H` while carrying `-Z` and `-n_jobs`,
# which `isabelle build` has never accepted.
BUILD_VALUE_OPTS = "ABDHPXdgjox"
BUILD_FLAG_OPTS = "NRSabceflnv"


def build_options(args: list[str]) -> "tuple[set[str], list[str]]":
    """`(flags given, sessions named)` for an `isabelle build ...` argv.

    Mirrors `src/Pure/System/getopts.scala` rather than approximating it,
    because three of its rules bite here and none is guessable:

      - a boolean option **bundles** -- `-bv` is `-b -v` -- so "is this token
        exactly `-b`" misses it;
      - a value option may carry its value **attached** -- `-dt` is `-d t` --
        so "does this token contain a b" false-positives on `-dbase`;
      - option processing **stops at the first non-option token**, so a `-b`
        after the session name sets nothing.

    Fidelity matters more than it used to.  Session names only ever enriched
    an error message, where being wrong costs nothing; the flags decide what
    goes in a *record*, and CLAUDE.md's standing rule is that a payload is
    held to a higher bar than a message.
    """
    try:
        i = args.index("build") + 1
    except ValueError:
        return set(), []
    flags: set[str] = set()
    while i < len(args):
        tok = args[i]
        if tok == "--":                      # everything after is positional
            return flags, args[i + 1:]
        if len(tok) < 2 or not tok.startswith("-"):
            return flags, args[i:]           # first positional ends options
        j = 1
        while j < len(tok):
            opt = tok[j]
            if opt in BUILD_FLAG_OPTS:
                flags.add(opt)
                j += 1
                continue
            if opt in BUILD_VALUE_OPTS and j + 1 == len(tok):
                i += 1                       # bare `-d`: value is next token
            # Either the value was attached (`-dt`) or the option is one
            # Isabelle will reject outright.  Nothing more can be read from
            # this token in either case, and an unknown option fails the
            # build loudly on its own rather than needing a guess here.
            break
        i += 1
    return flags, []


def _session_names(args: list[str]) -> list[str]:
    """Positional session names in an `isabelle build ...` command line."""
    return build_options(args)[1]


def stores_all_heaps(args: list[str]) -> bool:
    """Does this command line pass `-b`, making every session store a heap?

    Public because `build.py` asks it of the arguments it is about to pass
    through, and the grammar above should exist once.
    """
    return "b" in build_options(args)[0]


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
    """One-line timeout description for the attempt record.

    The theory is session-qualified — see *Summary formatters* below.  This
    one goes into `error_head`, so it is read years later by someone who
    cannot ask which session was being built."""
    if loop_key is not None:
        theory, lineno, cmd = loop_key
        return (f'{reason}: "{cmd}" line {lineno} of '
                f'{theory} ({loop_elapsed}s)')
    return f"{reason} timeout ({wall_timeout}s wall)"


def _record_attempt(args: list[str], outcome: str, exit_code: int,
                    timeout_reason: str, elapsed_s: float,
                    error_head: str, power: str = "unknown",
                    battery_factor: float = 1.0,
                    error_loci: "list[list[str]] | None" = None,
                    limits: "dict | None" = None,
                    contention_rec: "dict | None" = None,
                    sessions: "list[dict] | None" = None) -> None:
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
            contention=contention_rec, sessions=sessions,
        )
    guard.run_guarded("build-record", go, lost=guard.ATTEMPT_LOST)


# ---------------------------------------------------------------------------
# Summary formatters
#
# Theory names are printed **session-qualified**, as Isabelle gives them
# (`FSM_Tests.Util`, not `Util`).  These used to be shortened here on the
# reasoning that when every supervised build targets one session the qualifier
# is noise -- true of the build the operator meant to run, and false of the
# build Isabelle actually planned.  A stale dependency heap silently adds its
# parent sessions, and then the qualifier is the *only* thing separating the
# operator's own code from an AFP entry they have never opened:
#
#     LOOP  Util: "by" looping on line 1650 ...              <- whose Util?
#     LOOP  FSM_Tests.Util: "by" looping on line 1650 ...    <- not mine
#
# Unconditional rather than "qualify when it differs from the target session",
# which the report suggested: the watchdog supervises an argv and has no
# reliable notion of a target -- `isabelle-watchdog <cmd>` need not be an
# `isabelle build` at all -- so the condition would have to be plumbed in from
# `build.py` and would still be absent for a direct invocation.  A qualifier
# that is sometimes there is also a qualifier the reader has to know the rule
# for, where one that is always there reads the same way every time.
#
# `_error_loci` reached the same conclusion for the record earlier and for a
# sharper reason: 11 base names have lived in more than one session directory
# in ndtht alone (logging-design.md §13.2.1).  `error_head` is a record field
# too, so the display and the record now agree, and `attempts.theory_key`
# already collapses either spelling to the same key
# (github.com/ott2/isabelle-watchdog#3).
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
    """`<theory> line <N>` for the stuck command, or "" if unknown.
    Derived from the last `command running for ...s (line N of theory T)`
    warning the watchdog saw before the kill.  Session-qualified — see
    *Summary formatters*."""
    if loop_key is None:
        return ""
    theory, lineno, _cmd = loop_key
    return f"{theory} line {lineno}"


def _stuck_session(loop_key: "tuple[str, str, str] | None",
                   progress_theory: str = "") -> str:
    """The session the build was last inside, taken from the session-qualified
    theory name Isabelle prints (`FSM_Tests.Util` → `FSM_Tests`).

    The qualifier is the whole of the derivation, which is why #3 mattered
    for more than display: an unqualified name cannot answer this at all.
    "" when no theory was seen, or when the name carries no qualifier —
    there is nothing to guess from, and guessing a session here would put a
    wrong one in a diagnosis."""
    theory = loop_key[0] if loop_key else progress_theory
    return theory.split(".", 1)[0] if "." in theory else ""


def _budget_note(sessions_built: "list[tuple[str, str | None, float]]",
                 stuck_session: str) -> str:
    """One clause for the timeout summary saying where the budget actually
    went, or "" when there is nothing to add.

    An operator sets a wall budget with one session in mind.  Isabelle
    re-elaborates every out-of-date ancestor from source first, so the clock
    can run out having never reached that session — and the summary would
    then name a theory the operator does not own and has no edit in.  The
    report was 55s of dependency compilation inside a 20s budget, read as a
    proof stuck at line 444 of somebody else's file
    (github.com/ott2/isabelle-watchdog#4).

    Roles are Isabelle's, not ours — see SESSION_BUILD_RE.  Under `-b` they
    are `None`, because Isabelle's verb stops discriminating (see
    `roles_meaningful`), and every branch below then falls through to "" —
    which is the right answer rather than a lucky one: with heaps forced,
    nothing on the pipe says which session the budget was *owed* to."""
    if not sessions_built:
        return ""
    roles = {name: role for name, role, _ in sessions_built}
    if roles.get(stuck_session) == "dependency":
        return (f"the budget went on rebuilding dependency {stuck_session}, "
                f"not on the session you asked for")
    deps = [n for n, role, _ in sessions_built if role == "dependency"]
    if deps:
        return (f"rebuilt from source first: {', '.join(deps)}")
    return ""


# Share of the wall budget that has to go before the first session starts
# before `_startup_note` says so.  Below a quarter the budget the proof got is
# essentially the one that was configured; at or above it, it is materially
# not, and that changes what the operator should do -- warm the heaps, or
# raise the budget -- rather than sending them to read a theory.  Not tuned
# against a machine: it is a fraction of the operator's own number, so it means
# the same thing whatever that number is.
STARTUP_SHARE = 0.25


def _startup_note(sessions_built: "list[tuple[str, str | None, float]]",
                  wall_timeout: int) -> str:
    """One clause when most of the budget went before any session began.

    `_budget_note` above answers "whose sessions ate the clock", and cannot
    speak at all when the answer is "nobody's": Isabelle spends its first
    seconds starting a JVM, loading the session graph and verifying ancestor
    shasums, and announces nothing until a session actually starts.  A build
    that reached its target 19.9 s into a 40 s budget was measured against
    half the budget its operator set, with nothing in the summary saying so
    (github.com/ott2/isabelle-watchdog#6).

    Deliberately a *report* and not a correction.  Starting the wall clock at
    the first session instead was the other half of that suggestion, and it
    would leave the startup phase unsupervised -- which is where a hang is
    least visible, since there is no output to miss either.  It also needs a
    notion of "the target session", and the watchdog supervises an argv and has
    none; `_budget_note`'s own docstring turns down the same shortcut.

    Roles are not consulted, so this survives `-b`, where Isabelle's verb stops
    discriminating and `_budget_note` correctly falls silent.
    """
    if not sessions_built or wall_timeout <= 0:
        return ""
    name, _role, started = sessions_built[0]
    if started < wall_timeout * STARTUP_SHARE:
        return ""
    return (f"{started:g}s of the {wall_timeout}s budget went before {name} "
            f"started — Isabelle startup, not proof time")


def _contention_note(rec: "dict | None", elapsed_s: float,
                     window: float) -> str:
    """One clause about what the machine gave this build, or "".

    Quotes `rec` -- the same dict the attempt record keeps -- rather than
    re-deriving anything, so the message and the record cannot disagree.

    **A window measurement must be reported as one.**  `stalled` used to read
    "used no CPU — a hang, not a busy machine": three claims, of which the tool
    can support one.  It measures the *recent window*, so "used no CPU" over a
    whole run is not its finding; "a hang" is an inference; and "not a busy
    machine" rules out an alternative it never tested.  Beside a `cpu_time_s`
    of 27.73 in a 40.5 s run, the reader's correct conclusion was that the tool
    was wrong -- and the wording sent them away from the log, which had the
    answer (#6).  So: scope the observation to its window, carry the cumulative
    figure that would contradict it, and hedge the inference.
    """
    rec = rec or {}
    verdict = rec.get("verdict", "unknown")
    duty, cpu = rec.get("duty_cycle"), rec.get("cpu_time_s")
    used = (f", {cpu:g}s of CPU in {elapsed_s:.0f}s wall"
            if cpu is not None else "")
    if verdict == "starved" and duty:
        return (f"machine contended — this build got {duty:.2f} of a core, "
                f"and its budgets were scaled to match")
    if verdict == "stalled":
        return (f"no CPU in the last {window:.0f}s{used} — possibly a hang")
    return ""


def _log_line(log_path: Path, lines: list[str],
              loop_key: "tuple[str, str, str] | None" = None,
              reason: str = "") -> str:
    """The `log: <path>` summary line.  When the build was killed with a
    known stuck command, name it — this is the jump target a reader wants
    first on a hang.  Otherwise annotate with the distinct error-locus count
    when the parallel checker surfaced more than the one error the
    FAIL/timeout block displays.  Singular/plural so the line reads
    naturally; no annotation when there is nothing to point at.

    **"stuck" is a claim, and only two of the three kills earn it.** A loop
    kill and an activity kill both mean nothing else was happening.  A wall
    kill does not: the command may have been merely slow, or in a dependency
    Isabelle was re-elaborating, and `stuck at <theory> line <N>` then reads
    as a verdict on a theory that was working fine (#4).  Say where the
    build was, and leave the diagnosis to the line below."""
    stuck = _stuck_locus(loop_key)
    if stuck:
        word = "last at" if reason == "wall" else "stuck at"
        return f"log: {log_path} ({word} {stuck})"
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
    contention_rec: "dict | None" = None,
    elapsed_s: float = 0.0,
    cpu_window: float = 0.0,
    sessions_built: "list[tuple[str, str | None, float]] | None" = None,
) -> None:
    # log: first so `head -N` captures it even if the diagnostic
    # line ends up wrapped.  Name the stuck command's line when we have
    # it (any reason — activity/loop/wall); otherwise a timeout can still
    # carry already-elaborated error loci (the checker failed some
    # obligations before the slow one tripped the budget), so annotate the
    # count.
    print(_log_line(log_path, lines, loop_key, reason))
    # The budgets were already scaled for both of these (see main); they are
    # still worth saying on a timeout, because they change what the operator
    # should do about it.  `stalled` is the one that earns its line: it turns
    # "the build timed out" into "the build timed out without using the CPU",
    # which is a diagnosis rather than a report -- and it is the case where
    # nothing was scaled, so nothing else would hint at it.
    notes = []
    # First, because it is the one that can make the rest of the line
    # irrelevant: a budget spent re-elaborating somebody else's session is
    # not a statement about the theory named above it.
    budget = _budget_note(sessions_built or [],
                          _stuck_session(loop_key, progress_theory))
    if budget:
        notes.append(budget)
    # Second, and independent of the first: "whose session ate the clock" has
    # no answer when the clock went before any session began.
    startup = _startup_note(sessions_built or [], wall_timeout)
    if startup:
        notes.append(startup)
    if battery:
        notes.append("on battery — likely slowness, not a hang")
    con = _contention_note(contention_rec, elapsed_s, cpu_window)
    if con:
        notes.append(con)
    batt = f"  ({'; '.join(notes)})" if notes else ""
    if reason == "loop_progress" and loop_key is not None:
        # This is the one kill that earns the word: `loop_count` reached the
        # threshold with nothing else on the pipe between the warnings.  The
        # notes still apply — a tactic looping in a dependency's theory is
        # just as much not your proof.
        theory, lineno, cmd = loop_key
        print(f'LOOP  {theory}: "{cmd}" looping on line {lineno} '
              f'({loop_count}x same line, last {loop_elapsed}s elapsed){batt}')
    elif reason == "wall":
        # Surface the last line Isabelle complained about — its per-command
        # warnings make the culprit obvious and there is no reason to make
        # anyone grep for it — but do NOT call it a loop.  `loop_key` is a
        # *locus*; `loop_count` is the verdict, and here it did not reach the
        # threshold.  The key survives any other output precisely because it
        # is not a loop claim (see `consume`), so "looping on" asserted the
        # conclusion the detector had just declined to draw: on a dependency
        # rebuild it sent the reporter hunting a runaway tactic in a theory
        # that was merely being re-elaborated (#4).  The activity branch below
        # has always worded this correctly; the two are the same claim, and
        # now read the same way.
        if loop_key is not None:
            theory, lineno, cmd = loop_key
            print(f"TIMEOUT  {wall_timeout}s wall clock exceeded "
                  f'(last: "{cmd}" at {theory} line {lineno}, '
                  f'{loop_elapsed}s){batt}')
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
            loc = (f' (last: "{cmd}" at {theory} '
                   f'line {lineno}, {loop_elapsed}s)')
        if progress_theory:
            print(f"STUCK  {progress_theory} {progress_pct}%  "
                  f"no output for {activity_timeout}s{loc}{batt}")
        else:
            print(f"STUCK  no output for {activity_timeout}s{loc}{batt}")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Ignore SIGINT until there is a child to forward it to.  `main` installs
    # a real handler as soon as it has spawned one (see `_forward_sigint`);
    # this only covers the seconds before that, where a Ctrl-C would otherwise
    # print a traceback out of argument parsing or corpus resolution.
    #
    # It used to be the whole story, on the reasoning that the child shared
    # this process group and so received the keystroke directly.  It no longer
    # does -- `start_new_session` is what makes killing by group possible --
    # so leaving this as the only handler would have made a build
    # uninterruptible.  Both entry points matter: `build.py` reaches the
    # watchdog through `python -m`, which runs this block, while the console
    # script does not run it at all and relies on `main` alone.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    sys.exit(main())
