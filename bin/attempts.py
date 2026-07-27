#!/usr/bin/env python3
"""attempts.py — inspect the build-attempt trajectory.

Reads the records written by bin/build_record.py (t/logs/builds.jsonl by
default) and presents them.  Each record carries its own incremental diff
inline, so this tool needs no git object store and runs standalone against
any builds.jsonl — point it anywhere with -i/--input.

  attempts.py list [-n N]        recent attempts, one line each
  attempts.py show BUILD_ID [--full]
                                 full record + the diff this attempt made
  attempts.py episodes [-n N] [--diffs] [--full]
                                 segment into episodes: runs of failing
                                 attempts closed by a success (§12.4).
                                 --diffs interleaves each attempt's diff
                                 with its outcome (--full = full diff,
                                 else a stat summary) — associate change
                                 with outcome across a whole fail→fix run.
  attempts.py lengths [-n N] [--csv|--json]
                                 histogram of trajectory (episode) lengths:
                                 how many 1-step, 2-step, … runs — the
                                 power-law view.  --csv/--json for plotting.
  attempts.py classify BUILD_ID [-v]
                                 why a delta was judged code or doc-only
                                 (per-file verdict; -v shows the evidence)

Common options (accepted before *or* after the subcommand):

  -i/--input FILE  read this builds.jsonl instead of the default
  -n N             how many to show; **-n 0 shows all**
  --all            do not filter out doc-only deltas (see below)

Doc-only filtering (§ why the default view is smaller)
------------------------------------------------------
Many attempts change nothing but prose: a `text \\<open>…\\<close>` block,
a `\\<comment>`, a section heading, an ML comment, a `.md` memo, or pure
re-indentation.  Those build green first time by construction, so counting
them inflates the "one-shot correct edit" rate and flattens the trajectory
histogram.  By default every view keeps only **code** deltas — ones that
change a proof or a statement — and drops doc-only ones; `--all` restores
the raw view.

The classifier is heuristic and auditable: for each changed line it
computes a *code projection* (the line with prose spans, doc-command
keywords and whitespace removed), then compares the multiset of removed
projections against the added ones.  Equal ⇒ nothing but prose moved, even
when the touched lines are code-bearing (e.g. appending a `\\<comment>` to
a `by` line).  Run `classify -v` to see the evidence for any verdict.
"""

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
BUILDS_JSONL = PROJECT_DIR / "t" / "logs" / "builds.jsonl"

# outcome -> terminal-glyph (kept ASCII; the dataset itself is the point)
MARK = {"ok": "OK  ", "fail": "FAIL", "timeout": "TIME"}

# delta class -> terminal-glyph
CLASS_MARK = {"code": "code", "doc": "doc ", "none": "--  "}


# ---------------------------------------------------------------- loading

def _load(path: Path) -> list[dict]:
    if not path.exists():
        print(f"no attempts recorded yet ({path} absent)", file=sys.stderr)
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _trunc(s: str, n: int) -> str:
    return s if s is None or len(s) <= n else s[: n - 1] + "…"


def _tail(seq: list, n: int) -> list:
    """Last n items; n == 0 (or None) means all."""
    return seq if not n else seq[-n:]


# ------------------------------------------------- diff parsing / classing

# Files whose content is prose by definition — no code projection needed.
PROSE_SUFFIXES = {".md", ".tex", ".bib", ".txt", ".rst", ".bst", ".cls"}

# Isabelle document commands: everything from here to the closing cartouche
# is prose, not proof.  bin/prose-token.py has a sibling scanner over the
# same vocabulary, but it cannot be reused: it computes prose spans over a
# *whole file* from offset 0, whereas a diff hunk starts mid-file with an
# unknown state that has to be seeded and can be wrong (insights/274).  This
# tool also stays import-free so it runs standalone against any builds.jsonl.
_DOC_CMDS = (r"text_raw|text|txt|chapter|subsubsection|subsection|section"
             r"|subparagraph|paragraph")

# One pass over a line finds every state-changing token.  Longest-first
# alternation, with word guards so `context` / `text_of` never match `text`.
_TOK = re.compile(
    r"\(\*|\*\)|\\<open>|\\<close>|\\<comment>"
    rf"|(?<![\w'])(?:{_DOC_CMDS})(?![\w'])")

