"""Shared fixtures.

Three things every test here needs and none of them should build itself:

  `repo`        a scratch git repository, because almost nothing in this
                package means anything without one -- the recorder diffs
                against HEAD, the readers regenerate payloads from tree
                objects, and both resolve their paths from where the operator
                is standing.
  `watchdog`    a way to run the supervisor against a *fake* child.  The
                supervision logic does not care that Isabelle is on the other
                end of the pipe; it cares what comes out of it.  A shell
                script printing the right lines exercises every kill condition
                in seconds, and `test_isabelle_integration.py` then answers the
                one question a fake cannot: whether Isabelle still prints what
                we assume.
  `clean_env`   autouse.  This package's entire public API is environment
                variables, so a developer with `WATCHDOG_LOG_DIR` exported --
                which is everyone using it on a real project -- would
                otherwise run a different suite from CI.

Pure builders (`make_record`, `diff_of`, `Repo`) live in `helpers.py` and are
imported, not injected.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import isabelle_watchdog
from helpers import Repo

# Every variable the package reads.  Cleared for each test; a test that wants
# one sets it explicitly.  Keep in step with the table in CLAUDE.md.
PACKAGE_ENV = (
    "WATCHDOG_TIMEOUT", "WALL_TIMEOUT", "BATTERY_FACTOR",
    "LOOP_PROGRESS_THRESHOLD", "BUILD_PROGRESS_THRESHOLD", "LOG_NAME",
    "WATCHDOG_LOG_DIR", "BUILD_SOURCE_PATHSPECS", "BUILD_SESSION",
    "BUILD_SESSION_DIR", "BUILD_NOTE", "BUILD_NOTE_FILE", "BUILD_RECORD",
    "TRAJECTORY_CORPUS", "TRAJECTORY_ATTRIBUTION",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No ambient configuration reaches a test."""
    for var in PACKAGE_ENV:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def unfitted_attribution():
    """Attribution is a module global, fitted once per corpus.

    Cleared around every test so one test's fit cannot label another's
    records -- and so a view that needs attribution but did not ask for it
    fails loudly here exactly as it would in the field.
    """
    from isabelle_watchdog import attempts
    attempts._FITTED = None
    yield
    attempts._FITTED = None


# --------------------------------------------------------------- scratch repo

@pytest.fixture(scope="session")
def _repo_template(tmp_path_factory) -> Path:
    """Built once, copied per test.

    Not premature: `git` costs about half a second per invocation on a Mac
    with a security agent in the way, almost all of it process-spawn latency
    rather than work, so six calls to stand up a repository was three and a
    half seconds *per test* and dwarfed everything the tests actually did.  A
    freshly-initialised repository is relocatable, so copying the tree is
    exact -- and a copy is roughly a thousand times cheaper.
    """
    root = tmp_path_factory.mktemp("template") / "proj"
    r = Repo(root)
    r.write("thy/ROOT", "session Probe = HOL +\n  theories\n    Probe_A\n")
    r.write("thy/Probe_A.thy", "theory Probe_A\n  imports Main\nbegin\n\nend\n")
    # Tracked, and deliberately *not* source: the file whose edit explains an
    # outcome flip that the source diff cannot.
    r.write("Makefile", "build:\n\tWALL_TIMEOUT=40 isabelle-build\n")
    r.write(".gitignore", "logs/\n")
    r.commit("initial")
    return root


@pytest.fixture
def repo(_repo_template: Path, tmp_path: Path) -> Repo:
    """A repository with one theory, committed -- the state a real project is
    in on its first recorded build."""
    dest = tmp_path / "proj"
    shutil.copytree(_repo_template, dest)
    return Repo(dest, init=False)


@pytest.fixture
def logs(repo: Repo) -> Path:
    """The log directory, gitignored by the `repo` fixture as it is in a real
    project -- so a test that captures never captures its own output."""
    d = repo.root / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ----------------------------------------------------------- running the tool

def package_env(**extra: object) -> dict:
    """An environment that reaches this package, whatever put it on the path.

    An editable install resolves `-m isabelle_watchdog.watchdog` by itself,
    but a plain source checkout does not, and a subprocess that silently
    imported a *different* copy would be the least useful kind of green.
    """
    env = dict(os.environ)
    for var in PACKAGE_ENV:
        env.pop(var, None)
    src = str(Path(isabelle_watchdog.__file__).resolve().parent.parent)
    env["PYTHONPATH"] = f"{src}{os.pathsep}{env.get('PYTHONPATH', '')}"
    # Pinned so a laptop running on battery does not silently double every
    # budget and turn a 3-second timeout test into a 6-second one.  The
    # scaling itself is tested deliberately, with a stub `pmset`.
    env.setdefault("BATTERY_FACTOR", "1.0")
    # None *unsets*, so a test can ask for the resolution a real operator gets
    # rather than the one the fixtures find convenient.  Most of the contract
    # is only interesting when it is unset.
    env.update({k: str(v) for k, v in extra.items() if v is not None})
    for k, v in extra.items():
        if v is None:
            env.pop(k, None)
    return env


