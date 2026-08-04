# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

The Isabelle build **watchdog** and **build-trajectory** infrastructure, extracted
from the two application projects that grew it. It is a tool repo: nothing here
proves anything, and no Isabelle theory belongs in it.

**The repository is currently empty** (one branch, `main`, zero commits). The work
is a consolidation of code that lives in two places today:

| source | role | what it leads on |
|---|---|---|
| `~/projects/claudecode/ndtht` | where development concentrated (~2258 commits; ~46 touch this tool set, back to 2026-04-23) | the analysis readers, the design doc |
| `~/projects/43sp` | later application, added the generalisation hooks (7 commits touch the set) | `isabelle-watchdog.py`, `build_record.py`, the `bin/build` entry point |

`ndtht` is a **git worktree** of `~/projects/ndtht` — its `.git` is a gitdir
pointer, so history operations must target `~/projects/ndtht/.git`.

### Consolidation constraint: keep the history

The extraction must preserve per-file git history for the pieces taken, and must
*not* drag in the applications (Isabelle theories, papers, prose). `git-filter-repo`
is installed at `/opt/homebrew/bin/git-filter-repo`. The shape that fits: filter a
clone of each source repo down to the tool paths, then bring both filtered
histories into this repo (unrelated histories, ndtht as the trunk). Prefer this over
copying files in — a plain copy discards the reasoning trail that the commit
messages and the design doc jointly carry, and that trail is the main asset.

## Architecture

Four layers, each a separate process-or-import boundary. Understanding the stack
means reading all four; no single file explains it.

```
bin/build                 one call: carries the note AND runs the build
  └─ isabelle-watchdog.py supervises `isabelle build`; decides ok|fail|timeout
       └─ build_record.record(...)   appends one JSON line per attempt
            └─ builds.jsonl          the corpus (a symlink to a separate repo)
                 └─ trajectory.py / attempts.py    readers; never write
```

### 1. `isabelle-watchdog.py` — process supervision

Runs an Isabelle build, saves full output to `<LOG_DIR>/$LOG_NAME`, prints a
one-line summary. Three independent kill conditions, each a distinct diagnosis:

- **activity** — no stdout for `WATCHDOG_TIMEOUT` s (default 20)
- **wall** — total elapsed exceeds `WALL_TIMEOUT` (default 40)
- **loop** — `LOOP_PROGRESS_THRESHOLD` (default 3) consecutive Isabelle
  `command "X" running for Ns (line Y of theory Z)` warnings on the *same*
  `(theory, line, command)` triple. This is the one that names the stuck line.

The loop detector only works because of a tuning interaction that is easy to
break: the watchdog injects `-o build_progress_threshold=15` (default
`BUILD_PROGRESS_THRESHOLD`) into the `isabelle build` argv. Isabelle's own
default is 20 s — the *same* as `WATCHDOG_TIMEOUT`, so the line-bearing warning
and the activity kill fire at the same instant and the line is lost. At 15 s,
with Isabelle's 2 s re-emit, warnings land at 15/17/19 s: three consecutive
just under the 20 s activity kill. **Do not change one of these three constants
without the other two.**

**Battery normalisation.** On macOS the watchdog detects battery via `pmset -g ps`
and multiplies the activity budget, the wall budget, *and* the loop-warn threshold
by `BATTERY_FACTOR` (default 2.0). Scaling all three matters: scaling only the
budgets leaves a battery-slow-but-fine command crossing the unscaled loop
threshold and being spuriously loop-killed. This *normalises* to AC-equivalent
time rather than bypassing the budget, so the cost-regression signal survives.

Exit codes: `0` success, `124` watchdog kill, otherwise the child's.

### 2. `build_record.py` — trajectory capture

`record(...)` appends one JSON line per attempt. Design commitments, all
load-bearing:

- **The diff is the payload, as text.** Earlier prototypes chained snapshots on
  `refs/attempts/*`; that store is local-only and unshareable. Each record instead
  carries its incremental diff inline, anchored to the public `git_head`, so a
  corpus is portable with no git object store needed to read it. A throwaway tree
  object is written only to *compute* the diff; its id is kept as an integrity
  anchor.
- **Allowlist capture, tracked or not.** `git add -u` then `git add -A`, both over
  `SOURCE_PATHSPECS` (`*.thy`, `*ROOT`, `*ROOTS`; override with
  `BUILD_SOURCE_PATHSPECS`). Capture was tracked-only until 2026-07-27, which
  blinded it during exactly the highest-value work — while a new theory is
  authored every edit is invisible and a whole fail→fix run records as empty
  diffs. That accounted for 26 of 28 otherwise-inexplicable fail→ok flips in the
  first month of data. Non-source changes are still *named* in `other_changed`
  (names + line counts), so a flip with an empty source diff stays explicable.
