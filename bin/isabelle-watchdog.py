#!/usr/bin/env python3
"""
isabelle-watchdog.py — Build wrapper for Isabelle with terse output.

Runs an Isabelle build, saves full output to logs/$LOG_NAME
(default last-build.log), and prints a one-line summary to the terminal.

Usage: isabelle-watchdog.py command [args...]

Environment:
    WATCHDOG_TIMEOUT      Kill after N seconds of stalled stdout (default: 20).
    WALL_TIMEOUT          Absolute wall-clock limit (default: 40).  The 40 s
                          ceiling is project policy: a build hitting the wall
                          is a cost-regression signal (.claude/memory/
                          feedback_no_buildclean_reflex.md), not a tunable.
                          Override via env var only when investigating that
                          regression.
    REPETITION_THRESHOLD  Kill after N identical lines (default: 3)
    LOG_NAME              Log file basename under logs/ (default: last-build.log).
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
    rep_threshold = int(os.environ.get("REPETITION_THRESHOLD", "3"))
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
    last_line = ""
    rep_count = 0
    timeout_reason = ""  # "", "activity", "wall", "repetition"
    last_progress_theory = ""
    last_progress_pct = ""

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

                # Repetition detection
                if stripped and stripped == last_line:
                    rep_count += 1
                else:
                    rep_count = 0
                    if stripped:
                        last_line = stripped
            else:
                # No data — check timeouts
                now = time.monotonic()

                # Wall-clock
                if now - wall_start >= wall_timeout:
                    timeout_reason = "wall"
                    break

                # Repetition
                if rep_count >= rep_threshold:
                    timeout_reason = "repetition"
                    break

                # Activity
                idle = now - last_activity
                limit = activity_timeout if build_started else startup_timeout
                if idle >= limit:
                    timeout_reason = "activity"
                    break

        log_f.write(f"=== finished {datetime.now():%Y-%m-%d %H:%M:%S}"
                    f"  timeout={timeout_reason or 'none'}\n")

    # --- Handle timeout ---
    if timeout_reason:
        kill_tree(proc.pid)
        proc.wait()
        _print_summary_timeout(timeout_reason, lines, wall_timeout,
                               activity_timeout, rep_count, last_line,
                               last_progress_theory, last_progress_pct,
                               log_path)
        return 124

    # --- Normal exit ---
    proc.wait()
    exit_code = proc.returncode

    if exit_code == 0:
        _print_summary_ok(lines, log_path)
    else:
        _print_summary_fail(lines, log_path)

    return exit_code


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
    print("  ".join(parts))
    print(f"  log: {log_path}")


def _print_summary_fail(lines: list[str], log_path: Path) -> None:
    """Print FAIL summary including the first error block.

    The first line is `FAIL  <first *** line>`; up to FAIL_BLOCK_LINES
    additional contiguous `***` lines are shown indented, capturing the
    goal display / location info that follows.  Saves a follow-up
    grep on `t/logs/last-build.log` for the typical diagnose-the-failure
    workflow.
    """
    FAIL_BLOCK_LINES = 6  # total *** lines including the first
    block: list[str] = []
    in_block = False
    for l in lines:
        m = ERROR_RE.match(l)
        if m:
            if not in_block:
                in_block = True
            block.append(m.group(1).rstrip())
            if len(block) >= FAIL_BLOCK_LINES:
                break
        elif in_block:
            # First non-*** line after entering the block; stop.
            break

    def _trunc(s: str, n: int = 100) -> str:
        return s if len(s) <= n else s[: n - 3] + "..."

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
    print(f"  log: {log_path}")
    print(f"  session details: isabelle build_log -v NDTHT")


def _print_summary_timeout(
    reason: str,
    lines: list[str],
    wall_timeout: int,
    activity_timeout: int,
    rep_count: int,
    last_line: str,
    progress_theory: str,
    progress_pct: str,
    log_path: Path,
) -> None:
    if reason == "wall":
        print(f"TIMEOUT  {wall_timeout}s wall clock exceeded")
    elif reason == "repetition":
        short = last_line[:60] + "..." if len(last_line) > 60 else last_line
        print(f'LOOP  repeated {rep_count}x: "{short}"')
    elif reason == "activity":
        if progress_theory:
            short = progress_theory.split(".")[-1]  # drop session prefix
            print(f"STUCK  {short} {progress_pct}%  no output for {activity_timeout}s")
        else:
            print(f"STUCK  no output for {activity_timeout}s")
    print(f"  log: {log_path}")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Ignore SIGINT in parent — let the child handle it, we clean up after
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    sys.exit(main())
