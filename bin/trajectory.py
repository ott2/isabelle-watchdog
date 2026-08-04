#!/usr/bin/env python3
"""Diagnose, repair and replay a build-trajectory corpus.

Works against any corpus written by `build_record.py`, in any project: the
source repository is named with `--repo`, and every base tree is resolved from
the record's own `git_head`, so a corpus that starts partway into a project's
history -- or that re-baselines on mid-flight commits -- is handled correctly.

BASE TREE PER RECORD.  `build_record` anchors each diff on the previous
attempt's snapshot tree, *except* when HEAD has moved since (a commit landed
mid-run), in which case it re-baselines on the new HEAD's tree so the committed
content stays out of the payload.  Treating a corpus as one flat chain
therefore desynchronises at the first commit.  This tool applies the same rule
in reverse.

REGENERATION IS THE GROUND TRUTH.  Each record stores `tree`, the object id of
the snapshot it was computed from, and the base is derivable.  Where both
objects survive in the repository, the payload is exactly

    git diff --no-color -M <base> <tree>

so it can be *regenerated* and compared.  That is both the strongest available
check and the exact repair -- no inference about what was lost, no heuristic
about what belongs at the end of a hunk.  Git prunes unreferenced objects, so
this degrades over time; `--heuristic` enables a weaker fallback for records
whose objects are gone, and `replay` applies the payloads instead, which needs
no objects beyond an anchor.

Regeneration is NOT byte-stable over time -- see `normalise` -- so the
comparison canonicalises the one field that drifts, and a repair appends only
the missing tail rather than overwriting the record.

TWO INDEPENDENT AXES.  *Payload integrity* asks whether the diff faithfully
records what the snapshot saw; *capture coverage* asks whether the snapshot saw
the edit at all.  Regeneration settles only the first.  A record whose snapshot
never moved has a perfectly faithful empty payload and may still have missed
the whole edit, so empty payloads are judged on coverage, never on bytes.

DEFECT CLASSES (`check`):

  sound              payload regenerates identically.
  truncated          the stored payload is a strict *prefix* of the regenerated
                     one -- the signature of a diff passed through `.strip()`,
                     which eats the trailing newline and any trailing run of
                     whitespace-only context lines (a blank source line is a
                     context line holding a single space).  Exactly repairable.
  divergent          differs from the regenerated payload other than by
                     truncation.  Repairable by regeneration, but report it:
                     it means something other than the known `.strip()` bug.
  unverified         structurally well-formed, but the objects needed to confirm
                     the content have been pruned.  Deliberately NOT called
                     sound: hunk accounting cannot see a lost `+`/`-` line that
                     leaves the counts balanced.
  stripped?          as `unverified`, but the final hunk body is short by an
                     equal number of old and new lines, so whole trailing
                     context lines are missing.  Repairable only under
                     `--heuristic`, and only as blank lines.
  damaged            hunk body short by an unequal number -- a `+`/`-` line went
                     missing, which `.strip()` cannot do.  NOT repairable;
                     guessing the content would be fabrication.
  empty-consistent   empty payload and the snapshot tree equals the base.
                     Nothing changed -- a plain rebuild with no source edits,
                     not a defect.
  empty-recoverable  empty payload but the snapshot tree differs from the base.
                     Fully recoverable by regeneration.
  empty-blind        empty payload, snapshot tree unchanged, yet the outcome
                     flips between `fail` and `ok`.  A prover is deterministic
                     on identical sources, so the sources must have changed and
                     the snapshot did not see them -- the signature of
                     tracked-only capture while a new theory is being authored.
                     The payload is faithful; the *allowlist* is at fault.
                     Unrecoverable -- the content was never captured.  Flips
                     involving `timeout` are excluded, a timeout being
                     wall-clock dependent and so able to differ on identical
                     input.

ATTRIBUTING AN OUTCOME.  An outcome is only interpretable against the
conditions that produced it, so each record also carries the watchdog budgets
in force (`limits`) and the non-source files that changed (`other_changed`).
`flips` uses both: without them an `ok -> timeout` whose source diff is empty
is indistinguishable from a proof that got slower, when the cause may have been
a Makefile edit halving `WALL_TIMEOUT`.

Usage:
    bin/trajectory.py check  [CORPUS] [--repo PATH]
    bin/trajectory.py repair [CORPUS] [--repo PATH] [--apply] [--heuristic]
    bin/trajectory.py replay [CORPUS] [--repo PATH] [--from N] [--to N]
    bin/trajectory.py progress [CORPUS] [--repo PATH]
    bin/trajectory.py notes  [CORPUS] [--raw] [--loci]
    bin/trajectory.py flips  [CORPUS]
    bin/trajectory.py extract CORPUS N DEST [--repo PATH]
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import corpus

HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")

# The subcommands that read payloads back out of the object store, and so
# genuinely need `--repo`.  The rest read the corpus alone; requiring a
# repository of them made a reader of a shared or archived corpus supply one
# it had no reason to have.
NEEDS_REPO = ("check", "repair", "replay", "extract")

NOTE_REGENERATED = ("payload restored from a re-diff of the recorded base and "
                    "snapshot tree objects; only the lost tail was appended")
NOTE_HEURISTIC = ("trailing blank context lines restored from the hunk header; the tree "
                  "objects were unavailable, so this is inferred rather than regenerated")


# --------------------------------------------------------------------------- git

def git(repo: Path, args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, **kw)


def have_objects(repo: Path, ids) -> set[str]:
    """Which of `ids` exist in `repo`, in one batch call."""
    ids = sorted({i for i in ids if i})
    if not ids:
        return set()
    p = git(repo, ["cat-file", "--batch-check"], input="\n".join(ids) + "\n")
    return {f[0] for f in (l.split() for l in p.stdout.splitlines())
            if len(f) >= 2 and f[1] in ("tree", "commit", "blob")}


def head_trees(repo: Path, commits) -> dict[str, str]:
    """commit id -> its tree id, in one batch call."""
    commits = sorted({c for c in commits if c})
    if not commits:
        return {}
    q = git(repo, ["cat-file", "--batch"], input="\n".join(commits) + "\n")
    out, stream, pos = {}, q.stdout, 0
    for c in commits:
        idx = stream.find(f"{c} commit ", pos)
        if idx < 0:
            continue
        nl = stream.index("\n", idx)
        size = int(stream[idx:nl].split()[2])
        body = stream[nl + 1: nl + 1 + size]
        m = re.match(r"tree ([0-9a-f]{40})", body)
        if m:
            out[c] = m.group(1)
        pos = nl + 1 + size
    return out


def regenerate(repo: Path, base: str, tree: str) -> str | None:
    p = git(repo, ["diff", "--no-color", "-M", base, tree])
    return p.stdout if p.returncode == 0 else None


INDEX_LINE = re.compile(r"(?m)^index ([0-9a-f]+)\.\.([0-9a-f]+)")


def normalise(diff: str) -> str:
    """Canonicalise the parts of a diff that are not stable over time.

    `core.abbrev` defaults to `auto`, so the object ids on an `index` line are
    abbreviated to a length that grows with the repository.  A payload recorded
    when the repo was smaller therefore regenerates *differently* today -- two
    bytes longer per index line -- with no corruption whatever.  Comparing raw
    bytes would reclassify every such record as damaged and, worse, invite a
    "repair" that rewrites healthy history.

    Truncating both sides to the shortest abbreviation git ever emits removes
    the only field that drifts, and it appears solely in the header of each
    file section -- never in the trailing whitespace that `.strip()` ate -- so
    a tail computed on normalised text is byte-identical to the real one."""
    return INDEX_LINE.sub(lambda m: f"index {m.group(1)[:7]}..{m.group(2)[:7]}", diff)


# --------------------------------------------------------------- corpus analysis

def hunk_shortfalls(diff: str) -> list[tuple[str, int, int]]:
    """(header, old_missing, new_missing) for every hunk whose body is short."""
    lines, i, out = diff.split("\n"), 0, []
    while i < len(lines):
        m = HUNK.match(lines[i])
        if not m:
            i += 1
            continue
        want_old, want_new = int(m.group(1) or 1), int(m.group(2) or 1)
        header = lines[i]
        i += 1
        old = new = 0
        while i < len(lines) and (old < want_old or new < want_new):
            line = lines[i]
            if line.startswith("+"):
                new += 1
            elif line.startswith("-"):
                old += 1
            elif line.startswith(" ") or line == "":
                old += 1
                new += 1
            elif line.startswith("\\"):
                pass
            else:
                break
            i += 1
        if old < want_old or new < want_new:
            out.append((header, want_old - old, want_new - new))
    return out


def resolve_bases(records: list[dict], repo: Path) -> list[str | None]:
    """The base tree each record's diff was computed against (build_record's rule)."""
    trees = head_trees(repo, [r.get("git_head") for r in records])
    bases, last_tree, last_head = [], None, None
    for rec in records:
        head = rec.get("git_head")
        bases.append(trees.get(head) if (last_tree is None or last_head != head) else last_tree)
        last_tree, last_head = rec.get("tree"), head
    return bases


def classify(records: list[dict], repo: Path, verbose: bool = False) -> list[dict]:
    """One verdict per record, using regeneration wherever the objects survive."""
    bases = resolve_bases(records, repo)
    known = have_objects(repo, [r.get("tree") for r in records] + bases)
    verdicts = []
    for i, rec in enumerate(records):
        stored = rec.get("diff") or ""
        base, tree = bases[i], rec.get("tree")
        v = {"i": i, "outcome": rec.get("outcome"), "base": base, "tree": tree}
        regen = (regenerate(repo, base, tree)
                 if base in known and tree in known else None)

        # Payload integrity and capture coverage are orthogonal.  A record whose
        # snapshot never moved has a *faithful* empty payload -- the recorder
        # did its job -- and yet may still have missed the edit entirely, if the
        # file being edited was outside the snapshot's allowlist.  Regeneration
        # can only ever certify the first; the outcome flip is what detects the
        # second, so an empty payload is judged on coverage, never on bytes.
        if not stored.strip():
            if regen is not None and regen.strip():
                v["class"] = "empty-recoverable"
                v["repair"] = regen
                v["detail"] = f"{len(regen.splitlines())} lines recoverable"
            elif regen is None and base is not None and tree is not None and base != tree:
                v["class"] = "empty-lost-objects"
            else:
                prev = records[i - 1] if i else None
                oa = prev.get("outcome") if prev else None
                ob = rec.get("outcome")
                if prev and oa != ob and "timeout" not in (oa, ob):
                    v["class"] = "empty-blind"
                    v["detail"] = f"{oa} -> {ob} with an unchanged snapshot"
                else:
                    v["class"] = "empty-consistent"
        elif regen is not None:
            ns, nr = normalise(stored), normalise(regen)
            if ns == nr:
                v["class"] = "sound"
            elif nr.startswith(ns):
                # Append only what was lost, so the record keeps its original
                # abbreviations and every historically accurate byte.
                v["class"] = "truncated"
                v["repair"] = stored + nr[len(ns):]
                v["detail"] = f"{len(nr) - len(ns)} byte(s) lost from the tail"
            else:
                v["class"] = "divergent"
                v["repair"] = regen
                v["detail"] = f"stored {len(stored)}B, regenerated {len(regen)}B"
        else:
            # The objects have been pruned, so the payload can only be judged
            # against itself.  Never call that `sound`: hunk accounting cannot
            # see a lost `+`/`-` line that leaves the counts balanced.
            v["objects"] = False
            short = hunk_shortfalls(stored)
            if not short:
                v["class"] = "unverified"
            elif all(a == b for _, a, b in short) and len(short) == 1:
                v["class"] = "stripped?"
                v["detail"] = f"{short[0][1]} trailing context line(s) missing"
            else:
                v["class"] = "damaged"
                v["detail"] = "; ".join(f"{h}: -{a}/+{b}" for h, a, b in short)
        verdicts.append(v)
    return verdicts


CLEAN = ("sound", "empty-consistent")
EXACT = ("truncated", "divergent", "empty-recoverable")
UNRECOVERABLE = ("damaged", "empty-blind", "empty-lost-objects")

# `unverified` is neither clean nor broken -- the objects needed to judge it
# are gone.  It is reported separately so a corpus never looks healthier than
# the evidence supports.
UNJUDGED = ("unverified",)


# ------------------------------------------------------------------- subcommands

def cmd_check(records, repo, args) -> int:
    verdicts = classify(records, repo)
    counts = collections.Counter(v["class"] for v in verdicts)
    shown = 0
    for v in verdicts:
        if v["class"] in CLEAN:
            continue
        if shown < 40:
            print(f"  {v['i']:>4} [{v['outcome']:>7}]  {v['class']:<18} {v.get('detail','')}")
        shown += 1
    if shown > 40:
        print(f"  … and {shown - 40} more")
    print(f"\n{len(records)} records:")
    for k, n in counts.most_common():
        print(f"  {n:>5}  {k}")
    exact = sum(counts[k] for k in EXACT)
    heur = counts["stripped?"]
    lost = sum(counts[k] for k in UNRECOVERABLE)
    unjudged = sum(counts[k] for k in UNJUDGED)
    if exact:
        print(f"\n{exact} exactly repairable by regeneration: `repair --apply`.")
    if heur:
        print(f"{heur} repairable only by inference (objects gone): add `--heuristic`.")
    if unjudged:
        print(f"{unjudged} unverifiable -- structurally well-formed, but the tree "
              "objects needed to confirm the content have been pruned.")
    if counts["empty-blind"]:
        print(f"{counts['empty-blind']} empty payloads are FAITHFUL but BLIND: the "
              "snapshot did not move, yet the outcome flipped between fail and ok.\n"
              "  A prover is deterministic on identical sources, so the edit was to a "
              "file outside the\n  snapshot's allowlist.  Unrecoverable, and not a "
              "defect in the payload -- widen the allowlist.")
    if lost - counts["empty-blind"]:
        print(f"{lost - counts['empty-blind']} unrecoverable for other reasons.")
    return 0


def cmd_repair(records, repo, args) -> int:
    verdicts = classify(records, repo)
    changed = []
    for v in verdicts:
        rec = records[v["i"]]
        if v["class"] in EXACT:
            before = len(rec.get("diff") or "")
            rec["diff"] = v["repair"]
            rec["diff_repaired"] = NOTE_REGENERATED
            print(f"  {v['i']:>4} [{v['outcome']:>7}]  {v['class']:<18} "
                  f"{before} -> {len(v['repair'])} bytes")
            changed.append(v["i"])
        elif v["class"] == "stripped?" and args.heuristic:
            n = hunk_shortfalls(rec["diff"])[0][1]
            rec["diff"] = rec["diff"] + "\n" + "\n".join(" " for _ in range(n)) + "\n"
            rec["diff_repaired"] = NOTE_HEURISTIC
            print(f"  {v['i']:>4} [{v['outcome']:>7}]  stripped?          "
                  f"inferred {n} trailing context line(s)")
            changed.append(v["i"])

    if not changed:
        print("nothing to repair.")
        return 0
    if not args.apply:
        print(f"\n(dry run -- {len(changed)} records would change; pass --apply)")
        return 0

    # resolve() first: a corpus is usually a symlink into a trajectory repo, and
    # writing through the link would replace the *link* with a regular file and
    # orphan the real one.  The atomic rename below must target the real path.
    corpus_path = Path(args.corpus).resolve()

    # Corpora normally live in their own git repo, which is a better backup than
    # a .bak file -- so only fall back to one when git cannot serve as the net.
    net = git_safety_net(corpus_path)
    backup = None
    if net is None or args.backup:
        backup = corpus_path.with_suffix(".jsonl.bak")
        shutil.copy2(corpus_path, backup)

    tmp = corpus_path.with_suffix(".jsonl.new")
    with open(tmp, "w") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    tmp.replace(corpus_path)

    print(f"\nwrote {corpus_path}\n  {len(changed)} records repaired")
    if backup:
        print(f"  backup at {backup}")
    else:
        # Relative to the repo ROOT, not the corpus's own directory: a corpus
        # usually sits in a subdirectory, so the bare filename would not resolve.
        try:
            rel = corpus_path.relative_to(net)
        except ValueError:
            rel = corpus_path
        print(f"  revert with: git -C {net} checkout -- {rel}")
    return 0


def git_safety_net(corpus_path: Path) -> Path | None:
    """The repo root that can restore `corpus_path`, or None if git cannot.

    Requires the file to be both tracked *and* unmodified: a tracked file that
    is already dirty has a committed state that is not the pre-repair state, so
    reverting to it would discard whatever else is pending."""
    d = corpus_path.parent
    if git(d, ["ls-files", "--error-unmatch", corpus_path.name]).returncode != 0:
        return None
    if git(d, ["diff", "--quiet", "--", corpus_path.name]).returncode != 0:
        print(f"note: {corpus_path.name} is already modified relative to git, so the "
              "committed\n      copy is not the pre-repair state -- writing a .bak too.")
        return None
    root = git(d, ["rev-parse", "--show-toplevel"]).stdout.strip()
    return Path(root) if root else None


def cmd_replay(records, repo, args) -> int:
    """Apply each payload to its base and check the result -- an independent
    route that does not assume the payload equals a regeneration.

    Verification is per file rather than whole-tree: `git archive` honours
    `export-ignore` and `git add -A` honours `.gitignore`, so materialising a
    whole tree and re-hashing it is unreliable in a repo that uses either.
    Comparing the blob of each touched path against the recorded tree is exact
    and costs only the paths the payload actually mentions."""
    bases = resolve_bases(records, repo)
    known = have_objects(repo, [r.get("tree") for r in records] + bases)
    lo = args.start or 0
    hi = args.stop if args.stop is not None else len(records) - 1
    work = Path(subprocess.run(["mktemp", "-d"], capture_output=True,
                               text=True).stdout.strip())
    try:
        ok = bad = skipped = 0
        for i in range(lo, min(hi + 1, len(records))):
            rec, base, tree = records[i], bases[i], records[i].get("tree")
            diff = rec.get("diff") or ""
            if not diff.strip():
                continue
            if base not in known or tree not in known:
                skipped += 1
                continue
            pre, post = paths_in_diff(diff)
            for child in work.iterdir():
                shutil.rmtree(child) if child.is_dir() else child.unlink()
            for p in pre:
                blob = git(repo, ["cat-file", "blob", f"{base}:{p}"])
                if blob.returncode == 0:
                    dest = work / p
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(blob.stdout)
            patch = work / ".patch"
            patch.write_text(diff if diff.endswith("\n") else diff + "\n")
            res = subprocess.run(["git", "apply", "--whitespace=nowarn", "-p1", ".patch"],
                                 cwd=work, capture_output=True, text=True)
            patch.unlink(missing_ok=True)
            if res.returncode != 0:
                first = (res.stderr.strip().splitlines() or ["?"])[0]
                print(f"  {i:>4} [{rec.get('outcome'):>7}]: apply failed -- {first}")
                bad += 1
                continue
            mismatched = []
            for p in post:
                want = git(repo, ["rev-parse", f"{tree}:{p}"]).stdout.strip()
                f = work / p
                got = subprocess.run(["git", "hash-object", str(f)], capture_output=True,
                                     text=True).stdout.strip() if f.exists() else None
                if want != got:
                    mismatched.append(p)
            if mismatched:
                print(f"  {i:>4} [{rec.get('outcome'):>7}]: blob mismatch at "
                      + ", ".join(sorted(mismatched)))
                bad += 1
            else:
                ok += 1
        print(f"\n{ok} payloads reconstruct their recorded blobs; "
              f"{bad} failed, {skipped} skipped (objects unavailable).")
        return 1 if bad else 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


FILE_HEADER = re.compile(r"^\+\+\+ b/(.*)$")
HUNK_FULL = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
LOCI_IN_HEAD = [re.compile(r'\(line (\d+) of "([^"]+)"\)'),
                re.compile(r"line (\d+) of (\S+)")]


def theory_key(name: str) -> str:
    """Collapse the several ways a theory is named into one key.

    A compile error cites a path (`…/isabelle/SP_Slowdown.thy`); a watchdog kill
    cites a session-qualified name (`SPSlowdown.SP_Slowdown`); a diff cites a
    repo-relative path.  All three must compare equal or every transition looks
    like a cross-file edit."""
    b = Path(name).name
    return b[:-4] if b.endswith(".thy") else b.rsplit(".", 1)[-1]


def error_loci(rec: dict) -> list[tuple[str, int]]:
    """(theory, line) pairs, from `error_loci` or parsed out of `error_head`.

    Older corpora predate the `error_loci` field and carry only the first two
    `***` lines, so roughly half their failures name no location at all."""
    if rec.get("error_loci"):
        return [(theory_key(f), int(l)) for f, l in rec["error_loci"]]
    head = rec.get("error_head") or ""
    for pat in LOCI_IN_HEAD:
        found = [(theory_key(m.group(2)), int(m.group(1))) for m in pat.finditer(head)]
        if found:
            return found
    return []


def hunks_by_theory(diff: str) -> dict[str, list[tuple[int, int, int, int]]]:
    out, cur = collections.defaultdict(list), None
    for line in diff.split("\n"):
        m = FILE_HEADER.match(line)
        if m:
            cur = theory_key(m.group(1))
            continue
        m = HUNK_FULL.match(line)
        if m and cur:
            out[cur].append((int(m.group(1)), int(m.group(2) or 1),
                             int(m.group(3)), int(m.group(4) or 1)))
    return dict(out)


def map_line(hunks, ln: int) -> int | None:
    """Where old line `ln` ends up after the edit; None if the edit covers it.

    Without this correction an insertion anywhere above the error makes the
    error's line number grow, and 'the error moved later in the file' fires on
    pure drift rather than on progress."""
    delta = 0
    for old_start, old_len, new_start, new_len in sorted(hunks):
        if ln < old_start:
            break
        if old_start <= ln < old_start + old_len:
            return None
        delta = (new_start + new_len) - (old_start + old_len)
    return ln + delta


def cmd_progress(records, repo, args) -> int:
    """Classify each fail -> next-attempt transition: did the edit make progress?

    Line numbers alone cannot settle this (see INSIGHTS.md #19); the output
    separates what they *can* decide from what needs the error text."""
    tally = collections.Counter()
    for i in range(1, len(records)):
        prev, cur = records[i - 1], records[i]
        if prev.get("outcome") == "ok":
            continue                                   # a new trajectory starts
        pl = error_loci(prev)
        hunks = hunks_by_theory(cur.get("diff") or "")
        if not pl or not hunks:
            tally["unclassifiable (no locus, or no edit)"] += 1
            continue
        theory, old_line = min(pl, key=lambda x: x[1])
        old_line = min(ln for f, ln in pl if f == theory)
        if theory not in hunks:
            tally["edit was in a different theory"] += 1
            continue
        hs = hunks[theory]
        mapped = map_line(hs, old_line)

        if cur.get("outcome") == "ok":
            tally["OK  · edit covered the failing line" if mapped is None
                  else "OK  · failing line untouched (the fix was elsewhere)"] += 1
            continue
        cl = [ln for f, ln in error_loci(cur) if f == theory]
        if not cl:
            tally["error left the theory"] += 1
            continue
        new_line = min(cl)
        inside = any(ns <= new_line < ns + nl for _, _, ns, nl in hs)
        if mapped is None:
            if inside:
                # The edit rewrote the failing proof and it still fails there.
                # Whether that is partial progress or none is exactly what line
                # numbers cannot say -- fall back to the error text.
                moved = (prev.get("error_head") or "") != (cur.get("error_head") or "")
                tally["still failing in the edited region, DIFFERENT error"
                      if moved else
                      "still failing in the edited region, SAME error"] += 1
            else:
                tally["edited the failing line; error moved off it"] += 1
        elif new_line > mapped:
            tally["error advanced past the edit"] += 1
        elif new_line == mapped:
            tally["error at the same drift-corrected line"] += 1
        else:
            tally["error retreated (something earlier broke)"] += 1

    total = sum(tally.values())
    print(f"{total} transitions out of a fail:\n")
    for k, n in tally.most_common():
        print(f"  {n:>5}  {k}")
    return 0


def cmd_notes(records, repo, args) -> int:
    """Show the reasoning attached to each attempt, scored against what happened.

    The scoring is the point.  A note that predicted `ok` and got `fail` marks
    a place where the engineer's model of the system was wrong, which is
    exactly the record worth re-reading; a corpus of notes without outcomes
    attached is a diary, and diaries are not evidence."""
    noted = [(i, r) for i, r in enumerate(records) if r.get("note")]
    if not noted:
        print(f"{len(records)} records, none carry a note.\n"
              f"Write a note before building — see `make note`.")
        return 0

    scored = hits = posthoc = 0
    for i, rec in noted:
        outcome = rec.get("outcome")
        pred = rec.get("note_predicted")
        mark = ""
        if pred:
            scored += 1
            if pred == outcome:
                hits += 1
                mark = f"  predicted {pred} ✓"
            else:
                mark = f"  predicted {pred}, got {outcome}  <-- SURPRISE"
        if rec.get("note_pre_build") is False:
            posthoc += 1
            mark += "  [written after the build: not a prediction]"
        print(f"\n#{i}  {outcome}  {rec.get('elapsed_s')}s"
              f"  {rec.get('timestamp', '')}{mark}")
        fields = rec.get("note_fields") or {}
        if args.raw or not fields:
            for line in (rec.get("note") or "").strip().splitlines():
                print(f"    {line}")
        else:
            for key in ("diagnosis", "change", "expect", "ref", "notes"):
                if key in fields:
                    body = [ln.strip() for ln in fields[key].splitlines()] or [""]
                    print(f"    {key + ':':<11}{body[0]}")
                    for line in body[1:]:
                        print(f"    {'':<11}{line}")
        if args.loci:
            for thy, ln in error_loci(rec):
                print(f"      -> {thy}:{ln}")

    print(f"\n{len(noted)}/{len(records)} records carry a note.")
    if scored:
        print(f"{hits}/{scored} predictions correct "
              f"({100 * hits // scored}%); {scored - hits} surprises.")
    else:
        print("No note named an expected outcome (`expect: ok|fail|timeout`).")
    if posthoc:
        print(f"{posthoc} note(s) postdate their build and are summaries, "
              f"not predictions — excluded from nothing, but read them as such.")
    return 0


def limit_changes(prev: dict, rec: dict) -> list[str]:
    """Watchdog budgets that differ between two attempts, as 'key old->new'."""
    a, b = prev.get("limits") or {}, rec.get("limits") or {}
    if not a or not b:
        return []
    return [f"{k} {a.get(k)}->{b.get(k)}" for k in sorted(set(a) | set(b))
            if a.get(k) != b.get(k)]


def cmd_flips(records, repo, args) -> int:
    """Attribute every outcome change to something that actually changed.

    The case this exists for: a build flips ok -> timeout and the theory was
    never touched, because the Makefile halved WALL_TIMEOUT.  Recorded as
    outcome plus source diff alone, that is indistinguishable from a proof
    that got slower, and reads as a regression in the mathematics.  With the
    budgets in the record it is a one-line attribution."""
    rows = []
    for i in range(1, len(records)):
        prev, rec = records[i - 1], records[i]
        if prev.get("outcome") == rec.get("outcome"):
            continue
        causes = []
        if (rec.get("diff") or "").strip():
            thys = sorted(theory_key(p) for p in paths_in_diff(rec["diff"])[0])
            causes.append("sources: " + ", ".join(thys[:4]))
        for ch in limit_changes(prev, rec):
            causes.append("LIMIT " + ch)
        for path, add, dele in (rec.get("other_changed") or []):
            causes.append(f"non-source: {path} +{add}/-{dele}")
        if prev.get("limits") is None or rec.get("limits") is None:
            causes.append("limits not recorded for one side")
        rows.append((i, prev, rec, causes))

    if not rows:
        print(f"{len(records)} records, no outcome changes.")
        return 0

    for i, prev, rec, causes in rows:
        head = (f"#{i-1} {prev.get('outcome')} -> #{i} {rec.get('outcome')}"
                f"  ({rec.get('elapsed_s')}s")
        lim = rec.get("limits") or {}
        if rec.get("outcome") == "timeout":
            head += f", {rec.get('timeout_reason')}"
            if lim.get("wall_timeout"):
                head += f", wall {lim['wall_timeout']}s"
        print(head + ")")
        if not causes:
            print("    UNEXPLAINED — no source change, no budget change, "
                  "nothing else touched")
        for c in causes:
            print(f"    {c}")
        if rec.get("note_fields", {}) and (rec.get("note_fields") or {}).get("diagnosis"):
            print(f"    note: {rec['note_fields']['diagnosis'].splitlines()[0]}")

    unexplained = sum(1 for *_, c in rows if not c)
    blamed = sum(1 for *_, c in rows if any(x.startswith("LIMIT") for x in c))
    print(f"\n{len(rows)} outcome changes; {blamed} involve a budget change, "
          f"{unexplained} unexplained.")
    return 0


def paths_in_diff(diff: str) -> tuple[set[str], set[str]]:
    """Paths the patch reads (a/ side) and writes (b/ side)."""
    pre, post = set(), set()
    for line in diff.split("\n"):
        for marker, bucket in (("--- ", pre), ("+++ ", post)):
            if line.startswith(marker):
                p = line[len(marker):].strip()
                if p == "/dev/null":
                    continue
                bucket.add(p[2:] if p[:2] in ("a/", "b/") else p)
    return pre, post


def cmd_extract(records, repo, args) -> int:
    """Materialise the snapshot of one record, faithfully.

    Written out from `ls-tree` + `cat-file` rather than `git archive`, which
    would silently drop anything marked `export-ignore`."""
    n = int(args.n)
    tree = records[n].get("tree")
    if not tree or tree not in have_objects(repo, [tree]):
        print(f"record {n} has no surviving tree object", file=sys.stderr)
        return 1
    dest = Path(args.dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    listing = git(repo, ["ls-tree", "-r", tree]).stdout.splitlines()
    for line in listing:
        meta, path = line.split("\t", 1)
        _mode, kind, oid = meta.split()
        if kind != "blob":
            continue
        out = dest / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(subprocess.run(["git", "-C", str(repo), "cat-file", "blob", oid],
                                       capture_output=True).stdout)
    print(f"record {n} [{records[n].get('outcome')}]: {len(listing)} paths -> {dest}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, helptext in (("check", "classify every record (read-only)"),
                           ("repair", "regenerate repairable payloads"),
                           ("replay", "apply payloads and verify the resulting blobs"),
                           ("progress", "classify whether each edit made progress"),
                           ("notes", "show attached reasoning, scored against outcomes"),
                           ("flips", "attribute every outcome change to a cause"),
                           ("extract", "write one record's snapshot to a directory")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("corpus", nargs="?", default=None,
                       help="builds.jsonl to read (default: $TRAJECTORY_CORPUS, "
                            "else $WATCHDOG_LOG_DIR/builds.jsonl, else the "
                            "known layouts under the current project)")
        if name in NEEDS_REPO:
            s.add_argument("--repo", default=None,
                           help="source git repository the diffs came from "
                                "(default: the git top level of the current "
                                "directory)")
        if name == "repair":
            s.add_argument("--apply", action="store_true", help="write the corpus back")
            s.add_argument("--heuristic", action="store_true",
                           help="also infer lost context lines where objects are gone")
            s.add_argument("--backup", action="store_true",
                           help="write a .bak even when git can restore the corpus")
        if name == "replay":
            s.add_argument("--from", dest="start", type=int)
            s.add_argument("--to", dest="stop", type=int)
        if name == "notes":
            s.add_argument("--raw", action="store_true",
                           help="print notes verbatim instead of by section")
            s.add_argument("--loci", action="store_true",
                           help="also show the error loci each attempt reported")
        if name == "extract":
            s.add_argument("n", help="record index")
            s.add_argument("dest", help="destination directory")
    args = ap.parse_args()

    try:
        path = corpus.resolve(args.corpus)
        repo = (corpus.resolve_repo(getattr(args, "repo", None))
                if args.cmd in NEEDS_REPO else None)
    except corpus.CorpusError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 2

    # `repair` re-derives the path from args so it can resolve() symlinks
    # itself; hand it the resolved one rather than the None it may have been
    # given.
    args.corpus = str(path)
    records = corpus.load(path)

    return {"check": cmd_check, "repair": cmd_repair, "replay": cmd_replay,
            "progress": cmd_progress, "notes": cmd_notes, "flips": cmd_flips,
            "extract": cmd_extract}[args.cmd](records, repo, args)


if __name__ == "__main__":
    sys.exit(main())