- **Re-baselining on commit.** Each diff is incremental against the previous
  attempt's tree — *except* when HEAD moved mid-run, where it re-baselines on the
  new HEAD so committed content never leaks into a payload. Any consumer treating
  the corpus as one flat chain desynchronises at the first commit.
- **`limits` records the budgets in force.** Without them "the proof got slower"
  and "the clock got tighter" produce identical records, so a Makefile edit
  halving `WALL_TIMEOUT` reads as a regression in the theory. Values are
  *effective* (post-battery-scaling); divide by `battery_factor_applied` to recover
  what was configured.
- **Notes carry the reasoning the diff cannot.** Four recognised keys —
  `diagnosis:` / `change:` / `expect:` / `ref:` — parsed into `note_fields` while
  `note` keeps text verbatim. `expect:` is the field worth the trouble: a
  prediction recorded *before* the outcome is the one self-scoring signal in the
  corpus. That only holds if the note predates the build, so `note_pre_build` and
  `note_age_s` record whether it did rather than assuming.
- **Never breaks the build.** `record(...)` swallows every error into a one-line
  stderr warning; the caller's exit code is untouched. Preserve this in any
  refactor — capture must never cost a build.

### 3. Readers — `trajectory.py` (43sp) and `attempts.py` (ndtht)

Two tools that grew independently over the same corpus. Neither subsumes the
other; consolidating them is real design work, not a merge.

`trajectory.py` — **integrity and provenance**: `check` / `repair` / `replay` /
`progress` / `notes` / `flips` / `extract`. Its governing principle is that
**regeneration is the ground truth**: where both tree objects survive, the payload
is exactly `git diff --no-color -M <base> <tree>`, so it can be regenerated and
compared — that is both the strongest check and the exact repair. It classifies
defects on two independent axes (payload integrity vs capture coverage) and
deliberately refuses to repair what it would have to fabricate (`damaged`,
`empty-blind`).

`attempts.py` — **statistics and shape**: `list` / `show` / `episodes` / `lengths`
/ `classify` / `size`. Deliberately import-free and git-free; reads any
`builds.jsonl` via `-i/--input`. Its distinctive piece is the **code-vs-doc
classifier**: many attempts change nothing but prose and build green by
construction, so counting them inflates the one-shot rate and flattens the
trajectory histogram. Default views keep only code deltas; `--all` restores the raw
view; `classify -v` shows the evidence for any verdict.

An **episode** is a maximal run of attempts ending in a success. Boundaries are
successes, **not** commits — a mid-flight commit is just an attempt whose
`git_head` moved, flagged `committed_midflight`.

### 4. `bin/build` — the entry point

One call that carries the note and runs the build, `git commit -m` shaped. The
alternative (write a note file, then build) is two steps with state between them,
and its failure modes are a build with no note or — worse — a pending note that
attaches to a later attempt, since misattributed reasoning is indistinguishable
from the real thing. It also collapses to a single permission rule (`bin/build *`)
in a harness that gates by command prefix.

Capture happens in the *watchdog*, not here, so any path through the watchdog is
recorded. Running `isabelle build` directly is the one way to lose an attempt: the
sources are still picked up by the next recorded build (the diff is cumulative),
but that attempt's outcome, timing and error loci are gone forever.

## Environment contract

The whole stack is configured by environment variable — this is the public API and
should stay stable across the consolidation.

| var | default | layer |
|---|---|---|
| `WATCHDOG_TIMEOUT` | 20 | activity kill, seconds of stalled stdout |
| `WALL_TIMEOUT` | 40 | absolute wall cap |
| `BATTERY_FACTOR` | 2.0 | scales all three budgets on battery; 1.0 disables |
| `LOOP_PROGRESS_THRESHOLD` | 3 | consecutive same-line warnings before loop kill |
| `BUILD_PROGRESS_THRESHOLD` | 15 | injected as `-o build_progress_threshold=N` |
| `LOG_NAME` | `last-build.log` | log basename; override per stage so stages don't clobber |
| `WATCHDOG_LOG_DIR` | `<project>/t/logs` | escape hatch from the `t/` layout; read by watchdog *and* recorder |
| `BUILD_SOURCE_PATHSPECS` | `*.thy *ROOT *ROOTS` | what counts as source |
| `BUILD_NOTE` / `BUILD_NOTE_FILE` | — | note text / pending-note path |

`WATCHDOG_LOG_DIR` should be owned by the **entry point**, not exported by a
Makefile. When 43sp's Makefile owned it, `bin/build` run directly recorded into the
recorder's built-in default — a second corpus, a second instance id, and a build
that looks unrecorded because the records are elsewhere.

