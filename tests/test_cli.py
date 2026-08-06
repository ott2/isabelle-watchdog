"""Every subcommand runs, and the failures fail the way they promise.

Thirteen verbs over one file format, assembled from two scripts written in
two projects.  A smoke test over all of them is worth more than it looks:
several are thin adapters onto `attempts.py` whose signatures predate the
parser, and the historical failure mode there is not a wrong answer but a
`TypeError` nobody hit until someone tried that verb on that corpus.

The two failures checked at the foot are the ones a reader meets first, and
both used to be silent: a corpus that could not be resolved, and a corpus
that resolved to nothing.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from conftest import package_env

pytestmark = pytest.mark.slow

ALL_COMMANDS = ("check", "repair", "replay", "extract",
                "list", "show", "episodes", "notes", "classify",
                "lengths", "size", "progress", "flips")


def run(args, cwd, corpus=None, **env):
    argv = [sys.executable, "-m", "isabelle_watchdog.trajectory", *args]
    if corpus is not None:
        env.setdefault("TRAJECTORY_CORPUS", str(corpus))
    return subprocess.run(argv, cwd=str(cwd), env=package_env(**env),
                          capture_output=True, text=True)


@pytest.mark.parametrize("cmd", [
    ["check"], ["repair"], ["replay"], ["list"], ["episodes"], ["notes"],
    ["lengths"], ["lengths", "--fit"], ["lengths", "--by-project"],
    ["size"], ["progress"], ["flips"], ["list", "--all"],
    ["episodes", "--diffs"], ["notes", "--loci"],
])
def test_a_subcommand_runs_against_a_real_corpus(trajectory, cmd, tmp_path):
    p = run(cmd, cwd=trajectory.root, corpus=trajectory.corpus)
    assert p.returncode == 0, f"{cmd}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip(), f"{cmd} printed nothing"


def test_show_and_classify_take_a_build_id(trajectory):
    build_id = trajectory.records[0]["build_id"]
    for cmd in (["show", build_id], ["classify", build_id, "-v"]):
        p = run(cmd, cwd=trajectory.root, corpus=trajectory.corpus)
        assert p.returncode == 0, f"{cmd}\n{p.stdout}\n{p.stderr}"
        assert build_id in p.stdout or "code" in p.stdout


def test_extract_writes_a_snapshot(trajectory, tmp_path):
    dest = tmp_path / "snapshot"
    p = run(["extract", "1", str(dest)], cwd=trajectory.root,
            corpus=trajectory.corpus)
    assert p.returncode == 0, p.stderr
    assert (dest / "thy" / "Probe_A.thy").exists()


def test_every_documented_subcommand_is_reachable(trajectory):
    """The grouped epilog is the command list, so a verb that fell out of the
    parser would still be advertised there."""
    p = run(["--help"], cwd=trajectory.root, corpus=trajectory.corpus)
    assert p.returncode == 0
    for name in ALL_COMMANDS:
        assert name in p.stdout, f"{name} is not in the help"
    for group in ("integrity", "reading", "measuring"):
        assert group in p.stdout


def test_repo_is_only_offered_where_it_does_something(trajectory):
    """`--repo` on a view that never opens the repository would imply it did
    something -- the fault that left `check` silently defaulting to the tool's
    own directory."""
    needs = run(["check", "--help"], cwd=trajectory.root, corpus=trajectory.corpus)
    does_not = run(["notes", "--help"], cwd=trajectory.root,
                   corpus=trajectory.corpus)
    assert "--repo" in needs.stdout
    assert "--repo" not in does_not.stdout


# ------------------------------------------------------------------- failures

def test_an_unresolvable_corpus_is_reported_not_guessed(repo):
    p = run(["list"], cwd=repo.root)
    assert p.returncode == 2
    assert "no corpus found" in p.stderr


def test_an_empty_corpus_says_so(repo, tmp_path):
    empty = tmp_path / "builds.jsonl"
    empty.write_text("")
    p = run(["list"], cwd=repo.root, corpus=empty)
    assert p.returncode == 1
    assert "no attempts recorded yet" in p.stderr


def test_a_named_attribution_file_must_exist(trajectory, tmp_path):
    """A typo must not read as "no overrides": absence is what a rename looks
    like, and an oversight should not read as a decision."""
    p = run(["lengths", "--attribution", str(tmp_path / "nope.json")],
            cwd=trajectory.root, corpus=trajectory.corpus)
    assert p.returncode == 2
    assert "FAIL" in p.stderr


def test_an_unknown_subcommand_is_rejected(repo):
    p = run(["frobnicate"], cwd=repo.root)
    assert p.returncode == 2
