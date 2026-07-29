#!/bin/sh
# derive-session-map.sh -- re-derive attempts.SESSION_TARGETS from git.
#
# The last rung of the attribution ladder (logging-design.md §13.2.1) maps a
# build target back to its `t/<dir>`, and session names have been renamed
# twice (NDTHT_AR -> Alphabet_Reduction -> Multitape_Alphabet_Reduction).
# Rather than remember the renames, read every (session, directory) pairing
# that has ever appeared in a committed ROOT.  Print it in Python dict form,
# ready to diff against the constant in bin/attempts.py.
#
# Run after any session rename or new session, then paste the result in and
# re-run bin/audit-attribution.py.
#
# Not instant: it walks every commit that touched a ROOT.
set -e
git log --all --format=%H -- 't/*/ROOT' | while read -r sha; do
    git ls-tree -r --name-only "$sha" -- 't/' | grep '/ROOT$' | while read -r f; do
        dir=$(echo "$f" | sed 's|^t/||; s|/ROOT$||')
        git show "$sha:$f" 2>/dev/null |
            sed -n 's/^session[ 	][ 	]*"\{0,1\}\([A-Za-z0-9_]*\).*/\1/p' |
            sed "s|.*|    \"&\": \"$dir\",|"
    done
done | sort -u
