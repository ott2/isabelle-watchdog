#!/usr/bin/env python3
"""recount-lengths.py — three ways to count a trajectory, side by side.

`attempts.py` measures a trajectory's length as its number of **code-class
records**.  That was a conservative choice against doc-only noise, but it
interacts badly with the pre-2026-07-27 capture gap: while a theory was
untracked its edits produced *zero-byte* deltas, class `none`, so a run that
failed six times and then went green counts as length 1 — a one-shot.

Three scopes, over closed episodes:

  code    code-class records only                    (as shipped)
  proof   ditto, restricted to runs with a .thy edit (logging-design.md 13.2)
  attempt every recorded build in the episode, for runs that are real work —
          a .thy edit *or* a ROOT registering a theory absent at baseline

The third treats a failing build as an attempt whether or not its diff was
captured, which is what the error head shows it to be.  Comparing the three
says whether the shipped number is an artefact of counting rather than of
the underlying search.

Usage:  bin/recount-lengths.py [-i BUILDS_JSONL]
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A = _load("attempts")
_THY_NAME = re.compile(r"^\+\s*([A-Z][A-Za-z0-9_']*)\s*$")


def touches_proof(rec: dict) -> bool:
    for path, body in A._split_files(rec.get("diff") or ""):
        if path.endswith(".thy") and A._thy_code_change(A._hunks(body)):
            return True
    return False


def registers_theory(rec: dict) -> bool:
    """Does this record add a theory name to a ROOT?"""
    for path, body in A._split_files(rec.get("diff") or ""):
        if path.endswith(("ROOT", "ROOTS")) and any(
                _THY_NAME.match(ln) for ln in body
                if not ln.startswith("+++")):
            return True
    return False


def pct(xs: list[int]) -> str:
    return f"{100.0 * sum(1 for x in xs if x == 1) / len(xs):5.1f}%" if xs \
        else "    --"


def mean(xs: list[int]) -> str:
    return f"{sum(xs) / len(xs):5.2f}" if xs else "   --"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-i", "--input", default=str(A.BUILDS_JSONL))
    ns = ap.parse_args()

    scopes: dict[str, dict[str, list[int]]] = {
        "code": {}, "proof": {}, "attempt": {}}
    for ep in A._episodes(A._load(Path(ns.input))):
        if ep[-1]["outcome"] != "ok":
            continue
        k = sum(1 for r in ep if A.rec_class(r) == "code")
        if not k:
            continue
        sess = A.project(ep)
        scopes["code"].setdefault(sess, []).append(k)
        has_proof = any(touches_proof(r) for r in ep)
        if has_proof:
            scopes["proof"].setdefault(sess, []).append(k)
        if has_proof or any(registers_theory(r) for r in ep):
            scopes["attempt"].setdefault(sess, []).append(len(ep))

    print("trajectory length under three counting scopes\n")
    print("  sess     |      code       |      proof      |     attempt")
    print("           | 1-shot  mean  n | 1-shot  mean  n | 1-shot  mean  n")
    print("  ---------+-----------------+-----------------+-----------------")
    order = ["base", "ae", "ar", "ntr", "art"]
    for sess in order + [s for s in scopes["code"] if s not in order]:
        if sess not in scopes["code"]:
            continue
        cells = []
        for scope in ("code", "proof", "attempt"):
            xs = scopes[scope].get(sess, [])
            cells.append(f"{pct(xs)} {mean(xs)} {len(xs):4}")
        print(f"  {sess:8} | {' | '.join(cells)}")

    print("\n  code     code-class records only (as shipped before the filter)")
    print("  proof    ditto, runs with a .thy code edit only")
    print("  attempt  every recorded build, for runs that are real work")
    print("           (.thy edit, or a ROOT registering a new theory)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
