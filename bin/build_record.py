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

  - **Source deltas by allowlist, tracked or not** (seeded from HEAD):
    `git add -u` and then `git add -A`, both over `SOURCE_PATHSPECS`
    (`*.thy`, `ROOT`, `ROOTS`), so a theory git has not seen yet is
    captured from its first edit and an edit to the build scripts is not
    captured at all.  The allowlist reached the tracked pass late: until
    then a proof trajectory carried every change to the Makefile, the
    tooling and the prose docs, which are not proof steps — not what the
    prover read, and not what a corpus about proving is for.  What *did*
    change outside the sources is still reported, as names and line
    counts in `other_changed`, so an outcome flip with an empty source
    diff can still be explained; the patches themselves are in the
    repository's history, which is where they belong.  Capture was
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
  - **A note carries the reasoning the diff cannot.**  The diff records
    what changed; nothing recorded what the engineer believed was wrong,
    what they were testing, or what they expected.  That is the half a
    reader cannot reconstruct — an edit's effect is recoverable from the
    sources forever, its rationale only while someone remembers it.  Write
    `<LOG_DIR>/next-note.md` before building (or pass `BUILD_NOTE=...`);
    the attempt consumes it, so a note is attached to exactly the build it
    was written for and never drifts onto a later one.  Optional: absent
    notes record as null rather than blocking anything.

    Notes are free text, with four recognised section keys — `diagnosis:`,
    `change:`, `expect:`, `ref:` — parsed into `note_fields` for querying
    while `note` keeps the text verbatim.  `expect:` is the field worth the
    trouble: a *prediction*, recorded before the outcome is known, is the
    one signal in a build corpus that says whether the engineer understood
    the system or was guessing, and it can be scored automatically against
    `outcome`.  Because that only holds if the note predates the build,
    `note_pre_build` records whether it did, rather than assuming it.

  - **Never breaks the build.**  `record(...)` swallows every error
    into a one-line stderr warning; the caller's exit code is
    untouched.  Trajectory capture must never cost a build.

