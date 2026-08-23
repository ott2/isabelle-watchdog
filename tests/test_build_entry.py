"""`isabelle-build`: the one call that carries the note and runs the build.

The shape is `git commit -m`, and the reason it is one call rather than
"write a note, then build" is that two steps have state between them.  Its
failure modes are a build with no note -- silent, and the whole point lost --
or a pending note attaching to some later attempt, which is worse, because
misattributed reasoning is indistinguishable from the real thing.

So the tests here are mostly about argument and environment plumbing, and
that is not incidental: every one of them is a way for the note and the
attempt it explains to come apart.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import PACKAGE_ENV
from isabelle_watchdog import build as build_mod

REAL_RUN = subprocess.run


@pytest.fixture
def build_module(monkeypatch):
    """Load `build` with a chosen environment.

    `DEFAULT_SESSION` is read at import time, which is right for a
    command-line tool -- the process is always fresh -- and does mean a test
    of the default has to reload rather than just set the variable.
    """
    import isabelle_watchdog.build as build

    def load(**env):
        for var in PACKAGE_ENV:
            monkeypatch.delenv(var, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, str(v))
        return importlib.reload(build)

    yield load
    for var in PACKAGE_ENV:
        os.environ.pop(var, None)
    importlib.reload(build)


@pytest.fixture
def launched(monkeypatch):
    """Capture what would have been launched, without launching it.

    git still runs for real: `project_dir()` asks it where the project is,
    and faking that would test the fake.
    """
    calls = []

    def fake(cmd, **kw):
        if cmd and "git" in str(cmd[0]):
            return REAL_RUN(cmd, **kw)
        calls.append(SimpleNamespace(cmd=list(cmd), env=kw.get("env") or {},
                                     cwd=kw.get("cwd")))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake)
    return calls


def main_with(build, argv, repo):
    monkey = ["isabelle-build", *argv]
    old = sys.argv
    sys.argv = monkey
    cwd = os.getcwd()
    try:
        os.chdir(repo.root)
        return build.main()
    finally:
        sys.argv = old
        os.chdir(cwd)


# --------------------------------------------------------- reading a ROOT file

@pytest.mark.parametrize("line, want", [
    ("session Probe = HOL +", "Probe"),
    ("session SPSlowdown = HOL +", "SPSlowdown"),
    ('session "Probe (AFP)" = HOL +', "Probe (AFP)"),
    ("session Probe in sub = HOL +", "Probe"),
    ("  session Indented = HOL +", "Indented"),
    ("session With_Under.Dots-2 = HOL +", "With_Under.Dots-2"),
    ("chapter Foo", None),
    ("  theories", None),
    ("# not a session", None),
])
def test_a_session_declaration_is_read_the_way_isabelle_writes_it(tmp_path, line,
                                                                  want):
    """Isabelle's own declaration is the only authority on what a session is
    called -- the same shape `attempts.py` reads out of captured ROOT diffs,
    because a disagreement would attribute a build to a session it never
    claimed to build."""
    root = tmp_path / "ROOT"
    root.write_text(line + "\n")
    assert build_mod.sessions_in(root) == ([want] if want else [])


def test_several_sessions_in_one_root_are_all_seen(tmp_path):
    root = tmp_path / "ROOT"
    root.write_text("session A = HOL +\n  theories\n    X\n\nsession B = A +\n")
    assert build_mod.sessions_in(root) == ["A", "B"]


def test_a_root_that_cannot_be_read_yields_nothing(tmp_path):
    assert build_mod.sessions_in(tmp_path / "absent") == []


def test_roots_come_from_git_not_a_filesystem_walk(repo):
    """`.git`, ignored build trees, virtualenvs and vendored AFP checkouts are
    exactly where a stray ROOT lives.  A walk would need a pruning list, and a
    pruning list is a guess that goes stale."""
    (repo.root / "venv/share").mkdir(parents=True)
    (repo.root / "venv/share/ROOT").write_text("session Vendored = HOL +\n")
    (repo.root / ".gitignore").write_text("logs/\nvenv/\n")
    assert build_mod.root_files(repo.root) == [Path("thy/ROOT")]


def test_an_uncommitted_root_still_counts(repo):
    """The state a new project is in the first time it runs this."""
    (repo.root / "extra").mkdir()
    (repo.root / "extra/ROOT").write_text("session Fresh = HOL +\n")
    assert Path("extra/ROOT") in build_mod.root_files(repo.root)


def test_a_file_merely_ending_in_root_is_not_a_root(repo):
    """`*` crosses directory separators in a git pathspec, so the pathspec
    alone also matches `MY_ROOT`; the basename is the actual test."""
    (repo.root / "MY_ROOT").write_text("session Nope = HOL +\n")
    (repo.root / "thy/ROOTS").write_text("more\n")
    assert build_mod.root_files(repo.root) == [Path("thy/ROOT")]


# ------------------------------------------------------- deriving the session

def test_one_root_declaring_one_session_needs_no_configuration(repo):
    """43sp exactly: `isabelle/ROOT` declares `SPSlowdown` and nothing else,
    so `$BUILD_SESSION` was carrying information the repository already
    stated.  This is the rung that makes a per-project wrapper deletable."""
    assert build_mod.resolve_session(repo.root, None, None) == ("Probe", "thy")


def test_several_sessions_refuse_rather_than_guess(repo):
    """ndtht has ten ROOTs declaring thirteen sessions.  Building the wrong
    one is a confusing Isabelle failure minutes later -- and recording it puts
    a build of the wrong thing into the corpus, which is worse."""
    (repo.root / "other").mkdir()
    (repo.root / "other/ROOT").write_text("session Second = HOL +\n")
    with pytest.raises(ValueError, match="several sessions") as e:
        build_mod.resolve_session(repo.root, None, None)
    # Actionable: name them, and say the three ways to choose.
    assert "Probe" in str(e.value) and "Second" in str(e.value)
    assert ".isabelle-watchdog" in str(e.value) and "BUILD_SESSION" in str(e.value)


def test_two_sessions_in_one_root_is_equally_ambiguous(repo):
    """The ambiguity is in the *sessions*, not the ROOT count."""
    repo.write("thy/ROOT", "session A = HOL +\n\nsession B = HOL +\n")
    with pytest.raises(ValueError, match="several sessions"):
        build_mod.resolve_session(repo.root, None, None)


def test_no_root_at_all_says_so(repo):
    """A different failure from ambiguity, and it deserves a different
    message: nothing to choose between, rather than too much."""
    (repo.root / "thy/ROOT").unlink()
    with pytest.raises(ValueError, match="no ROOT under this project"):
        build_mod.resolve_session(repo.root, None, None)


def test_the_directory_is_wherever_the_root_was_found(repo):
    """Strictly better than the `.` this used to default to, and it means a
    project only ever has to state the session."""
    assert build_mod.resolve_session(repo.root, "Probe", None) == ("Probe", "thy")


def test_a_named_session_no_root_declares_falls_back_to_the_old_default(repo):
    """Reproduces exactly what this command did before any of this existed, so
    a project whose ROOT lives somewhere git cannot see is no worse off."""
    assert build_mod.resolve_session(repo.root, "Elsewhere", None) \
        == ("Elsewhere", ".")


def test_a_session_declared_by_two_roots_refuses(repo):
    (repo.root / "copy").mkdir()
    (repo.root / "copy/ROOT").write_text("session Probe = HOL +\n")
    with pytest.raises(ValueError, match="more than one ROOT"):
        build_mod.resolve_session(repo.root, "Probe", None)


# ------------------------------------------------ the ladder, rung by rung

def test_the_marker_beats_derivation(repo):
    """A project that cannot be derived states it once, in a committed file,
    instead of every caller exporting a variable."""
    (repo.root / "other").mkdir()
    (repo.root / "other/ROOT").write_text("session Second = HOL +\n")
    (repo.root / ".isabelle-watchdog").write_text("t/logs\nsession: Second\n")
    assert build_mod.resolve_session(repo.root, None, None) == ("Second", "other")


def test_the_marker_can_state_the_directory_too(repo):
    """For a layout where the ROOT is not where `-d` should point."""
    (repo.root / ".isabelle-watchdog").write_text("session: Odd\ndir: elsewhere\n")
    assert build_mod.resolve_session(repo.root, None, None) == ("Odd", "elsewhere")


def test_the_environment_beats_the_marker(repo):
    (repo.root / ".isabelle-watchdog").write_text("session: FromMarker\n")
    assert build_mod.resolve_session(repo.root, "FromEnv", None)[0] == "FromEnv"


def test_an_explicit_directory_beats_the_root_it_was_found_in(repo):
    assert build_mod.resolve_session(repo.root, "Probe", "custom") \
        == ("Probe", "custom")


# ------------------------------------------------------------------- session

def test_no_configuration_at_all_now_builds_the_obvious_session(build_module,
                                                                repo, launched):
    """There is still no defensible *constant* default -- it was "SPSlowdown"
    because the script was written inside 43sp -- but there is a defensible
    derivation, and a project with one ROOT declaring one session has already
    said which.  That is what makes a per-project wrapper deletable."""
    build = build_module()
    assert main_with(build, [], repo) == 0
    assert launched[0].cmd[-1] == "Probe"
    assert launched[0].cmd[-4:-2] == ["-d", "thy"]     # and where it lives


def test_an_ambiguous_project_is_a_clear_message_now(build_module, repo,
                                                     launched, capsys):
    """A wrong session is a confusing Isabelle error several seconds later,
    and an attempt recorded against a build of the wrong thing.  Both beat
    being told immediately, and neither is what should happen."""
    (repo.root / "other").mkdir()
    (repo.root / "other/ROOT").write_text("session Second = HOL +\n")
    build = build_module()
    assert main_with(build, [], repo) == 2
    assert not launched
    err = capsys.readouterr().err
    assert "--session" in err and "BUILD_SESSION" in err
    assert "Probe" in err and "Second" in err          # ...and what to choose from


def test_the_session_comes_from_the_environment_by_default(build_module, repo,
                                                            launched):
    build = build_module(BUILD_SESSION="MySession")
    assert main_with(build, [], repo) == 0
    assert launched[0].cmd[-1] == "MySession"


def test_an_explicit_session_wins(build_module, repo, launched):
    build = build_module(BUILD_SESSION="FromEnv")
    main_with(build, ["--session", "FromFlag"], repo)
    assert launched[0].cmd[-1] == "FromFlag"


# --------------------------------------------------------------- what is run

def test_the_watchdog_is_reached_as_a_module_not_a_path(build_module, repo,
                                                        launched):
    """So the subprocess uses the same installed copy as this process rather
    than whatever happens to sit next to `__file__`.

    The subprocess boundary itself stays: the watchdog installs signal
    handlers and reaps a process tree, which is not something to run inside a
    caller's interpreter.
    """
    build = build_module(BUILD_SESSION="S")
    main_with(build, [], repo)
    assert launched[0].cmd[:3] == [sys.executable, "-m",
                                   "isabelle_watchdog.watchdog"]
    assert launched[0].cmd[3:5] == ["isabelle", "build"]


def test_extra_arguments_pass_through_before_the_session(build_module, repo,
                                                          launched):
    build = build_module(BUILD_SESSION="S")
    main_with(build, ["--", "-o", "quick_and_dirty"], repo)
    cmd = launched[0].cmd
    assert cmd[-3:] == ["-o", "quick_and_dirty", "S"]


def test_the_session_directory_is_configurable(build_module, repo, launched):
    """`.` suits a project whose ROOT is at the top; 43sp used `isabelle/`
    and ndtht used `t/`."""
    build = build_module(BUILD_SESSION="S", BUILD_SESSION_DIR="thy")
    main_with(build, [], repo)
    assert "-d" in launched[0].cmd
    assert launched[0].cmd[launched[0].cmd.index("-d") + 1] == "thy"


def test_the_entry_point_owns_the_log_location(build_module, repo, launched):
    """It was a Makefile's to export, which meant running the command
    directly recorded into the recorder's built-in default -- a second
    corpus, a second instance id, and a build that looked unrecorded because
    its records were somewhere else."""
    build = build_module(BUILD_SESSION="S")
    main_with(build, [], repo)
    assert launched[0].env["WATCHDOG_LOG_DIR"].endswith("t/logs")


def test_owning_the_location_is_not_inventing_it(build_module, repo, launched):
    """The other half of the same bug.

    Owning the setting fixed the wrapper problem and left the value a guess,
    and in 43sp -- whose Makefile is exactly the wrapper above -- the guess
    was wrong: its corpus is in `results/isabelle-logs`, so a build run
    directly created a second one in `t/logs` rather than appending.
    """
    (repo.root / "results/isabelle-logs").mkdir(parents=True)
    (repo.root / "results/isabelle-logs/builds.jsonl").write_text("")
    build = build_module(BUILD_SESSION="S")
    main_with(build, [], repo)
    assert launched[0].env["WATCHDOG_LOG_DIR"].endswith("results/isabelle-logs")


def test_a_project_marker_decides_the_log_location(build_module, repo, launched):
    """The tier discovery cannot reach: a fresh clone has no corpus to find."""
    (repo.root / ".isabelle-watchdog").write_text("records\n")
    build = build_module(BUILD_SESSION="S")
    main_with(build, [], repo)
    assert launched[0].env["WATCHDOG_LOG_DIR"].endswith("/records")


def test_an_undecidable_location_stops_before_the_build(build_module, repo,
                                                        launched, capsys):
    """Refusing is not the failure the guard exists to swallow.

    A capture that breaks is instrumentation failing and the build must
    survive it; a project that cannot say which of two corpora to record into
    is a configuration error, decided before anything runs, with the fix in
    the message -- the same class as "no session to build".  Guessing would
    split an irreplaceable dataset in a way nothing downstream can detect.
    """
    for layout in ("t/logs", "results/isabelle-logs"):
        (repo.root / layout).mkdir(parents=True)
        (repo.root / layout / "builds.jsonl").write_text("")
    build = build_module(BUILD_SESSION="S")
    assert main_with(build, [], repo) == 2
    assert not launched
    assert "several corpora" in capsys.readouterr().err


def test_capture_can_be_turned_off_for_one_call(build_module, repo, launched):
    """Capture is on by default and stays that way; a project that wants only
    the supervision has to be able to say so, once or in a Makefile."""
    build = build_module(BUILD_SESSION="S")
    main_with(build, ["--no-record"], repo)
    assert launched[0].env["BUILD_RECORD"] == "0"


def test_no_flag_leaves_the_setting_alone(build_module, repo, launched):
    """The default lives in one place -- `guard.capture_enabled()` -- so the
    entry point must not restate it, or the two can drift apart."""
    build = build_module(BUILD_SESSION="S")
    main_with(build, [], repo)
    assert "BUILD_RECORD" not in launched[0].env


# --------------------------------------------------------------------- where

def test_where_reports_the_corpus_and_the_rule_that_chose_it(build_module, repo,
                                                             launched, capsys):
    """The question a project has when adopting this, and one the tools can
    answer about themselves -- the alternative is an operator deducing it from
    the source, which is how a wrong answer stays believed."""
    (repo.root / "results/isabelle-logs").mkdir(parents=True)
    (repo.root / "results/isabelle-logs/builds.jsonl").write_text("")
    build = build_module(BUILD_SESSION="S")
    assert main_with(build, ["--where"], repo) == 0
    out = capsys.readouterr().out
    assert "results/isabelle-logs/builds.jsonl" in out
    assert "existing corpus" in out
    assert not launched                              # reports, does not build


def test_where_names_the_marker_that_decided(build_module, repo, launched, capsys):
    (repo.root / ".isabelle-watchdog").write_text("records\n")
    build = build_module(BUILD_SESSION="S")
    main_with(build, ["--where"], repo)
    out = capsys.readouterr().out
    assert "declared by" in out and ".isabelle-watchdog" in out


def test_where_says_when_a_build_would_create_a_corpus(build_module, repo,
                                                       launched, capsys):
    """The state a new project is in, and the one moment the marker is worth
    mentioning -- afterwards the corpus exists and discovery finds it."""
    build = build_module(BUILD_SESSION="S")
    main_with(build, ["--where"], repo)
    out = capsys.readouterr().out
    assert "would create it" in out
    assert ".isabelle-watchdog" in out               # ...and what to do about it


def test_where_does_not_promise_records_it_will_not_write(build_module, repo,
                                                          launched, capsys):
    build = build_module(BUILD_SESSION="S")
    main_with(build, ["--where", "--no-record"], repo)
    assert "capture is off" in capsys.readouterr().out


def test_where_says_when_nothing_would_be_recorded_yet(build_module, tmp_path,
                                                       launched, capsys):
    """Resolution answers "where would records go"; this answers "would there
    be any".

    A repository with no commits records nothing -- a diff is anchored to a
    public commit and there is not one yet -- and `--where` is where a
    project asks what adopting this will do, *before* the build that would
    otherwise go uncaptured.
    """
    from helpers import Repo
    r = Repo(tmp_path / "fresh")            # init, deliberately no commit
    r.write("ROOT", "session S = HOL +\n  theories\n")
    build = build_module(BUILD_SESSION="S")
    main_with(build, ["--where"], r)
    out = capsys.readouterr().out
    assert "has no commits yet" in out, out
    assert "records:" in out, "and it still says where they would go"


def test_where_stays_quiet_about_capture_it_was_told_not_to_do(
        build_module, tmp_path, launched, capsys):
    """`--no-record` in a repository with no commits: nothing is missing,
    because nothing was going to be written."""
    from helpers import Repo
    r = Repo(tmp_path / "fresh")
    r.write("ROOT", "session S = HOL +\n  theories\n")
    build = build_module(BUILD_SESSION="S")
    main_with(build, ["--where", "--no-record"], r)
    out = capsys.readouterr().out
    assert "no commits yet" not in out
    assert "capture is off" in out


def test_where_needs_no_session(build_module, repo, launched, capsys):
    """It answers a question about the project, not about a build."""
    build = build_module()
    assert main_with(build, ["--where"], repo) == 0
    assert not launched


def test_the_marker_and_the_switch_are_both_in_the_help(build_module, repo):
    """A new project has to learn exactly two things, and `-h` is where it
    looks for them."""
    build = build_module()
    assert ".isabelle-watchdog" in build.EPILOG
    assert "BUILD_RECORD" in build.EPILOG


def test_help_shows_the_synopsis_not_the_rationale(build_module):
    """`-h` gets the first two paragraphs of the docstring.

    argparse prints `description` above the options, and the forty lines of
    design rationale below the synopsis are for someone reading the source --
    in `-h` they bury the two things a new project needs to find.
    """
    build = build_module()
    assert "isabelle-build --where" in build.SYNOPSIS
    assert "isabelle-build --no-record" in build.SYNOPSIS
    assert "Why a single command" not in build.SYNOPSIS


@pytest.mark.parametrize("flag", ["-V", "--version"])
def test_the_version_is_reported_without_building(build_module, repo, launched,
                                                  capsys, flag):
    """Both spellings, because someone reaching for one will not try the other
    before concluding the tool has no version.

    It also must not build: `-V` was an unrecognised argument, and on the
    watchdog the same typo supervised a program called `-V` and minted a
    corpus doing it.
    """
    build = build_module(BUILD_SESSION="S")
    with pytest.raises(SystemExit) as exc:
        main_with(build, [flag], repo)
    assert exc.value.code == 0
    assert "isabelle-watchdog" in capsys.readouterr().out
    assert not launched, "reporting a version started a build"


def test_the_build_runs_from_the_project_not_from_here(build_module, repo,
                                                        launched):
    build = build_module(BUILD_SESSION="S")
    main_with(build, [], repo)
    assert str(launched[0].cwd).endswith(repo.root.name)


# ---------------------------------------------------------------- the note

def test_a_note_reaches_the_build_as_an_environment_variable(build_module, repo,
                                                              launched):
    build = build_module(BUILD_SESSION="S")
    main_with(build, ["-m", "diagnosis: X; change: Y; expect: ok"], repo)
    assert launched[0].env["BUILD_NOTE"].startswith("diagnosis: X")


def test_a_note_can_come_from_a_file(build_module, repo, launched, tmp_path):
    """Kept for genuinely long notes, where shell quoting stops being
    pleasant."""
    note = tmp_path / "note.md"
    note.write_text("change: a long story\nexpect: fail\n")
    build = build_module(BUILD_SESSION="S")
    main_with(build, ["--note-file", str(note)], repo)
    assert "a long story" in launched[0].env["BUILD_NOTE"]


def test_no_note_is_not_an_error(build_module, repo, launched):
    """Notes are optional in every form; the default is no note, recorded as
    null."""
    build = build_module(BUILD_SESSION="S")
    assert main_with(build, [], repo) == 0
    assert "BUILD_NOTE" not in launched[0].env


def test_a_complaint_is_printed_but_the_build_still_runs(build_module, repo,
                                                          launched, capsys):
    """The restraint is the whole design.  A linter that can refuse a build
    teaches the operator to route around the recorded entry point and run the
    prover directly -- trading the irreplaceable half of the record for
    tidier prose in the half that is optional anyway."""
    build = build_module(BUILD_SESSION="S")
    assert main_with(build, ["-m", "just a thought"], repo) == 0
    assert launched, "a badly-formatted note must not stop the build"
    assert "note:" in capsys.readouterr().err


# --------------------------------------------------------------------- lint

def test_lint_checks_without_building(build_module, repo, launched, capsys):
    build = build_module()
    assert main_with(build, ["--lint", "-m",
                             "diagnosis: X; change: Y; expect: ok"], repo) == 0
    assert capsys.readouterr().out.strip() == "note: ok"
    assert launched == [], "--lint must not run a build"


def test_lint_reports_a_bad_note_with_a_non_zero_status(build_module, repo,
                                                         capsys):
    build = build_module()
    assert main_with(build, ["--lint", "-m", "change: x"], repo) == 1
    assert "expect" in capsys.readouterr().err


def test_lint_needs_something_to_lint(build_module, repo, capsys):
    build = build_module()
    assert main_with(build, ["--lint"], repo) == 2
    assert "no note to lint" in capsys.readouterr().err


def test_lint_does_not_require_a_session(build_module, repo):
    """Checking a note is not building, so the session check must not fire
    first -- otherwise the cheapest command in the tool needs the most
    configuration."""
    build = build_module()
    assert main_with(build, ["--lint", "-m", "expect: ok"], repo) == 0


# ------------------------------------------------- the heap this build leaves

def test_a_session_something_descends_from_is_worth_warning_about(repo):
    """Isabelle stores a heap only for a session something *in the same run*
    descends from (build_process.scala:95).  These builds name one session,
    so it is the leaf of its own plan and stores nothing -- and the next build
    of a descendant re-elaborates it from source (store.scala:559).

    Invisible until it happens, and when it happens it reads as a timeout in
    somebody else's theory: #4's misdiagnosis reached by a second route, which
    better *reporting* cannot prevent."""
    (repo.root / "upper").mkdir()
    (repo.root / "upper/ROOT").write_text("session Upper = Probe +\n")
    note = build_mod.heap_note(repo.root, "Probe", [])
    assert "stores no heap for Probe" in note
    assert "Upper descends from it" in note
    assert "-- -b" in note


def test_a_leaf_nothing_descends_from_says_nothing(repo):
    """The common case.  A note that fires on every build is one nobody
    reads by the time it matters."""
    assert build_mod.heap_note(repo.root, "Probe", []) == ""


def test_passing_the_flag_silences_it(repo):
    """The note has to be answerable, or it is a nag.  Once `-b` is there the
    premise is simply false -- not suppressed, false."""
    (repo.root / "upper").mkdir()
    (repo.root / "upper/ROOT").write_text("session Upper = Probe +\n")
    assert build_mod.heap_note(repo.root, "Probe", ["-b"]) == ""
    assert build_mod.heap_note(repo.root, "Probe", ["-bv"]) == ""


def test_building_the_descendant_too_needs_no_flag(repo):
    """`store_heap` quantifies over this run's graph, so an ancestor built
    alongside its descendant stores a heap without being asked.  Reading the
    ROOTs alone -- without the extra positionals -- would warn about a cost
    that is not being paid."""
    (repo.root / "upper").mkdir()
    (repo.root / "upper/ROOT").write_text("session Upper = Probe +\n")
    assert build_mod.heap_note(repo.root, "Probe", ["Upper"]) == ""


def test_every_descendant_is_named_rather_than_counted(repo):
    """Which sessions pay is what makes the note actionable -- `-b` is worth
    it only if you build them.  A count would be shorter and useless."""
    (repo.root / "a").mkdir()
    (repo.root / "b").mkdir()
    (repo.root / "a/ROOT").write_text("session Alpha = Probe +\n")
    (repo.root / "b/ROOT").write_text("session Beta = Probe +\n")
    note = build_mod.heap_note(repo.root, "Probe", [])
    assert "Alpha, Beta descend from it" in note


def test_a_grandchild_is_not_this_builds_problem(repo):
    """`store_heap` is about the direct parent edge: building Probe leaves
    Upper cold, and Upper's own build is where Top gets its answer.  Naming
    Top here would attach a cost to the wrong build."""
    (repo.root / "upper").mkdir()
    (repo.root / "top").mkdir()
    (repo.root / "upper/ROOT").write_text("session Upper = Probe +\n")
    (repo.root / "top/ROOT").write_text("session Top = Upper +\n")
    note = build_mod.heap_note(repo.root, "Probe", [])
    assert "Upper descends" in note and "Top" not in note
