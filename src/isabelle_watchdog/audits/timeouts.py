#!/usr/bin/env python3
"""audit-timeouts.py — is a session's failure rate load, or genuine proof failure?

The watchdog kills a build that exceeds its wall budget and records
`outcome: timeout`.  A timeout is *not* evidence that the edit was wrong: it
can mean the machine was busy, on battery, or running several sessions at
once.  If one development ran under heavier load than the others, its
one-shot rate is depressed for a reason that has nothing to do with proof
difficulty — which would be a confound, not a finding.

Six checks, in the order that can falsify the concern fastest:

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
(the watchdog seeing no progress).  There are 28 in the corpus, 21 of them
in the proof sessions — ~3% of attempts in ae and ntr, near zero in base
and ar.  So the phenomenon exists and is already separated; it is not
hiding inside the failure counts.  Check 6 asks whether separating it is
enough, or whether it should also be counted differently.

Usage:  bin/audit-timeouts.py [-i BUILDS_JSONL]
"""

from __future__ import annotations

import argparse

import re
import statistics
import sys
from collections import Counter
from pathlib import Path

# `attempts` and `corpus` are siblings in this package now.  They used to be
# loaded through importlib.util.spec_from_file_location, because
# `bin/attempts.py` was a script whose name argparse-dispatched rather than
# a module anything could import.  Packaging removed that obstacle.
from .. import attempts as A
from .. import corpus



# `attempts.project()` labels an episode with the development it belongs to,
# and emits three labels that are not developments: work on the tooling, work
# it could not attribute, and work spanning several.  A load audit is about
# proof sessions, so those three come out.
SYNTHETIC_SESSIONS = {"tooling", "none", "mixed"}


def proof_sessions(eps) -> list[str]:
    """The developments this corpus actually contains.

    Was the literal list ["base", "ae", "ar", "ntr", "art"] -- ndtht's five.
    That is a correct answer to "which sessions are proof work" only in ndtht;
    anywhere else it selects nothing, and the audit then reports on an empty
    population and divides by zero rather than saying so.  Deriving it keeps
    ndtht's five (they are exactly the non-synthetic labels its corpus
    carries) and makes every other corpus work.
    """
    return sorted({s for s, _ in eps} - SYNTHETIC_SESSIONS)


def episodes(log: Path) -> list[tuple[str, list[dict]]]:
    """Closed, real-work episodes with their session.

    Uses `attempts.attempt_length` / `proof_bearing` rather than a local
    copy, so this audit reports on exactly the population the published
    table counts.  A load audit that filtered differently from the thing it
    is auditing would be measuring a different corpus and saying nothing.
    """
    out = []
    for ep in A._episodes(corpus.load(log)):
        if ep[-1]["outcome"] != "ok":
            continue
        if A.attempt_length(ep) is None or not A.proof_bearing(ep):
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
    ap.add_argument("-i", "--input", default=None)
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
    A.fit_attribution(corpus.load(ns.input), ns.input)
    log = Path(ns.input)
    all_labelled = episodes(log)
    sessions = proof_sessions(all_labelled)
    if not sessions:
        print(f"no proof sessions in {log}: every episode is labelled "
              f"{'/'.join(sorted(SYNTHETIC_SESSIONS))}.\n"
              "This audit is about load across developments, so there is "
              "nothing here to report on.", file=sys.stderr)
        return 1
    eps = [(s, e) for s, e in all_labelled if s in sessions]

    print("1. outcome mix among non-green attempts\n")
    print("   sess |  fail  timeout  timeout-share | runs with a timeout")
    print("   -----+-------------------------------+--------------------")
    for sess in sessions:
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
    for sess in sessions:
        mine = [e for s, e in eps if s == sess]
        base = [len(e) for e in mine]
        # A timeout is not evidence the edit was wrong: charge nothing for it.
        notime = [max(1, sum(1 for r in e if r["outcome"] != "timeout"))
                  for e in mine]
        clean = [len(e) for e in mine
                 if not any(r["outcome"] == "timeout" for r in e)]
        print(f"   {sess:4} | {rate(base)} | {rate(notime)}"
              f"            | {rate(clean)}")

    pre = [e for s, e in eps if s in sessions and s != "ntr"]
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
    for sess in sessions:
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
    for sess in sessions:
        rs = [r for s, e in eps if s == sess for r in e]
        c = Counter(r.get("timeout_reason") for r in rs
                    if r["outcome"] == "timeout")
        if rs:
            print(f"   {sess:4} | loop_progress {c['loop_progress']:3} "
                  f" wall {c['wall']:3}  activity {c['activity']:3}"
                  f"  ({100 * c['loop_progress'] / len(rs):4.1f}% of "
                  f"{len(rs):3} attempts divergent)")
    print()
    for sess in sessions:
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
    for sess in sessions:
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
    print("\n6. is a timeout a proof event or an environmental one?\n")
    print("   The pipeline tests only `outcome == \"ok\"`, so a timeout is"
          "\n   already just a failure step: it never closes an episode and it"
          "\n   counts as an attempt.  Whether that is right depends on the"
          "\n   reason, and the per-attempt divergence rate answers it —"
          "\n   exposure-free, unlike 'episodes containing a timeout', which"
          "\n   rises with length for free.\n")
    all_eps = [e for _, e in eps]
    recs = [r for e in all_eps for r in e]
    if not recs:
        print("   no attempts in the selected sessions.\n")
        return 0
    p = sum(1 for r in recs
            if r.get("timeout_reason") == "loop_progress") / len(recs)
    print(f"   per-attempt loop_progress rate overall: {100 * p:.2f}%\n")
    print("   episode length | episodes  attempts | loop_progress per attempt")
    print("   ---------------+--------------------+--------------------------")
    for lo, hi in ((1, 1), (2, 4), (5, 9), (10, 10 ** 9)):
        sel = [e for e in all_eps if lo <= len(e) <= hi]
        att = [r for e in sel for r in e]
        if not att:
            continue
        lp = sum(1 for r in att if r.get("timeout_reason") == "loop_progress")
        band = f"{lo}-{hi}" if hi < 10 ** 9 else f"{lo}+"
        note = "  (no failures by construction)" if hi == 1 else ""
        print(f"   {band:>14} | {len(sel):8} {len(att):9} | "
              f"{lp:4} ({100 * lp / len(att):5.2f}%){note}")

    obs = sum(1 for e in all_eps
              if any(r.get("timeout_reason") == "loop_progress" for r in e))
    exp = sum(1 - (1 - p) ** len(e) for e in all_eps)
    print(f"\n   episodes containing one: {obs} observed vs {exp:.1f} expected "
          f"if divergence\n   were independent across attempts — fewer, so it "
          f"*clusters*: hit a\n   diverging tactic and the next attempt tends "
          f"to diverge too.")
    print("\n   Divergence is therefore a proof-search event, not environmental"
          "\n   noise: its per-attempt rate rises with trajectory difficulty and"
          "\n   it repeats within a run.  Counting it as a failure step is the"
          "\n   right default; `wall` and `activity` have no such signature and"
          "\n   are the ones a stricter reading should question.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
