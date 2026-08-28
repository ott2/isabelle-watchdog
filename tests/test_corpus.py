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


# ------------------------------------------------------------- the marker file

def marker(root: Path, text: str) -> Path:
    p = root / corpus.MARKER_NAME
    p.write_text(text)
    return p


def test_a_marker_names_the_log_directory(repo, monkeypatch):
    monkeypatch.chdir(repo.root)
    p = touch(repo.root / "records/builds.jsonl")
    marker(repo.root, "records\n")
    assert corpus.resolve().resolve() == p.resolve()


def test_a_marker_may_comment_itself(repo, monkeypatch):
    """Same shape as `.isabelle-query`, deliberately: a project already
    carrying one marker should not learn a second convention."""
    monkeypatch.chdir(repo.root)
    p = touch(repo.root / "records/builds.jsonl")
    marker(repo.root, "# where this project keeps its build trajectories\n"
                      "\n"
                      "records\n")
    assert corpus.resolve().resolve() == p.resolve()


def test_a_marker_beats_discovery(repo, monkeypatch):
    """A statement outranks a guess, whoever made it."""
    monkeypatch.chdir(repo.root)
    declared = touch(repo.root / "records/builds.jsonl")
    touch(repo.root / "t/logs/builds.jsonl")
    marker(repo.root, "records\n")
    assert corpus.resolve().resolve() == declared.resolve()


def test_the_operators_variable_beats_the_projects_marker(repo, monkeypatch):
    """Both are declarations; the operator at the command line is the later
    word, so a pooled corpus stays readable from inside a project that
    declares its own."""
    monkeypatch.chdir(repo.root)
    pooled = touch(repo.root / "pooled/builds.jsonl")
    touch(repo.root / "records/builds.jsonl")
    marker(repo.root, "records\n")
    monkeypatch.setenv("WATCHDOG_LOG_DIR", str(repo.root / "pooled"))
    assert corpus.resolve().resolve() == pooled.resolve()


def test_a_marker_is_found_from_a_subdirectory(repo, monkeypatch):
    monkeypatch.chdir(repo.root / "thy")
    p = touch(repo.root / "records/builds.jsonl")
    marker(repo.root, "records\n")
    assert corpus.resolve().resolve() == p.resolve()


def test_the_search_stops_at_the_project_root(tmp_path, monkeypatch):
    """Unlike `.isabelle-query`'s unbounded walk, and for a reason: projects
    are routinely nested, and overshooting here does not merely read the wrong
    thing -- it would pool two repositories' trajectories into one corpus.
    """
    from helpers import Repo
    outer = tmp_path / "projects"
    outer.mkdir()
    marker(outer, "shared-logs\n")
    inner = Repo(outer / "inner")
    monkeypatch.chdir(inner.root)
    p = touch(inner.root / "t/logs/builds.jsonl")
    assert corpus.find_marker() is None
    assert corpus.resolve().resolve() == p.resolve()


def test_a_marker_declaring_nothing_is_an_error(repo, monkeypatch):
    """Not a no-op.  Silently ignoring an empty declaration reproduces the bug
    the marker exists to prevent -- a project that believes it has said where
    its records go, and a writer that puts them somewhere else."""
    monkeypatch.chdir(repo.root)
    marker(repo.root, "# only a comment\n")
    with pytest.raises(corpus.CorpusError, match="declares nothing"):
        corpus.resolve()


# --------------------------------------------- a marker that says what to build

def test_a_marker_may_declare_only_a_session(repo, monkeypatch):
    """Having no opinion about where records go is not a parse failure.  A
    project whose layout is already discoverable still has something to say
    about which session to build."""
    monkeypatch.chdir(repo.root)
    marker(repo.root, "session: MySession\ndir: thy\n")
    fields = corpus.read_marker(repo.root / corpus.MARKER_NAME)
    assert fields == {"log_dir": None, "session": "MySession", "dir": "thy"}
    # ...and resolution carries on past it rather than stopping there.
    assert corpus.resolve_log_dir().resolve() \
        == (repo.root / corpus.DEFAULT_LAYOUT).resolve()


