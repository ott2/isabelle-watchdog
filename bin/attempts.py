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
  attempts.py lengths [-n N] [--fit] [--by-project] [--csv|--json]
                                 histogram of trajectory (episode) lengths:
                                 how many 1-step, 2-step, … runs.
                                 --fit separates the two regimes — the
                                 one-shot spike and the repair tail — and
                                 scores a power law against a geometric
                                 null on the same support.  --by-project
                                 splits by development (t/ae, t/ar, t/ntr,
                                 t/art, t/base): separate results built in
                                 different eras, so their one-shot rate and
                                 tail exponent are a *dynamic* difficulty
                                 measure no static lemma count captures.
                                 --csv/--json for plotting.
  attempts.py classify BUILD_ID [-v]
                                 why a delta was judged code or doc-only
                                 (per-file verdict; -v shows the evidence)
  attempts.py size [--compare DIR...] [--json]
                                 byte accounting for a project-size
                                 breakdown that does not count the git
                                 tree.  Every record carries its diff
                                 inline (§16), so the corpus is the only
                                 copy of what it describes; the hashes
                                 beside them are pointers into a store
                                 that accounting excludes.  Compression is
                                 the only real reduction, and it is bigger
                                 than any refinement of "relevant" would be.

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
import gzip
import json
import math
import re
import subprocess
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


# ------------------------------------------------------- project attribution

# The theory tree's top-level session directories are separate developments
# — different results, different eras, in effect different projects — so
# their trajectories should not be pooled into one distribution.  Attributed
# from the *paths a trajectory's code deltas touch*, not from the build
# target, because session names have been renamed twice (NDTHT_AE ->
# Alphabet_Enlargement -> Multitape_Alphabet_Enlargement) while `t/<dir>`
# has been stable throughout.
# Hyphens and digits are in the class deliberately: `t/scratch-nae/` is a
# real session directory, and a name class that cannot match it does not
# leave those trajectories unlabelled — it drops them into 'tooling', which
# reads as "no theory touched" and is the opposite of the truth.  23 of the
# 24 t/scratch-nae runs were mislabelled that way.
_PROJECT_DIR = re.compile(r"^t/([A-Za-z0-9_-]+)/")

# Directories that were the *same development* under another name.
# Attribution is by path (see above), so a directory rename is invisible to
# it and has to be declared here.
#
# The test is whether the work **graduated** into the session, not what the
# directory was called — a "scratch" name proves nothing either way:
#
#   t/aem         NDTHT_AE_Machinery: t/ae split for build caching
#                 (6f58ba5), folded back by ee630c4.
#   t/scratch-nae the [nae-prove] reverse-arm toolkit, developed in a
#                 staging tree and graduated by 68f95df ("graduate det-free
#                 reverse-arm toolkit into NDTHT_AE").  Its lemmas are in
#                 t/ae/AlphabetEnlargement_Reverse.thy today —
#                 ae_ss5_window_ofs_agree among them.
#
# t/scratch is deliberately absent, and is the contrast that makes the rule
# concrete: NDTHT_Scratch was the [substrate-value-arity] fork-(a) *decision
# prototype*, measuring whether a 'k-typed result transfers across a
# fixed-arity isomorphism.  It answered the question and was retired as
# spent (6f4d1fd); nothing graduated, and it no longer builds.  Search that
# settled a design question is not search that built a session.
SESSION_ALIASES = {"aem": "ae", "scratch-nae": "ae"}


# An Isabelle error head carries the path of the file it failed in.  That
# survives when the diff does not, which is the only handle on an episode
# whose edits the recorder missed entirely (§13.1's untracked-theory gap).
_THY_IN_ERROR = re.compile(r"/t/([A-Za-z0-9_-]+)/[A-Za-z0-9_]+\.thy")

# The watchdog's own error heads name a theory by *base* name and give the
# line it was elaborating: `loop_progress: "by" line 190 of EncodingWrap_WF`.
# That is proof-work evidence as good as a path, but it cannot attribute:
# 11 base names have lived in more than one session directory across the
# tree's re-layouts (`AlphabetReduction` in t/generic, t/base and t/ar), and
# the record has no era to disambiguate with that this tool can read.  So it
# feeds `proof_bearing` only, and attribution falls through to the command.
# The leading `[A-Za-z]` is what keeps ML frames out: `line 308 of
# "drule.ML"` and `line 144 of "~/…/Anti_Unify.ML"` both quote the file.
_THY_BY_LINE = re.compile(r"\bline \d+ of ([A-Za-z][A-Za-z0-9_]*)")

