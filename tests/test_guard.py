"""The never-break-a-build contract, and whether to capture at all.

Instrumentation is allowed to fail and the build's exit code is not allowed to
notice.  A bare `except Exception` is normally a smell; here it is the
specification, which is exactly why it needs a test -- the next person to read
it will be tempted to narrow it.
"""
from __future__ import annotations

import pytest

from isabelle_watchdog.guard import ATTEMPT_LOST, capture_enabled, run_guarded


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


def test_a_side_task_that_lost_something_says_what(capsys):
    """"skipped" is right for a side task whose failure costs nothing and was
    wrong for the one that costs an attempt.

    A downstream project read `build-record: skipped (CalledProcessError...)`
    beside a green build, took it for a note about something optional, and
    lost five attempts before working out what it had been signalling.  The
    consequence leads; the exception follows it as the cause.
    """
    def boom():
        raise RuntimeError("no tree")
    run_guarded("build-record", boom, lost=ATTEMPT_LOST)
    err = capsys.readouterr().err
    assert "build-record: FAILED" in err
    assert "NOT recorded" in err
    assert err.index("NOT recorded") < err.index("RuntimeError"), \
        "the consequence must come before the exception class"
    assert "RuntimeError: no tree" in err


def test_a_side_task_that_lost_nothing_still_just_says_skipped(capsys):
    """The distinction is the point.  If every guarded failure shouted, the
    shout would stop meaning "data is gone" -- `db-error` failing costs a
    nicer error message and nothing else."""
    run_guarded("db-error", lambda: (_ for _ in ()).throw(RuntimeError("z")))
    err = capsys.readouterr().err
    assert "db-error: skipped" in err
    assert "FAILED" not in err


def test_the_two_guards_around_capture_describe_it_the_same_way():
    """The recorder guards its own logic and the watchdog additionally guards
    the `import`; both lose the attempt, so both quote one constant."""
    import inspect
    from isabelle_watchdog import record, watchdog
    for mod in (record, watchdog):
        assert "ATTEMPT_LOST" in inspect.getsource(mod)


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit])
def test_an_interrupt_is_not_swallowed(exc):
    """`except Exception` deliberately does not catch these.

    A Ctrl-C during capture must still stop the build; swallowing it would
    make the tool feel wedged at precisely the moment the operator is trying
    to kill it.
    """
    with pytest.raises(exc):
        run_guarded("x", lambda: (_ for _ in ()).throw(exc()))


# ------------------------------------------------------------ capture on/off

def test_capture_is_on_by_default():
    """The capture is the reason the supervision was written, so a project
    that adopts the watchdog and never reads the docs still collects a corpus
    worth having."""
    assert capture_enabled() is True


@pytest.mark.parametrize("v", ["0", "no", "false", "off", "OFF", " off "])
def test_the_ways_to_say_off(monkeypatch, v):
    monkeypatch.setenv("BUILD_RECORD", v)
    assert capture_enabled() is False


@pytest.mark.parametrize("v", ["1", "yes", "true", "on", "True"])
def test_the_ways_to_say_on(monkeypatch, v):
    monkeypatch.setenv("BUILD_RECORD", v)
    assert capture_enabled() is True


@pytest.mark.parametrize("v", ["fasle", "", "2", "disabled"])
def test_an_unrecognised_value_is_an_error_not_a_default(monkeypatch, v):
    """The failure this avoids is one-sided.

    Read as *on*, a misspelt "off" quietly collects the data someone
    declined, and the first they hear of it is a corpus.  Erring loudly costs
    one confused build; erring quietly costs the trust in the setting.
    """
    monkeypatch.setenv("BUILD_RECORD", v)
    with pytest.raises(ValueError, match="neither on nor off"):
        capture_enabled()
