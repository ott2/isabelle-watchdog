#!/usr/bin/env python3
"""The `error_loci` path, end to end, and the attribution derived from it.

Isabelle error text -> `watchdog._error_loci` -> the record field ->
`Attribution.locus_dir`.  This branch only fires on a *broken* build, so it
cannot be validated by running a suite green, and it cannot be validated
retrospectively either — build logs rotate.  Hence a check against verbatim
error text taken from a real corpus.

Covers both shapes the loci arrive in, because they come from different sides
of the watchdog and only one of them is a path: a compile error gives
`~/…/t/ar/AlphabetReduction.thy`, a watchdog kill gives Isabelle's
session-qualified `Alphabet_Enlargement.EncodingWrap_WF`.  Also the
out-of-tree decline, and the case the whole ladder exists for — a trajectory
with no captured diff that still attributes and still counts.

The attribution is *built here*, from explicit inputs, rather than imported
from a table inside the package.  That is the point: there is no such table
any more, and a test that needed one would be re-asserting the bug.
"""
import sys
from pathlib import Path

from isabelle_watchdog import attempts as A
from isabelle_watchdog import watchdog as W


def ndtht_shaped() -> A.Attribution:
    """An attribution of ndtht's shape, stated rather than assumed.

    `HOAU_Spike` is deliberately absent: it was built *against* the tree's
    sessions rather than being one of them, so it must attribute to nothing.
    Absence is what that looks like now — the old code needed an explicit
    `None` entry because a missing key and a declared exclusion behaved
    differently there.
    """
    return A.Attribution(
        dirs={"t/ae", "t/ar", "t/art", "t/base", "t/ntr"},
        targets={"Alphabet_Enlargement": "t/ae",
                 "Multitape_Alphabet_Reduction": "t/ar",
                 "Nondeterministic_Tape_Reduction": "t/ntr"},
        aliases={})


def test_error_loci():
    # Install it: `proof_bearing` and friends ask the corpus-level
    # attribution, which is normally derived by `fit_attribution`.
    at = A.use_attribution(ndtht_shaped())

    # Verbatim shapes from the corpus.
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
        print(f"   {ln:>6}  {thy}  -> {at.locus_dir(thy)}")
    assert len(loci) == 2, f"dedup failed: {loci}"
    assert [at.locus_dir(t) for t, _ in loci] == ["ar", "ae"], loci

    # Watchdog-kill form: session-qualified, no path.
    for thy, want in [("Alphabet_Enlargement.EncodingWrap_WF", "ae"),
                      ("Multitape_Alphabet_Reduction.AlphabetReduction", "ar"),
                      ("Nondeterministic_Tape_Reduction.TapeReduce", "ntr"),
                      ("HOAU_Spike.Anti_Unify", None)]:
        got = at.locus_dir(thy)
        print(f"   qualified {thy:48} -> {got}")
        assert got == want, f"{thy}: got {got}, want {want}"

    # A record carrying loci must attribute and count as proof-bearing even
    # with no diff at all -- the case the whole ladder exists for.
    ep = [{"outcome": "fail", "diff": "", "error_head": "Failed to finish proof:",
           "error_loci": [[loci[0][0], loci[0][1]]], "command": []},
          {"outcome": "ok", "diff": "", "error_head": None, "command": []}]
    print(f"\n   diffless episode -> project={at.project(ep)!r} "
          f"proof_bearing={A.proof_bearing(ep)} length={A.attempt_length(ep)}")
    assert at.project(ep) == "ar" and A.proof_bearing(ep) \
        and A.attempt_length(ep) == 2
    print("\nPASS: loci extract, dedup, attribute in both forms, decline for HOAU")


def _rec(diff="", **kw):
    r = {"outcome": "ok", "diff": diff, "command": [], "error_loci": []}
    r.update(kw)
    return r


def _diff(path: str, body: str = "+lemma x: True by simp\n") -> str:
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,1 +1,2 @@\n {body}")