# Session *name* -> `t/<dir>`, for the last-resort attribution route below.
# Names were renamed twice, so this is every pairing that has ever appeared
# in a committed ROOT, derived rather than remembered:
#
#   git log --all --format=%H -- 't/*/ROOT' | while read sha; do
#       git ls-tree -r --name-only $sha -- t/ | grep '/ROOT$' | while read f; do
#           dir=${f#t/}; git show $sha:$f |
#               sed -n 's/^session *"\?\([A-Za-z0-9_]*\).*/\1 '"${dir%/ROOT}"'/p'
#       done; done | sort -u
#
# It is an allowlist, and that is load-bearing: a target that is not a `t/`
# session must yield *no* attribution rather than a guess.  The HOAU spike is
# the case that makes this concrete — it built `HOAU_Spike` from
# `-d scratch/hoau` against the tree's existing sessions, so the session it
# runs against is not the work it is about.
#
# Such a target maps to **None**, not to absence.  The two behave identically
# in `command_dir` and differently in the audit, which is the point: absence
# is what a renamed or newly-added session looks like, so leaving a
# deliberate exclusion implicit makes an oversight indistinguishable from a
# decision.  `bin/audit-attribution.py` fails on the first and passes on the
# second.
SESSION_TARGETS = {
    "HOAU_Spike": None,       # built against t/ sessions; not about them
    "Alphabet_Enlargement": "ae",
    "Alphabet_Reduction": "ar",
    "Alphabet_Roundtrip": "art",
    "MTTM": "base",
    "MTTM_Examples": "ex",
    "Multitape_Alphabet_Enlargement": "ae",
    "Multitape_Alphabet_Reduction": "ar",
    "Multitape_Alphabet_Roundtrip": "art",
    "Multitape_TM_Substrate": "base",
    "NDTHT_AE": "ae",
    "NDTHT_AE_Machinery": "aem",
    "NDTHT_AR": "ar",
    "NDTHT_Base": "base",
    "NDTHT_Scratch": "scratch",
    "NDTHT_ScratchAR": "scratch",
    "NDTHT_ScratchNAE": "scratch-nae",
    "Nondeterministic_Tape_Reduction": "ntr",
    "Substrate_Characterization": "sc",
}

# `isabelle build` flags that consume the following argument, so a target
# scan does not mistake their values for session names.
_FLAG_TAKES_ARG = {"-d", "-o", "-j", "-l", "-x", "-B", "-D", "-N", "-P", "-S"}


def _alias(d: str) -> str:
    return SESSION_ALIASES.get(d, d)


def _locus_dir(theory: str) -> str | None:
    """Session dir for one `error_loci` entry, whichever form it took.

    Post-2026-07-29 records carry the loci whole (§13.2.1), in two shapes
    depending on which side reported them: a compile error gives a *path*,
    `~/…/t/ae/Wrap_Defs.thy`; a watchdog kill gives Isabelle's
    *session-qualified* name, `Alphabet_Enlargement.EncodingWrap_WF`.  The
    qualifier is the session, which is why keeping it matters — the base
    name alone is ambiguous across the tree's re-layouts.
    """
    m = _THY_IN_ERROR.search(theory)
    if m:
        return _alias(m.group(1))
    if "." in theory:
        return _alias(SESSION_TARGETS.get(theory.split(".", 1)[0]) or "") or None
    return None


def error_dirs(ep: list[dict]) -> set[str]:
    """Session dirs this episode's errors point at.

    Prefers the structured `error_loci` and falls back to scraping the
    prose head, so the two eras of record read the same way.
    """
    out = set()
    for rec in ep:
        for theory, _line in rec.get("error_loci") or []:
            if (d := _locus_dir(theory)):
                out.add(d)
        m = _THY_IN_ERROR.search(rec.get("error_head") or "")
        if m:
            out.add(_alias(m.group(1)))
    return out


