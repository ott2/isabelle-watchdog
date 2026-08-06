#!/usr/bin/env python3
"""The watchdog against a real Isabelle, end to end.

Everything else in this suite works on recorded data or synthetic records.
That leaves the part most likely to break unexercised: the supervision itself
reads Isabelle's *live output*, and every judgement it makes -- the kill
conditions, the error loci, and above all the loop detector -- depends on the
exact wording and timing of what Isabelle prints.  A corpus cannot test that,
because a corpus is what the parsing already produced.

Three attempts against a two-lemma session:

    green    a build that succeeds
    fail     a false lemma, so an error head with a file and line to extract
    loop     a simp rule that rewrites `f x` to `f (Suc x)` forever

The loop case is the one worth the wall-clock.  Its detection rests on three
constants agreeing (docs/logging-design.md, and the comment on
`inject_progress_threshold`): the watchdog injects
`build_progress_threshold=15` because Isabelle's own default of 20 s
coincides with `WATCHDOG_TIMEOUT`, which would fire the activity kill at the
same instant as the one warning that names the line.  At 15 s, with
Isabelle's 2 s re-emit, the warnings land at 15/17/19 s -- three consecutive,
just under the kill.  That is a claim about another program's behaviour, it
held for Isabelle2025-2, and nothing but running it can say whether it still
does.

SKIPPED unless Isabelle is on the path *and* a HOL heap is already built.
Building HOL to run a test would take longer than the test is worth, so its
absence is a skip rather than a slow pass.  About 2m45s when it does run:
three builds each paying to load the HOL heap, plus the loop case waiting out
its 20 s of spinning.  Slow enough to keep out of a tight edit loop, and the
only thing here that can tell you Isabelle still behaves as assumed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import isabelle_watchdog

# `raise unittest.SkipTest` rather than a pytest decorator: pytest honours it,
# and so does plain `python tests/test_isabelle_integration.py`, which is how
# the rest of this suite is run.  No test dependency either way.
SkipTest = unittest.SkipTest

SESSION = "Probe"
ROOT = "session Probe = HOL +\n  theories\n    Probe_A\n"

GREEN = '''theory Probe_A
  imports Main
begin

lemma easy: "(n::nat) + 0 = n"
  by simp

end
'''

# `n + 1 = n` is false, so the proof fails at a known line and Isabelle names
# the file and the command.  The locus points at line 9, the `by` -- not at
# line 8 where the `lemma` is: a locus names the command that failed, which is
# the whole reason it is more useful than a theory name.
FALSE_LEMMA = '''theory Probe_A
  imports Main
begin

lemma easy: "(n::nat) + 0 = n"
  by simp

lemma broken: "(n::nat) + 1 = n"
  by simp

end
'''

# An axiom that rewrites `f x` to `f (Suc x)`, declared [simp].  The simplifier
# then rewrites forever without ever failing, which is the shape a real looping
# tactic has: it makes progress on every step and never terminates.  `=>` for
# the function arrow deliberately -- the ASCII form survives every encoding
# this file might be read through.
LOOPING = '''theory Probe_A
  imports Main
begin

lemma easy: "(n::nat) + 0 = n"
  by simp

axiomatization f :: "nat => nat" where
  f_unfold [simp]: "f x = f (Suc x)"

lemma spins: "f 0 = f 1"
  by simp

end
'''
LOOPING_LINE = "12"          # the `by simp` of `spins`


def isabelle_exe() -> str:
    """The Isabelle launcher, or skip."""
    exe = shutil.which("isabelle") or str(Path.home() / ".local/bin/isabelle")
    if not Path(exe).exists():
        raise SkipTest("no `isabelle` on PATH")
    return exe


def require_hol_heap(exe: str, heaps: Path | None = None) -> None:
    """Skip unless a HOL heap is already built.

    stdout only: a stray missing component prints to stderr on every
    invocation here and would otherwise be parsed as part of the path.
    `heaps` is injectable because `isabelle getenv` reads Isabelle's own
    settings rather than the ambient environment, so there is no other way to
    exercise the skip.
    """
    if heaps is None:
        p = subprocess.run([exe, "getenv", "-b", "ISABELLE_HEAPS"],
                           capture_output=True, text=True)
        heaps = Path(p.stdout.strip() or "/nonexistent")
    if not heaps.is_dir():
        raise SkipTest(f"cannot locate ISABELLE_HEAPS (got {heaps!r})")
    if not any((d / "HOL").exists() for d in heaps.iterdir() if d.is_dir()):
        raise SkipTest(f"no HOL heap under {heaps}; run `isabelle build HOL` first")


def make_project(root: Path, theory: str) -> None:
    """A one-session Isabelle project in a fresh git repository.

    Committed, because the recorder diffs against HEAD and an uncommitted
    tree would make every attempt's payload the whole file.
    """
    (root / "thy").mkdir(parents=True, exist_ok=True)
    (root / "thy" / "ROOT").write_text(ROOT)
    (root / "thy" / "Probe_A.thy").write_text(theory)
    git = ["git", "-C", str(root)]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["config", "user.email", "probe@example.invalid"], check=True)
    subprocess.run(git + ["config", "user.name", "probe"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "probe"], check=True)


def run_build(root: Path, logs: Path, note: str, **budgets) -> tuple[int, str]:
    """One recorded attempt.  Returns (exit code, terse summary)."""
    exe = isabelle_exe()
    env = dict(os.environ)
    env["PATH"] = f"{Path(exe).parent}{os.pathsep}{env.get('PATH', '')}"
    # The subprocess must import the same copy of the package this test did,
    # whether that is an install or a source tree on PYTHONPATH.
    src = str(Path(isabelle_watchdog.__file__).resolve().parent.parent)
    env["PYTHONPATH"] = f"{src}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env["WATCHDOG_LOG_DIR"] = str(logs)
    env["BUILD_NOTE"] = note
    env.update({k: str(v) for k, v in budgets.items()})
    p = subprocess.run(
        [sys.executable, "-m", "isabelle_watchdog.watchdog",
         "isabelle", "build", "-d", "thy", "-v", SESSION],
        cwd=root, env=env, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def last_record(logs: Path) -> dict:
    return json.loads(logs.joinpath("builds.jsonl").read_text().splitlines()[-1])


def test_heap_detection_skips_rather_than_builds():
    """A missing heap must skip: building HOL to run a test is not a test."""
    exe = isabelle_exe()
    empty = Path(tempfile.mkdtemp(prefix="wd-heaps-"))
    try:
        (empty / "polyml-x").mkdir()
        try:
            require_hol_heap(exe, heaps=empty)
        except SkipTest as e:
            print(f"   no HOL heap -> SkipTest({str(e)[:40]}...)")
        else:
            raise AssertionError("a heapless installation must skip")
    finally:
        shutil.rmtree(empty, ignore_errors=True)


def test_isabelle_end_to_end():
    exe = isabelle_exe()
    require_hol_heap(exe)

    tmp = Path(tempfile.mkdtemp(prefix="wd-isa-"))
    try:
        root, logs = tmp / "proj", tmp / "logs"
        make_project(root, GREEN)

        # --- green ------------------------------------------------------
        # A generous budget only here: the first build of a session pays for
        # loading the HOL heap, which is not what any of these budgets are
        # about.
        code, out = run_build(root, logs, "change: first build; expect: ok",
                              WALL_TIMEOUT=900, WATCHDOG_TIMEOUT=300)
        rec = last_record(logs)
        print(f"   green   exit={code} outcome={rec['outcome']} "
              f"{rec['elapsed_s']}s  predicted={rec['note_predicted']}")
        assert code == 0, out
        assert rec["outcome"] == "ok", rec["outcome"]
        assert not rec["error_loci"], rec["error_loci"]
        assert rec["limits"]["build_progress_threshold"] == 15, rec["limits"]

        # --- failure, with a locus --------------------------------------
        (root / "thy" / "Probe_A.thy").write_text(FALSE_LEMMA)
        code, out = run_build(root, logs,
                              "diagnosis: n+1=n is false; change: added it; "
                              "expect: fail",
                              WALL_TIMEOUT=900, WATCHDOG_TIMEOUT=300)
        rec = last_record(logs)
        loci = rec["error_loci"] or []
        print(f"   fail    outcome={rec['outcome']} loci={loci}")
        assert rec["outcome"] == "fail", rec["outcome"]
        assert loci, f"no locus extracted from:\n{rec['error_head']}"
        where, line = loci[0]
        assert where.endswith("Probe_A.thy"), where
        assert line == "9", f"expected the `by` at line 9, got {line}"
        # The edit is in the payload, so the corpus explains the outcome.
        assert "broken" in (rec["diff"] or ""), rec["diff"][:200]

        # --- the loop detector ------------------------------------------
        # Budgets well clear of the loop threshold, so what fires is the loop
        # detector rather than the wall.  With the shipped 40 s wall a cold
        # heap can exhaust it before the command has spun for 20 s: the line
        # is still named, but the diagnosis reads `wall`, and this test is
        # about the loop path specifically.
        (root / "thy" / "Probe_A.thy").write_text(LOOPING)
        code, out = run_build(root, logs,
                              "diagnosis: f_unfold rewrites forever; "
                              "change: declared it [simp]; expect: timeout",
                              WALL_TIMEOUT=180, WATCHDOG_TIMEOUT=180)
        rec = last_record(logs)
        print(f"   loop    outcome={rec['outcome']} reason={rec['timeout_reason']}")
        print(f"           head: {rec['error_head']}")
        assert rec["outcome"] == "timeout", rec["outcome"]
        assert rec["timeout_reason"] == "loop_progress", (
            f"expected the loop detector to fire, got {rec['timeout_reason']!r}. "
            f"If Isabelle stopped re-emitting its progress warning, the three "
            f"coupled constants no longer line up -- see "
            f"inject_progress_threshold.\n{out}")
        # The whole point: it names the line, not just 'something hung'.
        assert LOOPING_LINE in (rec["error_head"] or ""), rec["error_head"]
        assert any(LOOPING_LINE == ln for _thy, ln in rec["error_loci"] or []), \
            rec["error_loci"]
        assert code == 124, f"watchdog kill should exit 124, got {code}"

        print("\nPASS: green, locus-bearing failure, and a named looping line")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    try:
        test_heap_detection_skips_rather_than_builds()
        test_isabelle_end_to_end()
    except SkipTest as e:
        print(f"SKIPPED: {e}")