Inspect captured data with `bin/attempts.py`; `bin/trajectory.py notes`
shows the notes against their outcomes.
"""

import difflib
import fnmatch
import json
import os
import re
import secrets
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

from guard import run_guarded

PROJECT_DIR = Path(__file__).resolve().parent.parent
# WATCHDOG_LOG_DIR (same variable the watchdog honours) lets a project that
# does not use the `t/` theory-tree layout say where logs go; unset reproduces
# the original behaviour exactly.
LOG_DIR = Path(os.environ.get("WATCHDOG_LOG_DIR") or (PROJECT_DIR / "t" / "logs"))
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

# The source classes that decide whether a build runs and what it proves.
# Deliberately an allowlist — a new theory must be visible from its first
# edit, but a scratch script or a draft memo must not enter the dataset before
# anyone has decided to keep it.  Git pathspec globs, so `*` crosses directory
# separators.  Overridable for projects whose sources are not Isabelle
# theories; the fallback below keeps such a project from being silently
# blinded if the list matches nothing.
SOURCE_PATHSPECS = (os.environ.get("BUILD_SOURCE_PATHSPECS") or "").split() \
                   or ["*.thy", "*ROOT", "*ROOTS"]

# The note attached to the next attempt.  Written before the build, consumed by
# it.  Kept under LOG_DIR so it inherits whatever gitignore already covers the
# logs, and so a pending note is visible next to the corpus it will join.
NOTE_FILE = Path(os.environ.get("BUILD_NOTE_FILE") or (LOG_DIR / "next-note.md"))

# Recognised section keys.  Deliberately few: `diagnosis` (what I think is
# wrong), `change` (what I am doing about it), `expect` (what I predict will
# happen), `ref` (a pointer — an insight id, an issue, a prior attempt).
# Anything unrecognised is still captured, under "notes".
NOTE_KEYS = ("diagnosis", "change", "expect", "ref")
# Allow a leading markdown marker so the file reads as prose either way.
NOTE_KEY_RE = re.compile(r"^\s*[-*#]{0,3}\s*([A-Za-z_]+)\s*:\s*(.*)$")
# A one-liner is the common case from a command line, where newlines are
# awkward to type and easy to mangle through a shell.  `; ` immediately before
# a recognised key is treated as a line break, so
#   -m 'diagnosis: X; change: Y; expect: ok'
# parses the same as the three-line form.  The lookahead keeps it precise: a
# semicolon inside prose is only a separator when a section key follows it.
INLINE_SPLIT_RE = re.compile(
    r";[ \t]*(?=(?:" + "|".join(NOTE_KEYS) + r")[ \t]*:)", re.IGNORECASE)
# Anchored at the start of `expect:`, so the convention is `expect: ok — why`.
# Searching the whole section would read "expect: no timeout this time, ok" as
# predicting a timeout; an unscored note is better than a miscounted one.
OUTCOME_RE = re.compile(r"^\W*(ok|fail|timeout)\b", re.IGNORECASE)


def _git(args: list[str], env: dict | None = None) -> str:
    """Run a git command from the project root, return stripped stdout.

    Raises CalledProcessError on non-zero exit; callers that expect a
    command to fail (e.g. resolving a not-yet-created ref) catch it."""
    return subprocess.run(
        ["git", *args], cwd=PROJECT_DIR, env=env,
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _git_raw(args: list[str], env: dict | None = None) -> str:
    """Like _git but returns stdout VERBATIM -- for payloads where trailing
    whitespace is significant.

    A unified diff renders a blank source line as a context line holding a
    single space, so `.strip()` silently deletes any run of blank lines at the
    end of the last hunk.  The hunk header then promises more lines than the
    body carries and `git apply` rejects the whole patch as corrupt -- which,
    since replay is sequential, also desynchronises every later attempt.  This
    was measured: 2 of 19 records in the 43sp corpus lost trailing context that
    way and took 5 of 19 down with them."""
    return subprocess.run(
        ["git", *args], cwd=PROJECT_DIR, env=env,
        capture_output=True, text=True, check=True,
    ).stdout


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
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    new_id = secrets.token_hex(8)
    INSTANCE_ID_FILE.write_text(new_id + "\n")
    return new_id


def _read_last_attempt() -> tuple[str | None, str | None, str | None]:
    """The previous attempt's (tree, git_head, full_tree).

    The third field was added later, so a file written by an older version
    parses with it as None and the first attempt after an upgrade simply
    reports no non-source changes rather than failing."""
    if LAST_ATTEMPT_FILE.exists():
        parts = LAST_ATTEMPT_FILE.read_text().strip().split("\t")
        if len(parts) >= 2 and parts[0]:
            return parts[0], parts[1], (parts[2] if len(parts) > 2 else None)
    return None, None, None


def _write_last_attempt(tree: str, head: str, full_tree: str) -> None:
    LAST_ATTEMPT_FILE.write_text(f"{tree}\t{head}\t{full_tree}\n")


def _matching_pathspecs(env: dict) -> list[str]:
    """The subset of SOURCE_PATHSPECS matching at least one file git would
    add, so the caller never hands `git add` a pathspec that matches nothing.

    Checked against tracked-or-untracked-but-not-ignored files, which is
    exactly the set `git add -A -- <spec>` would stage."""
    out = []
    for spec in SOURCE_PATHSPECS:
        listed = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard",
             "--", spec],
            cwd=PROJECT_DIR, env=env, capture_output=True, text=True,
        )
        if listed.returncode == 0 and listed.stdout.strip():
            out.append(spec)
    return out


def _snapshot_tree(sources_only: bool) -> str:
    """Write the working tree's build-relevant state to a tree object.

    Two staging passes into a throwaway index seeded from HEAD (seeding
    from HEAD, rather than an empty index, is what keeps phantom
    deletions out):

      1. `git add -u [-- <SOURCE_PATHSPECS>]` — tracked files' modifications
         and deletions.
      2. `git add -A -- <SOURCE_PATHSPECS>` — new files git has not seen yet,
         restricted to the build-relevant source classes.

    Pass 2 exists because tracked-only capture is blind exactly where
    the data is most valuable: while a new theory is being authored, its
    every edit is invisible, the snapshot tree never moves, and a whole
    fail→fix run records as empty diffs (logging-design.md §13.1).  It
    is a narrow allowlist rather than a bare `git add -A` so that
    scratch scripts, draft memos, editor backups and stray artefacts
    stay out of the dataset — `.gitignore` alone is not a tight enough
    filter for files nobody has decided to keep yet.

    `sources_only` applies that same allowlist to pass 1, which until now was
    unrestricted — so a proof trajectory carried every edit to the build
    scripts, the Makefile and the prose docs alongside the theories.  Those
    are not proof steps: they are not what the prover read, they do not
    belong in a corpus about proving, and on this project they were 7 of 37
    records and the single largest payload in the file.  The index is still
    seeded from HEAD, so the tree stays complete — it simply does not absorb
    modifications outside the source classes.  A full snapshot is still taken
    separately, to report *that* something else changed without shipping it.

    If the allowlist matches nothing at all — a project whose sources are not
    Isabelle theories and that has not set BUILD_SOURCE_PATHSPECS — pass 1
    falls back to unrestricted, because capturing too much is recoverable and
    capturing nothing is not.

    The real index and working tree are untouched (GIT_INDEX_FILE
    override)."""
    env = {**os.environ, "GIT_INDEX_FILE": str(ATTEMPT_INDEX)}
    ATTEMPT_INDEX.unlink(missing_ok=True)
    try:
        _git(["read-tree", "HEAD"], env=env)
        # The pathspecs must be filtered to those that actually match:
        # `git add` exits 128 on a pathspec matching nothing, and
        # --ignore-errors does NOT cover that (it covers unreadable files).
        # A project with no ROOTS file, or none of a whole source class,
        # would otherwise lose every record to a fatal on that one pathspec.
        present = _matching_pathspecs(env)
        if sources_only and present:
            _git(["add", "-u", "--", *present], env=env)
        else:
            _git(["add", "-u"], env=env)
        # --ignore-errors so an unreadable stray file cannot cost a build.
        if present:
            _git(["add", "-A", "--ignore-errors", "--", *present], env=env)
        return _git(["write-tree"], env=env)
    finally:
        ATTEMPT_INDEX.unlink(missing_ok=True)


def _is_source(path: str) -> bool:
    """Whether a path falls under the source allowlist.  fnmatch's `*` crosses
    `/` just as a git pathspec glob does, so the two agree."""
    return any(fnmatch.fnmatch(path, spec) for spec in SOURCE_PATHSPECS)


def _other_changed(base: str, tree: str) -> list[list] | None:
    """Non-source files that changed since the previous attempt, as
    [path, added, deleted] — the *fact* of the change without its content.

    Focusing the payload would otherwise lose a real signal: an attempt whose
    outcome flipped with an empty source diff is a puzzle, and "the Makefile's
    timeout budget changed" is the answer.  Names and line counts are a few
    dozen bytes and answer it; the patch is kilobytes and belongs in the
    repository's own history, where it already is."""
    out = []
    for line in _git(["diff", "--numstat", base, tree]).splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and not _is_source(parts[2]):
            add, dele, path = parts
            out.append([path, None if add == "-" else int(add),
                        None if dele == "-" else int(dele)])
    return out or None