def command_dir(rec: dict) -> str | None:
    """The `t/` session dir this build targeted, or None if unattributable.

    Two readings, because the corpus contains a case where they disagree and
    the command is right.  An explicit `-d t/<dir>` names the tree being
    built and wins: the ten `-d t -d t/scratch-ar NDTHT_ScratchAR` runs built
    a staging directory that was never committed, so no ROOT records it and
    `SESSION_TARGETS` maps that name to the earlier `t/scratch` incarnation.
    Failing that, the target name is mapped.

    None means *no evidence*, not 'nothing was built' — an unmapped target is
    a session outside the theory tree, which this cannot and should not
    attribute to one of ours.
    """
    cmd = rec.get("command") or []
    dirs, targets, i = [], [], 0
    while i < len(cmd):
        arg = cmd[i]
        if arg == "-d":
            dirs.append(cmd[i + 1] if i + 1 < len(cmd) else "")
            i += 2
        elif arg in _FLAG_TAKES_ARG:
            i += 2
        elif arg.startswith("-") or arg in ("isabelle", "build"):
            i += 1
        else:
            targets.append(arg)
            i += 1
    sub = {d[2:].strip("/") for d in dirs if d.startswith("t/")}
    if len(sub) == 1:
        return _alias(sub.pop())
    named = {SESSION_TARGETS[t] for t in targets if SESSION_TARGETS.get(t)}
    return _alias(named.pop()) if len(named) == 1 else None


def project(ep: list[dict]) -> str:
    """Which development a trajectory belongs to: a `t/` session dir,
    'tooling' (no theory touched), or 'mixed' (several sessions at once).

    Three routes, tried strongest first, because they are not equally good
    evidence and the weaker ones exist only to rescue records the stronger
    ones cannot see:

    1. **Diff paths** — authoritative: the files the attempt actually edited.
    2. **Error heads** — what the build failed *in*.  Weaker, since a build
       can fail in a dependency it did not edit, but it is all that survives
       an episode the recorder captured no diff for, and it recovers 23 of
       the 35 such episodes rather than losing them.
    3. **The build target** — weakest, and last for a reason: you can build
       AE while editing base, so the target says what was *run*, not what was
       worked on.  For a run with no diff and an error head naming no file —
       a bare `wall timeout (40s wall)` — it is the only signal there is, and
       12 multi-attempt trajectories sat unattributed on exactly that.
    """
    dirs, other = set(), False
    for rec in ep:
        for path, body in _split_files(rec.get("diff") or ""):
            # Per *file*, not per record.  A record is code-class if any one
            # of its files is, and taking every path from it books the doc
            # files along for the ride: one run edited `bin/isabelle-watchdog.py`
            # and `t/document/glossary.tex` together, and `t/document/` — the
            # shared LaTeX include directory, not a session — became a phantom
            # session of one trajectory.
            if classify_file(path, body)[0] != "code":
                continue
            m = _PROJECT_DIR.match(path)
            if m:
                dirs.add(_alias(m.group(1)))
            else:
                other = True
    if not dirs:
        dirs = error_dirs(ep)
    # `other` is route 1 *succeeding*: paths were recorded and none was under
    # `t/`.  The target must not override that — a build of `-d t <session>`
    # whose only captured edit is a `bin/` script is tooling work, and
    # deferring to the target would relabel 9 such runs as proof search.
    if not dirs and not other:
        dirs = {d for rec in ep if (d := command_dir(rec))}
    if len(dirs) == 1:
        return dirs.pop()
    if dirs:
        return "mixed"
    return "tooling" if other else "none"


def is_attempt(rec: dict, prev: dict | None) -> bool:
    """Does this record represent a build attempt worth counting?

    A record counts unless it is a **no-op rebuild**: a green build with no
    code delta that did not follow a failure.  Everything else did work.

      - A *failure* is an attempt whether or not its diff was captured —
        something was built and it did not compile.  Before the 2026-07-27
        capture fix a theory being authored was untracked and therefore
        invisible, so 124 failing builds recorded an empty diff; counting
        only captured deltas scored 23 multi-attempt searches as one-shot.
      - A *green after a failure* is the repair that closed the run, and is
        an attempt for the same reason even when its diff was lost (56 such).
      - A *green after a green* with nothing recorded is a re-run of an
        unchanged tree (79 such).  That, and only that, is not an attempt.
    """
    if rec["outcome"] != "ok":
        return True
    return rec_class(rec) == "code" or (prev is not None
                                        and prev["outcome"] != "ok")


