"""Where a corpus is found, and what a run of records means.

Resolution is the module that encodes this repository's governing rule --
resolve from where the operator is standing, never from where the tool is
installed -- and every path bug found during consolidation was a violation of
it.  The tests below pin the *order*, because the failure mode is not an error
message: a reader that silently resolves to a different corpus answers a
different question and looks like a real result.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from helpers import make_record
from isabelle_watchdog import corpus


def touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"outcome": "ok"}\n')
    return p


# ------------------------------------------------------------- an explicit name

def test_an_explicit_path_is_taken_as_given(tmp_path):
    p = touch(tmp_path / "elsewhere.jsonl")
    assert corpus.resolve(p) == p


def test_a_named_corpus_that_does_not_exist_is_an_error(tmp_path, repo, monkeypatch):
    """A typo must fail rather than fall through to a default.

    Falling through would answer about a different corpus while looking like
    a successful run -- the same shape of failure as the recorder writing a
    faithful diff of the wrong repository.
    """
    monkeypatch.chdir(repo.root)
    touch(repo.root / "t/logs/builds.jsonl")
    with pytest.raises(corpus.CorpusError, match="no such corpus"):
        corpus.resolve(tmp_path / "typo.jsonl")


# ----------------------------------------------------------- the default ladder

def test_trajectory_corpus_outranks_everything(tmp_path, repo, monkeypatch):
    """The variable exists to read a pooled or archived corpus that no writer
    owns, so it has to beat what the project happens to have on disk.

    It did not: standing in a project with its own `builds.jsonl` and
    pointing the variable at a pool reported "several corpora found" and
    refused to read either -- the one situation the override is for.
    """
    monkeypatch.chdir(repo.root)
    pooled = touch(tmp_path / "pooled.jsonl")
    touch(repo.root / "t/logs/builds.jsonl")
    monkeypatch.setenv("TRAJECTORY_CORPUS", str(pooled))
    assert corpus.resolve() == pooled


def test_a_named_corpus_beats_the_writers_variable(tmp_path, repo, monkeypatch):
    """Both are named rather than discovered, so the tie is broken by rank,
    not reported as an ambiguity."""
    monkeypatch.chdir(repo.root)
    pooled = touch(tmp_path / "pooled.jsonl")
    touch(repo.root / "custom/builds.jsonl")
    monkeypatch.setenv("TRAJECTORY_CORPUS", str(pooled))
    monkeypatch.setenv("WATCHDOG_LOG_DIR", str(repo.root / "custom"))
    assert corpus.resolve() == pooled


def test_a_named_corpus_that_is_not_there_yet_falls_through(repo, monkeypatch):
    """`WATCHDOG_LOG_DIR` is set by *writers*, often before the first build
    has written anything, so pointing at a not-yet-existing corpus must not
    be fatal -- it simply is not a candidate."""
    monkeypatch.chdir(repo.root)
    p = touch(repo.root / "t/logs/builds.jsonl")
    monkeypatch.setenv("WATCHDOG_LOG_DIR", str(repo.root / "not-yet"))
    assert corpus.resolve().resolve() == p.resolve()


def test_the_reader_lands_where_the_writer_wrote(repo, monkeypatch):
    """`WATCHDOG_LOG_DIR` is the writers' variable; the readers honour it too.

    43sp lost a corpus to the other half of this once: with the Makefile
    owning the variable, a build run outside make recorded into the
    recorder's built-in default instead -- a second corpus, a second instance
    id, and a build that looked unrecorded.
    """
    monkeypatch.chdir(repo.root)
    p = touch(repo.root / "custom-logs/builds.jsonl")
    monkeypatch.setenv("WATCHDOG_LOG_DIR", str(repo.root / "custom-logs"))
    assert corpus.resolve() == p


@pytest.mark.parametrize("layout", ["t/logs", "results/isabelle-logs"])
def test_the_known_layouts_are_found_under_the_current_project(repo, monkeypatch,
                                                               layout):
    monkeypatch.chdir(repo.root)
    p = touch(repo.root / layout / "builds.jsonl")
    assert corpus.resolve().resolve() == p.resolve()


def test_resolution_starts_from_the_project_not_the_cwd(repo, monkeypatch):
    """Standing in a subdirectory still finds the project's corpus, because
    the root comes from `git rev-parse`, not from `Path.cwd()`."""
    p = touch(repo.root / "t/logs/builds.jsonl")
    monkeypatch.chdir(repo.root / "thy")
    assert corpus.resolve().resolve() == p.resolve()


def test_no_corpus_anywhere_names_what_it_tried(repo, monkeypatch):
    monkeypatch.chdir(repo.root)
    with pytest.raises(corpus.CorpusError) as e:
        corpus.resolve()
    msg = str(e.value)
    assert "no corpus found" in msg
    # The message has to be actionable: an operator who has not set anything
    # needs to see which paths were considered, or the only way forward is to
    # read the source.
    assert "t/logs" in msg and "TRAJECTORY_CORPUS" in msg


# ------------------------------------------------- one file, several routes to it

def test_two_routes_to_one_file_are_not_an_ambiguity(repo, monkeypatch):
    """The 43sp regression, exactly.

    Its Makefile sets `WATCHDOG_LOG_DIR` to `results/isabelle-logs` -- which
    is also one of the known layouts -- so both candidates named the same
    file and every reader refused to run there, reporting a choice between
    two corpora that were one corpus.
    """
    monkeypatch.chdir(repo.root)
    p = touch(repo.root / "results/isabelle-logs/builds.jsonl")
    monkeypatch.setenv("WATCHDOG_LOG_DIR", str(repo.root / "results/isabelle-logs"))
    assert corpus.resolve().resolve() == p.resolve()


def test_a_symlinked_corpus_counts_once(repo, tmp_path, monkeypatch):
    """Data lives in its own repository and is symlinked into the log dir, so
    two candidate paths pointing through a link at one file is the normal
    case, not a conflict."""
    monkeypatch.chdir(repo.root)
    real = touch(tmp_path / "trajectories" / "builds.jsonl")
    (repo.root / "t/logs").mkdir(parents=True)
    (repo.root / "t/logs/builds.jsonl").symlink_to(real)
    (repo.root / "results/isabelle-logs").mkdir(parents=True)
    (repo.root / "results/isabelle-logs/builds.jsonl").symlink_to(real)
    assert corpus.resolve().resolve() == real.resolve()


def test_two_genuinely_different_corpora_refuse_to_choose(repo, monkeypatch):
    """Picking by priority would quietly answer about whichever this tool
    happened to rank first, and both halves of a split corpus is a real
    possibility."""
    monkeypatch.chdir(repo.root)
    touch(repo.root / "t/logs/builds.jsonl")
    touch(repo.root / "results/isabelle-logs/builds.jsonl")
    with pytest.raises(corpus.CorpusError, match="several corpora"):
        corpus.resolve()


# ------------------------------------------------------------------ the repo

def test_resolve_repo_defaults_to_the_project_standing_in(repo, monkeypatch):
    monkeypatch.chdir(repo.root / "thy")
    assert corpus.resolve_repo().resolve() == repo.root.resolve()


def test_a_directory_that_is_not_a_repository_is_rejected(tmp_path):
    """The guard that `--repo` used to pass by accident: the *tool's* own
    directory is a git repository, so defaulting to it left `check` reporting
    every payload `unverified` instead of failing."""
    with pytest.raises(corpus.CorpusError, match="not a git repository"):
        corpus.resolve_repo(tmp_path)


# ------------------------------------------------------------------- loading

def test_blank_lines_do_not_become_records(tmp_path):
    p = tmp_path / "c.jsonl"
    p.write_text('{"outcome": "ok"}\n\n{"outcome": "fail"}\n\n')
    assert [r["outcome"] for r in corpus.load(p)] == ["ok", "fail"]


# ------------------------------------------------------------------ episodes

def eps(outcomes, heads=None):
    recs = [make_record(outcome=o, git_head=(heads or ["h"] * len(outcomes))[i])
            for i, o in enumerate(outcomes)]
    return [[r["outcome"] for r in ep] for ep in corpus.episodes(recs)]


def test_an_episode_ends_at_a_success():
    assert eps(["fail", "fail", "ok"]) == [["fail", "fail", "ok"]]


def test_consecutive_successes_are_separate_episodes():
    assert eps(["ok", "ok"]) == [["ok"], ["ok"]]


def test_a_trailing_run_with_no_success_is_still_returned():
    """An open episode is real work in progress; dropping it would make the
    most recent -- and usually hardest -- trajectory invisible."""
    assert eps(["ok", "fail", "fail"]) == [["ok"], ["fail", "fail"]]


def test_a_mid_flight_commit_is_not_a_boundary():
    """Boundaries are successes, not commits.

    Committing a failing state as a rewind point is a normal move during a
    hard proof; treating it as a boundary would cut exactly the long
    trajectories the corpus exists to measure.
    """
    assert eps(["fail", "fail", "ok"], heads=["h1", "h2", "h2"]) \
        == [["fail", "fail", "ok"]]


def test_no_records_no_episodes():
    assert corpus.episodes([]) == []


# --------------------------------------------------------------- interleaving

def rec_ids(ids):
    return [make_record(instance_id=i) for i in ids]


def test_sequential_handoff_between_worktrees_is_not_interleaving():
    """n instances used one after another cost exactly n-1 switches.

    Worktrees are used sequentially here, and the pooled log shows the same
    session failing at the same line either side of the handoff -- one
    repair, two working copies.  Splitting by instance would cut it in half.
    """
    assert corpus.interleaving(rec_ids(["a", "a", "b", "b", "c"])) == (3, 0)


def test_genuine_concurrency_is_reported():
    """Two instances whose records alternate cannot be segmented
    chronologically, and the views warn rather than pretending."""
    instances, excess = corpus.interleaving(rec_ids(["a", "b", "a", "b"]))
    assert instances == 2 and excess > 0