# Isabelle commands that cannot occur inside a document cartouche — used to
# resync when the initial prose state was *guessed* from a hunk header.
_RESYNC = re.compile(
    r"^(?:theory|imports|begin|end|lemma|theorem|corollary|proposition"
    r"|definition|fun|primrec|abbreviation|datatype|type_synonym|locale"
    r"|instantiation|proof|qed|next|by|apply|done|declare|notation"
    r"|interpretation|context|sublocale|termination|inductive)(?![\w'])")


class _Underflow(Exception):
    """An unmatched close token: the hunk began inside prose, not code."""


def _project_lines(lines: list[str], seed: int, guessed: bool) -> list[str]:
    """Code projection of each line, whitespace-normalised.

    Walks a state machine over the whole sequence (context lines included,
    so state carries across).  A line's projection is the concatenation of
    its non-prose spans; a prose line projects to "".  `seed` is the
    document-cartouche depth entering the first line; `guessed` marks that
    seed as inferred from the hunk header rather than observed, which
    licenses the resync escape hatch below.

    Raises _Underflow if a close token has no opener, which means the
    caller's seed was wrong (we started inside prose).
    """
    depth, ml, code_cart, pending = seed, 0, 0, False
    out: list[str] = []
    for raw in lines:
        # A guessed prose state ends the moment a line opens at column 0
        # with an Isabelle command — those cannot appear inside a cartouche.
        if guessed and depth and _RESYNC.match(raw):
            depth, pending = 0, False
        parts: list[str] = []
        pos = 0
        for m in _TOK.finditer(raw):
            tok = m.group(0)
            if ml == 0 and depth == 0:
                parts.append(raw[pos:m.start()])
            pos = m.end()
            if ml:                                   # inside (* … *)
                if tok == "(*":
                    ml += 1
                elif tok == "*)":
                    ml -= 1
            elif depth:                              # inside a doc cartouche
                if tok == "\\<open>":
                    depth += 1
                elif tok == "\\<close>":
                    depth -= 1
            else:                                    # code
                if tok == "(*":
                    ml = 1
                elif tok == "*)":
                    raise _Underflow
                elif tok == "\\<open>":
                    if pending:
                        depth, pending = 1, False
                    else:
                        code_cart += 1
                        parts.append(tok)            # inner-syntax cartouche
                elif tok == "\\<close>":
                    if code_cart:
                        code_cart -= 1
                        parts.append(tok)
                    else:
                        raise _Underflow
                else:                                # doc command / comment
                    pending = True
        if ml == 0 and depth == 0:
            parts.append(raw[pos:])
        out.append(" ".join("".join(parts).split()))
    return out


def _seed_from_context(ctx: str) -> int:
    """Document-cartouche depth implied by a hunk header's context line.

    git's `@@ … @@ <context>` names the nearest preceding column-0 line.
    When that is an unclosed document command, the hunk opens inside prose.
    The context is truncated by git, so this counts only what is visible —
    directionally right, and the resync rule covers the stale-context case.
    """
    if not ctx or not re.match(rf"\s*(?:{_DOC_CMDS})(?![\w'])", ctx):
        return 0
    return max(0, ctx.count("\\<open>") - ctx.count("\\<close>"))


def _split_files(diff: str) -> list[tuple[str, list[str]]]:
    """Unified diff -> [(path, body-lines)]."""
    files: list[tuple[str, list[str]]] = []
    cur: list[str] | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*?) b/(.*)$", line)
            cur = []
            files.append((m.group(2) if m else line, cur))
        elif cur is not None:
            cur.append(line)
    return files


def _hunks(body: list[str]) -> list[tuple[str, list[str]]]:
    """Body lines -> [(hunk-header context, hunk lines)]."""
    out: list[tuple[str, list[str]]] = []
    cur: list[str] | None = None
    for line in body:
        if line.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@ ?(.*)", line)
            cur = []
            out.append((m.group(1) if m else "", cur))
        elif cur is not None and line[:1] in " +-":
            cur.append(line)
    return out


def _side(lines: list[str], sign: str, seed: int, guessed: bool) -> list[str]:
    """Projections of the `sign` lines, with context lines carrying state."""
    seq = [ln[1:] for ln in lines if ln[:1] in (" ", sign)]
    flags = [ln[:1] == sign for ln in lines if ln[:1] in (" ", sign)]
    projs = _project_lines(seq, seed, guessed)
    return [p for p, f in zip(projs, flags) if f and p]


