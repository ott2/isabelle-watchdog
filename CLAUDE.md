# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

The Isabelle build **watchdog** and the **build-trajectory** corpus tooling it
feeds. A tool repo: nothing here proves anything, and no Isabelle theory, paper
or prose belongs in it.

Consolidated from the two application projects that grew it, with history —
`git log` reaches back to 2026-04-23. `~/projects/claudecode/ndtht` contributed
52 commits and the analysis side; `~/projects/43sp` contributed 7 and the
capture side. Both still hold their own copies; this repo is now the trunk.

**Reading history across the merge:** `git log -- <path>` uses history
simplification and at the merge follows only the parent whose version won, so
`isabelle-watchdog.py` looks like 2 commits when it has 22. Use
`git log --full-history -- <path>`.

## The rule this codebase keeps re-learning

**Resolve from where the operator is standing, never from where the tool is
installed.** Every path bug found during consolidation was one variable:

| what | was | why it broke |
|---|---|---|
| `attempts.BUILDS_JSONL` | `<tool>/t/logs/builds.jsonl` | named the tool repo after the move |
| `trajectory.DEFAULT_CORPUS` | `<tool>/results/isabelle-logs/…` | same, different layout |
| `trajectory --repo` | `<tool>` | tool repo *is* a git repo, so the guard passed and `check` reported everything `unverified` |
| `build_record.PROJECT_DIR` | `<tool>` | **recorded diffs of the tooling instead of the proof** |
| `build.SESSION` | `"SPSlowdown"` | only ever right for 43sp |

The `build_record` one is the instructive failure. It did not error — it wrote
a faithful, well-formed diff of the wrong repository, and `trajectory.py check`
then certified every such record `sound`, because the payload genuinely
regenerates from the trees it names. A corpus of perfectly-verified diffs of
the wrong project is harder to notice than a crash and worse to inherit.
`bin/corpus.py` states the rule; anything new that needs a project path should
go through `corpus.project_root()`.

## Architecture

Four layers. Understanding the stack means reading all four.

```
bin/build                one call: carries the note AND runs the build
  └─ isabelle-watchdog.py    supervises `isabelle build`; decides ok|fail|timeout
       └─ build_record.record(...)   appends one JSON line per attempt
            └─ builds.jsonl          the corpus (a symlink to a separate repo)
                 └─ trajectory.py    the single reader; never writes
```

`bin/guard.py` sits beside the top two: `run_guarded` swallows any failure in
capture and warns, so instrumentation can never change a build's exit code.
Two call sites guard deliberately different scopes — `build_record` guards its
record logic, the watchdog additionally guards the `import build_record` that a
guard inside `build_record` cannot cover. Do not collapse them into one.

### 1. `isabelle-watchdog.py` — process supervision

Three independent kill conditions, each a distinct diagnosis: **activity** (no
stdout for `WATCHDOG_TIMEOUT`), **wall** (`WALL_TIMEOUT` total), and **loop**
(`LOOP_PROGRESS_THRESHOLD` consecutive Isabelle `command "X" running for Ns
(line Y of theory Z)` warnings on the same `(theory, line, command)` triple).
The loop detector is the one that names the stuck line.

**Three coupled constants — do not change one alone.** The watchdog injects
`-o build_progress_threshold=15` into the `isabelle build` argv. Isabelle's own
default is 20 s, the *same* as `WATCHDOG_TIMEOUT`, so the line-bearing warning
and the activity kill fire at the same instant and the line is lost. At 15 s,
with Isabelle's 2 s re-emit, warnings land at 15/17/19 s: three consecutive
(`LOOP_PROGRESS_THRESHOLD`) just under the 20 s kill.

**Battery normalisation.** Detects battery via `pmset -g ps` (macOS) and scales
the activity budget, the wall budget *and* the loop-warn threshold by
`BATTERY_FACTOR` (default 2.0). Scaling all three matters: scaling only the
budgets leaves a battery-slow-but-fine command crossing the unscaled loop
threshold and being spuriously loop-killed. This normalises to AC-equivalent
time rather than bypassing the budget, so the cost-regression signal survives.

Exit codes: `0` success, `124` watchdog kill, otherwise the child's.

### 2. `build_record.py` — trajectory capture

One JSON line per attempt. Design commitments, all load-bearing:

