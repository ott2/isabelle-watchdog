#!/bin/sh
# Verify build_record._snapshot_tree() captures a brand-new untracked file.
#
# Regression guard for the 2026-07-27 `git add -u` -> `git add -A` fix
# (logging-design.md 13.1): before it, a not-yet-added theory was invisible
# to capture, so a whole fail->fix run on a new theory recorded as empty
# diffs.  Creates a probe file, snapshots, and checks the tree object.
set -e
PROBE=t/base/ZZ_SnapshotProbe.thy
trap 'rm -f "$PROBE"' EXIT

printf 'theory ZZ_SnapshotProbe imports Main begin end\n' > "$PROBE"

TREE=$(python3 -c "
import sys; sys.path.insert(0, 'bin')
import build_record
print(build_record._snapshot_tree())
")

if git ls-tree -r --name-only "$TREE" | grep -qx "$PROBE"; then
    echo "PASS: untracked $PROBE captured in snapshot tree $TREE"
else
    echo "FAIL: untracked $PROBE missing from snapshot tree $TREE"
    exit 1
fi

if git ls-tree -r --name-only "$TREE" | grep -q '^t/logs/'; then
    echo "FAIL: gitignored t/logs/ leaked into the snapshot"
    exit 1
fi
echo "PASS: gitignored paths stay out of the snapshot"