class Run:
    """One supervised attempt: what the operator saw, and what was recorded."""

    def __init__(self, code: int, out: str, logs: Path):
        self.code = code
        self.out = out
        self.logs = logs

    @property
    def records(self) -> list[dict]:
        f = self.logs / "builds.jsonl"
        if not f.exists():
            return []
        return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]

    @property
    def record(self) -> dict:
        recs = self.records
        assert recs, f"no attempt was recorded.\n--- output ---\n{self.out}"
        return recs[-1]

    def log_text(self, name: str = "last-build.log") -> str:
        return (self.logs / name).read_text()

    def __repr__(self) -> str:                       # for assertion failures
        return f"<Run exit={self.code}>\n{self.out}"


@pytest.fixture
def watchdog(repo: Repo, logs: Path):
    """Supervise a command in the scratch repo and return the attempt.

    The command is whatever the caller passes -- usually `sh -c` printing
    canned Isabelle output.  Every judgement the watchdog makes (the three
    kill conditions, the error head, the loci) is made from that text, so a
    fake child exercises the real code path.
    """
    def run(*cmd: str, cwd: Path | None = None, timeout: float = 90,
            **env_extra) -> Run:
        # The log directory is pointed at the fixture's by default, but a
        # caller may pass WATCHDOG_LOG_DIR=None to unset it and get the
        # resolution a real operator gets.
        env_extra.setdefault("WATCHDOG_LOG_DIR", str(logs))
        p = subprocess.run(
            [sys.executable, "-m", "isabelle_watchdog.watchdog", *cmd],
            cwd=str(cwd or repo.root),
            env=package_env(**env_extra),
            capture_output=True, text=True, timeout=timeout)
        return Run(p.returncode, (p.stdout + p.stderr).strip(), logs)
    return run


@pytest.fixture
def stub_bin(tmp_path: Path):
    """Install a stub executable and return the directory holding it.

    The only way to exercise the battery branch: `on_battery()` shells out to
    `pmset`, and no environment variable reaches it -- which is the right
    design (the power state is a fact about the machine, not a setting) and
    does mean the fake has to be a real file on PATH.
    """
    d = tmp_path / "stub-bin"
    d.mkdir(exist_ok=True)

    def install(name: str, script: str) -> Path:
        p = d / name
        p.write_text(f"#!/bin/sh\n{script}\n")
        p.chmod(0o755)
        return p

    install.dir = d
    return install


# ------------------------------------------------------- a recorded trajectory

CAPTURE = """
import json, sys
from isabelle_watchdog import record
record.record(**json.loads(sys.argv[1]))
"""


def capture(repo_root: Path, logs: Path, *, note: str | None = None,
            env: dict | None = None, **kw) -> None:
    """Record one attempt the way the watchdog does: a fresh process, run from
    the project directory.

    A subprocess rather than an in-process call, and not only for fidelity:
    `record` resolves the project and the log directory at *import* time, so
    exercising it in-process means reloading the module and then living with
    whichever repository the last test left it pointing at.  Paying a process
    to get a clean answer is the better trade -- and it is what production
    does anyway.

    Goes through the public `record()`, guard and all, then insists the guard
    stayed quiet: a capture that silently swallowed its own failure would
    otherwise look exactly like a capture that had nothing to say.
    """
    kw.setdefault("argv", ["isabelle", "build", "-d", "thy", "Probe"])
    kw.setdefault("outcome", "ok")
    kw.setdefault("exit_code", 0)
    kw.setdefault("timeout_reason", "")
    kw.setdefault("elapsed_s", 1.0)
    kw.setdefault("error_head", "")
    kw.setdefault("log_name", "last-build.log")
    # `note` is not a parameter of `record()` -- the recorder reads it from
    # the environment or the pending file, which is the whole point of
    # `note_pre_build`.  Passing it here sets the variable the operator would.
    extra = dict(env or {})
    if note is not None:
        extra["BUILD_NOTE"] = note
    corpus = logs / "builds.jsonl"
    before = len(corpus.read_text().splitlines()) if corpus.exists() else 0
    p = subprocess.run([sys.executable, "-c", CAPTURE, json.dumps(kw)],
                       cwd=str(repo_root),
                       env=package_env(WATCHDOG_LOG_DIR=str(logs), **extra),
                       capture_output=True, text=True)
    # Both words the guard can print: "skipped" for a side task whose failure
    # costs nothing, "FAILED" for one that lost the attempt.  Checking only
    # the first would have let the whole of `_snapshot_tree` fail unnoticed.
    assert not ("skipped" in p.stderr or "FAILED" in p.stderr), \
        f"capture failed silently:\n{p.stderr}"
    after = len(corpus.read_text().splitlines()) if corpus.exists() else 0
    assert after == before + 1, f"no record appended:\n{p.stdout}\n{p.stderr}"


