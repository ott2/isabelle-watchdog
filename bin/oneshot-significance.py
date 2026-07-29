#!/usr/bin/env python3
"""oneshot-significance.py — how much does the pre-ntr/ntr 1-shot gap survive?

The gap is large (.633 vs .354 on the pooled corpus) over hundreds of
trajectories, so a textbook two-proportion test returns an absurdly small p.
That test assumes each trajectory is an independent Bernoulli draw, and
these are not:

  - **Clustered in time.**  Work happened in bursts.  A hard afternoon
    produces a run of failures on one lemma; an easy one produces a run of
    greens.  Trajectories within a day share far more than trajectories
    across months, so the effective sample is nearer the number of working
    days than the number of trajectories.
  - **Heterogeneous within the group.**  `pre-ntr` pools four sessions whose
    own rates run .675 to .855.  Treating them as one binomial understates
    the variance of the pooled estimate.
  - **Confounded with calendar time.**  `ntr` is not just a different
    development, it is the *later* one: different tooling, different capture,
    a different phase of the project.  Nothing here separates "tape reduction
    is harder" from "August was different".

So this reports three things and lets them disagree: the naive test, a
bootstrap that resamples whole *days* rather than trajectories, and the
between-session spread that any pooled figure hides.

Usage:  bin/oneshot-significance.py [-i BUILDS_JSONL] [-B REPLICATES]
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import random
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent
PRE_NTR = ["base", "ae", "ar", "art"]
SEED = 20260729  # fixed so the interval is reproducible run to run


def _load_attempts():
    spec = importlib.util.spec_from_file_location("attempts", BIN / "attempts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A = _load_attempts()


def trials(log: Path) -> list[tuple[str, str, bool]]:
    """One (session, day, was-one-shot) per counted trajectory.

    Population and length come from `attempts.attempt_length` /
    `attempts.proof_bearing`, so this agrees with the table by construction
    rather than by a second copy of the rule that can drift from it."""
    out = []
    for ep in A._episodes(A._load(log)):
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

    Days are the cluster because that is the unit the dependence lives at:
    within a day the same lemma, the same context and the same tooling recur.
    Resampling trajectories would treat 92 correlated draws as 92 independent
    ones, which is exactly the assumption in doubt."""
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
    ap.add_argument("-i", "--input", default=str(A.BUILDS_JSONL))
    ap.add_argument("-B", type=int, default=10000, help="bootstrap replicates")
    ns = ap.parse_args()

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
