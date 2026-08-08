#!/usr/bin/env python3
"""attempts.py — the reading and measuring views over a trajectory corpus.

An implementation module, not a command: `bin/trajectory.py` is the entry
point for all thirteen views, and `list` / `show` / `episodes` / `classify` /
`lengths` / `size` are the ones defined here.  They were a separate script
until the two frontends were reconciled; the split had no meaning for a
caller, who only ever wanted a verb.

Each record carries its own incremental diff inline, so nothing here needs a
git object store -- these views run against any builds.jsonl, including a
pooled or archived one whose source repository is long gone.  That property is
worth protecting: it is what makes a corpus readable by someone who has the
data and not the project.

The interesting piece is `lengths --fit`, which separates the two regimes of
the trajectory histogram (the one-shot spike and the repair tail) and scores a
power law against a geometric null on the same support, and `--by-project`,
which splits by development rather than pooling -- separate results built in
different eras, so their one-shot rate and tail exponent are a *dynamic*
difficulty measure no static lemma count captures.

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
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

from . import corpus
from . import roots

# This module was kept deliberately import-free while it lived in an
# application repo, so that it could survive being split out.  It has now
# been split out, and `corpus` travels with it; the property that still
# matters -- reading any log with no git object store -- is unaffected.

# outcome -> terminal-glyph (kept ASCII; the dataset itself is the point)
MARK = {"ok": "OK  ", "fail": "FAIL", "timeout": "TIME"}

# delta class -> terminal-glyph
CLASS_MARK = {"code": "code", "doc": "doc ", "none": "--  "}


# ---------------------------------------------------------------- loading

def _trunc(s: str, n: int) -> str:
    return s if s is None or len(s) <= n else s[: n - 1] + "…"


def _tail(seq: list, n: int) -> list:
    """Last n items; n == 0 (or None) means all."""
    return seq if not n else seq[-n:]


# ------------------------------------------------- diff parsing / classing

# Files whose content is prose by definition — no code projection needed.
PROSE_SUFFIXES = {".md", ".tex", ".bib", ".txt", ".rst", ".bst", ".cls"}

# Isabelle document commands: everything from here to the closing cartouche
# is prose, not proof.  ndtht's bin/prose-token.py has a sibling scanner over
# the same vocabulary, but it cannot be reused: it computes prose spans over a
# *whole file* from offset 0, whereas a diff hunk starts mid-file with an
# unknown state that has to be seeded and can be wrong (insights/274).
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

# A project's theory tree is often several *separate developments* — different
# results, different eras, in effect different projects — and pooling their
# trajectories into one distribution averages over the thing you wanted to
# measure.  So each trajectory is attributed to the development it belongs to.
#
# What a development *is*, mechanically, is a **session directory**: the
# directory a `.thy` file lives in.  That is the one structural fact every
# Isabelle project shares, and it is derivable from the corpus rather than
# declared: the session directories are exactly the directories in which any
# `.thy` appears.  In ndtht that yields `t/{ae,ar,art,base,ntr}`; in 43sp,
# `isabelle/`.  Both are right, and neither had to be configured.
#
# This used to be `re.compile(r"^t/([A-Za-z0-9_-]+)/")` — ndtht's layout, in
# a tool that is supposed to read any project's corpus.  Anywhere else it
# matched nothing, and the consequence was not "unlabelled" but *wrong*:
# every trajectory fell through to 'tooling', which reads as "no theory was
# touched" and is the opposite of the truth.  The whole of the 43sp corpus
# was labelled that way.
#
# Deriving the set also *bounds* attribution, which the `t/` prefix used to
# do.  Without a bound, "the parent directory of a changed code file" would
# make a session out of `bin/`; with it, only directories that actually hold
# theories can name a development.


def _is_theory(path: str) -> bool:
    return path.endswith(".thy")


# `diff --git a/OLD b/NEW` with OLD != NEW is a rename: the recorder computes
# every payload with `git diff -M`, so a theory that moves between directories
# says so in the corpus rather than having to be remembered.
_DIFF_HEADER = re.compile(r"^diff --git a/(.*?) b/(.*)$", re.M)


def _theory_moves(diff: str) -> list[tuple[str, str]]:
    """(old dir, new dir) for every theory that changed directory."""
    out = []
    for old, new in _DIFF_HEADER.findall(diff or ""):
        if old == new or not (_is_theory(old) and _is_theory(new)):
            continue
        od, nd = str(PurePosixPath(old).parent), str(PurePosixPath(new).parent)
        if od != nd:
            out.append((od, nd))
    return out


def session_dirs(recs: list[dict]) -> set[str]:
    """The directories that hold a theory anywhere in this corpus.

    Derived, not declared.  A directory earns the name by containing proof
    source at some point in the corpus's history, so a session that was
    renamed or moved is picked up under both names without anyone recording
    the rename — which is exactly the bookkeeping the old hard-coded map
    existed to do, and kept getting wrong.
    """
    out = set()
    for rec in recs:
        for path, _body in _split_files(rec.get("diff") or ""):
            if _is_theory(path):
                out.add(str(PurePosixPath(path).parent))
    return out


def _label(dirpath: str) -> str:
    """Short name for a session directory: its last component.

    `t/ae` -> `ae`, `isabelle` -> `isabelle`.  Two directories can share a
    last component (`a/src` and `b/src`); `Attribution.learn` detects that
    and keeps the full path for the colliding ones rather than merging two
    developments into one line of a table.
    """
    return PurePosixPath(dirpath).name or dirpath


# `isabelle build` flags that consume the following argument, so a target
# scan does not mistake their values for session names.
_FLAG_TAKES_ARG = {"-d", "-o", "-j", "-l", "-x", "-B", "-D", "-N", "-P", "-S"}

# A theory path inside an Isabelle error head or locus.  Unanchored and
# layout-free: it finds the `.thy` and lets the directory rule do the rest,
# where the old `/t/([A-Za-z0-9_-]+)/[A-Za-z0-9_]+\.thy` could only see one
# project's tree.
_THY_PATH_IN_TEXT = re.compile(r"([^\s\"']*/)?([A-Za-z0-9_.\-]+\.thy)")

# The watchdog's own error heads name a theory by *base* name and give the
# line it was elaborating: `loop_progress: "by" line 190 of EncodingWrap_WF`.
# That is proof-work evidence as good as a path, but it cannot attribute: a
# base name can live in more than one session directory across a tree's
# re-layouts (`AlphabetReduction` in t/generic, t/base and t/ar), and the
# record has no era to disambiguate with that this tool can read.  So it
# feeds `proof_bearing` only, and attribution falls through to the command.
# The leading `[A-Za-z]` is what keeps ML frames out: `line 308 of
# "drule.ML"` and `line 144 of "~/…/Anti_Unify.ML"` both quote the file.
_THY_BY_LINE = re.compile(r"\bline \d+ of ([A-Za-z][A-Za-z0-9_]*)")

# How much more often a target must co-occur with one session directory than
# with any other before the pairing is believed.  You can build session X
# while editing session Y, so the signal is noisy but very lopsided in
# practice: in ndtht the true pairings win 286-to-1, 69-to-0 and 12-to-1.  A
# near-tie is not weak evidence, it is *absence* of evidence, and must not
# produce a label.
_TARGET_DOMINANCE = 3

# `session <Name>` at the head of a ROOT stanza, via the grammar `build.py`
# uses -- a private copy here spelt names differently, so a session declared
# `"Probe (AFP)"` was built under that name and attributed under `Probe`.
#
# Line-wise, deliberately: this reads *fragments of a captured diff*, where
# an enclosing `(*` may never have been in the payload, so a commented-out
# declaration can map a name to a directory.  That costs an unused entry;
# refusing to match without whole-file context would cost the mapping.
_root_session = roots.session_in_line


class Attribution:
    """How this corpus's trajectories map to developments.

    Fitted from the records, because every fact it needs is in them.  What
    cannot be derived — a directory rename, a session that is deliberately
    *not* one of the project's developments — is supplied as an override,
    and belongs to the project rather than to this tool: see `learn`.
    """

    def __init__(self, dirs: set[str], targets: dict, aliases: dict,
                 collisions: set[str] | None = None):
        self.dirs = dirs
        self.targets = targets          # build target -> session dir, or None
        self.aliases = aliases          # session dir label -> label
        self.collisions = collisions or set()

    # ---------------------------------------------------------------- fitting

    @classmethod
    def learn(cls, recs: list[dict], overrides: dict | None = None
              ) -> "Attribution":
        """Fit from the corpus, then apply the project's own declarations.

        `overrides` is a project's attribution facts, the ones no corpus can
        show: `{"aliases": {"aem": "ae"}, "targets": {"HOAU_Spike": null}}`.
        A target mapped to **null** is a declared exclusion — a session the
        project builds *against* rather than works *on* — and is distinct
        from a target that is merely absent, which is what an unmapped or
        newly-renamed session looks like.  Keeping the two distinguishable is
        why the exclusion is written down rather than left out.

        Overrides are loaded from a JSON file beside the corpus, so they live
        with the data they describe instead of in this package; see
        `load_overrides`.
        """
        overrides = overrides or {}
        dirs = session_dirs(recs)

        # Two directories sharing a last component would collapse into one
        # label, silently merging two developments.  Keep the full path for
        # those, which is ugly and correct, rather than short and wrong.
        seen: dict[str, list[str]] = {}
        for d in dirs:
            seen.setdefault(_label(d), []).append(d)
        collisions = {d for group in seen.values() if len(group) > 1
                      for d in group}

        aliases, moved = cls._learn_aliases(recs)
        # A directory that a theory moved *out of* was a session directory
        # too, even if nothing in the corpus window shows a theory sitting
        # there; otherwise the records from before the move attribute to
        # nothing.
        dirs |= moved
        aliases.update(overrides.get("aliases") or {})

        targets = cls._learn_targets(recs, dirs)
        targets.update(overrides.get("targets") or {})
        return cls(dirs, targets, aliases, collisions)

    @staticmethod
    def _learn_aliases(recs: list[dict]) -> tuple[dict, set]:
        """Directories that are the same development, from theories moving.

        A session that exists only to make builds faster -- ndtht split the
        settled part of `t/ae` into `t/aem` so the part being worked on
        re-checked quickly, then merged it back -- is not a second
        development, and counting it as one splits a single trajectory in
        two.  Naming such sessions in a config file is a kludge, as it should
        be: the split and the merge are *edits*, and the recorder captures
        edits with `git diff -M`, so both show up as theories changing
        directory.

        Directories connected by theory moves are one development.  Its name
        is wherever the theories ended up -- the last move wins -- so a split
        followed by a merge back resolves to the original, and a split that
        is still live resolves to the new home.

        Untested against real data: neither corpus contains a move (ndtht's
        `aem` predates its corpus window entirely), so this is exercised only
        by the synthetic cases in tests/.  It is here because the alternative
        is a hand-maintained list, which is the thing being removed.
        """
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        pairs = [(od, nd) for rec in recs
                 for od, nd in _theory_moves(rec.get("diff") or "")]
        for od, nd in pairs:
            ro, rn = find(od), find(nd)
            if ro != rn:
                parent[ro] = rn

        # Records are chronological, so the last destination named for a group
        # is where that development currently lives.
        latest: dict[str, str] = {}
        for _od, nd in pairs:
            latest[find(nd)] = nd

        moved = {d for pair in pairs for d in pair}
        out = {}
        for d in moved:
            dest = latest.get(find(d))
            if dest and _label(d) != _label(dest):
                out[_label(d)] = _label(dest)
        return out, moved

    @staticmethod
    def _learn_targets(recs: list[dict], dirs: set[str]) -> dict:
        """Build target -> session directory, from two signals in the corpus.

        1. **ROOT declarations** — authoritative.  `session X = ...` inside
           `<dir>/ROOT` is Isabelle's own statement that X lives in `<dir>`;
           nothing infers better than that, and the recorder already captures
           ROOT files because they are on the source allowlist.  This is the
           same fact ndtht's hand-maintained table was periodically
           re-derived from git to obtain.
        2. **Co-occurrence** — inference, for sessions whose ROOT was never
           touched inside the corpus window.  A record that edits exactly one
           session directory and names a target teaches one pairing.  Noisy
           (you can build X while editing Y) but very lopsided: in ndtht the
           true pairings win 286-to-1, 69-to-0 and 12-to-1, so a near-tie is
           treated as absence of evidence rather than weak evidence.

        Both are needed.  Co-occurrence alone misses a target that only ever
        appears on builds with no captured diff -- ndtht's
        `Multitape_Alphabet_Reduction` is exactly that, and losing it dropped
        two trajectories from `ar` to unattributed.
        """
        out = {}

        # (1) ROOT declarations.  Context and added lines both count: either
        # way the file at that path declares that session.
        for rec in recs:
            for path, body in _split_files(rec.get("diff") or ""):
                if PurePosixPath(path).name not in ("ROOT", "ROOTS"):
                    continue
                d = str(PurePosixPath(path).parent)
                if d not in dirs:
                    continue
                for line in body:
                    if line[:1] not in ("+", " "):
                        continue
                    name = _root_session(line[1:])
                    if name:
                        out[name] = d

        # (2) Co-occurrence, for whatever route 1 could not see.
        tally: dict[str, dict[str, int]] = {}
        for rec in recs:
            touched = {str(PurePosixPath(p).parent)
                       for p, _ in _split_files(rec.get("diff") or "")
                       if _is_theory(p)}
            touched &= dirs
            if len(touched) != 1:
                continue
            d = next(iter(touched))
            for t in _targets_of(rec):
                if t in out:
                    continue
                tally.setdefault(t, {})
                tally[t][d] = tally[t].get(d, 0) + 1

        for t, counts in tally.items():
            ranked = sorted(counts.items(), key=lambda kv: -kv[1])
            best, n = ranked[0]
            runner = ranked[1][1] if len(ranked) > 1 else 0
            if n >= max(1, runner * _TARGET_DOMINANCE):
                out[t] = best
        return out

    # --------------------------------------------------------------- labelling

    def label(self, dirpath: str) -> str:
        short = dirpath if dirpath in self.collisions else _label(dirpath)
        return self.aliases.get(short, short)

    def path_dir(self, path: str) -> str | None:
        """Session directory for a changed source path, or None if outside.

        Bounded by the derived set, which is what keeps `bin/` and
        `document/` from becoming developments: a directory that has never
        held a theory cannot name one.
        """
        d = str(PurePosixPath(path).parent)
        return self.label(d) if d in self.dirs else None

    def locus_dir(self, theory: str) -> str | None:
        """Session dir for one `error_loci` entry, whichever form it took.

        Post-2026-07-29 records carry the loci whole (§13.2.1), in two shapes
        depending on which side reported them: a compile error gives a *path*,
        `~/…/t/ae/Wrap_Defs.thy`; a watchdog kill gives Isabelle's
        *session-qualified* name, `Alphabet_Enlargement.EncodingWrap_WF`.  The
        qualifier is the session, which is why keeping it matters — the base
        name alone is ambiguous across a tree's re-layouts.
        """
        m = _THY_PATH_IN_TEXT.search(theory)
        if m and m.group(1):
            d = str(PurePosixPath(m.group(1).rstrip("/")))
            # An error path is absolute, or relative to somewhere else; match
            # it against the corpus's directories by suffix.
            for known in self.dirs:
                if d == known or d.endswith("/" + known):
                    return self.label(known)
            return None
        if "." in theory:
            got = self.targets.get(theory.split(".", 1)[0])
            return self.label(got) if got else None
        return None

    def loaded_outside(self, rec: dict) -> bool:
        """Did this build load its sessions from outside the project's tree?

        A session is only this project's work if it lives in the project.  A
        build that says `-d scratch/hoau` and names `HOAU_Spike` is building
        something defined elsewhere -- typically a spike or a downstream
        project scoped against these theories for convenience -- and its
        trajectory is not a trajectory of this development, however much of
        this development it happens to import.

        ndtht's HOAU_Spike is exactly that, and it used to need a declaration
        (`{"targets": {"HOAU_Spike": null}}`) because nothing looked at where
        the session came from.  Its eight builds all pass `-d scratch/hoau`,
        which reaches no session directory of this corpus, while every
        in-project build passes `-d t`, which reaches all five.  The
        distinction was in the data the whole time.

        A build with no `-d` at all says nothing either way -- the session is
        being found some other way -- so this returns False rather than
        guessing.
        """
        dirs = _load_dirs(rec)
        if not dirs:
            return False
        return not any(_reaches(d, s) for d in dirs for s in self.dirs)

    def command_dir(self, rec: dict) -> str | None:
        """The session dir this build targeted, or None if unattributable.

        Two readings, because a corpus contains cases where they disagree and
        the command is right.  An explicit `-d <dir>` naming a known session
        directory wins: ndtht's ten `-d t -d t/scratch-ar NDTHT_ScratchAR`
        runs built a staging directory that was never committed, so no ROOT
        records it.  Failing that, the target name is mapped.

        None means *no evidence*, not 'nothing was built' — an unmapped
        target is a session this corpus has never seen edited, which cannot
        and should not be attributed to one of the project's developments.
        """
        if self.loaded_outside(rec):
            return None
        dirs = _load_dirs(rec)
        named = {self.label(d) for d in dirs if d in self.dirs}
        if len(named) == 1:
            return named.pop()
        from_target = {self.targets[t] for t in _targets_of(rec)
                       if self.targets.get(t)}
        return self.label(from_target.pop()) if len(from_target) == 1 else None

    # ------------------------------------------------------------- the ladder

    def error_dirs(self, ep: list[dict]) -> set[str]:
        """Session dirs this episode's errors point at.

        Prefers the structured `error_loci` and falls back to scraping the
        prose head, so the two eras of record read the same way.
        """
        out = set()
        for rec in ep:
            for theory, _line in rec.get("error_loci") or []:
                if (d := self.locus_dir(theory)):
                    out.add(d)
            if (d := self.locus_dir(rec.get("error_head") or "")):
                out.add(d)
        return out

    def project(self, ep: list[dict]) -> str:
        """Which development a trajectory belongs to: a session dir,
        'tooling' (no theory touched), or 'mixed' (several at once).

        Three routes, tried strongest first, because they are not equally
        good evidence and the weaker ones exist only to rescue records the
        stronger ones cannot see:

        1. **Diff paths** — authoritative: the files the attempt actually
           edited.
        2. **Error heads** — what the build failed *in*.  Weaker, since a
           build can fail in a dependency it did not edit, but it is all that
           survives an episode the recorder captured no diff for, and in
           ndtht it recovers 23 of the 35 such episodes rather than losing
           them.
        3. **The build target** — weakest, and last for a reason: you can
           build AE while editing base, so the target says what was *run*,
           not what was worked on.  For a run with no diff and an error head
           naming no file — a bare `wall timeout (40s wall)` — it is the only
           signal there is, and 12 multi-attempt trajectories sat
           unattributed on exactly that.
        """
        dirs, other = set(), False
        for rec in ep:
            for path, body in _split_files(rec.get("diff") or ""):
                # Per *file*, not per record.  A record is code-class if any
                # one of its files is, and taking every path from it books
                # the doc files along for the ride: one ndtht run edited
                # `bin/isabelle-watchdog.py` and `t/document/glossary.tex`
                # together, and `t/document/` — a shared LaTeX include
                # directory, not a session — became a phantom session of one
                # trajectory.
                if classify_file(path, body)[0] != "code":
                    continue
                if (d := self.path_dir(path)):
                    dirs.add(d)
                else:
                    other = True
        if not dirs:
            dirs = self.error_dirs(ep)
        # `other` is route 1 *succeeding*: paths were recorded and none was in
        # a session directory.  The target must not override that — a build
        # whose only captured edit is a `bin/` script is tooling work, and
        # deferring to the target would relabel 9 such ndtht runs as proof
        # search.
        if not dirs and not other:
            dirs = {d for rec in ep if (d := self.command_dir(rec))}
        if len(dirs) == 1:
            return dirs.pop()
        if dirs:
            return "mixed"
        return "tooling" if other else "none"


def _load_dirs(rec: dict) -> list[str]:
    """The `-d` values on a build command: where sessions were loaded from."""
    cmd = rec.get("command") or []
    out, i = [], 0
    while i < len(cmd):
        if cmd[i] == "-d" and i + 1 < len(cmd):
            out.append(cmd[i + 1].strip("/"))
            i += 2
        elif cmd[i] in _FLAG_TAKES_ARG:
            i += 2
        else:
            i += 1
    return out


def _reaches(load_dir: str, session_dir: str) -> bool:
    """Does `-d load_dir` bring `session_dir` into scope?

    Compared component-wise from any suffix of the load path, because a `-d`
    may be repo-relative (`-d t`) or absolute (`-d /Users/.../43sp/isabelle`)
    while session directories are always repo-relative.  Three 43sp records
    use the absolute form, and a plain string comparison read them as loading
    from outside the project -- dropping two trajectories that were correctly
    attributed before.
    """
    dp, sp = [c for c in load_dir.split("/") if c], session_dir.split("/")
    for i in range(len(dp)):
        tail = dp[i:]
        if tail == sp[:len(tail)] or sp == tail[:len(sp)]:
            return True
    return False


def _targets_of(rec: dict) -> list[str]:
    """Session names named on the build command line."""
    cmd = rec.get("command") or []
    out, i = [], 0
    while i < len(cmd):
        arg = cmd[i]
        if arg in _FLAG_TAKES_ARG:
            i += 2
        elif arg.startswith("-") or arg in ("isabelle", "build"):
            i += 1
        else:
            out.append(arg)
            i += 1
    return out


ENV_ATTRIBUTION = "TRAJECTORY_ATTRIBUTION"


def load_overrides(path=None) -> dict:
    """A project's attribution facts, from a file it names.

    These are the things no corpus can show: which directories were the same
    development under an earlier name, and which build targets are sessions a
    project builds *against* rather than works *on*.

        {"aliases": {"aem": "ae"},
         "targets": {"HOAU_Spike": null}}

    Named explicitly -- `--attribution PATH`, or $TRAJECTORY_ATTRIBUTION --
    rather than discovered at a fixed place beside the corpus.  A conventional
    sidecar sounds convenient and is not: it makes overriding anything require
    *write access to the data*, so trying an alternative attribution, or
    reading a corpus you were sent, means editing someone's dataset.  It also
    changes published statistics from a file nobody passed on the command
    line, which is the kind of action-at-a-distance this codebase has been
    bitten by before.

    A named file must exist -- a typo silently falling back to "no overrides"
    would answer a different question and look like a real result.  Nothing
    named, no overrides: the right default, since a project with no renames
    and no out-of-tree sessions needs none.
    """
    given = path or os.environ.get(ENV_ATTRIBUTION)
    if not given:
        return {}
    p = Path(given).expanduser()
    if not p.exists():
        raise corpus.CorpusError(f"no such attribution file: {p}")
    data = json.loads(p.read_text())
    unknown = set(data) - {"aliases", "targets"} - {k for k in data
                                                    if k.startswith("_")}
    if unknown:
        raise corpus.CorpusError(
            f"{p}: unknown key(s) {sorted(unknown)}; expected 'aliases' "
            f"and/or 'targets' (keys starting with '_' are ignored, for notes)")
    return data


# The fitted attribution for the corpus currently being read.  Module-level
# because attribution is a property of the *corpus*, not of an episode, while
# every view asks the question one episode at a time; and every view loads the
# whole corpus before asking.  `fit` is called once, from the entry points.
_FITTED: "Attribution | None" = None


def fit_attribution(recs: list[dict], overrides_path=None) -> Attribution:
    """Derive the attribution for these records and make it the default.

    Not `fit`: this module already had one, for the power-law fit of the
    length distribution, defined further down.  The collision was silent --
    Python simply kept the later definition -- and every attribution call
    would have received the distribution fitter instead.
    """
    global _FITTED
    overrides = load_overrides(overrides_path)
    _FITTED = Attribution.learn(recs, overrides)
    return _FITTED


def use_attribution(at: Attribution) -> Attribution:
    """Install an attribution built by hand, instead of deriving one.

    For callers that already know their corpus's shape -- and for tests,
    which should state what they assume rather than inherit it from a table
    inside this package.
    """
    global _FITTED
    _FITTED = at
    return at


def fitted() -> Attribution:
    if _FITTED is None:
        raise RuntimeError(
            "attribution has not been fitted; call "
            "attempts.fit_attribution(records, path) "
            "after loading a corpus")
    return _FITTED


def project(ep: list[dict]) -> str:
    return fitted().project(ep)


def error_dirs(ep: list[dict]) -> set[str]:
    return fitted().error_dirs(ep)


def command_dir(rec: dict) -> str | None:
    return fitted().command_dir(rec)


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


# Episode segmentation and the pooled-log interleave check are shared with
# the integrity frontend, so they have exactly one definition; see corpus.py.
_episodes = corpus.episodes
interleaving = corpus.interleaving


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
    """Retired: these views are subcommands of bin/trajectory.py now.

    The two frontends grew in different projects and split the same thirteen
    verbs over two scripts, so using them meant knowing which script had which
    verb -- and, until bin/corpus.py, meant two different answers to "which
    corpus?".  Everything here is still the implementation; only the entry
    point moved.
    """
    argv = " ".join(sys.argv[1:])
    print(f"attempts.py is a module now; its views are subcommands of "
          f"trajectory.py:\n\n    bin/trajectory.py {argv or 'list'}\n\n"
          f"`bin/trajectory.py --help` lists all thirteen, grouped.",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
