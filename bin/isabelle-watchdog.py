#!/usr/bin/env python3
"""
isabelle-watchdog.py — Build wrapper for Isabelle with terse output.

Runs an Isabelle build, saves full output to logs/$LOG_NAME
(default last-build.log), and prints a one-line summary to the terminal.

Usage: isabelle-watchdog.py command [args...]

Environment:
    WATCHDOG_TIMEOUT          Kill after N seconds of stalled stdout (default: 20).
    WALL_TIMEOUT              Absolute wall-clock limit (default: 40).  The 40 s
                              ceiling is project policy: a build hitting the wall
                              is a cost-regression signal (.claude/memory/
                              feedback_no_buildclean_reflex.md), not a tunable.
                              Override via env var only when investigating that
                              regression.
    LOOP_PROGRESS_THRESHOLD   Kill after N consecutive Isabelle
                              `command "X" running for ...s (line Y of theory Z)`
                              warnings on the same (theory, line, command) triple
                              (default: 3).  Surfaces a tactic stuck in a search
                              loop on a single line before the wall timeout
                              fires, and the timeout summary names the line.
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
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    # --- Parse args ---
    args = sys.argv[1:]
    if not args:
        print(__doc__.strip(), file=sys.stderr)
        return 1

    activity_timeout = int(os.environ.get("WATCHDOG_TIMEOUT", "20"))
    wall_timeout = int(os.environ.get("WALL_TIMEOUT", "40"))
    # N consecutive `command "X" running for ...s (line Y...)` warnings
    # on the same (theory, line, command) triple = a tactic in a
    # search loop on a single line.  Threshold 3 kills ~4s after
    # Isabelle's first warning (which itself fires at ~20s of elapsed
    # tactic time), well under the 40s wall budget, with a more
    # informative LOOP-on-line message than the bare wall timeout.
    loop_progress_threshold = int(os.environ.get("LOOP_PROGRESS_THRESHOLD", "3"))
    startup_timeout = activity_timeout + 20

    # --- Log file setup ---
    script_dir = Path(__file__).resolve().parent
    project_dir = script_dir.parent
    log_dir = project_dir / "t" / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / os.environ.get("LOG_NAME", "last-build.log")

    # --- Start subprocess ---
    cmd = args
    if shutil.which("stdbuf"):
        cmd = ["stdbuf", "-oL"] + cmd

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
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

        # --- Read loop with select ---
        fd = proc.stdout.fileno()
        while True:
            ready, _, _ = select.select([fd], [], [], 1.0)

            if ready:
                raw = proc.stdout.readline()
                if not raw:
                    break  # EOF
                try:
                    text = raw.decode("utf-8", errors="replace")
                except Exception:
                    text = raw.decode("latin-1")
                text = text.rstrip("\n\r")
                stripped = strip_ansi(text)

                # Log everything
                log_f.write(stripped + "\n")
                log_f.flush()
                lines.append(stripped)
                last_activity = time.monotonic()

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
                    cmd, elapsed, lineno, theory = mloop.groups()
                    key = (theory, lineno, cmd)
                    if key == loop_key:
                        loop_count += 1
                    else:
                        loop_key = key
                        loop_count = 1
                    loop_elapsed = elapsed
            else:
                # No data — check timeouts
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

        log_f.write(f"=== finished {datetime.now():%Y-%m-%d %H:%M:%S}"
                    f"  timeout={timeout_reason or 'none'}\n")

    # --- Determine outcome, capture the attempt, then summarise ---
    elapsed_s = time.monotonic() - wall_start

    if timeout_reason:
        kill_tree(proc.pid)
        proc.wait()
        exit_code = 124
        outcome = "timeout"
        error_head = _timeout_head(timeout_reason, loop_key,
                                   loop_elapsed, wall_timeout)
    else:
        proc.wait()
        exit_code = proc.returncode
        outcome = "ok" if exit_code == 0 else "fail"
        error_head = "" if exit_code == 0 else _first_error(lines)

    # Trajectory capture (bin/build_record.py): snapshot the working tree
    # to refs/attempts + a builds.jsonl record.  Guarded so it never
    # affects the build's exit code.
    _record_attempt(args, outcome, exit_code, timeout_reason,
                    elapsed_s, error_head)

    if outcome == "timeout":
        _print_summary_timeout(timeout_reason, lines, wall_timeout,
                               activity_timeout,
                               last_progress_theory, last_progress_pct,
                               loop_key, loop_count, loop_elapsed,
                               log_path)
    elif outcome == "ok":
        _print_summary_ok(lines, log_path)
    else:
        _print_summary_fail(lines, log_path)

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
                    error_head: str) -> None:
    """Hand the attempt to build_record.  build_record itself never
    raises; this guard additionally covers an import failure, so a
    missing/broken capture module can never cost a build."""
    try:
        import build_record
        build_record.record(
            argv=args, outcome=outcome, exit_code=exit_code,
            timeout_reason=timeout_reason, elapsed_s=elapsed_s,
            error_head=error_head,
            log_name=os.environ.get("LOG_NAME", "last-build.log"),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"build-record: skipped ({type(exc).__name__}: {exc})",
              file=sys.stderr)


# ---------------------------------------------------------------------------
# Summary formatters
# ---------------------------------------------------------------------------

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

    print(f"log: {log_path}")
    if block:
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
) -> None:
    # log: first so `head -N` captures it even if the diagnostic
    # line ends up wrapped.
    print(f"log: {log_path}")
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
                  f'"{cmd}" running for {loop_elapsed}s)')
        else:
            print(f"TIMEOUT  {wall_timeout}s wall clock exceeded")
    elif reason == "activity":
        if progress_theory:
            short = progress_theory.split(".")[-1]  # drop session prefix
            print(f"STUCK  {short} {progress_pct}%  no output for {activity_timeout}s")
        else:
            print(f"STUCK  no output for {activity_timeout}s")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Ignore SIGINT in parent — let the child handle it, we clean up after
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    sys.exit(main())