- **The diff is the payload, as text.** An earlier prototype chained snapshots
  on `refs/attempts/*`; that store is local-only and unshareable. Each record
  instead carries its incremental diff inline, anchored to the public
  `git_head`, so a corpus is portable with no git object store needed to read
  it. A throwaway tree object is written only to *compute* the diff.
- **Allowlist capture, tracked or not.** `git add -u` then `git add -A`, both
  over `SOURCE_PATHSPECS` (`*.thy`, `*ROOT`, `*ROOTS`; override with
  `BUILD_SOURCE_PATHSPECS`). Capture was tracked-only until 2026-07-27, which
  blinded it during exactly the highest-value work — while a new theory is
  authored every edit is invisible and a whole fail→fix run records as empty
  diffs. That accounted for 26 of 28 otherwise-inexplicable fail→ok flips in
  the first month of data. Non-source changes are still *named* in
  `other_changed`, so a flip with an empty source diff stays explicable.
- **Re-baselining on commit.** Diffs are incremental against the previous
  attempt's tree — *except* when HEAD moved mid-run, where it re-baselines on
  the new HEAD so committed content never leaks into a payload. Any consumer
  treating the corpus as one flat chain desynchronises at the first commit.
- **`limits` records the budgets in force.** Without them "the proof got
  slower" and "the clock got tighter" produce identical records. Values are
  *effective* (post-battery-scaling); divide by `battery_factor_applied`.
- **Notes carry the reasoning the diff cannot.** Keys `diagnosis:` / `change:`
  / `expect:` / `ref:`, parsed into `note_fields` while `note` keeps text
  verbatim. `expect:` is the field worth the trouble: a prediction recorded
  *before* the outcome is the one self-scoring signal in the corpus — hence
  `note_pre_build` and `note_age_s` record whether it really predated.
- **Never breaks the build.** Preserve this in any refactor.

### 3. `trajectory.py` — the single reader

Thirteen subcommands, grouped by the question rather than by which of the two
original scripts implemented them:

- **integrity** — `check` `repair` `replay` `extract` (the only ones taking
  `--repo`)
- **reading** — `list` `show` `episodes` `notes` `classify`
- **measuring** — `lengths` `size` `progress` `flips`

Governing principle: **regeneration is the ground truth.** Where both tree
objects survive, the payload is exactly `git diff --no-color -M <base> <tree>`,
so it can be regenerated and compared — both the strongest check and the exact
repair. Defects are classified on two independent axes (payload integrity vs
capture coverage), and the tool refuses to repair what it would have to
fabricate (`damaged`, `empty-blind`).

`attempts.py` implements the reading and measuring views and is **a module, not
a command** — running it prints the equivalent `trajectory.py` line. Its
distinctive piece is the **code-vs-doc classifier**: many attempts change only
prose and build green by construction, so counting them inflates the one-shot
rate and flattens the histogram. Default views keep only code deltas; `--all`
restores the raw view; `classify -v` shows the evidence.

An **episode** is a maximal run of attempts ending in a success. Boundaries are
successes, **not** commits — a mid-flight commit is just an attempt whose
`git_head` moved.

### 4. `bin/build` — the entry point

One call carrying the note and running the build, `git commit -m` shaped. The
alternative (write a note file, then build) is two steps with state between
them; its failure modes are a build with no note or — worse — a pending note
attaching to a later attempt, since misattributed reasoning is
indistinguishable from the real thing. It also collapses to a single permission
rule (`bin/build *`) in a harness that gates by command prefix.

Capture happens in the *watchdog*, so any path through it is recorded. Running
`isabelle build` directly is the one way to lose an attempt: the sources are
picked up by the next recorded build (diffs are cumulative), but that attempt's
outcome, timing and error loci are gone.

## Environment contract

The public API. `--session` has no default on purpose: a wrong session is a
confusing Isabelle error seconds later, a missing one is a clear message now.

