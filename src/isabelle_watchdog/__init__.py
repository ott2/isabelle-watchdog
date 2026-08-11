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

# The version is stated once, in pyproject.toml, and read back from the
# installed metadata.  One file holds what the project *is* -- name, version,
# dependencies, entry points -- rather than splitting it across a manifest and
# a module that have to be kept agreeing.
#
# The cost is the uninstalled case: a source tree that was never `pip
# install`ed has no metadata to read.  That answer is `0+unknown` rather than
# a guess, because a wrong version is worse than an absent one -- a bug report
# quoting it would send someone to the wrong commit.  It is also a corner:
# this package is meant to be installed (`Test against an install, not
# PYTHONPATH=src` -- CLAUDE.md), because two defects have already been found
# that only appear under one.
try:
    from importlib.metadata import PackageNotFoundError, version as _version
    __version__ = _version("isabelle-watchdog")
except PackageNotFoundError:          # a source tree, never installed
    __version__ = "0+unknown"

__all__ = ["__version__"]