def test_derives_session_dirs_from_any_layout():
    """The directories that hold theories are the developments — whatever
    they are called.  This is the whole of the generalisation: ndtht's `t/x`
    and 43sp's flat `isabelle/` fall out of the same rule, and neither is
    named anywhere in the package."""
    for layout, want in ((["t/ae/A.thy", "t/ntr/B.thy"], {"t/ae", "t/ntr"}),
                         (["isabelle/SP.thy", "isabelle/S3.thy"], {"isabelle"}),
                         (["src/theories/X.thy"], {"src/theories"})):
        recs = [_rec(_diff(p)) for p in layout]
        got = A.session_dirs(recs)
        print(f"   {layout} -> {sorted(got)}")
        assert got == want, f"{layout}: got {got}, want {want}"

    # A directory that never holds a theory is not a development, which is
    # what stops `bin/` becoming one.
    recs = [_rec(_diff("t/ae/A.thy")), _rec(_diff("bin/tool.py"))]
    at = A.Attribution.learn(recs)
    assert at.path_dir("t/ae/A.thy") == "ae"
    assert at.path_dir("bin/tool.py") is None
    print("   bin/tool.py -> None (never holds a theory)")


def test_learns_targets_from_root_and_co_occurrence():
    """Both learning routes, and the case that needs the stronger one."""
    root = ("diff --git a/t/ar/ROOT b/t/ar/ROOT\n--- a/t/ar/ROOT\n"
            "+++ b/t/ar/ROOT\n@@ -0,0 +1 @@\n"
            "+session Multitape_Alphabet_Reduction = Multitape_TM_Substrate +\n")
    recs = [
        _rec(_diff("t/ar/A.thy")),                       # makes t/ar a session
        _rec(root),                                       # ROOT declares the name
        _rec(_diff("t/ntr/B.thy"),                        # co-occurrence only
             command=["isabelle", "build", "-d", "t", "Nondeterministic_Tape_Reduction"]),
    ]
    at = A.Attribution.learn(recs)
    print("   learned:", at.targets)
    assert at.targets["Multitape_Alphabet_Reduction"] == "t/ar", "ROOT route"
    assert at.targets["Nondeterministic_Tape_Reduction"] == "t/ntr", "co-occurrence"

    # The ROOT route exists because co-occurrence cannot see a target that
    # only ever appears on builds with no captured diff -- which is exactly
    # how two ndtht trajectories lost their label when it was missing.
    diffless = _rec(command=["isabelle", "build", "-d", "t",
                             "Multitape_Alphabet_Reduction"])
    assert at.command_dir(diffless) == "ar"
    print("   diffless build attributes via the ROOT-derived map -> ar")
    print("\nPASS: session dirs and target map derive from the corpus")


def test_overrides_are_named_not_discovered(tmp=None):
    """Attribution facts come from a path the caller gives, and a wrong path
    is an error rather than a silent fallback to no overrides."""
    import json, os, tempfile
    d = Path(tempfile.mkdtemp())

    good = d / "attr.json"
    good.write_text(json.dumps({"_note": "ignored", "targets": {"Spike": None},
                                "aliases": {"aem": "ae"}}))
    got = A.load_overrides(good)
    assert got["targets"] == {"Spike": None} and got["aliases"] == {"aem": "ae"}
    print(f"   {good.name} -> targets={got['targets']} aliases={got['aliases']}")

    # Nothing named, nothing loaded -- the right default for a project with
    # no renames and no out-of-tree sessions.
    os.environ.pop(A.ENV_ATTRIBUTION, None)
    assert A.load_overrides(None) == {}
    print("   nothing named -> {}")

    # The env var is the other channel, for a project that always wants it.
    os.environ[A.ENV_ATTRIBUTION] = str(good)
    assert A.load_overrides(None)["targets"] == {"Spike": None}
    os.environ.pop(A.ENV_ATTRIBUTION)
    print(f"   ${A.ENV_ATTRIBUTION} -> same")

    for bad, why in ((d / "absent.json", "a typo must not read as 'no overrides'"),
                     (None, None)):
        if bad is None:
            continue
        try:
            A.load_overrides(bad)
        except Exception as e:
            print(f"   {bad.name} -> {type(e).__name__}: {e}")
        else:
            raise AssertionError(why)

    # `target` for `targets` would otherwise be accepted and do nothing.
    typo = d / "typo.json"
    typo.write_text(json.dumps({"target": {"Spike": None}}))
    try:
        A.load_overrides(typo)
    except Exception as e:
        print(f"   {typo.name} -> {type(e).__name__}")
    else:
        raise AssertionError("a misspelled key must not be silently ignored")
    print("\nPASS: overrides are named explicitly and fail loudly")


if __name__ == "__main__":
    test_error_loci()
    test_derives_session_dirs_from_any_layout()
    test_learns_targets_from_root_and_co_occurrence()
    test_overrides_are_named_not_discovered()
