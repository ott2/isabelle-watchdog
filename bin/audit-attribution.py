#!/usr/bin/env python3
"""audit-attribution.py — is the attribution ladder still reaching everything?

`attempts.project()` tries three routes in descending order of evidential
strength (logging-design.md §13.2.1): the paths a trajectory's code deltas
touch, then the file named in an Isabelle error head, then the session named
on the `isabelle build` command line.  The last needs a declared map of
historical session names, `attempts.SESSION_TARGETS`.

That map is the failure mode this guards.  It is an allowlist, so an
unmapped target does not raise — it *declines*, and the trajectory quietly
lands in `none`.  Rename a session, add one, and the rows stop adding up
with no error anywhere.  So this reports:

  1. every build target in the corpus, and whether the map knows it;
  2. which rung actually carried each trajectory, so a route that stops
     firing is visible;
  3. the unattributed residue, itemised — it should be only work that is
     genuinely not a `t/` session (the HOAU spike builds against the
     tree's sessions but is not about them).

Exits non-zero on an unmapped target that a trajectory relied on.  Re-derive
the map itself with `bin/derive-session-map.sh`, which reads it back out of
every committed `t/*/ROOT`.

Usage:  bin/audit-attribution.py [-i BUILDS_JSONL]
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from collections import Counter
from pathlib import Path

BIN = Path(__file__).resolve().parent


def _load_attempts():
    spec = importlib.util.spec_from_file_location("attempts", BIN / "attempts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A = _load_attempts()


def targets(rec: dict) -> list[str]:
    """The non-flag arguments of a record's build command."""
    cmd = rec.get("command") or []
    out, i = [], 0
    while i < len(cmd):
        arg = cmd[i]
        if arg in A._FLAG_TAKES_ARG:
            i += 2
        elif arg.startswith("-") or arg in ("isabelle", "build"):
            i += 1
        else:
            out.append(arg)
            i += 1
    return out


def rung(ep: list[dict]) -> str:
    """Which route produced this episode's label — the ladder, re-walked.

    Deliberately a re-walk rather than an instrumented `project()`: the
    audit should agree with the shipped function by matching its *result*,
    not by sharing its internals, so a change to one shows up as a
    disagreement here instead of propagating silently.
    """
    dirs, other = set(), False
    for rec in ep:
        for path, body in A._split_files(rec.get("diff") or ""):
            if A.classify_file(path, body)[0] != "code":
                continue
            m = A._PROJECT_DIR.match(path)
            if m:
                dirs.add(A._alias(m.group(1)))
            else:
                other = True
    if dirs:
        return "1 diff path"
    if A.error_dirs(ep):
        return "2 error head"
    if other:
        return "-- outside t/"
    if {d for rec in ep if (d := A.command_dir(rec))}:
        return "3 build target"
    return "-- no evidence"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-i", "--input", default=str(A.BUILDS_JSONL))
    ns = ap.parse_args()

    recs = A._load(Path(ns.input))
    if not recs:
        return 1

    episodes = [ep for ep in A._episodes(recs) if ep[-1]["outcome"] == "ok"
                and A.attempt_length(ep) is not None]

    print("1. build targets in the corpus vs the declared map\n")
    seen: Counter[str] = Counter()
    for rec in recs:
        for t in targets(rec):
            seen[t] += 1
    unmapped = []
    for t, n in sorted(seen.items(), key=lambda kv: -kv[1]):
        if t not in A.SESSION_TARGETS:
            unmapped.append(t)
            verdict = "NOT IN MAP"
        elif A.SESSION_TARGETS[t] is None:
            # A declared exclusion, not an oversight — see SESSION_TARGETS.
            verdict = "-> (declared out of tree)"
        else:
            verdict = "-> " + A._alias(A.SESSION_TARGETS[t])
        print(f"   {n:5}  {t:34} {verdict}")

    print("\n2. which rung carried each trajectory\n")
    by_rung = Counter(rung(ep) for ep in episodes)
    for r, n in sorted(by_rung.items()):
        print(f"   {n:5}  {r}")

    print("\n3. unattributed residue\n")
    residue = [ep for ep in episodes if A.project(ep) in ("none", "tooling")]
    if not residue:
        print("   (none)")
    for ep in residue:
        tg = sorted({t for rec in ep for t in targets(rec)}) or ["(no target)"]
        print(f"   {ep[0]['build_id']}  k={A.attempt_length(ep)}  "
              f"{A.project(ep):8} {','.join(tg)}")

    # An unmapped target only matters if a trajectory actually needed it —
    # the stronger rungs carry most runs, so a stale name can sit in the
    # corpus for months doing no harm.  Fail on the ones that bite.
    biting = [ep for ep in residue
              if any(t in unmapped for rec in ep for t in targets(rec))]
    print()
    if biting:
        print(f"FAIL: {len(biting)} trajectories fell through to 'none' on "
              f"{len(unmapped)} unmapped target(s): {', '.join(unmapped)}")
        print("      re-derive with bin/derive-session-map.sh if a session "
              "was renamed")
        return 1
    # `tooling` and `none` are both unattributed but for opposite reasons:
    # the first is route 1 succeeding and saying "not a t/ path", the second
    # is every route declining.  Report them apart — a growing `none` is the
    # signal worth seeing, and pooling it into a total would hide it.
    n_tool = sum(1 for ep in residue if A.project(ep) == "tooling")
    n_none = len(residue) - n_tool
    print(f"PASS: {len(episodes)} trajectories; rungs "
          f"{by_rung.get('1 diff path', 0)}/{by_rung.get('2 error head', 0)}/"
          f"{by_rung.get('3 build target', 0)}, "
          f"{n_tool} tooling (no t/ path), {n_none} no evidence")
    return 0


if __name__ == "__main__":
    sys.exit(main())