def _thy_code_change(hunks: list[tuple[str, list[str]]]) -> list[str]:
    """Evidence that a .thy diff changed code: differing code projections.

    Returns the projections that are not matched on both sides (empty list
    ⇒ prose-only).  Falls back to a text-level compare if the state machine
    cannot find a consistent reading of the hunk.
    """
    evidence: list[str] = []
    for ctx, lines in hunks:
        seed = _seed_from_context(ctx)
        for attempt_seed, guessed in ((seed, seed > 0), (1, True), (None, False)):
            if attempt_seed is None:                 # no consistent reading
                old = [ln[1:].strip() for ln in lines if ln[:1] == "-"]
                new = [ln[1:].strip() for ln in lines if ln[:1] == "+"]
                break
            try:
                old = _side(lines, "-", attempt_seed, guessed)
                new = _side(lines, "+", attempt_seed, guessed)
                break
            except _Underflow:
                continue
        if sorted(old) != sorted(new):
            evidence.extend(f"-{p}" for p in old if p not in new)
            evidence.extend(f"+{p}" for p in new if p not in old)
    return evidence


def classify_file(path: str, body: list[str]) -> tuple[str, list[str]]:
    """('code'|'doc', evidence) for one file's diff."""
    suffix = "." + path.rsplit("/", 1)[-1].rsplit(".", 1)[-1] \
        if "." in path.rsplit("/", 1)[-1] else ""
    if suffix in PROSE_SUFFIXES or "/document/" in f"/{path}":
        return "doc", []
    if suffix == ".thy":
        ev = _thy_code_change(_hunks(body))
        return ("code" if ev else "doc"), ev
    # ROOT, ROOTS, Makefile, bin/*.py, … — build-relevant, and anything
    # unrecognised is treated as code so nothing is hidden by accident.
    ev = [ln for ln in body
          if ln[:1] in "+-" and not ln.startswith(("+++", "---")) and ln[1:].strip()]
    return ("code" if ev else "doc"), ev


def classify(diff: str) -> tuple[str, list[tuple[str, str, list[str]]]]:
    """('code'|'doc'|'none', [(path, verdict, evidence)]) for a whole delta."""
    files = _split_files(diff or "")
    if not files:
        return "none", []
    per_file = [(p, *classify_file(p, b)) for p, b in files]
    overall = "code" if any(v == "code" for _, v, _ in per_file) else "doc"
    return overall, per_file


def rec_class(rec: dict) -> str:
    """Memoised delta class of a record."""
    if "_class" not in rec:
        rec["_class"] = classify(rec.get("diff") or "")[0]
    return rec["_class"]


def keep(recs: list[dict], include_all: bool) -> list[dict]:
    return recs if include_all else [r for r in recs if rec_class(r) == "code"]


# --------------------------------------------------------------- rendering

def _diff_stat(diff: str, indent: str) -> str:
    """A git-diff --stat lookalike computed from the inline diff text."""
    stats, adds, dels = [], 0, 0
    for path, body in _split_files(diff):
        a = sum(1 for ln in body if ln.startswith("+") and not ln.startswith("+++"))
        d = sum(1 for ln in body if ln.startswith("-") and not ln.startswith("---"))
        stats.append((path, a, d, classify_file(path, body)[0]))
        adds, dels = adds + a, dels + d
    w = max((len(p) for p, *_ in stats), default=0)
    rows = [f"{indent} {p:{w}} | {a + d:4d} +{a} -{d}  [{v}]"
            for p, a, d, v in stats]
    rows.append(f"{indent} {len(stats)} file(s), {adds} insertion(s), "
                f"{dels} deletion(s)")
    return "\n".join(rows)


def _fmt_line(rec: dict) -> str:
    dirty = "*" if rec.get("head_dirty") else " "
    head = _trunc(rec.get("error_head") or "", 60)
    return (f"{rec['build_id']}  {MARK.get(rec['outcome'], rec['outcome'])}"
            f"  {CLASS_MARK[rec_class(rec)]}"
            f"  {rec['elapsed_s']:5.1f}s {dirty} {head}")


def _print_attempt_diff(rec: dict, full: bool, indent: str = "  ") -> None:
    """Print the diff this attempt introduced (inline in the record)."""
    diff = rec.get("diff")
    if not diff:
        print(f"{indent}(no tracked-file change vs the previous attempt)")
        return
    print(f"{indent}--- this attempt's change [{rec_class(rec)}] ---")
    print(diff if full else _diff_stat(diff, indent))


# -------------------------------------------------------------- subcommands

