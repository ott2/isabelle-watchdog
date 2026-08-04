#!/usr/bin/env python3
"""oneshot-significance.py — how much does the pre-ntr/ntr 1-shot gap survive?

The gap is large (.633 vs .354 on the pooled corpus) over hundreds of
trajectories, so a textbook two-proportion test returns an absurdly small p.
That test assumes each trajectory is an independent Bernoulli draw, which
has to be checked rather than assumed — work happened in bursts, and a hard
afternoon on one lemma could produce a run of failures that the test would
read as many independent observations.

So the check is run: the bootstrap resamples whole *days* rather than
trajectories, and the design effect says how much that costs.  It currently
comes back near 1, meaning within-day dependence is not in fact inflating
the naive interval.  That was not always so — swap `attempts.is_attempt`
back for the old count-the-captured-deltas rule, holding everything else
fixed, and it returns 2.4, because untracked-theory work on a given day was
scoring as one-shot together.  The clustering was the recorder's, not the
work's, which is why fixing the recorder removed it rather than some
modelling choice here.

What the number cannot settle, and no amount of resampling will:

  - **Heterogeneity.**  `pre-ntr` pools four developments whose own rates
    run .545 to .688 — narrow now, but they are still unlike tasks.
  - **Confounding with time.**  `ntr` is the later development: different
    tooling, different capture era, a different phase of the project.
    Nothing here separates "tape reduction is harder" from "that week was
    different".  Note that elapsed days is an *outcome* of difficulty here,
    not a nuisance variable — the earlier sessions ran long because they
    kept hitting problems — so conditioning on it would subtract part of the
    effect being measured.

Leave-one-day-out on `ntr` is the cheap robustness check against the last
of those: five days is few enough that one bad afternoon could carry it.

Usage:  bin/oneshot-significance.py [-i BUILDS_JSONL] [-B REPLICATES]
"""

from __future__ import annotations

import argparse

import math
import random
import sys
from pathlib import Path

# `attempts` and `corpus` are siblings in this package now.  They used to be
# loaded through importlib.util.spec_from_file_location, because
# `bin/attempts.py` was a script whose name argparse-dispatched rather than
# a module anything could import.  Packaging removed that obstacle.
from .. import attempts as A
from .. import corpus

PRE_NTR = ["base", "ae", "ar", "art"]
SEED = 20260729  # fixed so the interval is reproducible run to run




def trials(log: Path) -> list[tuple[str, str, bool]]:
    """One (session, day, was-one-shot) per counted trajectory.

    Population and length come from `attempts.attempt_length` /
    `attempts.proof_bearing`, so this agrees with the table by construction
    rather than by a second copy of the rule that can drift from it."""
    out = []
    for ep in A._episodes(corpus.load(log)):
        if ep[-1]["outcome"] != "ok":
            continue
        k = A.attempt_length(ep)
        if k is None or not A.proof_bearing(ep):
            continue
        out.append((A.project(ep), ep[0]["timestamp"][:10], k == 1))
    return out


def rate(rows: list[tuple[str, str, bool]]) -> float:
    return sum(1 for _, _, s in rows if s) / len(rows) if rows else float("nan")


def naive_z(a: list, b: list) -> tuple[float, float]:
    """Two-proportion z and two-sided p, assuming independent trials."""
    n1, n2 = len(a), len(b)
    x1 = sum(1 for _, _, s in a if s)
    x2 = sum(1 for _, _, s in b if s)
    p = (x1 + x2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    z = (x1 / n1 - x2 / n2) / se
    return z, math.erfc(abs(z) / math.sqrt(2))


def day_bootstrap(rows: list, B: int) -> list[float]:
    """Resample whole days with replacement; recompute the gap each time.

    Days are the candidate cluster: within a day the same lemma, the same
    context and the same tooling recur, so trajectories could be far from
    independent.  Whether they actually are is what the design effect
    reports — the point of resampling days is to find out, not to assume."""
    by_day: dict[str, list] = {}
    for row in rows:
        by_day.setdefault(row[1], []).append(row)
    days = list(by_day)
    rng = random.Random(SEED)
    gaps = []
    for _ in range(B):
        pool = [r for d in rng.choices(days, k=len(days)) for r in by_day[d]]
        pre = [r for r in pool if r[0] in PRE_NTR]
        ntr = [r for r in pool if r[0] == "ntr"]
        if pre and ntr:
            gaps.append(rate(pre) - rate(ntr))
    return sorted(gaps)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-i", "--input", default=None)
    ap.add_argument("-B", type=int, default=10000, help="bootstrap replicates")
    ns = ap.parse_args()
    # Resolve once, here: the default is not a constant any more
    # (bin/corpus.py -- it depends on where the operator is standing).
    try:
        ns.input = corpus.resolve(ns.input)
    except corpus.CorpusError as e:
        print(f'FAIL: {e}', file=sys.stderr)
        return 1

    rows = trials(Path(ns.input))
    pre = [r for r in rows if r[0] in PRE_NTR]
    ntr = [r for r in rows if r[0] == "ntr"]
    if not pre or not ntr:
        print("FAIL: need both groups; is this the merged log?", file=sys.stderr)
        return 1

    gap = rate(pre) - rate(ntr)
    print("pre-ntr vs ntr one-shot rate\n")
    print(f"  pre-ntr  {rate(pre):.3f}  n={len(pre)}   "
          f"({len({d for _, d, _ in pre})} days)")
    print(f"  ntr      {rate(ntr):.3f}  n={len(ntr)}   "
          f"({len({d for _, d, _ in ntr})} days)")
    print(f"  gap      {gap:.3f}")

    z, p = naive_z(pre, ntr)
    print(f"\n  naive two-proportion test:  z = {z:.2f}, p = {p:.1e}")
    print("    assumes every trajectory is an independent draw")

    gaps = day_bootstrap(rows, ns.B)
    lo, hi = gaps[int(0.025 * len(gaps))], gaps[int(0.975 * len(gaps))]
    crosses = sum(1 for g in gaps if g <= 0) / len(gaps)
    print(f"\n  day-clustered bootstrap ({len(gaps)} replicates):")
    print(f"    95% interval for the gap: {lo:+.3f} to {hi:+.3f}")
    print(f"    replicates with no gap or reversed: {crosses:.1%}")
    naive_se = (rate(pre) - rate(ntr) - 0) / z if z else float("nan")
    boot_se = (hi - lo) / (2 * 1.96)
    print(f"    design effect (boot SE / naive SE)^2: "
          f"{(boot_se / naive_se) ** 2:.1f}x")

    print("\n  per-session, since pre-ntr pools four unlike developments:")
    for sess in PRE_NTR + ["ntr"]:
        rs = [r for r in rows if r[0] == sess]
        if rs:
            print(f"    {sess:8} {rate(rs):.3f}  n={len(rs):4}  "
                  f"({len({d for _, d, _ in rs})} days)")

    # With so few ntr days, one atypical day could carry the result.  Drop
    # each in turn: if the gap holds without any single day, it is not a
    # story about one bad afternoon.
    ntr_days = sorted({d for _, d, _ in ntr})
    print(f"\n  ntr is {len(ntr_days)} days; gap with each dropped:")
    for day in ntr_days:
        kept = [r for r in ntr if r[1] != day]
        n_day = len(ntr) - len(kept)
        print(f"    without {day} ({n_day:2} traj)  ntr {rate(kept):.3f}  "
              f"gap {rate(pre) - rate(kept):+.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
