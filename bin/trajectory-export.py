#!/usr/bin/env python3
"""trajectory-export.py — materialise per-episode trajectory files from
the build log (logging-design.md §16).

An *episode* is a maximal run of attempts ending in a successful build
(outcome == "ok"); a trailing run with no success is an *open* episode.
Episode boundaries are SUCCESSES, not commits: a mid-flight commit
(committing a failing state as a rewind point) is just an attempt whose
`git_head` differs from the previous one — flagged `committed_midflight`,
NOT a boundary.

Each episode becomes one self-contained, portable file:

  { instance_id, branch,
    baseline:  <git_head of the first attempt>,    # a public commit
    attempts: [ { build_id, outcome, git_head, committed_midflight,
                  elapsed_s, error_head, diff } ... ],   # incremental diffs
    closed_by: { build_id, git_head } | null,      # the success, or null if open
    open: bool }

needing no git object store to interpret — the diffs are inline, anchored
to the public baseline commit.  One file per episode, so agglomerating
many instances' trajectories is a plain file union (logging-design.md §16).
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LOG = PROJECT_DIR / "t" / "logs" / "builds.jsonl"
DEFAULT_OUT = PROJECT_DIR / "t" / "logs" / "trajectories"


def load(log: Path) -> list[dict]:
    return [json.loads(l) for l in log.read_text().splitlines() if l.strip()]


def segment(recs: list[dict]):
    """Yield episodes: maximal runs ending in outcome=='ok'.  A trailing
    non-ok run is yielded as an open episode."""
    cur: list[dict] = []
    for r in recs:
        cur.append(r)
        if r.get("outcome") == "ok":
            yield cur
            cur = []
    if cur:
        yield cur


def to_episode(attempts: list[dict]) -> dict:
    first, last = attempts[0], attempts[-1]
    closed = last.get("outcome") == "ok"
    out, prev_head = [], None
    for i, r in enumerate(attempts):
        gh = r.get("git_head")
        out.append({
            "build_id": r.get("build_id"),
            "outcome": r.get("outcome"),
            "git_head": gh,
            # a commit landed between this attempt and the previous one,
            # mid-episode (a rewind point), not an episode boundary:
            "committed_midflight": i > 0 and gh != prev_head,
            "elapsed_s": r.get("elapsed_s"),
            "error_head": r.get("error_head"),
            "diff": r.get("diff"),
        })
        prev_head = gh
    return {
        "instance_id": first.get("instance_id"),
        "branch": first.get("branch"),
        "baseline": first.get("git_head"),
        "attempts": out,
        "closed_by": ({"build_id": last["build_id"], "git_head": last.get("git_head")}
                      if closed else None),
        "open": not closed,
    }


def episode_id(ep: dict) -> str:
    if ep["closed_by"]:
        return ep["closed_by"]["build_id"]            # the success build_id
    return (ep["attempts"][0]["build_id"] + "-open") if ep["attempts"] else "empty"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--apply", action="store_true",
                    help="write files (default: dry-run)")
    ns = ap.parse_args()

    log, out = Path(ns.log), Path(ns.out)
    if not log.exists():
        print(f"FAIL: no log at {log}", file=sys.stderr)
        return 1

    recs = load(log)
    episodes = [to_episode(a) for a in segment(recs)]
    closed = sum(1 for e in episodes if not e["open"])
    midflight = sum(1 for e in episodes
                    for a in e["attempts"] if a["committed_midflight"])
    print(f"trajectory-export [{'APPLY' if ns.apply else 'DRY-RUN'}]  log={log}")
    print(f"  {len(recs)} attempts -> {len(episodes)} episodes "
          f"({closed} closed, {len(episodes) - closed} open); "
          f"{midflight} mid-flight commits flagged")

    if ns.apply:
        for ep in episodes:
            d = out / (ep["instance_id"] or "unknown")
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{episode_id(ep)}.json").write_text(
                json.dumps(ep, indent=1) + "\n")
        print(f"PASS: wrote {len(episodes)} episode files under {out}/")
    else:
        print(f"PASS: dry-run — would write {len(episodes)} files under "
              f"{out}/ (--apply to write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