def _parse_note(text: str) -> dict | None:
    """Split a note into its recognised sections, forgivingly.

    A line `diagnosis: ...` opens a section that runs until the next
    recognised key, so a section can be a paragraph.  Text before the first
    key lands under "notes", and a note using no keys at all is captured whole
    the same way — a format that rejects free prose collects nothing, and the
    raw text is stored verbatim regardless.  The parse is a convenience for
    querying, never the record of what was written."""
    fields: dict[str, str] = {}
    key: str | None = None
    buf: list[str] = []
    preamble: list[str] = []
    for line in INLINE_SPLIT_RE.sub("\n", text).splitlines():
        m = NOTE_KEY_RE.match(line)
        if m and m.group(1).lower() in NOTE_KEYS:
            if key is not None:
                fields[key] = "\n".join(buf).strip()
            key, buf = m.group(1).lower(), [m.group(2)]
        elif key is not None:
            buf.append(line)
        else:
            preamble.append(line)
    if key is not None:
        fields[key] = "\n".join(buf).strip()
    leftover = "\n".join(preamble).strip()
    if leftover:
        fields["notes"] = leftover
    return {k: v for k, v in fields.items() if v} or None


def _predicted_outcome(fields: dict | None) -> str | None:
    """The outcome the note predicted, if it named one unambiguously.

    Two deliberate restrictions, both to keep the calibration statistic
    honest.  Only `expect:` counts — an outcome mentioned in passing inside a
    diagnosis is not a prediction.  And it must *open* the section, so the
    convention is `expect: fail — reasoning`; a note whose expectation is
    buried in prose scores as unpredicted rather than as a guess about what
    its author meant."""
    if not fields:
        return None
    m = OUTCOME_RE.match(fields.get("expect", ""))
    return m.group(1).lower() if m else None