def test_the_bare_line_is_found_by_shape_not_by_position(repo):
    """Identified by *not* being a `key: value`, so the file can be written in
    whichever order reads best."""
    marker(repo.root, "session: S\nrecords\ndir: thy\n")
    fields = corpus.read_marker(repo.root / corpus.MARKER_NAME)
    assert fields["log_dir"] == repo.root / "records"
    assert fields["session"] == "S" and fields["dir"] == "thy"


def test_a_marker_written_before_the_keys_existed_still_reads(repo):
    """The format grew rather than changed."""
    marker(repo.root, "# where the trajectories go\nresults/isabelle-logs\n")
    fields = corpus.read_marker(repo.root / corpus.MARKER_NAME)
    assert fields["log_dir"] == repo.root / "results/isabelle-logs"
    assert fields["session"] is None and fields["dir"] is None


def test_an_unrecognised_key_is_an_error(repo):
    """`sessions:` for `session:` would otherwise be accepted and do nothing,
    which is the failure this whole file is about."""
    marker(repo.root, "t/logs\nsessions: S\n")
    with pytest.raises(corpus.CorpusError, match="unrecognised key"):
        corpus.read_marker(repo.root / corpus.MARKER_NAME)


def test_a_marker_may_name_an_absolute_path(repo, tmp_path, monkeypatch):
    monkeypatch.chdir(repo.root)
    p = touch(tmp_path / "elsewhere/builds.jsonl")
    marker(repo.root, f"{tmp_path / 'elsewhere'}\n")
    assert corpus.resolve().resolve() == p.resolve()


# ------------------------------------------------- where a writer puts records

def test_an_existing_corpus_is_appended_to_not_shadowed(repo, monkeypatch):
    """The 43sp incident.

    Its corpus is in `results/isabelle-logs/`, named by a Makefile variable.
    A build run outside make had no variable, and the writer went straight to
    its built-in `t/logs` default -- minting a second corpus, with its own
    instance id, in a project that already had one.  Nothing errors, and each
    half is internally consistent, so `trajectory check` calls both sound.
    """
    monkeypatch.chdir(repo.root)
    touch(repo.root / "results/isabelle-logs/builds.jsonl")
    assert corpus.resolve_log_dir().resolve() \
        == (repo.root / "results/isabelle-logs").resolve()


def test_a_project_with_no_corpus_gets_the_default(repo, monkeypatch):
    """A default is a last resort, not a first guess -- but it is still a
    default: every corpus began by being created somewhere."""
    monkeypatch.chdir(repo.root)
    assert corpus.resolve_log_dir().resolve() \
        == (repo.root / corpus.DEFAULT_LAYOUT).resolve()


def test_the_writer_honours_the_marker_before_it_looks(repo, monkeypatch):
    """The tier discovery cannot reach: a fresh clone has no corpus to find,
    so without the marker its first build mints one in the default place
    rather than the project's."""
    monkeypatch.chdir(repo.root)
    marker(repo.root, "records\n")
    assert corpus.resolve_log_dir().resolve() == (repo.root / "records").resolve()


def test_the_writer_refuses_to_choose_between_two_corpora(repo, monkeypatch):
    """Guessing would split an irreplaceable dataset in a way nothing
    downstream can detect."""
    monkeypatch.chdir(repo.root)
    touch(repo.root / "t/logs/builds.jsonl")
    touch(repo.root / "results/isabelle-logs/builds.jsonl")
    with pytest.raises(corpus.CorpusError, match="several corpora"):
        corpus.resolve_log_dir()


def test_nothing_is_being_written_so_nothing_is_ambiguous(repo, monkeypatch):
    """With capture off there is no dataset to protect, and refusing to start
    a build over an ambiguity that cannot affect anything would be the tail
    wagging the dog.  The directory is still wanted -- `last-build.log` goes
    there."""
    monkeypatch.chdir(repo.root)
    touch(repo.root / "t/logs/builds.jsonl")
    touch(repo.root / "results/isabelle-logs/builds.jsonl")
    assert corpus.resolve_log_dir(recording=False).resolve() \
        == (repo.root / corpus.DEFAULT_LAYOUT).resolve()


