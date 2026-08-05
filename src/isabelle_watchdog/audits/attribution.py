#!/usr/bin/env python3
"""audit-attribution.py — is the attribution ladder still reaching everything?

`attempts.project()` tries three routes in descending order of evidential
strength (logging-design.md §13.2.1): the paths a trajectory's code deltas
touch, then the file named in an Isabelle error head, then the session named
on the `isabelle build` command line.  The last needs a declared map of
session names, now derived from the corpus (`attempts.Attribution`).

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

Exits non-zero on an unmapped target that a trajectory actually relied on --
i.e. one whose absence left a trajectory unattributed.  A target that is
merely unmapped and unneeded is reported, not failed.

Usage:  python -m isabelle_watchdog.audits.attribution [-i CORPUS]
"""

from __future__ import annotations

import argparse

import sys
from collections import Counter
from pathlib import Path

# `attempts` and `corpus` are siblings in this package now.  They used to be
# loaded through importlib.util.spec_from_file_location, because
# `bin/attempts.py` was a script whose name argparse-dispatched rather than
# a module anything could import.  Packaging removed that obstacle.
from .. import attempts as A
from .. import corpus





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
    at = A.fitted()
    dirs, other = set(), False
    for rec in ep:
        for path, body in A._split_files(rec.get("diff") or ""):
            if A.classify_file(path, body)[0] != "code":
                continue
            if (d := at.path_dir(path)):
                dirs.add(d)
            else:
                other = True
    if dirs:
        return "1 diff path"
    if A.error_dirs(ep):
        return "2 error head"
    if other:
        return "-- outside any session dir"
    if {d for rec in ep if (d := A.command_dir(rec))}:
        return "3 build target"
    return "-- no evidence"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-i", "--input", default=None)
    ap.add_argument("--attribution", metavar="FILE", default=None,
                    help="JSON of attribution facts this corpus cannot show "
                         "(default: $TRAJECTORY_ATTRIBUTION)")
    ns = ap.parse_args()
    # Resolve once, here: the default is not a constant any more
    # (bin/corpus.py -- it depends on where the operator is standing).
    try:
        ns.input = corpus.resolve(ns.input)
    except corpus.CorpusError as e:
        print(f'FAIL: {e}', file=sys.stderr)
        return 1
    # Attribution is derived from the whole corpus (see attempts.Attribution),
    # so it is fitted once here before any episode is labelled.
    try:
        A.fit_attribution(corpus.load(ns.input), ns.attribution)
    except corpus.CorpusError as e:
        print(f'FAIL: {e}', file=sys.stderr)
        return 1

    recs = corpus.load(corpus.resolve(ns.input))
    if not recs:
        return 1

    episodes = [ep for ep in A._episodes(recs) if ep[-1]["outcome"] == "ok"
                and A.attempt_length(ep) is not None]

    at = A.fitted()
    print("1. build targets in the corpus vs the derived map\n")
    seen: Counter[str] = Counter()
    for rec in recs:
        for t in targets(rec):
            seen[t] += 1
    unmapped = []
    for t, n in sorted(seen.items(), key=lambda kv: -kv[1]):
        if t not in at.targets:
            unmapped.append(t)
            verdict = "NOT DERIVED"
        elif at.targets[t] is None:
            # A declared exclusion, not an oversight — see load_overrides.
            verdict = "-> (declared out of tree)"
        else:
            verdict = "-> " + at.label(at.targets[t])
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
        print("      The map is derived from the corpus: `session` lines in "
              "captured ROOT diffs,\n      else which directory a build's "
              "edits touched.  A target appearing only on\n      builds with "
              "no captured diff, whose ROOT this corpus never saw edited, "
              "cannot\n      be derived.  Declare it -- as a directory, or as "
              "null if the project builds\n      against it rather than works "
              "on it -- in a JSON file passed with\n      --attribution FILE "
              "(or $TRAJECTORY_ATTRIBUTION):\n\n"
              '          {"targets": {"%s": null}}\n' % unmapped[0])
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
          f"{n_tool} tooling (outside any session dir), {n_none} no evidence")
    return 0


if __name__ == "__main__":
    sys.exit(main())