def lint_note(text: str) -> list[str]:
    """Advisory complaints about a note's format.  Never blocks a build.

    That restraint is the whole design.  A linter that can refuse a build
    teaches the operator to route around the recorded entry point and run the
    prover directly, which costs the *diff* as well as the note — trading the
    irreplaceable half of the record for tidier prose in the half that is
    optional anyway.  So: complain at write time, record the complaints, and
    capture whatever was written."""
    out: list[str] = []
    fields = _parse_note(text) or {}
    seen_keys = [k for k in fields if k in NOTE_KEYS]

    # Near-miss section keys: `expects:` or `diagnose:` silently become prose,
    # which looks identical to a note that simply omitted the section.
    for line in INLINE_SPLIT_RE.sub("\n", text).splitlines():
        m = NOTE_KEY_RE.match(line)
        if m and m.group(1).lower() not in NOTE_KEYS:
            near = difflib.get_close_matches(m.group(1).lower(), NOTE_KEYS, 1, 0.6)
            if near:
                out.append(f"line starts `{m.group(1)}:` — did you mean "
                           f"`{near[0]}:`?  Unrecognised keys are kept as prose.")

    if not seen_keys:
        out.append("no recognised section (diagnosis/change/expect/ref); "
                   "captured whole as free text, which is fine but not queryable.")
    elif "expect" not in fields:
        out.append("no `expect:` — the prediction is the one field that can be "
                   "scored against the outcome automatically.")
    elif _predicted_outcome(fields) is None:
        out.append("`expect:` does not open with ok/fail/timeout, so the "
                   "prediction cannot be scored.  Write `expect: fail — why`.")
    return out


def _read_note(elapsed_s: float):
    """The note for this attempt as (text, source, pre_build, age_s).

    `BUILD_NOTE` wins over the note file so a one-liner can override a stale
    pending note.  Nothing is consumed here: the file is removed only once the
    record is safely on disk, so a capture that fails does not also destroy
    the reasoning it was meant to carry.

    The last two elements are the integrity bits that make `expect:` worth
    having.  A note written *after* the build finished is a summary, and its
    prediction is not a prediction; comparing the file's mtime against the
    build's start (now minus its elapsed time) distinguishes the two without
    trusting anyone.  `age_s` is how long the note sat before the build picked
    it up — a note left pending across an hour of further editing is
    technically pre-build and describes something else, and only the age
    exposes that.  Both are None for `BUILD_NOTE`, which is set before the
    process starts and so is neither post-hoc nor stale — provided it is
    passed per-invocation rather than exported into a shell, which is why the
    single-call form is the recommended route."""
    env_note = (os.environ.get("BUILD_NOTE") or "").strip()
    if env_note:
        return env_note, "env", None, None
    try:
        if NOTE_FILE.exists():
            text = NOTE_FILE.read_text()
            if text.strip():
                started = time.time() - (elapsed_s or 0.0)
                mtime = NOTE_FILE.stat().st_mtime
                return text, "file", mtime <= started, round(started - mtime, 1)
    except OSError:
        pass
    return None, None, None, None


def record(*, argv: list[str], outcome: str, exit_code: int,
           timeout_reason: str, elapsed_s: float, error_head: str,
           log_name: str, power: str = "unknown",
           battery_factor: float = 1.0,
           error_loci: "list[list[str]] | None" = None,
           limits: "dict | None" = None) -> None:
    """Capture one build attempt.  Never raises into the caller (the
    shared `run_guarded` swallows and warns on any failure)."""
    run_guarded("build-record", lambda: _record(
        argv, outcome, exit_code, timeout_reason,
        elapsed_s, error_head, log_name, power, battery_factor,
        error_loci or [], limits))