def attempt_length(ep: list[dict]) -> int | None:
    """Attempts in a closed episode, or None if it is not real work.

    Returns None for a no-op rebuild — a lone green with no code delta —
    and for a doc-only run, so prose edits do not pile onto length 1.  Both
    fall out of `is_attempt` counting nothing, so there is no second rule.
    """
    prev = None
    n = 0
    for rec in ep:
        if is_attempt(rec, prev):
            n += 1
        prev = rec
    return n or None


def proof_bearing(ep: list[dict]) -> bool:
    """Did this episode work on a theory, as opposed to build furniture?

    The filter exists to keep *free greens* out of the rate: a bare
    `t/<sess>/ROOT` edit is code by `classify_file`'s deliberate
    treat-the-unknown-as-code rule, and it builds green by construction, so
    without this it enters the histogram as a one-shot success that had no
    proof to get wrong (logging-design.md §13.2).  So the test to apply is
    not "is a theory named?" but **could this trajectory have failed for a
    proof reason?** — anything that could not is what the filter is for.

    Four kinds of evidence, in descending directness:

      - a `.thy` code change, by §13.1's own code test;
      - an error head naming a theory *file* (`error_dirs`);
      - an error head naming a theory by base name and the line it was
        elaborating — `"by" line 190 of EncodingWrap_WF`.  50 records carry
        this and no path, all of them watchdog kills;
      - a **timeout**.  Build furniture cannot time out: registering a
        theory in a ROOT does not take 40 seconds, and a build the watchdog
        had to kill was demonstrably deep in elaboration.  It is also the
        one kind of evidence that cannot reintroduce the bias being guarded
        against, since a timeout is by definition not a green.
    """
    for rec in ep:
        for path, body in _split_files(rec.get("diff") or ""):
            if path.endswith(".thy") and _thy_code_change(_hunks(body)):
                return True
    if error_dirs(ep):
        return True
    return any(rec.get("error_loci")
               or rec["outcome"] == "timeout"
               or _THY_BY_LINE.search(rec.get("error_head") or "")
               for rec in ep)


# ------------------------------------------------------------------ fitting

def _ks(sample: list[int], cdf) -> float:
    """Kolmogorov-Smirnov distance between a sample and a fitted CDF."""
    xs = sorted(sample)
    n = len(xs)
    return max(max(abs((i + 1) / n - cdf(x)), abs(cdf(x) - i / n))
               for i, x in enumerate(xs))


def fit(lengths: list[int]) -> dict | None:
    """Two-regime fit of the trajectory-length distribution.

    Regime 1 is the one-shot spike (k == 1): routine edits that go green
    first time.  Regime 2 is the repair tail (k >= 2), where the question
    is *which* law it follows, because that is the part that reflects proof
    difficulty rather than task mix.

    Two candidate laws are fitted to the same tail and compared on the same
    statistic (KS distance), rather than asserting one:

      - **geometric** — each attempt independently succeeds with a fixed
        probability p.  The null: repair is memoryless, one difficulty.
      - **power law** — no characteristic scale; a few proofs are
        qualitatively harder.  Discrete MLE (Clauset et al.)
        alpha = 1 + n / sum ln(x / (xmin - 0.5)), with xmin chosen to
        minimise KS as in that method.

    A heavy tail shows up as the power law winning on KS *and* a large
    observed/expected excess far out in the tail, where the two laws differ
    most.  Returns None if the tail is too small to say anything.
    """
    tail = [x for x in lengths if x >= 2]
    if len(tail) < 20:
        return None
    n = len(tail)

    # Geometric fitted to the tail, shifted to start at 1.
    p = 1 / (sum(x - 1 for x in tail) / n)
    ks_geom = _ks(tail, lambda x: 1 - (1 - p) ** (x - 1))

    # Power law: scan xmin, keep the KS-minimising fit (Clauset's rule).
    best = None
    for xmin in sorted({x for x in tail if x >= 2}):
        sub = [x for x in tail if x >= xmin]
        if len(sub) < 15:
            break
        denom = sum(math.log(x / (xmin - 0.5)) for x in sub)
        if denom <= 0:
            continue
        alpha = 1 + len(sub) / denom
        d = _ks(sub, lambda x, a=alpha, m=xmin: 1 - (x / (m - 0.5)) ** (1 - a))
        if best is None or d < best["ks"]:
            best = {"xmin": xmin, "alpha": alpha, "ks": d, "n": len(sub)}
    if best is None:
        return None

    # Head-to-head: refit the geometric on the *same* support the power law
    # was scored on.  Comparing a KS from n=21 against one from n=160 would
    # be meaningless — the statistic depends on the sample it is computed on.
    sub = [x for x in tail if x >= best["xmin"]]
    p_sub = 1 / (sum(x - best["xmin"] + 1 for x in sub) / len(sub))
    ks_geom_sub = _ks(sub, lambda x, m=best["xmin"]:
                      1 - (1 - p_sub) ** (x - m + 1))

    # Where the laws disagree most: the far tail, against the whole-tail
    # geometric (the null for the repair process as a whole).
    probe = max(10, best["xmin"] * 4)
    observed = sum(1 for x in tail if x >= probe)
    expected = n * (1 - p) ** (probe - 1)
    return {
        "one_shot": sum(1 for x in lengths if x == 1),
        "total": len(lengths),
        "tail_n": n,
        "geometric_p": p,
        "geometric_ks": ks_geom,
        "power_law": best,
        "geometric_same_support": {"p": p_sub, "ks": ks_geom_sub,
                                   "n": len(sub)},
        "probe": probe,
        "probe_observed": observed,
        "probe_expected": expected,
    }