def test_the_reader_override_does_not_redirect_writes(repo, tmp_path, monkeypatch):
    """`$TRAJECTORY_CORPUS` is for reading someone else's dataset.  Honouring
    it here would mean looking at a pooled corpus silently redirects your next
    build's records into it."""
    monkeypatch.chdir(repo.root)
    pooled = touch(tmp_path / "pooled.jsonl")
    monkeypatch.setenv("TRAJECTORY_CORPUS", str(pooled))
    assert corpus.resolve_log_dir().resolve() \
        == (repo.root / corpus.DEFAULT_LAYOUT).resolve()
    assert corpus.resolve() == pooled


def arrange_nothing(root, mp):
    """A project that has never been built: the writer creates the default."""
    return "t/logs"


def arrange_existing_elsewhere(root, mp):
    """43sp: a corpus in the other known layout, and nothing to point at it."""
    touch(root / "results/isabelle-logs/builds.jsonl")
    return "results/isabelle-logs"


def arrange_marker(root, mp):
    """A fresh clone of a project that declares its layout."""
    marker(root, "records\n")
    return "records"


def arrange_variable(root, mp):
    mp.setenv("WATCHDOG_LOG_DIR", str(root / "chosen"))
    return "chosen"


@pytest.mark.parametrize("arrange", [arrange_nothing, arrange_existing_elsewhere,
                                     arrange_marker, arrange_variable])
def test_writer_and_reader_agree(repo, monkeypatch, arrange):
    """The invariant the whole module exists for, over every tier.

    Both halves of it have been wrong in production, one after the other: the
    reader ignored where the writer had been told to write, and then the
    writer ignored where the reader could plainly see the corpus was.
    """
    monkeypatch.chdir(repo.root)
    expected = arrange(repo.root, monkeypatch)
    written = corpus.resolve_log_dir()
    assert written.resolve() == (repo.root / expected).resolve()
    # A real build, from here: the writer creates the file, the reader finds
    # that same file with no further configuration.
    touch(written / corpus.BASENAME)
    assert corpus.resolve().resolve() == (written / corpus.BASENAME).resolve()


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


# ------------------------------------------------------------------ the writer

def test_the_version_filter_survives_a_double_digit_minor():
    """The reason this helper exists rather than `>=` on the strings.
    `"0.10.0" >= "0.6.0"` is False, so the obvious filter works until the
    minor number reaches two digits and then silently drops the *newest*
    records -- which is the half an era question is usually about.  It reports
    fewer records rather than raising, which is what makes it expensive."""
    assert not ("0.10.0" >= "0.6.0")             # the trap, stated

    assert corpus.writer_at_least({"writer_version": "0.10.0"}, "0.6.0")
    assert corpus.writer_at_least({"writer_version": "1.0.0"}, "0.6.0")
    assert not corpus.writer_at_least({"writer_version": "0.5.1"}, "0.6.0")


def test_a_release_equals_itself_however_it_is_spelled():
    """`0.6` and `0.6.0` are the same release, so neither may sort under the
    other by length alone."""
    assert corpus.writer_at_least({"writer_version": "0.6"}, "0.6.0")
    assert corpus.writer_at_least({"writer_version": "0.6.0"}, "0.6")


@pytest.mark.parametrize("rec", [
    {},                                    # written before the field existed
    {"writer_version": None},
    {"writer_version": "0+unknown"},       # an uninstalled source tree
])
def test_a_record_that_cannot_say_is_excluded_rather_than_assumed(rec):
    """All three mean *cannot confirm*, and the conservative direction is to
    exclude: a reader asking which records carry post-0.6.0 semantics must not
    be told yes by one that does not know.  Same rule as a null duty cycle
    reading `unknown` rather than `stalled`."""
    assert not corpus.writer_at_least(rec, "0.6.0")


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
