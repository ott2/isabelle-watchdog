"""corpus.py — where a corpus lives, and what a corpus is.

The two frontends grew in different projects and each answered these
questions for itself.  They did not answer them the same way:
`attempts.py` defaulted to `<project>/t/logs/builds.jsonl`,
`trajectory.py` to `<project>/results/isabelle-logs/builds.jsonl`, and both
computed `<project>` from **the tool's own location** — so moving the
scripts into their own repository silently repointed every default at the
tool repo, which holds no corpus at all.  `trajectory.py --repo` had the
same shape and the same fault, and there it was worse than a wrong path:
the tool repo *is* a git repository, so the "is this a git repo" guard
still passed and `check` would have reported every payload `unverified`
rather than failing.

The rule this module encodes: **a default is resolved from where the
operator is standing, never from where the tool is installed.**  A tool
that answers "the corpus of the project I was installed beside" has one
correct deployment; one that answers "the corpus of the project I am
standing in" has no wrong ones.

The same reasoning already cost 43sp a bug once in the other direction:
when its Makefile owned `WATCHDOG_LOG_DIR`, `bin/build` run directly
recorded into the recorder's built-in default instead — a second corpus, a
second instance id, and a build that looked unrecorded because its records
were somewhere else.  Reader and writer must agree on the location, so the
reader honours `WATCHDOG_LOG_DIR` too.

That fixed the reader and left the writer guessing, which cost 43sp the
*same* bug a second time: with the variable unset, the recorder still fell
back to a built-in `t/logs`, so a build run outside the Makefile minted a
fresh corpus in a project whose real one sits in `results/isabelle-logs/`.
Hence `resolve_log_dir()` — the writer now looks before it creates, using
the same discovery the reader uses, and only mints a corpus where a project
genuinely has none.  **A default is a last resort, not a first guess.**
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

# The variable the watchdog and build_record already honour.  Reading it here
# is what makes the reader land where the writer wrote.
ENV_LOG_DIR = "WATCHDOG_LOG_DIR"
# A direct override, for pointing a reader at a pooled or archived corpus that
# no writer owns.
ENV_CORPUS = "TRAJECTORY_CORPUS"

BASENAME = "builds.jsonl"

# A committed, project-owned declaration of the log directory, deliberately
# symmetric with `.isabelle-query` (same file shape, same search, same place):
# a project that already carries one marker to tell a tool where its theories
# are should not learn a second convention to tell this one where its records
# go.  First non-blank, non-comment line names the directory, relative to the
# marker itself.
#
# This is the tier discovery cannot reach.  Discovery answers "where is the
# corpus that already exists", so it is silent about a fresh clone (no corpus
# yet, and the first build would mint one in the default place rather than the
# project's) and about any layout not listed below.  A marker is checked in, so
# a clone is configured before its first build and the layout is stated rather
# than pattern-matched.
MARKER_NAME = ".isabelle-watchdog"

# Layouts seen in the wild, tried under the *current* project, not the tool's.
# Kept so an operator standing in either source project gets what they always
# got; new projects should drop a marker or set WATCHDOG_LOG_DIR rather than
# adopt one of these.
LEGACY_LAYOUTS = ("t/logs", "results/isabelle-logs")

# Where a corpus is created when a project has none and says nothing.  Only
# ever reached after the marker and discovery have both come up empty, which
# is the whole point: as an unconditional default it put a second corpus in
# 43sp, whose real one is the other layout above.
DEFAULT_LAYOUT = "t/logs"


class CorpusError(Exception):
    """No corpus could be resolved, or the one named does not exist."""


# ------------------------------------------------------------------ location

# Keyed on the resolved directory asked about, not on "the answer", so a
# process that changes directory still gets a correct one.  Resolution asks
# this question two or three times per call, and on a Mac with a security
# agent in the exec path a `git` invocation costs ~0.5s -- enough to be worth
# not paying twice for an answer that cannot change under a fixed directory.
_ROOTS: dict[Path, Path] = {}


def project_root(start: Path | None = None) -> Path:
    """The git top level containing `start` (default: cwd), or `start` itself.

    Deliberately *not* derived from `__file__`: see the module docstring.
    """
    start = (start or Path.cwd()).resolve()
    if start in _ROOTS:
        return _ROOTS[start]
    p = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        # Not cached: a directory can become a repository (`git init`, a
        # clone finishing) and the fallback is a guess, not an answer.  Only
        # a real top level is stable enough to remember.
        return start
    _ROOTS[start] = Path(p.stdout.strip())
    return _ROOTS[start]


def find_marker(start: Path | None = None) -> Path | None:
    """The nearest `.isabelle-watchdog`, at or above `start`, within the project.

    Bounded at the project root, unlike `.isabelle-query`'s unbounded walk.
    The difference is what happens when the search overshoots: a wrong session
    directory yields an empty index and an obviously wrong answer, whereas a
    wrong log directory *creates a dataset in it* and pools two projects'
    trajectories into one corpus.  Projects are routinely nested here
    (`~/projects/claudecode/ndtht`), so a stray marker in a parent must not
    capture every repository beneath it.
    """
    here = (start or Path.cwd()).resolve()
    root = project_root(here).resolve()
    chain = [here, *here.parents]
    chain = chain[:chain.index(root) + 1] if root in chain else [here]
    for d in chain:
        marker = d / MARKER_NAME
        if marker.is_file():
            return marker
    return None


def read_marker(marker: Path) -> Path:
    """The log directory a marker names, resolved against the marker itself.

    A marker that names nothing is an error rather than a no-op.  Silently
    ignoring an empty declaration reproduces the bug this file exists to
    prevent — a project that believes it has said where its records go, and a
    writer that puts them somewhere else — and it is the same reasoning that
    makes a missing `--attribution` file fatal in `attempts.py`.
    """
    for line in marker.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        p = Path(s).expanduser()
        return p if p.is_absolute() else (marker.parent / p)
    raise CorpusError(
        f"{marker} names no log directory.\n"
        "  Its first non-blank, non-comment line should be the directory "
        "holding builds.jsonl,\n  relative to the marker — e.g. 't/logs'.")


def declared() -> list[Path]:
    """Corpora something *stated*, in descending order of authority.

    Two kinds of statement, ranked the way they are meant: the operator's
    environment beats the project's committed marker, so a pooled or archived
    corpus can be read from inside a project that declares its own.

    These are instructions rather than discoveries, so they short-circuit the
    search the way an explicit path argument does, and none of them can be
    ambiguous.
    """
    out: list[Path] = []
    if os.environ.get(ENV_CORPUS):
        out.append(Path(os.environ[ENV_CORPUS]).expanduser())
    if os.environ.get(ENV_LOG_DIR):
        out.append(Path(os.environ[ENV_LOG_DIR]).expanduser() / BASENAME)
    marker = find_marker()
    if marker is not None:
        out.append(read_marker(marker) / BASENAME)
    return out


def discovered(start: Path | None = None) -> list[Path]:
    """Corpora found by looking, under the project the caller is standing in."""
    root = project_root(start)
    return [root / layout / BASENAME for layout in LEGACY_LAYOUTS]


def candidates() -> list[Path]:
    """Every place a corpus might be, in descending order of authority."""
    return declared() + discovered()


def _distinct_existing(paths: list[Path]) -> list[Path]:
    """Those that exist, one per underlying file.

    Distinct *routes* to the same file are not an ambiguity, and the routes
    coincide constantly: a corpus normally *is* a symlink into a separate
    trajectory repository, so both known layouts can legitimately name one
    file.  Reporting that as a choice between two corpora made every reader
    unusable in 43sp, whose Makefile points WATCHDOG_LOG_DIR at one of the
    layouts -- a case the declared tier now short-circuits before reaching
    here, though the symlink one still arrives.
    """
    out, seen = [], set()
    for c in paths:
        if not c.exists():
            continue
        key = c.resolve()
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def resolve(given: str | os.PathLike | None = None) -> Path:
    """The corpus to read.

    An explicit path is taken as given and must exist -- a typo must fail
    rather than fall through to a default, which would answer a different
    question and look like a real result.
    """
    if given is not None:
        path = Path(given).expanduser()
        if not path.exists():
            raise CorpusError(f"no such corpus: {path}")
        return path

    # A declared corpus wins outright, in the order it was declared.
    # $TRAJECTORY_CORPUS exists precisely to read a pooled or archived corpus
    # that no writer owns, and it could not do that while it merely *competed*
    # with whatever the project happened to have on disk: standing in a
    # project with its own builds.jsonl, pointing the variable at the pool
    # reported "several corpora found" and refused to read either.  Ambiguity
    # is a property of *guessing*, and none of these is a guess.
    #
    # A declared location that holds no corpus yet is simply not a candidate,
    # rather than an assertion that none exists: a project declares where its
    # records will go before its first build has written any.
    for c in declared():
        if c.exists():
            return c

    found = _distinct_existing(discovered())
    if len(found) == 1:
        return found[0]
    if not found:
        tried = "\n".join(f"    {c}" for c in candidates())
        raise CorpusError(
            f"no corpus found. Name one explicitly, set ${ENV_CORPUS} or "
            f"${ENV_LOG_DIR},\n  or commit a {MARKER_NAME} naming the log "
            f"directory.\n  tried:\n{tried}")
    # Genuinely different files, though: two layouts both populated under one
    # project is ambiguous, and picking by priority would quietly answer about
    # whichever this tool happened to rank first.  Both halves of a split
    # corpus is a real possibility.
    listed = "\n".join(f"    {c}  ->  {c.resolve()}" for c in found)
    raise CorpusError(
        f"several corpora found; name the one you mean:\n{listed}")


def resolve_log_dir(start: Path | None = None, *, recording: bool = True) -> Path:
    """Where a *writer* should put its records.

    Unlike `resolve()`, finding nothing is not an error: a project with no
    corpus gets one created, which is how every corpus began.  What *is* an
    error is being unable to tell which of two existing ones is meant --
    unless `recording` is false, in which case there is no dataset to protect
    and the question has shrunk to "where does `last-build.log` go".  Refusing
    to start a build over an ambiguity that cannot affect anything would be
    the tail wagging the dog.

    The reader's tiers, plus a fourth the reader has no use for:

      1. `$WATCHDOG_LOG_DIR` — the operator's instruction.
      2. the project's committed `.isabelle-watchdog` marker.
      3. an existing corpus under one of the known layouts.  This is the tier
         that was missing, and its absence is a bug you cannot see: appending
         to the wrong file is loud, but *creating* the wrong file is silent
         and looks exactly like a first build.
      4. `DEFAULT_LAYOUT`, for a project that genuinely has no corpus.

    `$TRAJECTORY_CORPUS` is deliberately absent.  It is documented as a reader
    override, for pointing a view at a corpus this project does not own;
    honouring it here would mean that reading someone else's dataset silently
    redirects your next build's records into it.  A variable that changes
    where data is *written* should never be one people set casually to look
    at something.
    """
    if os.environ.get(ENV_LOG_DIR):
        return Path(os.environ[ENV_LOG_DIR]).expanduser()

    marker = find_marker(start)
    if marker is not None:
        return read_marker(marker)

    found = _distinct_existing(discovered(start))
    if len(found) == 1:
        return found[0].parent
    if found and recording:
        # Refusing is not the same failure as a capture that breaks a build,
        # and must not be softened into one.  This is a configuration error,
        # decided before anything runs, with the fix in the message -- the
        # class `build.py` already puts "no session to build" in.  Guessing
        # instead would split an irreplaceable dataset in a way nothing
        # downstream can detect, since each half is internally consistent.
        listed = "\n".join(f"    {c}  ->  {c.resolve()}" for c in found)
        raise CorpusError(
            f"several corpora under {project_root(start)}; say which to "
            f"record into.\n  Set ${ENV_LOG_DIR}, or commit a {MARKER_NAME} "
            f"naming the log directory.\n{listed}")

    return project_root(start) / DEFAULT_LAYOUT


def resolve_repo(given: str | os.PathLike | None = None) -> Path:
    """The source repository the diffs came from.

    Needed only by the subcommands that verify or materialise payloads
    (`check`, `repair`, `replay`, `extract`); the rest read the corpus alone
    and must not be made to supply one.
    """
    repo = Path(given).expanduser() if given is not None else project_root()
    if not (repo / ".git").exists():
        raise CorpusError(f"{repo} is not a git repository (use --repo)")
    return repo


# -------------------------------------------------------------------- loading

def load(path: Path) -> list[dict]:
    """Records in file order, which is chronological within one instance."""
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ------------------------------------------------------------------- episodes

def episodes(recs: list[dict]) -> list[list[dict]]:
    """Maximal runs of non-ok attempts closed by an ok (logging-design.md
    §12.4).  A trailing run with no closing ok is returned as an open episode.

    Boundaries are SUCCESSES, not commits: a mid-flight commit (committing a
    failing state as a rewind point) is just an attempt whose `git_head`
    differs from the previous one, not a boundary.

    Segmentation is chronological, **not** per working copy, even on a pooled
    log (several instances unioned by concatenation, logging-design.md §16.5).
    That is deliberate: worktrees are used sequentially here, so an unfinished
    run on one instance genuinely continues on the next — in the
    main→stac/wip pool the handoff shows the same session failing at the same
    `by` line on either side of the seam, one repair, two working copies.
    Splitting by instance would cut that trajectory in half.

    The assumption that fails is *concurrent* instances, whose records
    interleave; then a run really would splice unrelated work.
    `interleaving(recs)` measures it, and the views warn when it is non-zero.
    """
    out: list[list[dict]] = []
    cur: list[dict] = []
    for rec in recs:
        cur.append(rec)
        if rec["outcome"] == "ok":
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def interleaving(recs: list[dict]) -> tuple[int, int]:
    """(instances, interleave excess) for a possibly-pooled log.

    Sequential handoff between n instances costs exactly n-1 switches between
    consecutive records; anything beyond that is genuine concurrency, which
    chronological episode segmentation cannot model.
    """
    ids = [r.get("instance_id") for r in recs]
    switches = sum(1 for a, b in zip(ids, ids[1:]) if a != b)
    return len(set(ids)), max(0, switches - (len(set(ids)) - 1))
