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

import sys
from typing import Callable, TypeVar

_T = TypeVar("_T")


def run_guarded(label: str, thunk: Callable[[], _T]) -> "_T | None":
    """Run `thunk()`; on any exception warn on stderr and return None.

    The warning names `label` so an operator reading a build log can tell
    which side task was skipped, and that a side task was skipped at all --
    silence here would make broken capture indistinguishable from a build
    that simply had nothing to record.
    """
    try:
        return thunk()
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        print(f"{label}: skipped ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return None
