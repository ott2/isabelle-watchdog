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

# Layouts seen in the wild, tried under the *current* project, not the tool's.
# Kept so an operator standing in either source project gets what they always
# got; new projects should set WATCHDOG_LOG_DIR rather than adopt one of these.
LEGACY_LAYOUTS = ("t/logs", "results/isabelle-logs")


class CorpusError(Exception):
    """No corpus could be resolved, or the one named does not exist."""


# ------------------------------------------------------------------ location

def project_root(start: Path | None = None) -> Path:
    """The git top level containing `start` (default: cwd), or `start` itself.

    Deliberately *not* derived from `__file__`: see the module docstring.
    """
    start = (start or Path.cwd()).resolve()
    p = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    return Path(p.stdout.strip()) if p.returncode == 0 else start


def candidates() -> list[Path]:
    """Every place a corpus might be, in descending order of authority."""
    out: list[Path] = []
    if os.environ.get(ENV_CORPUS):
        out.append(Path(os.environ[ENV_CORPUS]).expanduser())
    if os.environ.get(ENV_LOG_DIR):
        out.append(Path(os.environ[ENV_LOG_DIR]).expanduser() / BASENAME)
    root = project_root()
    out.extend(root / layout / BASENAME for layout in LEGACY_LAYOUTS)
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

    found = [c for c in candidates() if c.exists()]
    if len(found) == 1:
        return found[0]
    if not found:
        tried = "\n".join(f"    {c}" for c in candidates())
        raise CorpusError(
            "no corpus found. Name one explicitly, or set "
            f"${ENV_CORPUS} or ${ENV_LOG_DIR}.\n  tried:\n{tried}")
    # Two layouts present under one project is ambiguous, and picking by
    # priority would quietly answer about whichever the tool happened to
    # prefer.  Both halves of a split corpus are a real possibility here.
    listed = "\n".join(f"    {c}" for c in found)
    raise CorpusError(
        f"several corpora found; name the one you mean:\n{listed}")


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
