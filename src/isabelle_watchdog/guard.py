"""guard.py — run a best-effort side task that must never break its caller.

Trajectory capture is instrumentation.  It runs beside a build, it is allowed
to fail, and a failure in it must not change the build's exit code or lose the
build's output.  A bare `except Exception` is normally a smell; here it is the
contract, and this module exists so that contract has one definition and one
message format rather than a scattering of silent `try` blocks.

Two call sites, guarding deliberately different scopes:

  - `build_record.record()` wraps its own record-building logic.
  - `isabelle-watchdog.py` wraps the whole call *including* `import
    build_record`, which a guard living inside build_record cannot cover.

This was `isabelle_query.common.run_guarded`, imported from the sibling
`query` repository.  That was the whole of this tooling's dependency on it --
six lines -- and `query` had already marked the function deprecated and unused
there, kept alive only for these callers.  Two repositories each holding a
function for the other's sake is worse than a copy, and the copy costs
nothing: the watchdog and the recorder are now standalone.
"""

from __future__ import annotations

import os
import sys
from typing import Callable, TypeVar

_T = TypeVar("_T")

# Whether to capture at all.  On by default: the supervision and the capture
# ship together because the second is the reason the first was written, and a
# project that adopts the watchdog and never notices the corpus has still
# collected one worth having.  But supervision is useful on its own -- killing
# a looping tactic and naming its line needs no dataset -- so a project that
# only wants that must be able to say so, rather than accumulating records it
# will never read into a directory it did not ask for.
ENV_RECORD = "BUILD_RECORD"
_OFF = {"0", "no", "false", "off"}
_ON = {"1", "yes", "true", "on"}


def capture_enabled() -> bool:
    """Whether trajectory capture should run, from `$BUILD_RECORD`.

    An unrecognised value is an error rather than a default.  The failure this
    avoids is one-sided: read as *on*, a misspelt "off" quietly collects the
    data someone declined, and the first they hear of it is a corpus.  It is
    the same reasoning that makes an empty `.isabelle-watchdog` fatal -- a
    setting that silently does nothing is worse than one that is absent,
    because absence is at least visible in the file.
    """
    raw = os.environ.get(ENV_RECORD)
    if raw is None:
        return True
    v = raw.strip().lower()
    if v in _OFF:
        return False
    if v in _ON:
        return True
    raise ValueError(
        f"${ENV_RECORD}={raw!r} is neither on nor off "
        f"(on: {'/'.join(sorted(_ON))}; off: {'/'.join(sorted(_OFF))})")


# What was lost when capture fails, stated once so the two guards that wrap
# it -- the recorder's own and the watchdog's around the import -- cannot
# drift into describing the same event two ways.
ATTEMPT_LOST = (
    "this attempt was NOT recorded.\n"
    "  The build itself is unaffected.  The source changes will be picked up "
    "by the next\n"
    "  recorded build (diffs are cumulative), but this attempt's outcome, "
    "timing and\n"
    "  error loci are gone -- they cannot be reconstructed afterwards."
)


def run_guarded(label: str, thunk: Callable[[], _T], *,
                lost: str | None = None) -> "_T | None":
    """Run `thunk()`; on any exception warn on stderr and return None.

    The warning names `label` so an operator reading a build log can tell
    which side task was skipped, and that a side task was skipped at all --
    silence here would make broken capture indistinguishable from a build
    that simply had nothing to record.

    `lost` names the consequence, and is why this takes an argument at all.
    "skipped" is the honest word for a side task that failed and it was the
    wrong one for this side task: a downstream project read

        build-record: skipped (CalledProcessError: ... exit status 128.)

    beside a green `OK 1 theories`, took it for a note about something
    optional, and lost five attempts -- including the two most informative --
    before anyone worked out what it had been signalling.  A parenthetical
    naming a Python exception class reports the *event*; what an operator
    needs in a build log is the *consequence*, first, in the words of the
    thing they will miss.  Side tasks whose failure costs nothing irreplaceable
    (enriching an error message, say) still say "skipped", which is now a
    distinction that carries information rather than the only word available.
    """
    try:
        return thunk()
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        detail = f"{type(exc).__name__}: {exc}"
        if lost is None:
            print(f"{label}: skipped ({detail})", file=sys.stderr)
        else:
            print(f"{label}: FAILED -- {lost}\n  cause: {detail}",
                  file=sys.stderr)
        return None
