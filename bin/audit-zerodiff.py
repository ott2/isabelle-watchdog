#!/usr/bin/env python3
"""audit-zerodiff.py — what is a build whose recorded diff is empty?

A record with no diff (delta class `none`) is dropped from the length count
and, if a whole episode is empty, from the dataset.  That is only safe if
such a record is genuinely not an attempt.  It splits by outcome, and the
two halves mean opposite things:

  - **empty + failure** — something was built and it did not compile.  The
    edit existed; the recorder did not see it.  Before 2026-07-27 that was
    routine: `_snapshot_tree()` staged tracked files only, so a theory still
    untracked was invisible while it was being written (logging-design.md
    13.1).  Dropping these silently discards real failed attempts.
  - **empty + success** — no change was seen and the build passed.  Either
    a no-op rebuild of an unchanged tree, or the same capture gap hiding the
    edit that fixed a failure.  Which one is decided by what came *before*:
    an empty green after a failure is a repair whose diff was lost; an empty
    green after a green is a re-run.

So this reports class x outcome x era, and splits the empty greens by their
predecessor.  The 2026-07-27 fix is the era boundary: after it, untracked
`.thy`/`ROOT`/`ROOTS` are staged, so an empty diff should mean what it says.

Usage:  bin/audit-zerodiff.py [-i BUILDS_JSONL]
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from collections import Counter
from pathlib import Path

BIN = Path(__file__).resolve().parent
FIX = "2026-07-27"          # untracked-source capture fix (logging-design 13.1)

# An Isabelle error head carries the failing file's path, which survives even
# when the diff does not — the one handle on an episode the recorder missed.
_THY_IN_ERROR = re.compile(r"/t/([A-Za-z0-9_-]+)/([A-Za-z0-9_]+)\.thy")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A = _load("attempts")


def era(rec: dict) -> str:
    return "post-fix" if (rec.get("timestamp") or "") >= FIX else "pre-fix"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-i", "--input", default=str(A.BUILDS_JSONL))
    ns = ap.parse_args()
    recs = A._load(Path(ns.input))
    eps = A._episodes(recs)

    print(f"records: {len(recs)} in {len(eps)} episodes "
          f"(era boundary {FIX})\n")

    print("1. delta class by outcome and era\n")
    print("   era      | class |    ok   fail  timeout |  total   share")
    print("   ---------+-------+-----------------------+---------------")
    for e in ("pre-fix", "post-fix"):
        sub = [r for r in recs if era(r) == e]
        if not sub:
            continue
        for cls in ("code", "doc", "none"):
            c = Counter(r["outcome"] for r in sub if A.rec_class(r) == cls)
            n = sum(c.values())
            print(f"   {e:8} | {cls:5} | {c['ok']:5} {c['fail']:6} "
                  f"{c['timeout']:8} | {n:6} {100 * n / len(sub):6.1f}%")
        print(f"   {'':8} |       |                       | {len(sub):6}")

    print("\n2. empty diffs by what preceded them\n")
    print("   An empty *failure* is a build of work the recorder could not")
    print("   see.  An empty *green* is a repair whose diff was lost if a")
    print("   failure preceded it, and a no-op re-run if a green did.\n")
    print("   era      | outcome | after fail/timeout | after ok | first in log")
    print("   ---------+---------+--------------------+----------+-------------")
    for e in ("pre-fix", "post-fix"):
        for outcome in ("ok", "fail", "timeout"):
            after_bad = after_ok = first = 0
            for i, r in enumerate(recs):
                if era(r) != e or A.rec_class(r) != "none":
                    continue
                if r["outcome"] != outcome:
                    continue
                if i == 0:
                    first += 1
                elif recs[i - 1]["outcome"] == "ok":
                    after_ok += 1
                else:
                    after_bad += 1
            if after_bad or after_ok or first:
                print(f"   {e:8} | {outcome:7} | {after_bad:18} "
                      f"| {after_ok:8} | {first:12}")

    print("\n3. what the dataset loses\n")
    empty_eps = [ep for ep in eps
                 if not any(A.rec_class(r) == "code" for r in ep)]
    closed_empty = [ep for ep in empty_eps if ep[-1]["outcome"] == "ok"]
    shortened = [ep for ep in eps
                 if ep[-1]["outcome"] == "ok"
                 and any(A.rec_class(r) == "code" for r in ep)
                 and any(A.rec_class(r) == "none" for r in ep)]
    print(f"   episodes dropped entirely (no code delta at all): "
          f"{len(empty_eps)} ({len(closed_empty)} closed)")
    print(f"   closed episodes kept but shortened by an empty record: "
          f"{len(shortened)}")
    if shortened:
        under = sum(len(ep) - sum(1 for r in ep if A.rec_class(r) == "code")
                    for ep in shortened)
        print(f"   attempts not counted in those: {under} "
              f"(mean {under / len(shortened):.1f} per affected episode)")
        was_one = sum(1 for ep in shortened
                      if sum(1 for r in ep if A.rec_class(r) == "code") == 1)
        print(f"   of which scored as one-shot despite failing: {was_one}")

    print("\n4. of the wholly-dropped episodes, which were real?\n")
    failing = [ep for ep in empty_eps
               if any(r["outcome"] != "ok" for r in ep)]
    print(f"   contain a failure — real work, invisible: {len(failing)}"
          f" ({sum(len(e) for e in failing)} attempts)")
    print(f"   a lone green — a no-op rebuild, correctly dropped: "
          f"{len(empty_eps) - len(failing)}")

    print("\n   These are recoverable in part.  The *diff* is gone, but an")
    print("   Isabelle error head names the file it failed in, so a diffless")
    print("   failing episode can still be attributed to a session:\n")
    named = Counter()
    for ep in failing:
        hits = {m.group(1) for r in ep
                for m in [_THY_IN_ERROR.search(r.get("error_head") or "")]
                if m}
        if hits:
            named[",".join(sorted(hits))] += 1
    print(f"   {sum(named.values())} of {len(failing)} name a t/<sess>/*.thy:")
    for k, v in named.most_common():
        print(f"      {k:14} {v}")
    print("\n   (t/scratch-ar appears only here — it was never committed, so")
    print("   it has no captured diff anywhere in the corpus and no git")
    print("   history to say whether its work graduated.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
