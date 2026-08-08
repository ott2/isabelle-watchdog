#!/usr/bin/env python3
"""convert-legacy-trajectory.py — ONE-SHOT migration of the prototype
git-chain capture into the diff-bearing log format (logging-design.md §16).

HISTORICAL RECORD.  The build-trajectory capture began as a prototype that
chained working-tree snapshots on a git ref (refs/attempts/<branch>) and
logged outcomes *without* diffs.  We then pivoted to portable per-episode
patch files (logging-design.md §16).  This script reconstructs the
diff-bearing log for that prototype data, so the early trajectories carry
forward in the new format.  It is run ONCE; thereafter it is kept only as
a record — in git history — of how the initial dataset was produced.
Going forward, bin/build_record.py writes diffs directly and
`python -m isabelle_watchdog.export` materialises episodes; this script is not on that
path.

Inputs (READ-ONLY — the source data is never mutated):
  - the legacy attempt chain (a ref, default refs/attempts/main): each
    'attempt <build_id> <outcome>' commit's incremental diff is
    `git diff <parent> <commit>` — the chain's parent links already ARE
    the per-attempt increments.
  - the legacy builds.jsonl (<checkout>/t/logs/builds.jsonl): outcome,
    timing, git_head, error head — joined to the chain by build_id.

Output:
  - a diff-bearing builds.jsonl written to --out, with instance_id +
    provenance stamped and the incremental `diff` added per record.  Feed
    it to `python -m isabelle_watchdog.export` to materialise the episode files.

Identity continuity: the checkout's t/logs/instance-id is reused if
present, else minted and written there — so the legacy records and the
checkout's *future* builds share one instance_id across the prototype->new
boundary.  (That file is the new identity, not legacy data, so writing it
does not violate the read-only-on-legacy-data rule.)
"""

import argparse
import json
import re
import secrets
import socket
import subprocess
import sys
from pathlib import Path

# build_id is build_record's strftime stamp: YYYYMMDD-HHMMSS-mmm.  Matching
# the exact shape avoids false positives like the real "attempt 3 bootstrap"
# commit (project Attempt 3), whose subject also starts with "attempt".
SUBJ = re.compile(r"^attempt (\d{8}-\d{6}-\d{3}) ")


def git_out(checkout: Path, args: list[str]) -> str:
    return subprocess.run(["git", *args], cwd=checkout,
                          capture_output=True, text=True).stdout


def chain_trees(checkout: Path, ref: str) -> dict:
    """Map build_id -> the attempt commit's tree sha, from the chain."""
    lines = git_out(checkout, ["log", "--reverse",
                               "--format=%H%x09%T%x09%s", ref]).splitlines()
    out = {}
    for line in lines:
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        _sha, tree, subj = parts
        m = SUBJ.match(subj)
        if m:                              # skip the grafted real history
            out[m.group(1)] = tree
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--checkout", required=True,
                    help="checkout holding the legacy data (read-only)")
    ap.add_argument("--ref", default="refs/attempts/main")
    ap.add_argument("--out", required=True,
                    help="path to write the diff-bearing builds.jsonl")
    ns = ap.parse_args()

    checkout = Path(ns.checkout).resolve()
    legacy_log = checkout / "t" / "logs" / "builds.jsonl"
    if not legacy_log.exists():
        print(f"FAIL: no legacy log at {legacy_log}", file=sys.stderr)
        return 1

    inst_file = checkout / "t" / "logs" / "instance-id"
    if inst_file.exists() and inst_file.read_text().strip():
        instance, established = inst_file.read_text().strip(), False
    else:
        instance = secrets.token_hex(8)
        inst_file.parent.mkdir(parents=True, exist_ok=True)
        inst_file.write_text(instance + "\n")
        established = True
    prov = {
        "hostname": socket.gethostname(),
        "contributor": git_out(checkout, ["config", "user.email"]).strip()
                       or "unknown",
        "origin_url": git_out(checkout, ["config", "remote.origin.url"]).strip()
                      or None,
    }
    chain = chain_trees(checkout, ns.ref)
    head_tree_cache: dict = {}

    def head_tree(h: str) -> str:
        if h not in head_tree_cache:
            head_tree_cache[h] = git_out(
                checkout, ["rev-parse", f"{h}^{{tree}}"]).strip()
        return head_tree_cache[h]

    # Records are in build (chronological) order, matching the chain, so we
    # apply the same re-baseline-on-commit rule as bin/build_record.py: diff
    # vs the previous attempt's tree, unless HEAD moved (a mid-flight commit),
    # in which case diff vs the new HEAD's tree so committed content is excluded.
    recs = [json.loads(l) for l in legacy_log.read_text().splitlines()
            if l.strip()]
    matched, out_recs = 0, []
    prev_tree, prev_head = None, None
    for r in recs:
        nr = dict(r)
        nr.setdefault("instance_id", instance)
        for k, v in prov.items():
            nr.setdefault(k, v)
        nr["backfilled"] = True            # provenance reconstructed, not captured
        bid, head = r.get("build_id"), r.get("git_head")
        at = chain.get(bid)
        if at is not None and head:
            base = head_tree(head) if (prev_tree is None or prev_head != head) \
                else prev_tree
            nr["diff"] = git_out(checkout, ["diff", "--no-color", "-M", base, at])
            nr.setdefault("tree", at)
            matched += 1
            prev_tree, prev_head = at, head
        else:
            nr.setdefault("diff", None)    # record with no chain commit
        out_recs.append(nr)

    outp = Path(ns.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text("\n".join(json.dumps(r) for r in out_recs) + "\n")
    print(f"convert-legacy: {len(recs)} records, {matched} matched to chain "
          f"diffs, {len(chain)} chain attempts")
    print(f"  instance_id={instance} "
          f"({'established in ' if established else 'reused from '}{inst_file})")
    print(f"PASS: wrote diff-bearing log to {outp}")
    print(f"  next: python -m isabelle_watchdog.export --log {outp} --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