CODE_EDIT = """theory Probe_A
  imports Main
begin

text \\<open>Some prose about the development.\\<close>

lemma easy: "(n::nat) + 0 = n"
  by simp

end
"""

PROSE_EDIT = CODE_EDIT.replace("Some prose about the development.",
                               "Some rather longer prose about the development.")


@pytest.fixture(scope="session")
def trajectory(tmp_path_factory, _repo_template: Path):
    """Five real attempts, recorded once and shared read-only.

    Real captures, not synthetic records: the payloads have to be genuine
    `git diff` output against genuine tree objects, or the integrity readers
    -- whose whole claim is that regeneration is the ground truth -- would be
    checking a fiction.  One capture costs about twenty-five `git`
    invocations, so this is built once per session and consumers copy the
    corpus before mutating it.

    The five cover, in order: a tracked edit; an untracked theory alongside
    files the allowlist must exclude; a mid-run commit (re-baselining); a
    rebuild with nothing changed; and a prose-only edit.
    """
    base = tmp_path_factory.mktemp("recorded")
    root, log_dir = base / "proj", base / "proj" / "logs"
    shutil.copytree(_repo_template, root)
    log_dir.mkdir(parents=True, exist_ok=True)
    r = Repo(root, init=False)

    # 0 -- an edit to a tracked theory, which fails at a known line.
    r.write("thy/Probe_A.thy", CODE_EDIT)
    capture(root, log_dir, outcome="fail", exit_code=1,
            error_head='Failed to finish proof | At command "by"',
            error_loci=[["thy/Probe_A.thy", "8"]],
            limits={"activity_timeout": 20, "wall_timeout": 40})

    # 1 -- a NEW theory git has never seen, plus two files the allowlist must
    #      keep out of the payload but still report the existence of.
    r.write("thy/Probe_B.thy", "theory Probe_B\n  imports Main\nbegin\nend\n")
    r.write("scratch.py", "# throwaway\nprint(1)\n")
    r.write("NOTES.md", "# thinking out loud\n")
    r.write("Makefile", "build:\n\tWALL_TIMEOUT=90 isabelle-build\n")
    capture(root, log_dir, outcome="fail", exit_code=1,
            error_head="*** Undefined constant",
            note="diagnosis: Probe_B is not in ROOT; change: add it; expect: ok")

    # 2 -- a mid-run commit, then a further edit.  The payload must be the
    #      edit, not the commit.
    r.commit("land the new theory")
    r.write("thy/Probe_A.thy", CODE_EDIT.replace("by simp", "by auto"))
    capture(root, log_dir, outcome="ok", elapsed_s=12.5,
            note="change: auto instead of simp; expect: ok")

    # 3 -- a rebuild with nothing changed at all.
    capture(root, log_dir, outcome="ok", elapsed_s=2.0)

    # 4 -- prose only: green by construction, and not a proof attempt.
    r.write("thy/Probe_A.thy", PROSE_EDIT.replace("by simp", "by auto"))
    capture(root, log_dir, outcome="ok", elapsed_s=3.0)

    corpus_path = log_dir / "builds.jsonl"
    records = [json.loads(l) for l in corpus_path.read_text().splitlines()]
    assert len(records) == 5
    return SimpleNamespace(repo=r, root=root, logs=log_dir,
                           corpus=corpus_path, records=records)


@pytest.fixture
def corpus_file(tmp_path: Path):
    """Write a list of records to a corpus file and return its path."""
    def write(records: list[dict], name: str = "builds.jsonl") -> Path:
        p = tmp_path / name
        p.write_text("".join(json.dumps(r) + "\n" for r in records))
        return p
    return write