def cmd_list(recs: list[dict], n: int, include_all: bool) -> None:
    shown = keep(recs, include_all)
    for rec in _tail(shown, n):
        print(_fmt_line(rec))
    if not include_all:
        print(f"\n{len(shown)} code deltas of {len(recs)} attempts "
              f"({len(recs) - len(shown)} doc-only or no-change; --all to show)")


def cmd_show(recs: list[dict], build_id: str, full: bool) -> None:
    match = [r for r in recs if r["build_id"].startswith(build_id)]
    if not match:
        print(f"no attempt matching {build_id!r}", file=sys.stderr)
        return
    rec = match[-1]
    for k, v in rec.items():
        if k not in ("diff", "_class"):
            print(f"  {k:16} {v}")
    print(f"  {'delta_class':16} {rec_class(rec)}")
    print()
    _print_attempt_diff(rec, full)


def cmd_classify(recs: list[dict], build_id: str, verbose: bool) -> None:
    match = [r for r in recs if r["build_id"].startswith(build_id)]
    if not match:
        print(f"no attempt matching {build_id!r}", file=sys.stderr)
        return
    rec = match[-1]
    overall, per_file = classify(rec.get("diff") or "")
    print(f"{rec['build_id']}  {rec['outcome']}  -> {overall}")
    for path, verdict, ev in per_file:
        print(f"  [{verdict:4}] {path}")
        if verbose:
            for line in ev[:40]:
                print(f"           {_trunc(line, 100)}")
            if len(ev) > 40:
                print(f"           … {len(ev) - 40} more")
    if not per_file:
        print("  (empty delta — a rebuild of an unchanged tree)")


def _episodes(recs: list[dict]) -> list[list[dict]]:
    """Maximal runs of non-ok attempts closed by an ok (§12.4).  A trailing
    run with no closing ok is returned as an open episode."""
    episodes: list[list[dict]] = []
    cur: list[dict] = []
    for rec in recs:
        cur.append(rec)
        if rec["outcome"] == "ok":
            episodes.append(cur)
            cur = []
    if cur:
        episodes.append(cur)
    return episodes


def cmd_episodes(recs: list[dict], n: int, include_all: bool,
                 diffs: bool = False, full: bool = False) -> None:
    """With diffs, interleave each attempt's introduced diff under its
    outcome line, so a whole fail→fix run reads as change-then-verdict."""
    episodes = _episodes(recs)
    if not include_all:
        episodes = [ep for ep in episodes
                    if any(rec_class(r) == "code" for r in ep)]

    for ep in _tail(episodes, n):
        closed = ep[-1]["outcome"] == "ok"
        fails = sum(1 for r in ep if r["outcome"] != "ok")
        code = sum(1 for r in ep if rec_class(r) == "code")
        span = f"{ep[0]['build_id']} → {ep[-1]['build_id']}"
        status = "closed" if closed else "OPEN (no success yet)"
        print(f"episode  {span}  [{len(ep)} attempts, {code} code, "
              f"{fails} fail, {status}]")
        for r in ep:
            print(f"    {_fmt_line(r)}")
            if diffs:
                _print_attempt_diff(r, full, indent="      ")
        print()