| var | default | layer |
|---|---|---|
| `WATCHDOG_TIMEOUT` | 20 | activity kill, seconds of stalled stdout |
| `WALL_TIMEOUT` | 40 | absolute wall cap |
| `BATTERY_FACTOR` | 2.0 | scales all three budgets on battery; 1.0 disables |
| `LOOP_PROGRESS_THRESHOLD` | 3 | consecutive same-line warnings before loop kill |
| `BUILD_PROGRESS_THRESHOLD` | 15 | injected as `-o build_progress_threshold=N` |
| `LOG_NAME` | `last-build.log` | log basename; override per stage |
| `WATCHDOG_LOG_DIR` | `<project>/t/logs` | where records go; read by watchdog, recorder *and* readers |
| `BUILD_SOURCE_PATHSPECS` | `*.thy *ROOT *ROOTS` | what counts as source |
| `BUILD_SESSION` / `BUILD_SESSION_DIR` | — / `.` | session to build, and where its ROOT is |
| `BUILD_NOTE` / `BUILD_NOTE_FILE` | — | note text / pending-note path |
| `TRAJECTORY_CORPUS` | — | read a specific corpus, ignoring the above |

Corpus resolution (`bin/corpus.py`): `$TRAJECTORY_CORPUS`, then
`$WATCHDOG_LOG_DIR/builds.jsonl` — the same variable the writers honour, so a
reader lands where the writer wrote — then the known layouts (`t/logs`,
`results/isabelle-logs`) under the current project. Several matches is an
error, not a silent preference.

## Commands

```sh
# build (from the project being built, not from here)
BUILD_SESSION=MySession bin/build -m 'diagnosis: X; change: Y; expect: ok'
bin/build --lint -m '...'              # check the note, do not build
bin/build -- -o quick_and_dirty        # extra args to isabelle build

# read the corpus — CORPUS is optional everywhere
bin/trajectory.py --help               # all thirteen, grouped
bin/trajectory.py check                # regenerate every payload, compare
bin/trajectory.py repair --apply [--heuristic]
bin/trajectory.py replay [--from N] [--to N]
bin/trajectory.py extract N DEST       # materialise attempt N's sources
bin/trajectory.py lengths --fit --by-project
bin/trajectory.py classify BUILD_ID -v
```

`bin/check-snapshot-untracked.sh` is the regression guard for the capture
allowlist and checks **both** directions — build-relevant source gets in,
scratch and gitignored paths stay out. It is the closest thing to a test suite
the capture layer has; its probe paths are still ndtht-shaped (`t/base/...`).

The six `audit-*.py` / `recount-lengths.py` / `oneshot-significance.py` scripts
are the validation suite for the readers: each interrogates one measurement
decision (is the 1-shot rate measuring search or bookkeeping? is a timeout a
proof event or load?). They import `attempts.py` by path via `importlib`.

### Verifying a change

Both corpora survive and are the regression suite. There is no mocking to do —
run every subcommand against both, before and after, and diff:

- `~/projects/ndtht-trajectories/stac-wip/builds.jsonl` (695 records)
- `~/projects/trajectories/43sp/builds.jsonl` (40 records)

For the *write* path, `git init` a scratch repo, write a `.thy`, run the
watchdog with `WATCHDOG_LOG_DIR` set, then `trajectory.py check` and `replay`
against it. That is what caught the `PROJECT_DIR` bug, and no amount of reading
would have.

## Corpora are separate repos

Data does not live with the tools. Each project symlinks its log dir's
`builds.jsonl` at a corpus repo (`~/projects/trajectories`,
`~/projects/ndtht-trajectories`). Keep this split: diffs are stored inline, so
a corpus is irreplaceable data rather than a derived cache, with a different
backup and sharing story from the tools.

## Known follow-up work

- **`attempts.project()` hard-codes ndtht's layout** — `^t/([A-Za-z0-9_-]+)/`
  is the first rung of the attribution ladder, so on any other project every
  episode labels `tooling` or `none`. This is why the 43sp corpus attributes
  nothing. Generalising it moves published numbers, so it needs its own change
  with both corpora re-measured.
- `bin/check-snapshot-untracked.sh` probes `t/base/...` paths.
- `bin/convert-legacy-trajectory.py` was a one-shot migration off the git-chain
  prototype; kept as a record of how the initial dataset was produced, not on
  any live path.

## What stayed behind

`ndtht/bin/shape-vs-trajectory.py` and `probe-noproof.py` join trajectory data
to one project's Isabelle proof shapes (they shell out to `query` and glob
`t/<dir>`), so they belong to the application. The rule they illustrate is
general: **keep project-specific joining in separate scripts, out of the
readers.** Readers take their input path as an argument and hold repo-relative
paths as overridable defaults, never constants.
