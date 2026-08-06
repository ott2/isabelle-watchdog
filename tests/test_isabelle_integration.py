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
only thing here that can tell you Isabelle still behaves as assumed:

    pytest -m "not isabelle"      everything else
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import package_env

pytestmark = pytest.mark.isabelle

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
        pytest.skip("no `isabelle` on PATH")
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
        pytest.skip(f"cannot locate ISABELLE_HEAPS (got {heaps!r})")
    if not any((d / "HOL").exists() for d in heaps.iterdir() if d.is_dir()):
        pytest.skip(f"no HOL heap under {heaps}; run `isabelle build HOL` first")


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
    """One recorded attempt.  Returns (exit code, terse summary).

    `package_env` both reaches this package from the subprocess and clears the
    ambient configuration, so a developer with `WATCHDOG_LOG_DIR` or
    `WALL_TIMEOUT` exported does not quietly run a different test.
    """
    exe = isabelle_exe()
    env = package_env(
        PATH=f"{Path(exe).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        WATCHDOG_LOG_DIR=str(logs), BUILD_NOTE=note, **budgets)
    p = subprocess.run(
        [sys.executable, "-m", "isabelle_watchdog.watchdog",
         "isabelle", "build", "-d", "thy", "-v", SESSION],
        cwd=root, env=env, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def last_record(logs: Path) -> dict:
    return json.loads(logs.joinpath("builds.jsonl").read_text().splitlines()[-1])


# One session, three attempts, run once: each build pays about 25 seconds to
# load the HOL heap, and the diffs are incremental, so the chain has to happen
# in order.  The assertions then read as separate tests, which is what makes a
# failure say *which* of the three broke.
@pytest.fixture(scope="session")
def isabelle_run(tmp_path_factory):
    exe = isabelle_exe()
    require_hol_heap(exe)

    tmp = tmp_path_factory.mktemp("isa")
    root, logs = tmp / "proj", tmp / "logs"
    make_project(root, GREEN)
    out = {}

    # --- green ----------------------------------------------------------
    # A generous budget only here: the first build of a session pays for
    # loading the HOL heap, which is not what any of these budgets are about.
    code, text = run_build(root, logs, "change: first build; expect: ok",
                           WALL_TIMEOUT=900, WATCHDOG_TIMEOUT=300)
    out["green"] = (code, text, last_record(logs))

    # --- failure, with a locus ------------------------------------------
    (root / "thy" / "Probe_A.thy").write_text(FALSE_LEMMA)
    code, text = run_build(root, logs,
                           "diagnosis: n+1=n is false; change: added it; "
                           "expect: fail",
                           WALL_TIMEOUT=900, WATCHDOG_TIMEOUT=300)
    out["fail"] = (code, text, last_record(logs))

    # --- the loop detector ----------------------------------------------
    # Budgets well clear of the loop threshold, so what fires is the loop
    # detector rather than the wall.  With the shipped 40 s wall a cold heap
    # can exhaust it before the command has spun for 20 s: the line is still
    # named, but the diagnosis reads `wall`, and this is about the loop path.
    (root / "thy" / "Probe_A.thy").write_text(LOOPING)
    code, text = run_build(root, logs,
                           "diagnosis: f_unfold rewrites forever; "
                           "change: declared it [simp]; expect: timeout",
                           WALL_TIMEOUT=180, WATCHDOG_TIMEOUT=180)
    out["loop"] = (code, text, last_record(logs))
    return out


def test_a_missing_heap_skips_rather_than_building_one(tmp_path):
    """Building HOL to run a test would cost more than the test is worth, so
    its absence must be a skip and not a slow pass."""
    exe = isabelle_exe()
    (tmp_path / "polyml-x").mkdir()
    with pytest.raises(pytest.skip.Exception):
        require_hol_heap(exe, heaps=tmp_path)


def test_a_real_green_build_is_recorded(isabelle_run):
    code, out, rec = isabelle_run["green"]
    assert code == 0, out
    assert rec["outcome"] == "ok"
    assert not rec["error_loci"]
    assert rec["note_predicted"] == "ok"


def test_the_injected_threshold_survives_a_real_invocation(isabelle_run):
    """Isabelle accepted the option and the record says which value was in
    force -- the two halves of the constant that makes the loop detector
    possible."""
    _code, _out, rec = isabelle_run["green"]
    assert rec["limits"]["build_progress_threshold"] == 15


def test_a_locus_is_extracted_from_genuine_isabelle_output(isabelle_run):
    """And it points at the `by` on line 9, not at the `lemma` on line 8: a
    locus names the command that failed, which is the whole reason it beats a
    theory name."""
    _code, _out, rec = isabelle_run["fail"]
    assert rec["outcome"] == "fail"
    loci = rec["error_loci"] or []
    assert loci, f"no locus extracted from:\n{rec['error_head']}"
    where, line = loci[0]
    assert where.endswith("Probe_A.thy"), where
    assert line == "9", f"expected the `by` at line 9, got {line}"


def test_the_edit_that_caused_the_failure_is_in_the_payload(isabelle_run):
    """So the corpus explains its own outcome."""
    _code, _out, rec = isabelle_run["fail"]
    assert "broken" in (rec["diff"] or "")


def test_the_loop_detector_fires_on_a_real_looping_tactic(isabelle_run):
    """The claim this whole file exists to check.

    Three constants have to agree, and two of them belong to Isabelle: its
    progress threshold and its 2-second re-emit.  Nothing but running it can
    say whether they still do.
    """
    code, out, rec = isabelle_run["loop"]
    assert rec["outcome"] == "timeout"
    assert rec["timeout_reason"] == "loop_progress", (
        f"expected the loop detector to fire, got {rec['timeout_reason']!r}. "
        f"If Isabelle stopped re-emitting its progress warning, the three "
        f"coupled constants no longer line up -- see "
        f"inject_progress_threshold.\n{out}")
    assert code == 124, f"a watchdog kill should exit 124, got {code}"


def test_the_looping_line_is_named(isabelle_run):
    """"The build hung" and "`by simp` at Probe_A:12 hung" are different
    amounts of help."""
    _code, _out, rec = isabelle_run["loop"]
    assert LOOPING_LINE in (rec["error_head"] or ""), rec["error_head"]
    assert any(LOOPING_LINE == ln for _thy, ln in rec["error_loci"] or []), \
        rec["error_loci"]