def cmd_lengths(recs: list[dict], n: int, include_all: bool,
                fmt: str) -> None:
    """Frequency of 1-step, 2-step, … trajectories.

    A trajectory's *length* counts the attempts it took to reach a green
    build, **including the closing successful one** — so length 1 is a
    one-shot correct edit and length k means k-1 failed repairs then a
    green build.  (Counting only the failures would put one-shot edits at
    0 and lose them off the left of the plot.)

    By default only code-changing attempts count — a doc-only attempt is
    not a proof step — and episodes with no code attempt at all are
    dropped; without that, prose edits pile up on length 1 and flatten the
    tail.  Open (unclosed) episodes are excluded: their length is not yet
    known.
    """
    episodes = [ep for ep in _episodes(recs) if ep[-1]["outcome"] == "ok"]
    episodes = _tail(episodes, n)
    if include_all:
        lengths = [len(ep) for ep in episodes]
        label = "attempts per trajectory, all deltas"
    else:
        lengths = [c for ep in episodes
                   if (c := sum(1 for r in ep if rec_class(r) == "code"))]
        label = "code-changing attempts per trajectory"

    hist: dict[int, int] = {}
    for length in lengths:
        hist[length] = hist.get(length, 0) + 1

    if fmt == "csv":
        print("length,trajectories")
        for k in sorted(hist):
            print(f"{k},{hist[k]}")
        return
    if fmt == "json":
        print(json.dumps({"metric": label,
                          "histogram": {str(k): hist[k] for k in sorted(hist)},
                          "trajectories": len(lengths),
                          "steps": sum(lengths)}, indent=1))
        return

    if not lengths:
        print("no closed trajectories")
        return
    print(f"trajectory lengths — {label}; "
          f"length k = k-1 failures then a green build\n")
    width = max(hist.values())
    for k in sorted(hist):
        bar = "#" * max(1, round(hist[k] * 50 / width))
        print(f"{k:4d} | {bar} {hist[k]}")
    ordered = sorted(lengths)
    mean = sum(lengths) / len(lengths)
    median = ordered[len(ordered) // 2]
    dropped = len(episodes) - len(lengths)
    print(f"\n  {len(lengths)} closed trajectories, {sum(lengths)} steps; "
          f"mean {mean:.2f}, median {median}, max {max(lengths)}")
    print(f"  one-shot (length 1): {hist.get(1, 0)} "
          f"({100 * hist.get(1, 0) / len(lengths):.1f}%)")
    if dropped:
        print(f"  {dropped} doc-only trajectories dropped (--all to include)")
    print("  --csv / --json for plotting")


# --------------------------------------------------------------------- main

def main() -> int:
    # Shared options live on a parent parser attached to *both* the top
    # level and every subparser, so `-i FILE list` and `list -i FILE` both
    # work.  default=SUPPRESS is load-bearing: without it the subparser
    # re-applies its own default and clobbers a value given before the
    # subcommand, since both write the same namespace attribute.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-i", "--input", metavar="FILE",
                        default=argparse.SUPPRESS,
                        help=f"builds.jsonl to read (default {BUILDS_JSONL})")
    common.add_argument("--all", action="store_true",
                        default=argparse.SUPPRESS,
                        help="include doc-only deltas (default: code only)")

    p = argparse.ArgumentParser(
        description=__doc__, parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    pl = sub.add_parser("list", parents=[common],
                        help="recent attempts, one line each")
    pl.add_argument("-n", type=int, default=30,
                    help="how many to show; 0 shows all (default 30)")

    ps = sub.add_parser("show", parents=[common],
                        help="full record + the attempt's diff")
    ps.add_argument("build_id")
    ps.add_argument("--full", action="store_true",
                    help="full diff instead of a stat summary")

    pc = sub.add_parser("classify", parents=[common],
                        help="why a delta counts as code or doc-only")
    pc.add_argument("build_id")
    pc.add_argument("-v", "--verbose", action="store_true",
                    help="show the changed code projections behind the verdict")

    pe = sub.add_parser("episodes", parents=[common],
                        help="fail-runs closed by a success")
    pe.add_argument("-n", type=int, default=10,
                    help="how many to show; 0 shows all (default 10)")
    pe.add_argument("--diffs", action="store_true",
                    help="interleave each attempt's diff under its outcome")
    pe.add_argument("--full", action="store_true",
                    help="with --diffs, full diff instead of a stat summary")

    pg = sub.add_parser("lengths", parents=[common],
                        help="histogram of trajectory lengths (the power-law view)")
    pg.add_argument("-n", type=int, default=0,
                    help="use only the last N trajectories; 0 = all (default)")
    pg.add_argument("--csv", action="store_true", help="emit CSV for plotting")
    pg.add_argument("--json", action="store_true", help="emit JSON")

    ns = p.parse_args()
    given = getattr(ns, "input", None)
    include_all = getattr(ns, "all", False)
    path = Path(given) if given else BUILDS_JSONL
    if given and not path.exists():
        print(f"FAIL: no such build log: {path}", file=sys.stderr)
        return 1
    recs = _load(path)
    if not recs:
        return 1 if given else 0

    if ns.cmd == "show":
        cmd_show(recs, ns.build_id, ns.full)
    elif ns.cmd == "classify":
        cmd_classify(recs, ns.build_id, ns.verbose)
    elif ns.cmd == "episodes":
        cmd_episodes(recs, ns.n, include_all, diffs=ns.diffs, full=ns.full)
    elif ns.cmd == "lengths":
        cmd_lengths(recs, ns.n, include_all,
                    "csv" if ns.csv else "json" if ns.json else "text")
    else:  # list (default)
        cmd_list(recs, getattr(ns, "n", 30), include_all)
    return 0


if __name__ == "__main__":
    sys.exit(main())
