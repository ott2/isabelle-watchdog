#!/usr/bin/env python3
"""audit-timeouts.py — is a session's failure rate load, or genuine proof failure?

The watchdog kills a build that exceeds its wall budget and records
`outcome: timeout`.  A timeout is *not* evidence that the edit was wrong: it
can mean the machine was busy, on battery, or running several sessions at
once.  If one development ran under heavier load than the others, its
one-shot rate is depressed for a reason that has nothing to do with proof
difficulty — which would be a confound, not a finding.

Five checks, in the order that can falsify the concern fastest:

  1. **Outcome mix** — what share of each session's non-green attempts are
     timeouts rather than failures?  If timeouts are rare everywhere, the
     concern is answered and the rest is detail.
  2. **Rate with timeouts excluded** — recount treating a timeout attempt as
     no attempt at all, which is the most generous correction the concern
     could ask for.  If the gap survives it, load is not driving it.
  3. **Load conditions** — battery and concurrent instances per session.
     `interleaving` in attempts.py distinguishes a sequential worktree
     handoff (n-1 switches for n instances) from genuine concurrency.
  4. **Disguised timeouts** — a build killed by the watchdog before
     `timeout_reason` was recorded could look like a plain failure.  Long
     elapsed times among `fail` records are the signature.
  5. **What the failures say** — the direct test, independent of all
     timing: a load-induced failure carries no proof content, so classify
     the error heads and see.

`Failed to finish proof` is **not** a timeout, though it is easy to assume
so.  It is the deterministic outcome of a method that *ran to completion*
and left goals behind — 167 of 176 such heads go on to enumerate them
(`goal (1 subgoal):`).  Three independent checks agree: its elapsed times
are indistinguishable from other failures (median 20.8s vs 20.5s) and well
under the timeouts (median 34.5s, min 20.2s); no such record carries a
`timeout_reason`, while all 88 timeouts do; and no `fail` record anywhere
mentions a timeout in its head.

Divergent search — a method that never returns — is real, but it lands in
the other bucket: `outcome: timeout` with `timeout_reason: loop_progress`
(the watchdog seeing no progress).  There are 28 of those, ~3% of attempts
in ae and ntr and near zero in base and ar.  So the phenomenon exists and
is already separated; it is not hiding inside the failure counts.

Usage:  bin/audit-timeouts.py [-i BUILDS_JSONL]
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

BIN = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A = _load("attempts")
_THY_NAME = re.compile(r"^\+\s*([A-Z][A-Za-z0-9_']*)\s*$")
PROOF_SESSIONS = ["base", "ae", "ar", "ntr", "art"]


def touches_proof(rec: dict) -> bool:
    return any(p.endswith(".thy") and A._thy_code_change(A._hunks(b))
               for p, b in A._split_files(rec.get("diff") or ""))


def registers_theory(rec: dict) -> bool:
    return any(p.endswith(("ROOT", "ROOTS"))
               and any(_THY_NAME.match(ln) for ln in b
                       if not ln.startswith("+++"))
               for p, b in A._split_files(rec.get("diff") or ""))


def episodes(log: Path) -> list[tuple[str, list[dict]]]:
    """Closed, real-work episodes with their session, on the attempt scope."""
    out = []
    for ep in A._episodes(A._load(log)):
        if ep[-1]["outcome"] != "ok":
            continue
        if not sum(1 for r in ep if A.rec_class(r) == "code"):
            continue
        if not (any(touches_proof(r) for r in ep)
                or any(registers_theory(r) for r in ep)):
            continue
        out.append((A.project(ep), ep))
    return out


def rate(lengths: list[int]) -> str:
    if not lengths:
        return "   --  (n=0)"
    one = 100 * sum(1 for x in lengths if x == 1) / len(lengths)
    return f"{one:5.1f}%  (n={len(lengths):3})"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-i", "--input", default=str(A.BUILDS_JSONL))
    ns = ap.parse_args()
    log = Path(ns.input)
    eps = [(s, e) for s, e in episodes(log) if s in PROOF_SESSIONS]

    print("1. outcome mix among non-green attempts\n")
    print("   sess |  fail  timeout  timeout-share | runs with a timeout")
    print("   -----+-------------------------------+--------------------")
    for sess in PROOF_SESSIONS:
        mine = [e for s, e in eps if s == sess]
        c = Counter(r["outcome"] for e in mine for r in e)
        bad = c["fail"] + c["timeout"]
        share = f"{100 * c['timeout'] / bad:5.1f}%" if bad else "   -- "
        hit = sum(1 for e in mine
                  if any(r["outcome"] == "timeout" for r in e))
        pc = f"{100 * hit / len(mine):4.1f}%" if mine else " -- "
        print(f"   {sess:4} | {c['fail']:5} {c['timeout']:8}  {share:>13} "
              f"| {hit:3} of {len(mine):3} ({pc})")

    print("\n2. one-shot rate, timeouts discounted\n")
    print("   sess | as counted    | timeout attempts dropped | "
          "runs with any timeout dropped")
    print("   -----+---------------+--------------------------+"
          "------------------------------")
    for sess in PROOF_SESSIONS:
        mine = [e for s, e in eps if s == sess]
        base = [len(e) for e in mine]
        # A timeout is not evidence the edit was wrong: charge nothing for it.
        notime = [max(1, sum(1 for r in e if r["outcome"] != "timeout"))
                  for e in mine]
        clean = [len(e) for e in mine
                 if not any(r["outcome"] == "timeout" for r in e)]
        print(f"   {sess:4} | {rate(base)} | {rate(notime)}"
              f"            | {rate(clean)}")

    pre = [e for s, e in eps if s in PROOF_SESSIONS and s != "ntr"]
    ntr = [e for s, e in eps if s == "ntr"]
    for name, group in (("pre-ntr", pre), ("ntr", ntr)):
        base = [len(e) for e in group]
        notime = [max(1, sum(1 for r in e if r["outcome"] != "timeout"))
                  for e in group]
        clean = [len(e) for e in group
                 if not any(r["outcome"] == "timeout" for r in e)]
        print(f"   {name:7}| {rate(base)} | {rate(notime)}"
              f"            | {rate(clean)}")

    print("\n3. load conditions\n")
    print("   sess | on battery | mean elapsed | instances | concurrent excess")
    print("   -----+------------+--------------+-----------+------------------")
    for sess in PROOF_SESSIONS:
        recs = [r for s, e in eps if s == sess for r in e]
        if not recs:
            continue
        batt = sum(1 for r in recs if (r.get("battery_factor") or 1) != 1
                   or r.get("power") == "battery")
        inst, excess = A.interleaving(recs)
        print(f"   {sess:4} | {100 * batt / len(recs):9.1f}% "
              f"| {statistics.mean(r['elapsed_s'] for r in recs):11.1f}s "
              f"| {inst:9} | {excess:17}")

    print("\n4. failures long enough to be disguised timeouts\n")
    print("   Also the timeout reasons, since `loop_progress` is divergent")
    print("   search — the case `Failed to finish proof` is often mistaken")
    print("   for, and it is already counted separately.\n")
    for sess in PROOF_SESSIONS:
        rs = [r for s, e in eps if s == sess for r in e]
        c = Counter(r.get("timeout_reason") for r in rs
                    if r["outcome"] == "timeout")
        if rs:
            print(f"   {sess:4} | loop_progress {c['loop_progress']:3} "
                  f" wall {c['wall']:3}  activity {c['activity']:3}"
                  f"  ({100 * c['loop_progress'] / len(rs):4.1f}% of "
                  f"{len(rs):3} attempts divergent)")
    print()
    for sess in PROOF_SESSIONS:
        fails = [r["elapsed_s"] for s, e in eps if s == sess
                 for r in e if r["outcome"] == "fail"]
        tos = [r["elapsed_s"] for s, e in eps if s == sess
               for r in e if r["outcome"] == "timeout"]
        if not fails:
            continue
        cut = min(tos) if tos else None
        near = sum(1 for x in fails if cut and x >= cut)
        print(f"   {sess:4} | fail median {statistics.median(fails):6.1f}s "
              f"max {max(fails):7.1f}s | "
              + (f"timeout min {cut:6.1f}s -> {near} fail(s) at or above it"
                 if cut else "no timeouts to compare"))
    print("\n5. what the failures say\n")
    print("   The direct test, and the one that does not depend on timing: a"
          "\n   load-induced failure has no proof content.  A `Failed to apply"
          "\n   initial proof method` is the opposite — the chosen tactic did"
          "\n   not even engage the goal, which is a wrong-method choice.\n")
    print("   `unmatched` is NOT a load signal: those heads begin with"
          "\n   truncated goal text rather than a message (`⟦⋀qa buf'. | ⟦q ="
          "\n   qa;`), so the keyword list misses them.  Inspection found no"
          "\n   load-induced failure in any session.  The live column is"
          "\n   initial-method.\n")
    print("   sess |  fail | finish-proof | initial-method | unmatched")
    print("   -----+-------+--------------+----------------+----------")
    for sess in PROOF_SESSIONS:
        heads = [(r.get("error_head") or "").lower()
                 for s, e in eps if s == sess
                 for r in e if r["outcome"] == "fail"]
        if not heads:
            continue
        fin = sum(1 for h in heads if "failed to finish proof" in h)
        init = sum(1 for h in heads if "failed to apply initial proof" in h)
        proofish = sum(1 for h in heads if any(
            k in h for k in ("failed to finish proof", "failed to apply",
                             "exception thm", "type unification",
                             "undefined constant", "undefined fact",
                             "failed to refine", "outer syntax",
                             "undefined type")))
        n = len(heads)
        print(f"   {sess:4} | {n:5} | {100 * fin / n:11.1f}% "
              f"| {100 * init / n:13.1f}% | {100 * (n - proofish) / n:8.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
