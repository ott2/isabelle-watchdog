#!/bin/sh
# Verify build_record._snapshot_tree()'s untracked-file allowlist.
#
# Regression guard for the 2026-07-27 capture fix (logging-design.md 13.1).
# Tracked-only staging was blind while a new theory was being authored, so a
# whole fail->fix run recorded as empty diffs.  The fix stages untracked
# files too, but by allowlist (UNTRACKED_PATHSPECS) rather than `git add -A`,
# so this checks BOTH directions: build-relevant source gets in, scratch and
# gitignored paths stay out.
set -e
THY=t/base/ZZ_SnapshotProbe.thy
ROOTP=t/zz-probe/ROOT
SCRATCH=zz-probe-scratch.py
trap 'rm -f "$THY" "$SCRATCH"; rm -rf t/zz-probe' EXIT

mkdir -p t/zz-probe
printf 'theory ZZ_SnapshotProbe imports Main begin end\n' > "$THY"
printf 'session ZZ_Probe = HOL +\n  theories\n' > "$ROOTP"
printf '# throwaway\n' > "$SCRATCH"

TREE=$(python3 -c "
import sys; sys.path.insert(0, 'bin')
import build_record
print(build_record._snapshot_tree())
")
LISTING=$(git ls-tree -r --name-only "$TREE")

rc=0
check_in() {
    if printf '%s\n' "$LISTING" | grep -qx "$1"; then
        echo "PASS: $2 captured ($1)"
    else
        echo "FAIL: $2 missing from snapshot tree $TREE ($1)"; rc=1
    fi
}
check_out() {
    if printf '%s\n' "$LISTING" | grep -qx "$1"; then
        echo "FAIL: $2 leaked into snapshot tree $TREE ($1)"; rc=1
    else
        echo "PASS: $2 stays out ($1)"
    fi
}

check_in  "$THY"    "untracked theory"
check_in  "$ROOTP"  "untracked session ROOT"
check_out "$SCRATCH" "untracked scratch script"

if printf '%s\n' "$LISTING" | grep -q '^t/logs/'; then
    echo "FAIL: gitignored t/logs/ leaked into the snapshot"; rc=1
else
    echo "PASS: gitignored paths stay out"
fi
exit $rc
