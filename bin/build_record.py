#!/usr/bin/env python3
"""build_record.py — trajectory capture for `make build` (prototype).

On every build the watchdog calls `record(...)` here, which:

  1. Snapshots the working tree (tracked-file deltas) to a chained
     commit on `refs/attempts/<instance_id>/<branch>` — failures
     included — so the sequence of code states between committed
     checkpoints is recoverable and diffable.
  2. Appends one JSON line to `t/logs/builds.jsonl` with the outcome,
     timing, error head, and the snapshot's object id (the join key
     between code state and verdict).

Design choices (see logging-design.md §§12–16 for the full design;
this is the §14 MVP — "the snapshot ref + outcome stops the bleeding";
§16 covers the multi-instance pooling/federation the format enables):

  - **Tracked-file deltas only** (`git add -u`, seeded from HEAD):
    captures edits to theories git already tracks — the proof delta —
    with no untracked noise.  A brand-new *untracked* `.thy` is not
    snapshotted until its first `git add`; the common case (editing
    existing theories) is fully covered.
  - **Per-instance isolation, poolable later.**  The ref is keyed by a
    stable per-working-copy `instance_id` (minted once, gitignored in
    `t/logs/instance-id`), and every record carries it plus
    host/contributor/origin provenance — so parallel worktrees and
    independent clones never collide and their trajectories merge by
    union into one dataset (logging-design.md §16).  The ref is still
    never pushed and `t/logs/` is still gitignored: capture stays local
    until an explicit pool/publish step (§12.5 loss #6).
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
    instance = _instance_id()
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]) or "DETACHED"
    head = _git(["rev-parse", "HEAD"])
    head_tree = _git(["rev-parse", "HEAD^{tree}"])
    # Instance-keyed, not branch-keyed: deleting a branch leaves its old
    # refs/attempts/<branch> dangling, so a reused name would silently
    # concatenate two unrelated efforts.  Keying by the minted instance id
    # keeps every working copy's chain independent (logging-design.md §16).
    ref = f"refs/attempts/{instance}/{branch}"

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

    # Provenance for cross-instance pooling: the (instance_id, build_id)
    # pair is the global merge key; host/contributor/origin attribute a
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
        "git_head": head,                    # committed checkpoint this sits on
        "head_dirty": tree != head_tree,     # False = rebuild of an unchanged tree
        "snapshot": commit,                  # refs/attempts/<instance>/<branch> tip
        "parent_snapshot": parent,
        "log": log_name,
    }
    with open(BUILDS_JSONL, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