## Dependency

Both `isabelle-watchdog.py` and `build_record.py` import
`isabelle_query.common.run_guarded` (a best-effort capture guard). That package is
a **sibling repo** at `~/projects/query`, installed with `pip install -e ../query`.
This is the only non-stdlib dependency, and it is the precedent for this
extraction: `query` was itself once repo-local `bin/query`.

## Corpora are separate repos

Data does not live with the tools. Each project symlinks its log dir's
`builds.jsonl` at a corpus repo:

- `~/projects/trajectories/43sp/builds.jsonl` — 40 records
- `~/projects/ndtht-trajectories/stac-wip/builds.jsonl` — 695 records

Keep this split. The corpus is the only copy of what it describes (diffs are
inline), so it has a different backup and sharing story from the tools.

## Commands

There are no commands in this repo yet. These are the invocations to carry over,
from `43sp/Makefile` (the cleaner of the two — a small alias layer over
`bin/build`) and `ndtht/Makefile` (per-session budgets and the `WALL` knob):

```sh
bin/build                                          # build, note recorded as null
bin/build -m 'diagnosis: X; change: Y; expect: ok' # the normal form
bin/build -m - < note.md                           # long note on stdin
bin/build --lint -m '...'                          # check the note, do not build
bin/build -- -o quick_and_dirty                    # extra args to isabelle build

bin/trajectory.py check                            # regenerate every payload, compare
bin/trajectory.py repair --apply [--heuristic]     # exact repair; --heuristic infers
bin/trajectory.py replay [--from N] [--to N]       # independent route: apply the diffs
bin/trajectory.py extract N DEST                   # materialise attempt N's sources
bin/trajectory.py notes | flips | progress

bin/attempts.py list [-n N]                        # -n 0 means all
bin/attempts.py episodes [--diffs] [--full]
bin/attempts.py lengths --fit [--by-project]       # power law vs geometric null
bin/attempts.py classify BUILD_ID -v               # why a delta was code or doc-only
bin/attempts.py show BUILD_ID --full
```

`bin/check-snapshot-untracked.sh` (ndtht) is the regression guard for the capture
allowlist and checks **both** directions — build-relevant source gets in, scratch
and gitignored paths stay out. It is the closest thing to a test suite the capture
layer has; port it and generalise its hard-coded `t/base/...` probe paths.

### The `WALL` override, and why it is the only sanctioned one

ndtht's Makefile exposes `make build-<sess> WALL=N`, which sets `WALL_TIMEOUT=N`
and `WATCHDOG_TIMEOUT=sqrt(20*N)` — the geometric mean, not `N`, so a true silent
hang is still caught well before the wall. This exists because the *unwatchdogged*
fallback (`make pdf-*`, which shells `isabelle build` directly) records **no**
trajectory attempt at all, not even the success that closes an episode. Bypassing
the watchdog to get a verdict has cost a whole large-refactor episode. Keep any
"more time" knob on the watchdog path.

Keep `N` modest (≈120–180). Never 300: it exceeds Claude Code's session-cache
window, forcing a full-context reload per call.

## Reference documents to carry over

- `ndtht/logging-design.md` — ~1450 lines, the design doc the code comments cite by
  section (§12 trajectory axis, §13.1 code-vs-doc, §13.2 attribution, §16 portable
  episode files). Code comments reference these section numbers directly, so it
  must move with the code or the comments dangle.
- `43sp/INSIGHTS.md` #14, #16–#19, #25–#27 — the corpus-methodology entries,
  explicitly marked as generalising past Isabelle. #16 (repair by regenerating, not
  by inferring) and #18 (a stronger check silently subsuming a weaker one that
  measured something different) are the ones that justify `trajectory.py`'s design.
- `ndtht/.claude/memory/feedback_battery_watchdog.md`, `feedback_build_invocation.md`,
  `feedback_pdf_unwatchdogged.md`, `feedback_no_buildclean_reflex.md` — the
  operational rules above, with the incidents that produced them.

## What stays behind

`ndtht/bin/shape-vs-trajectory.py` is the *join* between generic trajectory data
and one project's Isabelle proof shapes: it shells out to `query` and globs
`t/<dir>`. It belongs in the application, not here. The rule it illustrates is
general — **keep project-specific joining in separate scripts, out of the
readers** — and applies to anything new: readers take their input path as an
argument and hold repo-relative paths as overridable defaults, never constants.

`ndtht/bin/convert-legacy-trajectory.py` was a one-shot migration of the
git-chain prototype into the diff-bearing format. It is not on any live path;
carry it only as a historical record of how the initial dataset was produced.