def _print_fit(f: dict, indent: str = "  ") -> None:
    pl = f["power_law"]
    pct = 100 * f["one_shot"] / f["total"]
    print(f"\n{indent}fit — two regimes")
    print(f"{indent}  regime 1  one-shot      {f['one_shot']}/{f['total']} "
          f"({pct:.1f}%) — routine edits, green first time")
    print(f"{indent}  regime 2  repair tail   {f['tail_n']} trajectories (k >= 2)")
    gs = f["geometric_same_support"]
    print(f"{indent}    geometric, whole tail   p={f['geometric_p']:.3f}"
          f"      KS={f['geometric_ks']:.3f}  n={f['tail_n']}")
    print(f"{indent}    head-to-head on k >= {pl['xmin']} (n={pl['n']}):")
    print(f"{indent}      power law   alpha={pl['alpha']:.2f}   KS={pl['ks']:.3f}")
    print(f"{indent}      geometric   p={gs['p']:.3f}       KS={gs['ks']:.3f}")
    exp = f["probe_expected"]
    ratio = f"{f['probe_observed'] / exp:.0f}x" if exp >= 0.05 else ">100x"
    print(f"{indent}    far tail    k>={f['probe']}: {f['probe_observed']} observed "
          f"vs {exp:.1f} expected under geometric ({ratio})")
    better = "power law" if pl["ks"] < gs["ks"] else "geometric"
    print(f"{indent}    lower KS on the same support: {better}")


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
    run with no closing ok is returned as an open episode.

    Segmentation is chronological, **not** per working copy, even on a
    pooled log (several instances unioned by concatenation,
    logging-design.md §16.5).  That is deliberate: worktrees are used
    sequentially here, so an unfinished run on one instance genuinely
    continues on the next — in the main→stac/wip pool the handoff shows the
    same session failing at the same `by` line on either side of the seam,
    one repair, two working copies.  Splitting by instance would cut that
    trajectory in half.

    The assumption that fails is *concurrent* instances, whose records
    interleave; then a run really would splice unrelated work.
    `interleaving(recs)` measures it, and the views warn when it is
    non-zero.
    """
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


def interleaving(recs: list[dict]) -> tuple[int, int]:
    """(instances, interleave excess) for a possibly-pooled log.

    Sequential handoff between n instances costs exactly n-1 switches
    between consecutive records; anything beyond that is genuine
    concurrency, which chronological episode segmentation cannot model.
    """
    ids = [r.get("instance_id") for r in recs]
    switches = sum(1 for a, b in zip(ids, ids[1:]) if a != b)
    return len(set(ids)), max(0, switches - (len(set(ids)) - 1))


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


def cmd_lengths_by_project(pairs: list[tuple[int, str]], label: str,
                           do_fit: bool, fmt: str = "text") -> None:
    """Per-development breakdown.  `t/ae`, `t/ar`, `t/ntr` and `t/art` are
    separate results built in different eras; pooling their trajectories
    into one distribution averages over four different problems.  Reported
    separately, the one-shot rate and the tail exponent become a *dynamic*
    measure of how hard each development actually was — something no static
    count of lemmas or proof lines captures."""
    groups: dict[str, list[int]] = {}
    for k, proj in pairs:
        groups.setdefault(proj, []).append(k)

    if fmt == "csv":
        print("project,length,trajectories")
        for proj in sorted(groups):
            hist: dict[int, int] = {}
            for k in groups[proj]:
                hist[k] = hist.get(k, 0) + 1
            for k in sorted(hist):
                print(f"{proj},{k},{hist[k]}")
        return
    if fmt == "json":
        out = {}
        for proj, L in groups.items():
            hist = {}
            for k in L:
                hist[str(k)] = hist.get(str(k), 0) + 1
            entry = {"histogram": {k: hist[k] for k in sorted(hist, key=int)},
                     "trajectories": len(L), "steps": sum(L),
                     "one_shot": sum(1 for x in L if x == 1), "max": max(L)}
            if do_fit:
                entry["fit"] = fit(L)
            out[proj] = entry
        print(json.dumps({"metric": label, "projects": out}, indent=1))
        return

    print(f"trajectory lengths by development — {label}\n")
    print(f"  {'project':10} {'traj':>5} {'steps':>6} {'1-shot':>7} "
          f"{'mean':>5} {'max':>4}  {'tail alpha':>10}")
    for proj in sorted(groups, key=lambda p: -len(groups[p])):
        L = groups[proj]
        one = sum(1 for x in L if x == 1)
        f = fit(L)
        alpha = f"{f['power_law']['alpha']:.2f}" if f else "-"
        print(f"  {proj:10} {len(L):5d} {sum(L):6d} "
              f"{100 * one / len(L):6.1f}% {sum(L) / len(L):5.2f} "
              f"{max(L):4d}  {alpha:>10}")
    print("\n  alpha is fitted on the k >= 2 repair tail; '-' = tail too "
          "small to fit")
    if do_fit:
        for proj in sorted(groups, key=lambda p: -len(groups[p])):
            f = fit(groups[proj])
            if f:
                print(f"\n  --- {proj} ---")
                _print_fit(f, indent="  ")


# ------------------------------------------------------------------- size

# Hashes into the git object store.  They look like data and cost real
# bytes, but they carry nothing on their own: resolving one needs the
# repository, and a corpus sized *without* counting the repository cannot
# claim their referents as content it holds.  Reported separately so the
# distinction is visible rather than assumed.
POINTER_FIELDS = ("git_head", "snapshot", "parent_snapshot", "tree")

# Present only here at any price: no git object, however reachable, records
# why a build failed.
ERROR_FIELDS = ("error_head", "error_loci", "timeout_reason")


def _field_bytes(rec: dict) -> dict[str, int]:
    """Serialised cost of each field, key and JSON escaping included.

    Measured by re-encoding rather than slicing the source line: a field's
    cost is its own encoding plus its key, not its offset in someone else's
    byte layout.  The per-field total therefore slightly exceeds the raw
    string lengths -- a diff's newlines cost two bytes each once escaped.
    """
    return {k: len(json.dumps(k)) + len(json.dumps(v)) + 2 for k, v in rec.items()}


def _gz(payload: str) -> int:
    """Compressed size -- the stand-in for information content.

    The one honest reduction available here.  Successive attempts on one
    lemma re-emit the same context lines, and no definition of "relevant"
    measures that redundancy as directly as compressing it does.
    """
    return len(gzip.compress(payload.encode(), compresslevel=6)) if payload else 0


def _tracked_thy_bytes(root: Path) -> int:
    """Bytes of git-tracked .thy under *root*, for scale comparison.

    Tracking is what separates a project's own theories from a vendored AFP
    checkout beside them: ndthtf carries 41 vendored theories against its
    own 22, so an unfiltered walk overstates it tenfold.
    """
    try:
        out = subprocess.run(["git", "ls-files", "-z", "*.thy"], cwd=root,
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return 0
    total = 0
    for rel in out.split("\0"):
        if rel:
            try:
                total += (root / rel).stat().st_size
            except OSError:
                pass
    return total


def size_report(recs: list[dict], path: Path) -> dict:
    """Byte accounting for the corpus, on its own terms.

    The corpus is self-contained by construction (logging-design.md §16):
    every record carries its incremental diff inline, which is what lets
    this tool run against any builds.jsonl with no object store present.
    That property is also the accounting rule -- when the git tree is not
    itself being counted as project size, these diffs are the only copy of
    the content they describe, and all of them count.  The kept/discarded
    split below is descriptive, not a discount: it says how much of the
    record is search and how much is result.
    """
    per_field: dict[str, int] = {}
    outcomes: dict[str, int] = {}
    kept, discarded, errors = [], [], []
    doc_only = 0

    for rec in recs:
        for key, value in _field_bytes(rec).items():
            per_field[key] = per_field.get(key, 0) + value
        outcome = rec.get("outcome") or "?"
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        diff = rec.get("diff") or ""
        (kept if outcome == "ok" else discarded).append(diff)
        if diff and rec_class(rec) == "doc":
            doc_only += len(diff)
        for key in ERROR_FIELDS:
            if rec.get(key):
                errors.append(str(rec[key]))

    gross = path.stat().st_size if path.exists() else sum(per_field.values())
    all_diffs = kept + discarded
    return {
        "path": str(path),
        "records": len(recs),
        "outcomes": outcomes,
        "per_field": per_field,
        "gross": gross,
        "gross_gz": _gz(path.read_text(errors="replace")) if path.exists() else 0,
        "diff_raw": sum(len(d) for d in all_diffs),
        "diff_gz": _gz("\n".join(all_diffs)),
        "kept_raw": sum(len(d) for d in kept),
        "discarded_raw": sum(len(d) for d in discarded),
        "doc_only_raw": doc_only,
        "pointer_bytes": sum(per_field.get(k, 0) for k in POINTER_FIELDS),
        "error_bytes": sum(per_field.get(k, 0) for k in ERROR_FIELDS),
        "error_gz": _gz("\n".join(errors)),
    }


def _mb(n: int) -> str:
    return f"{n / 1_000_000:6.2f} MB"


def cmd_size(recs: list[dict], path: Path, compare: list[str], fmt: str) -> None:
    """What this corpus contributes to a project-size breakdown.

    Answers "how big is the trajectory data" for an accounting that does not
    count the git tree.  Under that rule the whole corpus counts: the diffs
    are inline and self-contained, and the hashes beside them are pointers
    into a store the accounting excludes, so nothing here is double-counted
    against the theory tree.  Compression is the only real reduction, and it
    is a large one -- large enough that refining what "relevant" means moves
    the answer far less than gzip already does.
    """
    s = size_report(recs, path)
    if fmt == "json":
        s["compare"] = {d: _tracked_thy_bytes(Path(d)) for d in compare}
        print(json.dumps(s, indent=1))
        return

    gross = s["gross"]
    pct = lambda v: f"{100 * v / gross:5.1f}%" if gross else "    -"
    print(f"{s['path']} — {s['records']:,} records, "
          + ", ".join(f"{k} {v:,}" for k, v in sorted(s["outcomes"].items())))

    print("\n  field                  bytes     share")
    ranked = sorted(s["per_field"].items(), key=lambda kv: -kv[1])
    for key, value in ranked[:5]:
        tag = "  (pointer)" if key in POINTER_FIELDS else ""
        print(f"    {key:<18} {_mb(value)}   {pct(value)}{tag}")
    rest = gross - sum(v for _, v in ranked[:5])
    print(f"    {'(rest)':<18} {_mb(rest)}   {pct(rest)}")

    print("\n  what it is             bytes     share")
    print(f"    gross              {_mb(gross)}   {pct(gross)}")
    print(f"    gzipped            {_mb(s['gross_gz'])}   {pct(s['gross_gz'])}"
          f"   <- the irreducible figure")
    print(f"    pointers (inert)   {_mb(s['pointer_bytes'])}   "
          f"{pct(s['pointer_bytes'])}   <- hashes; need the object store")

    print("\n  of the diffs           bytes     share")
    for label, value in (("kept (built green)", s["kept_raw"]),
                         ("discarded (search)", s["discarded_raw"]),
                         ("doc-only deltas", s["doc_only_raw"])):
        print(f"    {label:<18} {_mb(value)}   {pct(value)}")
    print(f"    {'gzipped':<18} {_mb(s['diff_gz'])}   {pct(s['diff_gz'])}")

    print("\n  errors                 bytes     share")
    print(f"    {'raw':<18} {_mb(s['error_bytes'])}   {pct(s['error_bytes'])}")
    print(f"    {'gzipped':<18} {_mb(s['error_gz'])}   {pct(s['error_gz'])}")
    print("    the highest-value-density field and the one set to grow; "
          "watch this\n    row against the gzipped diff figure above")

    for directory in compare:
        root = Path(directory)
        if root.exists():
            print(f"\n  {directory + ' .thy':<20} {_mb(_tracked_thy_bytes(root))}"
                  f"   (tracked only)")


def cmd_lengths(recs: list[dict], n: int, include_all: bool, fmt: str,
                do_fit: bool = False, by_project: bool = False) -> None:
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
        pairs = [(len(ep), project(ep)) for ep in episodes]
        label = "attempts per trajectory, all deltas"
    else:
        pairs = [(c, project(ep)) for ep in episodes
                 if (c := sum(1 for r in ep if rec_class(r) == "code"))]
        label = "code-changing attempts per trajectory"
    lengths = [k for k, _ in pairs]

    if by_project:
        cmd_lengths_by_project(pairs, label, do_fit, fmt)
        return

    hist: dict[int, int] = {}
    for length in lengths:
        hist[length] = hist.get(length, 0) + 1

    if fmt == "csv":
        print("length,trajectories")
        for k in sorted(hist):
            print(f"{k},{hist[k]}")
        return
    if fmt == "json":
        out = {"metric": label,
               "histogram": {str(k): hist[k] for k in sorted(hist)},
               "trajectories": len(lengths),
               "steps": sum(lengths)}
        if do_fit:
            out["fit"] = fit(lengths)
        print(json.dumps(out, indent=1))
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
    # A zero-byte delta right after a failure is a claim about the recorder,
    # not the attempt: capture staged tracked files only until 2026-07-27, so
    # a new theory's whole development recorded as empty diffs
    # (logging-design.md §13.1).  Such trajectories under-report their length.
    blind = sum(1 for ep in episodes
                if any(rec_class(b) == "none" and a["outcome"] != "ok"
                       for a, b in zip(ep, ep[1:])))
    if blind:
        print(f"  {blind} trajectories contain a zero-byte delta after a "
              f"failure — length under-reported (capture blind spot, §13.1)")
    instances, excess = interleaving(recs)
    if instances > 1:
        note = (f", {excess} interleaved switches — episodes may splice "
                f"concurrent work" if excess else ", used sequentially")
        print(f"  pooled log: {instances} working copies{note}")
    print("  --csv / --json for plotting")
    if do_fit:
        f = fit(lengths)
        if f:
            _print_fit(f)
        else:
            print("\n  fit: repair tail too small to fit")


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
    pg.add_argument("--fit", action="store_true",
                    help="fit the two regimes: one-shot spike + repair tail, "
                         "power law vs geometric on the same KS statistic")
    pg.add_argument("--by-project", action="store_true",
                    help="split by development (t/ae, t/ar, t/ntr, t/art, "
                         "t/base) — they are separate results, not one pool")

    pz = sub.add_parser("size", parents=[common],
                        help="byte accounting: what this corpus adds to a "
                             "project-size breakdown")
    pz.add_argument("--compare", nargs="*", default=[], metavar="DIR",
                    help="also size the git-tracked .thy under these trees")
    pz.add_argument("--json", action="store_true", help="emit JSON")

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
    elif ns.cmd == "size":
        cmd_size(recs, path, ns.compare, "json" if ns.json else "text")
    elif ns.cmd == "lengths":
        cmd_lengths(recs, ns.n, include_all,
                    "csv" if ns.csv else "json" if ns.json else "text",
                    do_fit=ns.fit, by_project=ns.by_project)
    else:  # list (default)
        cmd_list(recs, getattr(ns, "n", 30), include_all)
    return 0


if __name__ == "__main__":
    sys.exit(main())
