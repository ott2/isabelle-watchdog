#!/usr/bin/env python3
"""build_record.py — trajectory capture for `make build` (prototype).

On every build the watchdog calls `record(...)` here, which appends one
JSON line to `t/logs/builds.jsonl` capturing the attempt: its outcome,
timing, error head, provenance, the commit it built against
(`git_head`), and — the payload — the **incremental diff** of the
tracked-file changes since the previous attempt.  Episodes (runs of
attempts ending in a success) are materialised into portable per-episode
files by `bin/trajectory-export.py`; see logging-design.md §16.

Design choices (see logging-design.md §§12–16 for the full design):

  - **Tracked deltas, plus untracked source by allowlist** (seeded from
    HEAD): `git add -u` for every tracked file, then `git add -A` over
    `UNTRACKED_PATHSPECS` (`*.thy`, `ROOT`, `ROOTS`) so a theory git has
    not seen yet is captured from its first edit.  Capture was
    tracked-only until 2026-07-27, which silently blinded it during
    exactly the highest-value work: while a new theory is authored its
    every edit is invisible, the snapshot tree never moves, and a whole
    fail→fix run records as a sequence of empty diffs.  On the first
    month of data that accounted for 26 of the 28 fail→ok flips that
    appeared to change nothing at all (logging-design.md §13.1).  The
    allowlist is narrow on purpose: scratch scripts, draft memos and
    editor backups are not proof deltas and must not enter the dataset.
  - **The diff is the payload, kept as text — not a git ref chain.**
    Earlier prototypes chained snapshots on `refs/attempts/*`; that store
    is local-only and unshareable (logging-design.md §16).  Instead each
    record carries the incremental diff directly, anchored to the public
    `git_head` commit, so an episode is a portable file needing no git
    object store to interpret.  A throwaway tree object is written only to
    *compute* the diff (its id is kept as an integrity / no-op anchor); no
    commit chain is retained.
  - **Per-instance attribution.**  Each record carries a stable
    `instance_id` (minted once in gitignored `t/logs/instance-id`) plus
    host/contributor/origin provenance, so trajectories from parallel
    worktrees / clones agglomerate by file union (logging-design.md §16).
  - **Never breaks the build.**  `record(...)` swallows every error
    into a one-line stderr warning; the caller's exit code is
    untouched.  Trajectory capture must never cost a build.

Inspect captured data with `bin/attempts.py`.
"""

import json
import os
import secrets
import socket
import subprocess
from datetime import datetime
from pathlib import Path

from isabelle_query.common import run_guarded

PROJECT_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_DIR / "t" / "logs"
BUILDS_JSONL = LOG_DIR / "builds.jsonl"
# Throwaway index for snapshotting, kept under gitignored t/logs/ so it
# never lands in a snapshot or a commit.  Per-call, removed after use.
ATTEMPT_INDEX = LOG_DIR / ".attempt-index"
# Stable per-working-copy id, minted once and persisted (gitignored).
# Distinguishes parallel worktrees / clones so their trajectories pool
# without collision (logging-design.md §16).
INSTANCE_ID_FILE = LOG_DIR / "instance-id"
# The previous attempt's (tree, git_head), so the next attempt's diff is
# incremental vs the prior attempt — except when a commit moved HEAD in
# between, where we re-baseline on the new HEAD so committed content never
# leaks into an attempt's diff (logging-design.md §16).  Gitignored.
LAST_ATTEMPT_FILE = LOG_DIR / ".last-attempt"

# Untracked files worth capturing: the source classes that decide whether a
# build runs and what it proves.  Deliberately an allowlist, not `git add -A`
# — a new theory must be visible from its first edit, but a scratch script or
# a draft memo must not enter the dataset before anyone has decided to keep
# it.  Git pathspec globs, so `*` crosses directory separators.
UNTRACKED_PATHSPECS = ["*.thy", "*ROOT", "*ROOTS"]


