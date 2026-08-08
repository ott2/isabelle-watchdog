#!/usr/bin/env python3
"""trajectory audit lengths — three ways to count a trajectory, side by side.

`attempts.py` measures a trajectory's length as its number of **code-class
records**.  That was a conservative choice against doc-only noise, but it
interacts badly with the pre-2026-07-27 capture gap: while a theory was
untracked its edits produced *zero-byte* deltas, class `none`, so a run that
failed six times and then went green counts as length 1 — a one-shot.

Three scopes, over closed episodes:

  code    code-class records only                    (as shipped)
  proof   ditto, restricted to runs with a .thy edit (logging-design.md 13.2)
  attempt every recorded build in the episode (attempts.attempt_length) —
          the scope the published table uses; drops only no-op rebuilds

The third treats a failing build as an attempt whether or not its diff was
captured, which is what the error head shows it to be.  Comparing the three
says whether the shipped number is an artefact of counting rather than of
the underlying search.

Usage:  trajectory audit lengths [-i CORPUS]
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

# `attempts` and `corpus` are siblings in this package now.  They used to be
# loaded through importlib.util.spec_from_file_location, because
# `bin/attempts.py` was a script whose name argparse-dispatched rather than
# a module anything could import.  Packaging removed that obstacle.
from .. import attempts as A
from .. import audits
from .. import corpus


def touches_proof(rec: dict) -> bool:
    for path, body in A._split_files(rec.get("diff") or ""):
        if path.endswith(".thy") and A._thy_code_change(A._hunks(body)):
            return True
    return False


def compare(a_name: str, a: list[int], b_name: str, b: list[int]) -> None:
    """Two pooled groups head to head, on the honest (attempt) scope.

    Reports the one-shot rate with a two-proportion test, then the repair
    tail separately, because the two carry very different weight.  The
    one-shot rates rest on hundreds of runs; the tail fits rest on the
    handful of points above the KS-chosen xmin, and are printed with their
    support size and the same-support geometric KS precisely so a reader can
    see when the power law is *not* winning.  Trajectories from one session
    are also autocorrelated in time, so the p-value is indicative, not a
    clean experiment.
    """
    print(f"\n{a_name} vs {b_name} (attempt scope, pooled)\n")
    for name, xs in ((a_name, a), (b_name, b)):
        print(f"  {name:26} n={len(xs):4}  "
              f"1-shot={100 * sum(1 for x in xs if x == 1) / len(xs):5.1f}%  "
              f"mean={sum(xs) / len(xs):4.2f}  max={max(xs):3}")
    k1, n1, k2, n2 = (sum(1 for x in a if x == 1), len(a),
                      sum(1 for x in b if x == 1), len(b))
    p1, p2 = k1 / n1, k2 / n2
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    z = (p1 - p2) / se if se else 0.0
    ci = 1.96 * math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    pval = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    print(f"\n  one-shot difference {100 * (p1 - p2):+.1f}pp "
          f"(95% CI +/-{100 * ci:.1f}), z={z:.2f}, p={pval:.1e}")

    print("\n  repair tail (k >= 2), where the fits are weak — read with care:")
    for name, xs in ((a_name, a), (b_name, b)):
        f = A.fit(xs)
        if not f:
            print(f"    {name:26} tail too small to fit")
            continue
        pl, g = f["power_law"], f["geometric_same_support"]
        better = "power law" if pl["ks"] < g["ks"] else "GEOMETRIC"
        print(f"    {name:26} tail n={f['tail_n']:3}  xmin={pl['xmin']}  "
              f"alpha={pl['alpha']:.2f} over {pl['n']:3} pts  "
              f"KS {pl['ks']:.3f} vs geom {g['ks']:.3f} -> {better}")


def pct(xs: list[int]) -> str:
    return f"{100.0 * sum(1 for x in xs if x == 1) / len(xs):5.1f}%" if xs \
        else "    --"


def mean(xs: list[int]) -> str:
    return f"{sum(xs) / len(xs):5.2f}" if xs else "   --"


def main() -> int:
    ap = audits.parser(__name__, __doc__)
    ap.add_argument("--split", metavar="SESS",
                    help="also pool SESS against the other proof sessions "
                         "and compare (e.g. --split ntr)")
    ns = ap.parse_args()
    # Resolve once, here: the default is not a constant any more
    # (corpus.py -- it depends on where the operator is standing).
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

    scopes: dict[str, dict[str, list[int]]] = {
        "code": {}, "proof": {}, "attempt": {}}
    for ep in A._episodes(corpus.load(corpus.resolve(ns.input))):
        if ep[-1]["outcome"] != "ok":
            continue
        sess = A.project(ep)
        # The attempt scope is computed first and unconditionally: the older
        # two scopes drop an episode with no captured delta, and those are
        # precisely the episodes this column exists to recover, so gating it
        # behind their filter would hide what it is meant to show.
        n = A.attempt_length(ep)
        if n is not None and A.proof_bearing(ep):
            scopes["attempt"].setdefault(sess, []).append(n)
        k = sum(1 for r in ep if A.rec_class(r) == "code")
        if not k:
            continue
        scopes["code"].setdefault(sess, []).append(k)
        if any(touches_proof(r) for r in ep):
            scopes["proof"].setdefault(sess, []).append(k)

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
    print("  attempt  every recorded build (attempts.attempt_length) --")
    print("           the shipped scope; drops only no-op rebuilds")

    if ns.split:
        # Only the proof sessions: `tooling`/`mixed`/`none` are not a
        # development, and the retired dirs (`aem`, `scratch`) are too small
        # to pool meaningfully.
        proof_sess = [s for s in order if s in scopes["attempt"]]
        if ns.split not in proof_sess:
            print(f"\nFAIL: {ns.split} is not one of {proof_sess}",
                  file=sys.stderr)
            return 2
        rest = [s for s in proof_sess if s != ns.split]
        compare(f"pre-{ns.split} ({'+'.join(rest)})",
                [x for s in rest for x in scopes["attempt"][s]],
                ns.split, scopes["attempt"][ns.split])
    return 0


if __name__ == "__main__":
    sys.exit(main())
