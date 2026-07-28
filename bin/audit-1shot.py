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


def thy_code_files(rec: dict) -> list[str]:
    """The .thy files this record changes *as code*, by attempts.py's own test."""
    out = []
    for path, body in A._split_files(rec.get("diff") or ""):
        if path.endswith(".thy") and A._thy_code_change(A._hunks(body)):
            out.append(path)
    return out


def episode_has_thy(ep: list[dict]) -> bool:
    return any(thy_code_files(r) for r in ep)


def trajectories(log: Path) -> dict[str, list[tuple[int, int]]]:
    """Session -> [(reported length, proof-edit length)] per closed trajectory.

    The first is shape-vs-trajectory's own procedure: segment the *unfiltered*
    log, keep closed episodes, count the code-class records inside.  The second
    counts only records carrying a `.thy` code change, and is 0 for a
    trajectory that never touched a proof.

    The two corrections pull opposite ways, which is why both are needed:
    dropping the no-proof trajectories removes free greens (pushes the rate
    down), while not counting `ROOT`/`Makefile`/`bin` records as attempts
    shortens the survivors (pushes it up)."""
    by: dict[str, list[tuple[int, int]]] = {}
    for ep in A._episodes(A._load(log)):
        if ep[-1]["outcome"] != "ok":
            continue
        k = sum(1 for r in ep if A.rec_class(r) == "code")
        if not k:
            continue
        by.setdefault(A.project(ep), []).append(
            (k, sum(1 for r in ep if thy_code_files(r))))
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
    print("  sess     | reported        | proof-bearing   | proof edits only"
          " | no-proof")
    print("  ---------+-----------------+-----------------+-----------------"
          "-+---------")

    order = ["base", "ae", "ar", "ntr", "art", "mixed", "tooling", "none"]
    for sess in order + [s for s in by_sess if s not in order]:
        eps = by_sess.get(sess)
        if not eps:
            continue
        rep = [k for k, _ in eps]
        bearing = [k for k, t in eps if t]          # reported length, proof eps
        strict = [t for _, t in eps if t]           # proof-edit length only
        print(f"  {sess:8} | {pct(sum(1 for k in rep if k == 1), len(rep))}"
              f" of {len(rep):4} "
              f"| {pct(sum(1 for k in bearing if k == 1), len(bearing))}"
              f" of {len(bearing):4} "
              f"| {pct(sum(1 for k in strict if k == 1), len(strict))}"
              f" of {len(strict):4} "
              f"| {len(eps) - len(strict):4}")

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
