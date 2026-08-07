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
never eaten here.

Options:
    --no-record / --record    Turn trajectory capture off / on for this run,
                              overriding $BUILD_RECORD.  Capture is on by
                              default, and is the reason the watchdog exists;
                              but the supervision is useful alone, so a
                              project that wants only that can say so instead
                              of accumulating records it will never read.
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
    BATTERY_FACTOR            On a laptop running on battery (detected via
                              `pmset -g ps`, macOS only) the machine runs
                              ~this-many times slower, so both WATCHDOG_TIMEOUT
                              and WALL_TIMEOUT are multiplied by this factor
                              (default: 2.0).  This *normalises* the budgets to
                              AC-equivalent time rather than bypassing them — a
                              build that is slow in AC-equivalent terms still
                              trips, so the cost-regression signal stays
                              meaningful, while a battery-throttled-but-fine
                              build no longer spuriously trips the activity
                              timeout.  Set to 1.0 to disable scaling.
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

from . import corpus  # resolve_log_dir — one answer to "where do records go"
from . import guard  # run_guarded — capture must never break the build

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

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

def on_battery() -> "bool | None":
    """True on battery, False on AC, None if undetermined.

    macOS only (via `pmset -g ps`); returns None on other platforms or
    on any error, so the caller treats power state as unknown and applies
    no scaling.  `pmset -g ps` reports e.g.
    `Now drawing from 'Battery Power'` / `'AC Power'`."""
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

def _parse_leading_flags(argv: list[str]) -> tuple[list[str], bool | None, bool]:
    """Split the watchdog's own flags off the front of the command.

    Only *leading* flags are ours, the way `env`, `nice` and `timeout` do it:
    everything from the first non-flag word onwards belongs to the child, and
    `--` ends the split explicitly for a command whose own name starts with a
    dash.  Anything else would mean this wrapper silently eating an option
    meant for `isabelle build`.

    Returns `(command, record_override, help_wanted)`.
    """
    record: bool | None = None
    while argv:
        if argv[0] in ("-h", "--help"):
            return [], record, True
        if argv[0] == "--no-record":
            record = False
        elif argv[0] == "--record":
            record = True
        elif argv[0] == "--":
            return argv[1:], record, False
        else:
            break
        argv = argv[1:]
    return argv, record, False


def main() -> int:
    # --- Parse args ---
    args, record_override, help_wanted = _parse_leading_flags(sys.argv[1:])
    if help_wanted:
        # To stdout, exit 0.  `--help` used to be taken as the command to
        # supervise -- the one entry point named after the package, and asking
        # it for help ran it.
        print(__doc__.strip())
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

            # Wall-clock
            if now - wall_start >= wall_timeout:
                timeout_reason = "wall"
                break

            # Loop-on-line: a single tactic emitted N+ consecutive
            # progress warnings on the same line — it's searching
            # in a loop, not making progress.
            if loop_count >= loop_progress_threshold:
                timeout_reason = "loop_progress"
                break

            # Activity
            idle = now - last_activity
            limit = activity_timeout if build_started else startup_timeout
            if idle >= limit:
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
        _record_attempt(args, outcome, exit_code, timeout_reason,
                        elapsed_s, error_head, power, applied_factor,
                        error_loci, limits)

    if outcome == "timeout":
        _print_summary_timeout(timeout_reason, lines, wall_timeout,
                               activity_timeout,
                               last_progress_theory, last_progress_pct,
                               loop_key, loop_count, loop_elapsed,
                               log_path, battery)
    elif outcome == "ok":
        _print_summary_ok(lines, log_path)
    else:
        _print_summary_fail(err_lines, log_path)

    return exit_code


# ---------------------------------------------------------------------------
# Attempt capture (trajectory axis — see bin/build_record.py)
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
                    limits: "dict | None" = None) -> None:
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
        )
    guard.run_guarded("build-record", go)


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
) -> None:
    # log: first so `head -N` captures it even if the diagnostic
    # line ends up wrapped.  Name the stuck command's line when we have
    # it (any reason — activity/loop/wall); otherwise a timeout can still
    # carry already-elaborated error loci (the checker failed some
    # obligations before the slow one tripped the budget), so annotate the
    # count.
    print(_log_line(log_path, lines, loop_key))
    # On battery the budgets are already scaled (see main); still flag it
    # on any timeout, since slowness — not a hang — is the likely cause.
    batt = "  (on battery — likely slowness, not a hang)" if battery else ""
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