def _git(args: list[str], env: dict | None = None) -> str:
    """Run a git command from the project root, return stripped stdout.

    Raises CalledProcessError on non-zero exit; callers that expect a
    command to fail (e.g. resolving a not-yet-created ref) catch it."""
    return subprocess.run(
        ["git", *args], cwd=PROJECT_DIR, env=env,
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _git_opt(args: list[str]) -> str:
    """Like _git but returns "" instead of raising on non-zero exit — for
    optional provenance (user.email, origin url) that may be unset, so a
    missing value never drops the whole record."""
    return subprocess.run(
        ["git", *args], cwd=PROJECT_DIR,
        capture_output=True, text=True,
    ).stdout.strip()


def _instance_id() -> str:
    """A stable, globally-unique id for this working copy.

    Minted once (64-bit random hex — UUID-grade collision-free with no
    central registry) and persisted in gitignored t/logs/instance-id, so
    parallel worktrees and independent clones each own a distinct id and
    their trajectories merge by union (logging-design.md §16)."""
    if INSTANCE_ID_FILE.exists():
        existing = INSTANCE_ID_FILE.read_text().strip()
        if existing:
            return existing
    LOG_DIR.mkdir(exist_ok=True)
    new_id = secrets.token_hex(8)
    INSTANCE_ID_FILE.write_text(new_id + "\n")
    return new_id


def _read_last_attempt() -> tuple[str | None, str | None]:
    """The previous attempt's (tree, git_head); (None, None) for the first."""
    if LAST_ATTEMPT_FILE.exists():
        parts = LAST_ATTEMPT_FILE.read_text().strip().split("\t")
        if len(parts) == 2 and parts[0]:
            return parts[0], parts[1]
    return None, None


def _write_last_attempt(tree: str, head: str) -> None:
    LAST_ATTEMPT_FILE.write_text(f"{tree}\t{head}\n")


def _snapshot_tree() -> str:
    """Write the working tree's build-relevant state to a tree object.

    Two staging passes into a throwaway index seeded from HEAD (seeding
    from HEAD, rather than an empty index, is what keeps phantom
    deletions out):

      1. `git add -u` — every *tracked* file's modification or deletion,
         whatever its type.  This is the long-standing behaviour.
      2. `git add -A -- <UNTRACKED_PATHSPECS>` — new files git has not
         seen yet, restricted to the build-relevant source classes.

    Pass 2 exists because tracked-only capture is blind exactly where
    the data is most valuable: while a new theory is being authored, its
    every edit is invisible, the snapshot tree never moves, and a whole
    fail→fix run records as empty diffs (logging-design.md §13.1).  It
    is a narrow allowlist rather than a bare `git add -A` so that
    scratch scripts, draft memos, editor backups and stray artefacts
    stay out of the dataset — `.gitignore` alone is not a tight enough
    filter for files nobody has decided to keep yet.

    The real index and working tree are untouched (GIT_INDEX_FILE
    override)."""
    env = {**os.environ, "GIT_INDEX_FILE": str(ATTEMPT_INDEX)}
    ATTEMPT_INDEX.unlink(missing_ok=True)
    try:
        _git(["read-tree", "HEAD"], env=env)
        _git(["add", "-u"], env=env)
        # --ignore-errors so an unreadable stray file cannot cost a build;
        # the pathspecs are globs, so a no-match is not an error.
        _git(["add", "-A", "--ignore-errors", "--", *UNTRACKED_PATHSPECS],
             env=env)
        return _git(["write-tree"], env=env)
    finally:
        ATTEMPT_INDEX.unlink(missing_ok=True)


def record(*, argv: list[str], outcome: str, exit_code: int,
           timeout_reason: str, elapsed_s: float, error_head: str,
           log_name: str, power: str = "unknown",
           battery_factor: float = 1.0,
           error_loci: "list[list[str]] | None" = None) -> None:
    """Capture one build attempt.  Never raises into the caller (the
    shared `run_guarded` swallows and warns on any failure)."""
    run_guarded("build-record", lambda: _record(
        argv, outcome, exit_code, timeout_reason,
        elapsed_s, error_head, log_name, power, battery_factor,
        error_loci or []))


def _record(argv, outcome, exit_code, timeout_reason,
            elapsed_s, error_head, log_name,
            power="unknown", battery_factor=1.0, error_loci=None) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    instance = _instance_id()
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]) or "DETACHED"
    head = _git(["rev-parse", "HEAD"])
    head_tree = _git(["rev-parse", "HEAD^{tree}"])

    # Snapshot the tracked working tree to a tree object purely to diff it;
    # the id is kept as an integrity / no-op-rebuild anchor, but no commit
    # chain is retained (logging-design.md §16 — episodes are portable
    # patch files, not a git ref chain).
    tree = _snapshot_tree()

    # The payload: the incremental change this attempt introduced.  Normally
    # that is vs the previous attempt's tree; but if HEAD moved since then (a
    # mid-flight commit — committing a failing state as a rewind point, or any
    # commit), re-baseline on the new HEAD's tree so the committed content is
    # excluded and the diff stays the small uncommitted edit.
    last_tree, last_head = _read_last_attempt()
    base = head_tree if (last_tree is None or last_head != head) else last_tree
    diff = _git(["diff", "--no-color", "-M", base, tree])
    _write_last_attempt(tree, head)

    build_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]

    # Provenance for cross-instance agglomeration: the (instance_id,
    # build_id) pair is the global key; host/contributor/origin attribute a
    # record once federated (logging-design.md §16).  Optional lookups use
    # _git_opt so an unset user.email / missing origin never drops a record.
    hostname = socket.gethostname()
    contributor = _git_opt(["config", "user.email"]) or "unknown"
    origin_url = _git_opt(["config", "remote.origin.url"])

    rec = {
        "build_id": build_id,
        "instance_id": instance,             # this working copy
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "branch": branch,
        "hostname": hostname,
        "contributor": contributor,          # git config user.email or "unknown"
        "origin_url": origin_url or None,     # the clone this came from
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
        # Every `(theory, line)` the build reported an error at, whole:
        # a path for a compile error, a session-qualified name for a
        # watchdog kill.  The head keeps only the first two `***` lines,
        # which for a failed proof are truncated goal text, so 210 records
        # in the corpus name no file at all and analysis had to re-derive
        # attribution from weaker signals (logging-design.md §13.2.1).
        # Costs ~30 bytes against a 9.5 kB mean record — the diff is 95%
        # of the corpus and the error fields 0.5%, so there is no reason
        # to be sparing with the cheap half.
        "error_loci": [list(x) for x in error_loci] or None,
        "git_head": head,                    # commit built against (episode baseline / mid-flight-commit marker)
        "head_dirty": tree != head_tree,     # False = rebuild of an unchanged tree
        "tree": tree,                        # working-tree content id (integrity / no-op anchor)
        "diff": diff,                        # incremental change vs the previous attempt
        "log": log_name,
    }
    with open(BUILDS_JSONL, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
