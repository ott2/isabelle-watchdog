#!/usr/bin/env python3
"""attempts.py — inspect the build-attempt trajectory (prototype).

Reads the records written by bin/build_record.py (t/logs/builds.jsonl
+ the refs/attempts/<branch> snapshot chain) and presents them.

  attempts.py list [-n N]        recent attempts, one line each
  attempts.py show BUILD_ID [--full]
                                 full record + the diff this attempt made
  attempts.py episodes [-n N]    segment into episodes: runs of failing
                                 attempts closed by a success (§12.4)

This is the read side of the §14 MVP — deliberately thin, so we learn
what views are worth having from early use rather than guessing now.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
BUILDS_JSONL = PROJECT_DIR / "t" / "logs" / "builds.jsonl"

# outcome -> terminal-glyph (kept ASCII; the dataset itself is the point)
MARK = {"ok": "OK  ", "fail": "FAIL", "timeout": "TIME"}


def _load() -> list[dict]:
    if not BUILDS_JSONL.exists():
        print(f"no attempts recorded yet ({BUILDS_JSONL} absent)",
              file=sys.stderr)
        return []
    out = []
    for line in BUILDS_JSONL.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _trunc(s: str, n: int) -> str:
    return s if s is None or len(s) <= n else s[: n - 1] + "…"


def _git(args: list[str]) -> str:
    return subprocess.run(["git", *args], cwd=PROJECT_DIR,
                          capture_output=True, text=True).stdout.rstrip()


def _fmt_line(rec: dict) -> str:
    dirty = "*" if rec.get("head_dirty") else " "
    head = _trunc(rec.get("error_head") or "", 64)
    return (f"{rec['build_id']}  {MARK.get(rec['outcome'], rec['outcome'])}"
            f"  {rec['elapsed_s']:5.1f}s {dirty} {head}")


def cmd_list(recs: list[dict], n: int) -> None:
    for rec in recs[-n:]:
        print(_fmt_line(rec))


def cmd_show(recs: list[dict], build_id: str, full: bool) -> None:
    match = [r for r in recs if r["build_id"].startswith(build_id)]
    if not match:
        print(f"no attempt matching {build_id!r}", file=sys.stderr)
        return
    rec = match[-1]
    for k, v in rec.items():
        print(f"  {k:16} {v}")
    parent, snap = rec.get("parent_snapshot"), rec.get("snapshot")
    if parent and snap:
        print(f"\n  --- diff {parent[:9]}..{snap[:9]} (this attempt's change) ---")
        diff_args = ["diff"] + ([] if full else ["--stat"]) + [parent, snap]
        out = _git(diff_args)
        print(out if out else "  (no tracked-file change vs parent snapshot)")


def cmd_episodes(recs: list[dict], n: int) -> None:
    """Segment into episodes: a maximal run of non-ok attempts ended by
    an ok (§12.4).  A trailing run with no closing ok is shown as open."""
    episodes: list[list[dict]] = []
    cur: list[dict] = []
    for rec in recs:
        cur.append(rec)
        if rec["outcome"] == "ok":
            episodes.append(cur)
            cur = []
    if cur:
        episodes.append(cur)  # open episode (no closing success yet)

    for ep in episodes[-n:]:
        closed = ep[-1]["outcome"] == "ok"
        fails = sum(1 for r in ep if r["outcome"] != "ok")
        span = f"{ep[0]['build_id']} → {ep[-1]['build_id']}"
        status = "closed" if closed else "OPEN (no success yet)"
        print(f"episode  {span}  [{len(ep)} attempts, {fails} fail, {status}]")
        for r in ep:
            print(f"    {_fmt_line(r)}")
        print()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    pl = sub.add_parser("list", help="recent attempts, one line each")
    pl.add_argument("-n", type=int, default=30)

    ps = sub.add_parser("show", help="full record + the attempt's diff")
    ps.add_argument("build_id")
    ps.add_argument("--full", action="store_true",
                    help="full diff instead of --stat")

    pe = sub.add_parser("episodes", help="fail-runs closed by a success")
    pe.add_argument("-n", type=int, default=10)

    ns = p.parse_args()
    recs = _load()
    if not recs:
        return 0

    if ns.cmd == "show":
        cmd_show(recs, ns.build_id, ns.full)
    elif ns.cmd == "episodes":
        cmd_episodes(recs, ns.n)
    else:  # list (default)
        cmd_list(recs, getattr(ns, "n", 30))
    return 0


if __name__ == "__main__":
    sys.exit(main())
