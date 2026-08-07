"""Isabelle build watchdog, and the build-trajectory corpus it records.

Two halves that ship together because one calls the other:

  - the **watchdog** (`isabelle_watchdog.watchdog`) supervises an
    `isabelle build`, killing it on a stalled stdout, a wall-clock budget, or
    a tactic looping on one line -- and, in the loop case, naming the line;
  - the **recorder** (`isabelle_watchdog.record`) appends one JSON line per
    attempt to a corpus: the outcome, the budgets in force, the error loci,
    the reasoning the engineer wrote beforehand, and the incremental diff of
    the sources.

Read a corpus with `isabelle_watchdog.trajectory` (the `trajectory` command),
which both measures it and verifies it -- every payload can be regenerated
from the tree objects it names, so a corpus can prove its own integrity.

The layering runs the way round that is easy to get backwards: the *watchdog*
is the Isabelle-specific part (it parses Isabelle's progress warnings and
injects Isabelle's options), while the *recorder* and the corpus tools are
plain git and JSON, generic over what is being built.  Nothing below the
watchdog imports it, and it should stay that way.
"""

# The single source of the version; pyproject.toml reads it from here
# (`[tool.hatch.version]`).  Kept in the package rather than stated statically
# in pyproject so that `isabelle_watchdog.__version__` works when the package
# is imported from a source tree that was never installed -- which is how the
# watchdog is often run, and how its own tests run it.  Deriving it from
# `importlib.metadata` instead would raise `PackageNotFoundError` in exactly
# that case.
__version__ = "0.3.0"

__all__ = ["__version__"]
