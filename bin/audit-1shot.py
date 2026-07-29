#!/usr/bin/env python3
"""audit-1shot.py — is the 1-shot rate measuring proof search, or bookkeeping?

`attempts.py` keeps a delta when *any* file in it looks code-bearing, and
attributes the trajectory to a `t/<dir>` session from the paths its code-class
records touch.  Two gaps follow, and both would push the one-shot rate up:

1. **Non-theory code.**  A delta touching only `bin/*.py`, `ROOT`, or
   `Makefile` is classed `code` (deliberately: unrecognised suffixes are not
   hidden).  If it also touches anything under `t/<sess>/` -- a
   `document/root.tex`, say -- `project()` finds one session dir and returns
   it, so a tooling build is booked against that session.  Such a build is a
   confirmation run, green by construction.

2. **Confirmation runs.**  A build after a stretch of non-theory work carries a
   large diff, none of it a proof edit.  Nothing in the pipeline distinguishes
   "the edit was right first time" from "there was no proof edit to get wrong".

This tool re-derives each session's one-shot rate over the trajectories that
contain a genuine `.thy` **code** change -- the same evidence test
`classify_file` applies, so the strictening is in *scope*, not in kind -- and
reports what the restriction moves.  It answers whether the static/dynamic
contrast survives, not whether the classifier is right.

Usage:  bin/audit-1shot.py [-i BUILDS_JSONL] [--examples N]
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent


def _load_attempts():
    """Import bin/attempts.py as a module (its name is not importable)."""
    spec = importlib.util.spec_from_file_location("attempts", BIN / "attempts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A = _load_attempts()


def trajectories(log: Path) -> dict[str, list[tuple[int, bool]]]:
    """Session -> [(attempt length, is proof-bearing)] per closed trajectory.

    Length and the proof test both come from `attempts.py`, which owns them:
    `attempt_length` counts recorded builds rather than captured diffs, and
    `proof_bearing` falls back to the Isabelle error head when a run's diff
    was lost.  This tool re-derives the *rate* under the filter and without
    it; it does not re-implement either rule."""
    by: dict[str, list[tuple[int, bool]]] = {}
    for ep in A._episodes(A._load(log)):
        if ep[-1]["outcome"] != "ok":
            continue
        k = A.attempt_length(ep)
        if k is None:
            continue
        by.setdefault(A.project(ep), []).append((k, A.proof_bearing(ep)))
    return by


def pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:5.1f}%" if d else "    --"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-i", "--input", default=str(A.BUILDS_JSONL),
                    help="builds.jsonl to read")
    ap.add_argument("--examples", type=int, default=0, metavar="N",
                    help="show N no-theory trajectories booked against a session")
    ns = ap.parse_args()

    by_sess = trajectories(Path(ns.input))

    print("one-shot rate, as reported vs over proof-bearing trajectories only")
    print(f"  input: {ns.input}\n")
    print("  sess     | unfiltered      | proof-bearing   | dropped")
    print("  ---------+-----------------+-----------------+--------")

    order = ["base", "ae", "ar", "ntr", "art", "mixed", "tooling", "none"]
    for sess in order + [s for s in by_sess if s not in order]:
        eps = by_sess.get(sess)
        if not eps:
            continue
        allk = [k for k, _ in eps]
        kept = [k for k, bearing in eps if bearing]
        print(f"  {sess:8} | {pct(sum(1 for k in allk if k == 1), len(allk))}"
              f" of {len(allk):4} "
              f"| {pct(sum(1 for k in kept if k == 1), len(kept))}"
              f" of {len(kept):4} "
              f"| {len(eps) - len(kept):4}  "
              f"{pct(len(eps) - len(kept), len(eps))}")

    if ns.examples:
        print(f"\n  no-theory trajectories booked against a session "
              f"(first {ns.examples}):")
        shown = 0
        for ep in A._episodes(A._load(Path(ns.input))):
            if ep[-1]["outcome"] != "ok":
                continue
            if not sum(1 for r in ep if A.rec_class(r) == "code"):
                continue
            sess = A.project(ep)
            if sess in ("tooling", "none", "mixed") or episode_has_thy(ep):
                continue
            paths = sorted({p for r in ep
                            for p, _ in A._split_files(r.get("diff") or "")})
            print(f"\n    {ep[0]['build_id']}  -> {sess}  "
                  f"({len(ep)} attempt{'s' if len(ep) > 1 else ''}, "
                  f"{ep[-1]['outcome']})")
            for p in paths[:6]:
                print(f"      {p}")
            if len(paths) > 6:
                print(f"      ... and {len(paths) - 6} more")
            shown += 1
            if shown >= ns.examples:
                break
        if not shown:
            print("    none -- every session-attributed trajectory has a .thy edit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
