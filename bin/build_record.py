#!/usr/bin/env python3
"""build_record.py — trajectory capture for `make build` (prototype).

On every build the watchdog calls `record(...)` here, which:

  1. Snapshots the working tree (tracked-file deltas) to a chained
     commit on `refs/attempts/<branch>` — failures included — so the
     sequence of code states between committed checkpoints is
     recoverable and diffable.
  2. Appends one JSON line to `t/logs/builds.jsonl` with the outcome,
     timing, error head, and the snapshot's object id (the join key
     between code state and verdict).

Design choices (see logging-design.md §§12–14 for the full design;
this is the §14 MVP — "the snapshot ref + outcome stops the bleeding"):

  - **Tracked-file deltas only** (`git add -u`, seeded from HEAD):
    captures edits to theories git already tracks — the proof delta —
    with no untracked noise.  A brand-new *untracked* `.thy` is not
    snapshotted until its first `git add`; the common case (editing
    existing theories) is fully covered.
  - **Local-only, commit-invisible.**  The ref is never pushed and
    `t/logs/` is gitignored, so nothing here enters the user's commits
    or the proof workflow.  Portability/export is a later step
    (logging-design.md §12.5, loss #6).
  - **Never breaks the build.**  `record(...)` swallows every error
    into a one-line stderr warning; the caller's exit code is
    untouched.  Trajectory capture must never cost a build.

Inspect captured data with `bin/attempts.py`.
"""

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from common import run_guarded

PROJECT_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_DIR / "t" / "logs"
BUILDS_JSONL = LOG_DIR / "builds.jsonl"
# Throwaway index for snapshotting, kept under gitignored t/logs/ so it
# never lands in a snapshot or a commit.  Per-call, removed after use.
ATTEMPT_INDEX = LOG_DIR / ".attempt-index"


def _git(args: list[str], env: dict | None = None) -> str:
    """Run a git command from the project root, return stripped stdout.

    Raises CalledProcessError on non-zero exit; callers that expect a
    command to fail (e.g. resolving a not-yet-created ref) catch it."""
    return subprocess.run(
        ["git", *args], cwd=PROJECT_DIR, env=env,
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _snapshot_tree() -> str:
    """Write the working tree's tracked-file state to a tree object.

    Seed a throwaway index from HEAD, then `git add -u` to apply the
    working-tree deltas to tracked files — the same delta a normal
    commit would record, so there are no phantom deletions from a
    fresh index diverging against `.gitignore`.  The real index and
    working tree are untouched (GIT_INDEX_FILE override)."""
    env = {**os.environ, "GIT_INDEX_FILE": str(ATTEMPT_INDEX)}
    ATTEMPT_INDEX.unlink(missing_ok=True)
    try:
        _git(["read-tree", "HEAD"], env=env)
        _git(["add", "-u"], env=env)
        return _git(["write-tree"], env=env)
    finally:
        ATTEMPT_INDEX.unlink(missing_ok=True)


def record(*, argv: list[str], outcome: str, exit_code: int,
           timeout_reason: str, elapsed_s: float, error_head: str,
           log_name: str, power: str = "unknown",
           battery_factor: float = 1.0) -> None:
    """Capture one build attempt.  Never raises into the caller (the
    shared `run_guarded` swallows and warns on any failure)."""
    run_guarded("build-record", lambda: _record(
        argv, outcome, exit_code, timeout_reason,
        elapsed_s, error_head, log_name, power, battery_factor))


def _record(argv, outcome, exit_code, timeout_reason,
            elapsed_s, error_head, log_name,
            power="unknown", battery_factor=1.0) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]) or "DETACHED"
    head = _git(["rev-parse", "HEAD"])
    head_tree = _git(["rev-parse", "HEAD^{tree}"])
    ref = f"refs/attempts/{branch}"

    tree = _snapshot_tree()

    # Chain onto the previous attempt snapshot so `git log <ref>` is the
    # attempt sequence; the first attempt parents off HEAD.
    try:
        parent = _git(["rev-parse", "--verify", "-q", ref])
    except subprocess.CalledProcessError:
        parent = head

    build_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    tag = outcome + (f"/{timeout_reason}" if timeout_reason else "")
    msg = f"attempt {build_id} {tag}"
    if error_head:
        msg += f"\n\n{error_head}"
    commit = _git(["commit-tree", tree, "-p", parent, "-m", msg])
    _git(["update-ref", ref, commit])

    rec = {
        "build_id": build_id,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "branch": branch,
        "command": argv,
        "outcome": outcome,                  # ok | fail | timeout
        "exit_code": exit_code,
        "timeout_reason": timeout_reason or None,
        "elapsed_s": round(elapsed_s, 1),
        # Power state and the scaling factor the watchdog applied to its
        # timeouts (battery runs ~factor times slower).  elapsed_s_ac is
        # elapsed_s normalised to AC-equivalent seconds so timings compare
        # across power states; on AC / unknown the factor is 1.0 and
        # elapsed_s_ac == elapsed_s.
        "power": power,                      # battery | ac | unknown
        "battery_factor": battery_factor,
        "elapsed_s_ac": round(elapsed_s / battery_factor, 1)
                        if battery_factor else round(elapsed_s, 1),
        "error_head": error_head or None,
        "git_head": head,                    # committed checkpoint this sits on
        "head_dirty": tree != head_tree,     # False = rebuild of an unchanged tree
        "snapshot": commit,                  # refs/attempts/<branch> tip
        "parent_snapshot": parent,
        "log": log_name,
    }
    with open(BUILDS_JSONL, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
