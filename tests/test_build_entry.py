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
from types import SimpleNamespace

import pytest

from conftest import PACKAGE_ENV

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


# ------------------------------------------------------------------- session

def test_no_session_anywhere_is_a_clear_message_now(build_module, repo, capsys):
    """There is no defensible default -- it was "SPSlowdown" because the
    script was written inside 43sp.  A wrong session is a confusing Isabelle
    error several seconds later; a missing one should be a clear message
    immediately."""
    build = build_module()
    assert main_with(build, [], repo) == 2
    err = capsys.readouterr().err
    assert "--session" in err and "BUILD_SESSION" in err


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