def _record(argv, outcome, exit_code, timeout_reason,
            elapsed_s, error_head, log_name,
            power="unknown", battery_factor=1.0, error_loci=None,
            limits=None) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    instance = _instance_id()
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]) or "DETACHED"
    head = _git(["rev-parse", "HEAD"])
    head_tree = _git(["rev-parse", "HEAD^{tree}"])

    # Snapshot the tracked working tree to a tree object purely to diff it;
    # the id is kept as an integrity / no-op-rebuild anchor, but no commit
    # chain is retained (logging-design.md §16 — episodes are portable
    # patch files, not a git ref chain).
    tree = _snapshot_tree(sources_only=True)
    tree_full = _snapshot_tree(sources_only=False)

    # The payload: the incremental change this attempt introduced.  Normally
    # that is vs the previous attempt's tree; but if HEAD moved since then (a
    # mid-flight commit — committing a failing state as a rewind point, or any
    # commit), re-baseline on the new HEAD's tree so the committed content is
    # excluded and the diff stays the small uncommitted edit.
    last_tree, last_head, last_full = _read_last_attempt()
    same_run = last_tree is not None and last_head == head
    base = last_tree if same_run else head_tree
    # _git_raw, not _git: trailing blank context lines are part of the patch.
    diff = _git_raw(["diff", "--no-color", "-M", base, tree])
    # Same baseline logic, over the unfiltered snapshot, for the report of
    # what changed outside the sources.
    other = _other_changed(last_full if (same_run and last_full) else head_tree,
                           tree_full)
    _write_last_attempt(tree, head, tree_full)

    note, note_source, note_pre_build, note_age_s = _read_note(elapsed_s)
    note_fields = _parse_note(note) if note else None

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
        # The watchdog budgets actually in force, effective (post battery
        # scaling).  Without them an outcome change cannot be attributed: a
        # build that times out because the theory got slower and one that
        # times out because the Makefile halved WALL_TIMEOUT record
        # identically, and the second reads as a proof regression.  The
        # harness's own parameters are part of the experiment's conditions.
        "limits": limits,
        "git_head": head,                    # commit built against (episode baseline / mid-flight-commit marker)
        "head_dirty": tree != head_tree,     # False = rebuild of an unchanged tree
        "tree": tree,                        # source snapshot id (integrity / no-op anchor)
        "diff": diff,                        # incremental SOURCE change vs the previous attempt
        # Non-source files that changed too, as [path, +lines, -lines].  The
        # fact, not the content: enough to explain an outcome flip that the
        # source diff cannot, without putting build scripts and prose into a
        # corpus about proving.  Their patches are in the repository history.
        "other_changed": other,
        # WHY this attempt looks the way it does.  The diff is a complete
        # record of *what* changed and no record at all of the reasoning that
        # produced it — the diagnosis of the last failure, the hypothesis
        # being tested, the prediction.  That reasoning is the expensive part
        # and the only part not reconstructible afterwards: a reader can
        # re-derive what an edit did, never why it was believed to help, and
        # never what its author expected to happen.  Null when no note was
        # left, which is honest; a stale note wrongly attached to a later
        # attempt would be worse than none, so the file is consumed on use.
        "note": note,                        # verbatim, exactly as written
        "note_fields": note_fields,          # parsed sections, for querying
        "note_predicted": _predicted_outcome(note_fields),   # ok | fail | timeout
        "note_source": note_source,          # file | env
        "note_pre_build": note_pre_build,    # False = written after the fact
        "note_age_s": note_age_s,            # how long it sat pending; large = stale
        # Advisory only, and recorded rather than enforced: it makes note
        # quality measurable across a corpus without ever having refused a
        # build over it (see lint_note).
        "note_lint": (lint_note(note) or None) if note else None,
        "log": log_name,
    }
    with open(BUILDS_JSONL, "a") as fh:
        fh.write(json.dumps(rec) + "\n")

    # Consume the note only now.  Had the append above raised, the note would
    # stay on disk and attach to the next attempt — a duplicated note is
    # recoverable, a deleted one is not.
    if note_source == "file":
        NOTE_FILE.unlink(missing_ok=True)
