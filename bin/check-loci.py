#!/usr/bin/env python3
"""check-loci.py — the `error_loci` path, end to end.

Isabelle error text -> `isabelle-watchdog._error_loci` -> the record field
-> `attempts._locus_dir` attribution.  This branch only fires on a *broken*
build, so it cannot be validated by running the suite green and it cannot
be validated retrospectively either — build logs rotate.  Hence a check
against verbatim error text from the corpus.

Covers both shapes the loci arrive in, because they come from different
sides of the watchdog and only one of them is a path: a compile error
gives `~/…/t/ar/AlphabetReduction.thy`, a watchdog kill gives Isabelle's
session-qualified `Alphabet_Enlargement.EncodingWrap_WF`.  Also the
out-of-tree decline, and the case the whole ladder exists for — a
trajectory with no captured diff that still attributes and still counts.

Usage:  bin/check-loci.py     (prints PASS / raises on failure)
"""
import importlib.util, sys
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"


def load(name):
    spec = importlib.util.spec_from_file_location(name, BIN / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


W = load("isabelle-watchdog") if (BIN / "isabelle-watchdog.py").exists() else None
A = load("attempts")

# Verbatim shapes from the corpus (bin/attempts.py reads both).
LINES = [
    '*** Failed to finish proof:',
    '*** goal (1 subgoal):',
    '*** At command "by" (line 1441 of "~/projects/ndtht/t/ar/AlphabetReduction.thy")',
    '*** Undefined constant: "time_cleanup"',
    '*** At command "lemma" (line 41 of "~/projects/claudecode/ndtht/t/ae/Wrap_Convention.thy")',
    '*** At command "by" (line 1441 of "~/projects/ndtht/t/ar/AlphabetReduction.thy")',
]
loci = W._error_loci(LINES)
print("extracted:")
for thy, ln in loci:
    print(f"   {ln:>6}  {thy}  -> {A._locus_dir(thy)}")
assert len(loci) == 2, f"dedup failed: {loci}"
assert [A._locus_dir(t) for t, _ in loci] == ["ar", "ae"], loci

# Watchdog-kill form: session-qualified, no path.
for thy, want in [("Alphabet_Enlargement.EncodingWrap_WF", "ae"),
                  ("Multitape_Alphabet_Reduction.AlphabetReduction", "ar"),
                  ("Nondeterministic_Tape_Reduction.TapeReduce", "ntr"),
                  ("HOAU_Spike.Anti_Unify", None)]:
    got = A._locus_dir(thy)
    print(f"   qualified {thy:48} -> {got}")
    assert got == want, f"{thy}: got {got}, want {want}"

# A record carrying loci must attribute and count as proof-bearing even
# with no diff at all -- the case the whole ladder exists for.
ep = [{"outcome": "fail", "diff": "", "error_head": "Failed to finish proof:",
       "error_loci": [[loci[0][0], loci[0][1]]], "command": []},
      {"outcome": "ok", "diff": "", "error_head": None, "command": []}]
print(f"\n   diffless episode -> project={A.project(ep)!r} "
      f"proof_bearing={A.proof_bearing(ep)} length={A.attempt_length(ep)}")
assert A.project(ep) == "ar" and A.proof_bearing(ep) and A.attempt_length(ep) == 2
print("\nPASS: loci extract, dedup, attribute in both forms, and decline for HOAU")
