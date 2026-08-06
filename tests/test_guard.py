"""The never-break-a-build contract.

Six lines of module with one job: instrumentation is allowed to fail and the
build's exit code is not allowed to notice.  A bare `except Exception` is
normally a smell; here it is the specification, which is exactly why it needs
a test -- the next person to read it will be tempted to narrow it.
"""
from __future__ import annotations

import pytest

from isabelle_watchdog.guard import run_guarded


def test_a_working_thunk_returns_its_value():
    assert run_guarded("x", lambda: 42) == 42


def test_a_failing_thunk_returns_none():
    def boom():
        raise RuntimeError("no")
    assert run_guarded("build-record", boom) is None


def test_the_failure_is_announced_with_its_label(capsys):
    """Silence would make broken capture indistinguishable from a build that
    simply had nothing to record -- a corpus quietly missing every attempt
    looks the same as a quiet week."""
    def boom():
        raise ValueError("bad tree")
    run_guarded("build-record", boom)
    err = capsys.readouterr().err
    assert "build-record" in err
    assert "ValueError" in err and "bad tree" in err


def test_the_warning_goes_to_stderr_not_stdout(capsys):
    """stdout is the build's own summary, which callers parse and pipe to
    `head`; a capture warning must not displace the FAIL line."""
    run_guarded("x", lambda: (_ for _ in ()).throw(RuntimeError("z")))
    out = capsys.readouterr()
    assert out.out == ""
    assert "x: skipped" in out.err


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit])
def test_an_interrupt_is_not_swallowed(exc):
    """`except Exception` deliberately does not catch these.

    A Ctrl-C during capture must still stop the build; swallowing it would
    make the tool feel wedged at precisely the moment the operator is trying
    to kill it.
    """
    with pytest.raises(exc):
        run_guarded("x", lambda: (_ for _ in ()).throw(exc()))
