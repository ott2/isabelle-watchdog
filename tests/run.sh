#!/bin/sh
# Run the suite.  Plain scripts, no test framework: the package has no runtime
# dependencies and its tests should not add one.  pytest also collects these
# if you have it (they raise unittest.SkipTest, which it honours).
#
# The Isabelle integration test needs a real Isabelle and a built HOL heap,
# takes about 2m45s, and skips cleanly without them.  Pass --fast to leave it out.
set -e
cd "$(dirname "$0")/.."
python3 tests/test_error_loci.py
[ "${1:-}" = "--fast" ] || python3 tests/test_isabelle_integration.py
