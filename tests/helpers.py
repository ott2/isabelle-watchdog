"""Builders shared by the tests, importable rather than injected.

Fixtures are for *state* that needs setting up and tearing down.  These are
pure constructors -- a record, a diff, a repository wrapper -- and making them
fixtures would only mean threading them through signatures that do not
otherwise need arguments.  `tests/` is on `sys.path` while pytest runs, so
`from helpers import make_record` works from any test module and from
`conftest.py`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class Repo:
    """A git repository to record against.

    Identity and branch name are pinned rather than inherited: a record
    carries `branch` and `contributor`, so a test asserting on them would
    otherwise pass or fail according to the developer's global git config.
    """

    def __init__(self, root: Path, init: bool = True):
        self.root = root
        if not init:
            return                       # adopting a repository copied from a template
        root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "probe@example.invalid")
        self.git("config", "user.name", "probe")

    def git(self, *args: str) -> str:
        p = subprocess.run(["git", "-C", str(self.root), *args],
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise AssertionError(f"git {' '.join(args)}: {p.stderr.strip()}")
        return p.stdout.strip()

    def write(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def commit(self, msg: str = "wip", add: str = "-A") -> str:
        self.git("add", add)
        self.git("commit", "-q", "-m", msg)
        return self.head()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")


def make_record(**kw) -> dict:
    """A record with every field a reader dereferences.

    The defaults are the *minimum* shape rather than the current one, because
    readers must tolerate a corpus written by an older version; a test wanting
    a richer field supplies it.
    """
    rec = {
        "build_id": "20260101-000000-000",
        "instance_id": "inst",
        "timestamp": "2026-01-01T00:00:00",
        "outcome": "ok",
        "exit_code": 0,
        "timeout_reason": None,
        "elapsed_s": 1.0,
        "command": [],
        "diff": "",
        "error_head": None,
        "error_loci": None,
        "limits": None,
        "other_changed": None,
        "note": None,
        "note_fields": None,
        "note_predicted": None,
        "git_head": "0" * 40,
        "tree": None,
    }
    rec.update(kw)
    return rec


def diff_of(path: str, added: list[str], context: str = "theory X") -> str:
    """A unified diff adding lines to `path`, shaped as git writes them."""
    body = "".join(f"+{l}\n" for l in added)
    return (f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,1 +1,{1 + len(added)} @@\n {context}\n{body}")


def rename_of(old: str, new: str) -> str:
    """A rename as `git diff -M` writes it."""
    return (f"diff --git a/{old} b/{new}\nsimilarity index 100%\n"
            f"rename from {old}\nrename to {new}\n")
