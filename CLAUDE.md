# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

The Isabelle build **watchdog** and the **build-trajectory** corpus tooling it
feeds, packaged for PyPI as `isabelle-watchdog` (import `isabelle_watchdog`).
A tool repo: nothing here proves anything, and no Isabelle theory, paper or
prose belongs in it.

    src/isabelle_watchdog/     the package
        watchdog.py            process supervision       -> isabelle-watchdog
        record.py              trajectory capture
        corpus.py  guard.py    location/loading; the never-break-a-build guard
        roots.py               what an Isabelle ROOT declares
        trajectory.py          the single reader CLI     -> trajectory
        attempts.py            its reading/measuring views (a module, not a CLI)
        build.py               note-carrying entry point -> isabelle-build
        export.py  legacy_convert.py
        audits/                validation suite for the readers' statistics
    tests/                     pytest; conftest.py holds the fixtures
    docs/logging-design.md     the design doc the code cites by section
    docs/working-on-the-tooling.md   validating against a real project safely

Consolidated from the two application projects that grew it, with history —
`git log` reaches back to 2026-04-23. `~/projects/claudecode/ndtht` contributed
52 commits and the analysis side; `~/projects/43sp` contributed 7 and the
capture side. Both still hold their own copies; this repo is now the trunk.

**Reading history needs `--follow`, and sometimes `--full-history`.** Two
things hide commits from a plain `git log -- <path>`: the `bin/` → `src/`
rename (needs `--follow`), and history simplification at the consolidation
merge, which follows only the parent whose version won (needs
`--full-history`). Use `--follow` by default:

```sh
git log --follow -- src/isabelle_watchdog/watchdog.py   # 24, back to 2026-04-23
git log -- src/isabelle_watchdog/watchdog.py            # 1 — believe the former
```

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
| `trajectory._attempts()` | `spec_from_file_location` | no parent package, so `attempts`' own relative import failed |
| `watchdog` log dir | `<tool>/t/logs` | installed, that is `site-packages/t/logs`; found last, because it only misplaces a *log file* |
| `export.PROJECT_DIR` | `<tool>` | same, in a reader nothing routine calls |

The `build_record` one is the instructive failure. It did not error — it wrote
a faithful, well-formed diff of the wrong repository, and `trajectory.py check`
then certified every such record `sound`, because the payload genuinely
regenerates from the trees it names. A corpus of perfectly-verified diffs of
the wrong project is harder to notice than a crash and worse to inherit.
`corpus.py` states the rule; anything new that needs a project path should go
through `corpus.project_root()` — or, for a log directory,
`corpus.resolve_log_dir()`. The one path that *is* legitimately
`__file__`-relative is a reference to a sibling module — that names the tool,
which is the question being asked.

The last two rows are the same rule caught a second time, and they say
something about how it hides: the ones fixed during consolidation were the
ones that touched a *payload*, where being wrong produces a record someone
eventually reads. A misplaced log file produces nothing to read, so it
survived until a project asked where its records had gone.

## Architecture

Four layers. Understanding the stack means reading all four.

```
build.py                 one call: carries the note AND runs the build
  └─ watchdog.py             supervises `isabelle build`; decides ok|fail|timeout
       └─ record.record(...)      appends one JSON line per attempt
            └─ builds.jsonl       the corpus (a symlink to a separate repo)
                 └─ trajectory.py the single reader; never writes
```

`build.py` reaches the watchdog with `python -m isabelle_watchdog.watchdog`
rather than a path, so the subprocess uses the installed copy. The subprocess
boundary itself stays: the watchdog installs signal handlers and reaps a
process tree, which is not something to run inside a caller's interpreter.

`guard.py` sits beside the top two: `run_guarded` swallows any failure in
capture and warns, so instrumentation can never change a build's exit code.
Two call sites guard deliberately different scopes — `record` guards its own
record logic, the watchdog additionally guards the `import` itself, which a
guard inside `record` cannot cover. Do not collapse them into one.

**A guarded failure must name its consequence, not its exception.** Both of
those call sites pass `lost=guard.ATTEMPT_LOST`, and the resulting warning
opens `build-record: FAILED -- this attempt was NOT recorded`. It used to
open `build-record: skipped (CalledProcessError: …)`, which beside a green
`OK 1 theories` reads as a note about something optional; a project took it
that way and lost five attempts. Side tasks that lose nothing irreplaceable
(`db-error`, enriching a message) still say "skipped" — the word now carries
information rather than being the only one available. Same argument as
`limits` and `contention` one layer down: what a reader needs is the thing
they cannot reconstruct, stated first.

`record._git` raises **`GitFailed`**, a `CalledProcessError` subclass whose
`__str__` appends git's stderr. The base class prints the argv and the exit
status only, so `fatal: pathspec '*.thy' did not match any files` — the
whole diagnosis — was captured and discarded, one line above the operator
who needed it. Subclassing keeps the `except CalledProcessError` callers
(which expect certain commands to fail) working unchanged.

### 1. `watchdog.py` — process supervision

Three independent kill conditions, each a distinct diagnosis: **activity** (no
stdout for `WATCHDOG_TIMEOUT`), **wall** (`WALL_TIMEOUT` total), and **loop**
(`LOOP_PROGRESS_THRESHOLD` consecutive Isabelle `command "X" running for Ns
(line Y of theory Z)` warnings on the same `(theory, line, command)` triple).
The loop detector is the one that names the stuck line.

**"Consecutive" means among all output, not among warnings** — and getting
that wrong killed a healthy build. Isabelle builds a session's theories in
parallel, so a merely slow `by` emits its warnings *while the rest of the
session progresses*; a counter that only compares a warning against the
previous warning cannot see the thirteen other theories that started in
between, and three warnings spread over 73 seconds read as a tactic spinning
on one line. Any other line therefore resets the count.

Two things about that rule are deliberate:

- **Any line, not an enumerated set of "progress" lines.** A list of what
  progress looks like is a guess that goes stale — the same argument as
  `git ls-files` over a filesystem walk in *Deriving what to build*. The two
  failures are also not symmetric: a missed loop kill costs the gap between
  the loop budget and the wall budget, and `_summary` names the line on a
  wall kill anyway, whereas a false loop kill destroys a partial build.
  Isabelle discards a session's heap image when rebuilding one, so a
  mid-rebuild kill leaves nothing to resume from — the reporter lost two AFP
  entries.
- **The count resets; the key does not.** `loop_key` is what lets a wall or
  activity kill name the last line Isabelle complained about, which is the
  one thing those two diagnoses cannot otherwise supply.

This postpones the kill on a parallel build rather than surrendering it, and
postpones it to exactly the moment its claim becomes true. Measured against a
real looping `by` beside three theories building in parallel: the others
finished at 5.6 s, and the warnings then ran uninterrupted every 2 s for the
rest of a 75 s capture. A theory holding an unfinished forked proof never
reports `100%`, so nothing from the stuck theory itself resets the count
either. The question the detector answers is now "is this command the only
thing still happening" rather than "has this command been slow three times".

**Theory names are printed session-qualified** — `FSM_Tests.Util`, not `Util`
— in the summaries and in the record's `error_head`. They were shortened on
the reasoning that when every supervised build targets one session the
qualifier is noise: true of the build the operator *meant* to run, false of
the one Isabelle planned. A stale dependency heap silently adds its parent
sessions, and then the qualifier is the only thing separating the operator's
own code from an AFP entry they have never opened — `LOOP Util: "by" looping
on line 1650` sent someone looking for a file that was not theirs.
Unconditional rather than "qualify when it differs from the target", because
the watchdog supervises an argv and has no reliable notion of a target; and
because a qualifier that is sometimes there is one the reader has to know the
rule for. `_error_loci` reached this conclusion for the record earlier and
for a sharper reason — 11 base names have lived in more than one session
directory in ndtht alone — so the display and the record now agree, and
`attempts.theory_key` already collapses either spelling to one key.

**Three coupled constants — do not change one alone.** The watchdog injects
`-o build_progress_threshold=15` into the `isabelle build` argv. Isabelle's own
default is 20 s, the *same* as `WATCHDOG_TIMEOUT`, so the line-bearing warning
and the activity kill fire at the same instant and the line is lost. At 15 s,
with Isabelle's 2 s re-emit, warnings land at 15/17/19 s: three consecutive
(`LOOP_PROGRESS_THRESHOLD`) just under the 20 s kill.

**Two ways a machine can be slow, and they need different instruments.**
Both scale the activity budget, the wall budget *and* the loop-warn threshold
— scaling only the budgets leaves a slow-but-fine command crossing the
unscaled loop threshold and being spuriously loop-killed. Both normalise to
uncontended, AC-equivalent time rather than bypassing the budget, so the
cost-regression signal survives. What differs is where the number comes from:

| | what changes | can you measure it afterwards? |
|---|---|---|
| **battery/thermal** | how much work a CPU-second *buys* | **no** — the process still gets its CPU-second, it just accomplishes less. Hence an assumed `BATTERY_FACTOR` |
| **contention** | how many CPU-seconds you *get* per wall-second | **yes** — a descheduled process accrues no CPU time at all |

*Battery* is detected via `pmset -g ps` (macOS) or `/sys/class/power_supply`
(Linux); anywhere else the state is unknown and nothing is scaled — which is
why running elsewhere was never broken, only unscaled.

*Contention* is measured, not predicted. The watchdog samples its process
tree's CPU time (`ps`, every `CPU_SAMPLE_INTERVAL`) and computes a **duty
cycle** — CPU-seconds per wall-second. That quantity is dimensionless: 1.0 is
a whole core's worth on any machine, fast or slow, so a threshold expressed in
it is not fitted to the machine it was written on. Three regimes:

- **stalled** (`duty < STALL_DUTY`) — no CPU. Not slowness, the *absence* of
  work, and no extension helps. Critically, `1/duty` is unbounded as duty → 0,
  so the naive rule would hand a deadlock four times its deadline. It also
  sharpens the activity kill: "no output" could be a build working quietly,
  "no output and no CPU" could not, and the summary says so.
- **starved** (`STALL_DUTY ≤ duty < RUNNING_DUTY`) — progressing on less than
  a core. Budgets × `min(LOAD_FACTOR_MAX, 1/duty)`, restoring what the build
  would have had uncontended.
- **running** (`duty ≥ RUNNING_DUTY`) — a core or more. Nothing owed. **This
  is the row the design turns on**: a proof that got genuinely more expensive
  burns CPU at full rate, so it is never mistaken for a starved one and still
  trips its budget on time. Scaling by an *estimated* load factor would have
  made those two indistinguishable.

`RUNNING_DUTY` is 0.9, not 1.0, because nothing is scheduled for 100.0% of
wall time; a strict boundary labels every healthy single-threaded build
`starved`. A parallel build starved to one core reads as `running` and gets
nothing — deliberate under-compensation, since erring toward killing keeps the
budget meaningful.

**Load average was tried and rejected.** It is free to read (0.45 µs) and
useless here: a 60 s damped average has a longer time constant than the whole
40 s budget it would govern — measured, a workload already 1.27× slower after
5 s still read as idle — and on a heterogeneous CPU (4 P + 4 E cores on the
development machine) scheduler migration swamps what signal remains, with no
correct denominator. `LOAD_FACTOR_MAX=1.0` disables the measurement entirely,
`ps` calls included.

**The read loop is `select()` + `os.read()`, and both halves are load-bearing.**
Two defects lived here until the supervision tests went in, and each made a
kill condition silently unenforceable:

- *Read the pipe raw.* `proc.stdout.readline()` goes through a buffered
  reader, which pulls a whole chunk into userspace and hands back one line —
  after which `select()` sees an empty pipe, reports not-ready, and the rest
  of the chunk is stranded until more output arrives. A child that printed
  four lines and went quiet had exactly one of them logged. The case it broke
  is precisely a burst followed by a hang, which is when the error block and
  the line-naming warnings arrive.
- *Check the budgets every pass, not only when the pipe goes quiet.* With the
  checks inside the `select()`-timed-out branch, a child emitting more than a
  line a second kept the pipe permanently ready and was never measured against
  the wall clock at all.

Exit codes: `0` success, `124` watchdog kill, otherwise the child's.

### 2. `record.py` — trajectory capture

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
  `other_changed`, so a flip with an empty source diff stays explicable —
  tracked ones, that is: both snapshots stage untracked files by the same
  allowlist, so a brand-new untracked non-source file appears in neither.
  Narrow for Isabelle, where the untracked things able to flip an outcome are
  `.thy` and `ROOT` files, which the allowlist already admits.

  **The two passes need different filters, and one filter for both cost a
  downstream project its first five attempts.** A pathspec matching nothing
  makes `git add` exit 128 — fatal, and not covered by `--ignore-errors` —
  so each pass is handed only specs that match. But `add -u` can stage only
  *tracked* files while `add -A` can stage untracked ones too, and the filter
  asked one question (`ls-files --cached --others`) for both. A spec matching
  untracked files alone passed the filter and killed pass 1. That is the
  state of every source pathspec before the first commit, so it was the first
  build of every new project — the case with no earlier record to notice the
  absence against. `_matching_pathspecs(env, tracked_only=)` now asks per
  pass. Every corpus that exists was written by a project that already had a
  committed theory, which is why nothing downstream ever saw it.
- **Re-baselining on commit.** Diffs are incremental against the previous
  attempt's tree — *except* when HEAD moved mid-run, where it re-baselines on
  the new HEAD so committed content never leaks into a payload. Any consumer
  treating the corpus as one flat chain desynchronises at the first commit.
- **`limits` records the budgets in force.** Without them "the proof got
  slower" and "the clock got tighter" produce identical records. Values are
  *effective* (post-battery-scaling); divide by `battery_factor_applied`.
- **`contention` records what the machine gave, not what the budget allowed.**
  `cpu_time_s`, `duty_cycle`, `verdict`, `load_factor_applied`. The
  *observations* are stored, not just the derived factor — the policy above
  them will change and these will not, and without them "that timeout was a
  hard proof" and "that timeout was a busy laptop" are indistinguishable,
  which is the same failure `limits` exists to prevent one layer up.
  `limits.load_factor_max` holds the *ceiling* rather than the factor, since
  the factor is measured during the run and can vary. All four are `null`
  when nothing was measured, and that includes a run too short to sample: the
  first sample is taken at *spawn*, as the baseline the first duty cycle is
  measured against, so it describes the machine a millisecond before the build
  rather than the build. Reporting it gave a 3.4 s build `cpu_time_s: 0.02`
  beside a correctly-null `duty_cycle` — two fields about one run, one of them
  false. `cpu_total` is now set only from an in-loop sample, so the baseline
  cannot reach a record at all.
- **Notes carry the reasoning the diff cannot.** Keys `diagnosis:` / `change:`
  / `expect:` / `ref:`, parsed into `note_fields` while `note` keeps text
  verbatim. `expect:` is the field worth the trouble: a prediction recorded
  *before* the outcome is the one self-scoring signal in the corpus — hence
  `note_pre_build` and `note_age_s` record whether it really predated.

  A section opens the note or a line, or follows `; ` **or `. `**. The full
  stop was not a separator until 2026-08-21, and its absence cost predictions
  rather than tidiness: a prose diagnosis is a sentence, a sentence ends in a
  full stop, so `diagnosis: X. expect: ok` swallowed the prediction into the
  diagnosis and scored the note as unpredicted. Every note that fails to
  parse *lowers* the measured prediction rate rather than erroring, so a
  parser that is stricter than the writing it accepts is a parser that quietly
  edits a published statistic. A full stop needs the trailing space a
  semicolon does not, because it is also a decimal point and a filename
  separator — `19.5s` and `v1.2.ref:` must not split.

  **A complaint has to be true of the note, not just of the parse.** The
  linter's "no `expect:`" fired on notes that visibly contained `expect: ok`,
  which reads as a broken linter — and the reasonable response, ignoring the
  warnings, is expensive, because the near-miss check beside it is the only
  thing standing between `expects:` and a section that silently does not
  exist. A key present in the text but absent from the parse now says so.
- **A precondition is not a failure.** `capture_blocker()` separates the two
  states where capture cannot work at all — no repository, and a repository
  with no commits — from the ones where something broke. Both were raw git
  errors, and both are what a formalisation *starts* in, so the highest-value
  attempts were the ones being lost. They get `NOT recorded -- <what is
  missing, why capture needs it, the command that supplies it>`, and nothing
  is created: a project with no corpus should not acquire an `instance-id`
  for one. `isabelle-build --where` reports the same thing before the first
  build, which is where it does the most good.
- **Never breaks the build.** Preserve this in any refactor. That includes
  the precondition path: a project that wanted supervision and not a corpus
  is not misconfigured, so this warns and continues.

### 3. `trajectory.py` — the single reader

Subcommands grouped by the question rather than by which of the two original
scripts implemented them:

- **integrity** — `check` `repair` `replay` `extract` (the only ones taking
  `--repo`)
- **reading** — `list` `show` `episodes` `notes` `classify`
- **measuring** — `lengths` `size` `progress` `flips`
- **auditing** — `audit`, the gateway to `audits/` (below)

Governing principle: **regeneration is the ground truth.** Where both tree
objects survive, the payload is exactly `git diff --no-color -M <base> <tree>`,
so it can be regenerated and compared — both the strongest check and the exact
repair. Defects are classified on two independent axes (payload integrity vs
capture coverage), and the tool refuses to repair what it would have to
fabricate (`damaged`, `empty-blind`).

`empty-blind` has **two causes and one date**, and `check` separates them
(`corpus.UNTRACKED_CAPTURE_FIX`, which `audits/zerodiff.py` now shares rather
than keeping its own copy of). Before 2026-07-27 it is the tracked-only
capture gap — a recorder bug, since fixed, and nothing a reader can act on.
After it, the edit was to a path `$BUILD_SOURCE_PATHSPECS` does not admit,
which is a question about the project rather than a fault. `check` used to
close by telling everyone to "widen the allowlist", which contradicted a
narrowing the recorder makes deliberately and, on both real corpora, named a
cause that applies to none of the records: all 46 of ndtht's blind payloads
predate the fix. An instruction that cannot be followed is worse than none.

`attempts.py` implements the reading and measuring views and is **a module, not
a command** — running it prints the equivalent `trajectory.py` line. Its
distinctive piece is the **code-vs-doc classifier**: many attempts change only
prose and build green by construction, so counting them inflates the one-shot
rate and flattens the histogram. Default views keep only code deltas; `--all`
restores the raw view; `classify -v` shows the evidence.

An **episode** is a maximal run of attempts ending in a success. Boundaries are
successes, **not** commits — a mid-flight commit is just an attempt whose
`git_head` moved.

**Attribution is derived, not declared** (`attempts.Attribution`), on four
signals — all of them already in the corpus:

| question | signal |
|---|---|
| what is a development? | a directory holding a `.thy`. ndtht's `t/{ae,ar,…}` and 43sp's flat `isabelle/` fall out of one rule, and `bin/` never becomes one |
| which target is which? | `session` lines in captured ROOT diffs (Isabelle's own declaration), else which directory a build's edits touched (3× dominance margin, since you can build X while editing Y) |
| is this even our work? | the `-d` load path. A build reaching no session directory is building something defined elsewhere |
| are two directories one development? | a `.thy` that changed directory. The recorder diffs with `-M`, so a split or a merge-back is recorded as a rename |

The last two replace what used to be hand-maintained lists. ndtht's
`HOAU_Spike` — a preliminary investigation scoped against `t/` for convenience,
later split out — loads from `-d scratch/hoau`, which reaches no session
directory, while every in-project build passes `-d t`, which reaches all five.
A temporary session like `t/aem` (the settled half of `t/ae`, split out so the
active half re-checked quickly, later merged back) is one development because
its theories moved: the last move wins, so a split-and-merge resolves to the
original and a still-live split resolves to the new home. **Neither corpus
needs a declaration file.**

`-d` is matched component-wise from any suffix, because a load path may be
absolute while session directories are repo-relative — three 43sp records use
the absolute form.

Because it is fitted from the whole corpus, `trajectory.py` calls
`attempts.fit_attribution(records, path)` after loading, for the subcommands
that attribute (`lengths`); the audits fit for themselves. A view that needs
attribution without asking fails loudly rather than labelling from an empty
map.

For anything still not derivable, there is an escape hatch — a file the caller
**names**, via `--attribution FILE` or `$TRAJECTORY_ATTRIBUTION`. Neither
corpus currently needs one; reach for it only after checking the signal is
genuinely absent from the data rather than merely unlooked-for:

```json
{"_note": "free-text, ignored", "aliases": {"aem": "ae"},
 "targets": {"HOAU_Spike": null}}
```

Named, not discovered beside the corpus. A conventional sidecar path sounds
convenient and is not: it makes overriding anything require *write access to
the data*, so trying an alternative attribution — or reading a corpus someone
sent you — means editing a dataset. It also lets a file nobody passed on the
command line change published statistics.

A named file must exist (a typo must not read as "no overrides"), and an
unrecognised key is an error — `target` for `targets` would otherwise be
accepted and do nothing. A target mapped to `null` is a *declared exclusion*,
and stays distinguishable from one merely absent: absence is what a rename
looks like, and an oversight should not read as a decision.

### 4. `build.py` — the entry point

One call carrying the note and running the build, `git commit -m` shaped. The
alternative (write a note file, then build) is two steps with state between
them; its failure modes are a build with no note or — worse — a pending note
attaching to a later attempt, since misattributed reasoning is
indistinguishable from the real thing. It also collapses to a single permission
rule (`isabelle-build *`) in a harness that gates by command prefix.

Capture happens in the *watchdog*, so any path through it is recorded. Running
`isabelle build` directly is the one way to lose an attempt: the sources are
picked up by the next recorded build (diffs are cumulative), but that attempt's
outcome, timing and error loci are gone.

## Environment contract

The public API. `--session` has no constant default on purpose — it was
`"SPSlowdown"` because `build.py` was written inside 43sp — but it does have a
derivation; see *Deriving what to build* below.

| var | default | layer |
|---|---|---|
| `WATCHDOG_TIMEOUT` | 20 | activity kill, seconds of stalled stdout |
| `WALL_TIMEOUT` | 40 | absolute wall cap |
| `BATTERY_FACTOR` | 2.0 | scales all three budgets on battery (*assumed*); 1.0 disables |
| `LOAD_FACTOR_MAX` | 4.0 | cap on the *measured* contention factor; 1.0 disables the sampling |
| `CPU_SAMPLE_INTERVAL` | 5.0 | seconds between process-tree CPU samples |
| `LOOP_PROGRESS_THRESHOLD` | 3 | same-line warnings, uninterrupted by any other output, before loop kill |
| `BUILD_PROGRESS_THRESHOLD` | 15 | injected as `-o build_progress_threshold=N` |
| `LOG_NAME` | `last-build.log` | log basename; override per stage |
| `WATCHDOG_LOG_DIR` | resolved, see below | where records go; read by watchdog, recorder *and* readers |
| `BUILD_SOURCE_PATHSPECS` | `*.thy *ROOT *ROOTS` | what counts as source |
| `BUILD_SESSION` / `BUILD_SESSION_DIR` | derived | session to build, and where its ROOT is |
| `BUILD_NOTE` / `BUILD_NOTE_FILE` | — | note text / pending-note path |
| `BUILD_RECORD` | on | trajectory capture on/off (`--no-record`); `1/yes/true/on`, `0/no/false/off` |
| `TRAJECTORY_CORPUS` | — | read a specific corpus, ignoring the above |
| `TRAJECTORY_ATTRIBUTION` | — | attribution facts a corpus cannot show (`--attribution`) |

Corpus resolution (`corpus.py`) has two tiers, and the distinction is the
whole of it: **a corpus that was declared wins outright; ambiguity is a
property of guessing.**

1. *declared* — `$TRAJECTORY_CORPUS`, then `$WATCHDOG_LOG_DIR/builds.jsonl`
   (the same variable the writers honour, so a reader lands where the writer
   wrote), then the directory named by a committed `.isabelle-watchdog`. The
   first that exists is the answer; one that does not exist yet is simply not
   a candidate, since a project declares where its records go before its first
   build has written any.
2. *discovered* — the known layouts (`t/logs`, `results/isabelle-logs`) under
   the current project. Two distinct files here is an error, not a silent
   preference; two routes to the *same* file is not an ambiguity at all.

Treating the declared ones as mere candidates broke exactly what
`$TRAJECTORY_CORPUS` is for: standing in a project with its own `builds.jsonl`
and pointing the variable at a pooled corpus reported "several corpora found"
and refused to read either.

**`.isabelle-watchdog`** is a committed, project-owned declaration of the log
directory: first non-blank, non-comment line, relative to the marker. Same
file shape and same search as `.isabelle-query`, deliberately — a project
already carrying one marker should not learn a second convention. It is the
tier discovery cannot reach: discovery answers "where is the corpus that
already exists", so it is silent about a fresh clone and about any layout not
in `LEGACY_LAYOUTS`. One difference from `.isabelle-query`: the search is
**bounded at the project root**, because projects are routinely nested here
(`~/projects/claudecode/ndtht`) and overshooting does not merely read the
wrong thing — it would pool two repositories' trajectories into one corpus.
A marker that names nothing is an error, for the same reason a missing
`--attribution` file is: a declaration that silently does nothing is the bug
it was meant to prevent.

### Writers resolve too — the same way

`corpus.resolve_log_dir()`, used by all three writers (`build.py`,
`watchdog.py`, `record.py`). The reader's tiers minus `$TRAJECTORY_CORPUS`,
plus a default:

1. `$WATCHDOG_LOG_DIR` → 2. the marker → 3. an existing corpus under a known
layout → 4. `DEFAULT_LAYOUT` (`t/logs`).

Tier 3 is the one that was missing, and 43sp paid for it twice. Its corpus is
in `results/isabelle-logs`, named by a Makefile variable; a build run outside
make had no variable and the writer went straight to a built-in `t/logs`,
**creating a second corpus** — new instance id, empty history, every record in
it perfectly valid. Appending to the wrong file is loud; creating the wrong
file is silent and looks exactly like a first build. `trajectory check` calls
both halves sound, because each is.

Three consequences worth keeping:

- **`$TRAJECTORY_CORPUS` is deliberately not honoured by writers.** It is the
  reader override for looking at a corpus this project does not own; if it
  redirected writes, reading someone else's dataset would silently record your
  next build into it.
- **Two discovered corpora make a writer refuse, before the build.** That is
  *not* the failure `guard.py` exists to swallow: a capture that breaks is
  instrumentation failing and the build must survive it, whereas this is
  configuration, decided before anything runs, with the fix in the message —
  the class `build.py` already puts "no session to build" in. Guessing would
  split an irreplaceable dataset in a way nothing downstream can detect.
- **Creating a corpus prints one line to stderr.** Resolution only sees
  layouts it knows and markers that were committed, so a project keeping
  records elsewhere entirely still gets a fresh one minted. "creating" where
  the operator expected "appending" is the whole of the bug, in one line, once
  per corpus.

The watchdog also *publishes* its answer into `$WATCHDOG_LOG_DIR` before
importing the recorder, so the two writers in one run share a resolution
rather than deriving two that agree by construction.

### Deriving what to build

`build.resolve_session()`, the same four-rung ladder for the other question a
build needs answered — `--session`/`--dir`, then `$BUILD_SESSION`/
`$BUILD_SESSION_DIR`, then the marker's `session:`/`dir:`, then **derived**.

Derivation: the ROOT files git can see under the project (`git ls-files
--cached --others --exclude-standard -- '*ROOT'`, filtered by basename).
Exactly one ROOT declaring exactly one session is unambiguous — that session,
and the ROOT's own directory as `-d`. Anything else is an **error naming what
it found**, not a guess: building the wrong session is a confusing Isabelle
failure minutes later, and recording it puts a build of the wrong thing into
the corpus, which is worse.

The two projects are exactly the two cases. **43sp** has one ROOT declaring
`SPSlowdown`, so `$BUILD_SESSION` was carrying information the repository
already stated — which is what made its `bin/build` shim deletable. **ndtht**
has ten ROOTs declaring thirteen sessions and builds several of them, so it
cannot be derived and keeps `$BUILD_SESSION` per invocation; the error lists
all thirteen with their ROOTs.

**The ROOT parser is `isabelle-layout`'s** — the one runtime dependency, and
the only place this package imports anything external. `roots.py` is a
twenty-line adapter over `parse_root_sessions`, holding no regex and no notion
of what a session name may contain.

It is also where a missing `isabelle-layout` is restated as
`roots.MISSING_LAYOUT` — **at the import**, so both callers inherit one
message, and `trajectory` turns it into `FAIL: …` and exit 2 rather than a
traceback. Worth the six lines because of *which* half fails: `check` and the
builds themselves never reach the ROOT parser, so an absent dependency looks
like "the writer works, the readers don't", which reads as a corpus problem.
The message names the editable-install trap by name, since that is how it
actually arrived — an install made before the dependency was declared keeps
serving the metadata it was made with, so `pip show` reports no requirements
and the source it imports is nonetheless the new one.

It used to hold a grammar, in two copies. `build.py` and `attempts.py` each
had a regex and they disagreed: `attempts.py` matched `"?([A-Za-z0-9_']+)"?`,
which stops at the first character outside that class *inside* the quotes, so
`session "HOL-Analysis"` was built under that name and attributed to `HOL` — a
different real session — with nothing downstream able to tell.

**Why a dependency, given the no-dependencies rule.** The rule was formed
against `isabelle_query.common`: a parser reachable only by installing an
11k-line querying CLI, with that tool's release cadence and its userbase's
constraints attached. That is what "anything it imports is something that can
break a build" was really refusing. `isabelle-layout` is that parser split out
for this exact purpose — it declares no dependencies of its own, so the
transitive tree stays empty, and it is the parser rather than a tool that
contains one. Keeping a private copy after the weight was removed would have
been applying the proxy after the thing it stood for was fixed.

Nor does the import land mid-build: `build.py` reaches it while deriving the
session, *before* `isabelle build` is spawned, and `attempts.py` long after,
reading a corpus. A failure there is configuration — loud, before anything
runs — which is the class `build.py` already puts "no session to build" in,
not the class `guard.py` swallows.

Two things stay local, and `roots.py` is exactly those two:

- **A fragment is not a file.** `attempts.py` reads the added and context
  lines of a hunk in a ROOT — a ROOT with holes — and the public entry point
  takes a path, so `sessions_in_fragment` writes the fragment to a temporary
  ROOT and parses that. Sixty hunks across both real corpora, so the cost is
  nothing; it is a bridge, and a text-taking entry point upstream removes it.
  Parsing per hunk rather than per line is *better* than what it replaced: a
  `(* … *)` wholly inside the payload now hides what it encloses. What no
  reader can see is an enclosing `(*` that was never captured, so a
  commented-out session may still map a name to a directory — an unused entry,
  against losing the mapping entirely.
- **`<anon>` is not a name.** `isabelle-layout` calls a `session` keyword with
  nothing after it `<anon>`; Isabelle rejects that outright. It is the absence
  of a name, so `roots.py` drops it. Not hypothetical for the fragment reader:
  the tokeniser's identifier class excludes `#`, so `# not a session` reduces
  to the bare keyword, and prose reaching a hunk boundary does that easily.

**Which ROOTs to read stays here too**, and that is a question about this
project rather than about ROOT syntax: `build.py` asks git
(`git ls-files --cached --others`), where `isabelle_layout.discover_roots`
walks the filesystem. See below for why.

`tests/test_roots.py` is therefore not a conformance suite — whether that
parser matches Isabelle is checked in its own repository, against a corpus it
regenerates from a real `isabelle sessions -d`. Asserting a dependency's
behaviour back at it would fail whenever it legitimately improved. What is
tested here is the fragment seam and the spellings this project acts on.

Two details worth keeping:

- **Git, not a filesystem walk.** `.git`, ignored build trees, virtualenvs and
  vendored AFP checkouts are exactly where a stray ROOT lives. A walk needs a
  pruning list, and a pruning list is a guess that goes stale. `--others`
  includes untracked-but-not-ignored, so a project whose ROOT is not committed
  yet still resolves.
- **The directory is derived separately.** Given a session name from any rung,
  `-d` is wherever the ROOT declaring it lives — strictly better than the `.`
  this used to default to, and it means a project only ever states the
  *session*. If no visible ROOT declares the named session it falls back to
  `.`, reproducing the old behaviour rather than failing a project whose ROOT
  git cannot see.

`root_files()` returns **project-relative** paths, because that is what `-d`
wants and what an error should show; reading one means `sessions_in(project /
r)`. Resolving it against the cwd works only when the operator happens to be
standing at the top level — the same class of bug as every row in the table
above, and the tests caught it.

`isabelle-build --where` reports the resolved session and directory alongside
the corpus, since with both derived, "which session does this project build"
is no longer answerable by reading a Makefile.

### Capture is optional; supervision is the part that always runs

`guard.capture_enabled()` reads `$BUILD_RECORD`, and `--no-record` on both
`isabelle-build` and `isabelle-watchdog` sets it for one call. On by default,
because the capture is the reason the supervision was written — but the
supervision is genuinely useful alone (killing a looping tactic and naming its
line needs no dataset), and a project that only wants that should not
accumulate records it will never read in a directory it did not choose.

Three details that are load-bearing:

- **The check is in the watchdog, not inside `record()`.** `record` resolves
  the project, the log directory and the pending note at *import* time, so a
  project that declined capture should not be paying for — or failing on —
  any of that. Skipped entirely, not entered and short-circuited.
- **An unrecognised value is an error.** The failure is one-sided: read as
  *on*, a misspelt "off" quietly collects the data someone declined, and the
  first they hear of it is a corpus. Same reasoning as the empty marker.
- **`recording=False` softens the ambiguity refusal** in `resolve_log_dir()`.
  With nothing being written there is no dataset to protect, and refusing to
  start a build over it would be the tail wagging the dog. The directory is
  still resolved — `last-build.log` goes there.

`isabelle-build --where` reports the resolved corpus, the rule that chose it,
and whether capture is on. It exists because a tool that resolves a path by
four rules should be able to say which one fired; the alternative is an
operator deducing it from the source, which is how a wrong answer stays
believed. It is also the answer to "what will adopting this do to my repo",
asked before the first build rather than after.

## Commands

```sh
# build (from the project being built, not from here)
BUILD_SESSION=MySession isabelle-build -m 'diagnosis: X; change: Y; expect: ok'
isabelle-build --lint -m '...'         # check the note, do not build
isabelle-build -- -o quick_and_dirty   # extra args to isabelle build

# read the corpus — CORPUS is optional everywhere
trajectory --help                      # all of them, grouped
trajectory check                       # regenerate every payload, compare
trajectory repair --apply [--heuristic]
trajectory replay [--from N] [--to N]
trajectory extract N DEST              # materialise attempt N's sources
trajectory lengths --fit --by-project
trajectory classify BUILD_ID -v
trajectory audit                       # list the audits
trajectory audit oneshot [-i CORPUS]   # run one
```

`audits/` is the validation suite for the readers: each module interrogates one
measurement decision (is the 1-shot rate measuring search or bookkeeping? is a
timeout a proof event or load?) and re-derives the quantity a second way, so
agreement is evidence rather than tautology.

**The catalogue is derived, not declared** (`audits.catalogue()`, which parses
each module's docstring rather than importing it, so one broken audit does not
hide the other five). The docstring here used to carry the list and it drifted
— it named a `loci` module that has never existed, those checks being
`tests/test_attribution.py`. An inventory beside the thing it inventories
reads as authoritative and is the last thing anyone updates; the same argument
as *attribution is derived, not declared* one section up.

Reachability was the other half. These were `python -m
isabelle_watchdog.audits.<name>` and unmentioned in `trajectory --help`, so
finding them required already knowing they were there — for a suite whose
whole job is guarding numbers that get published, that is not reachable at
all. `audit` is deliberately unlike the other subcommands: it takes `-i
CORPUS` and passes its remaining argv through, because an audit resolves its
own corpus and fits its own attribution.

### Packaging

`hatchling`, src-layout, one runtime dependency (`isabelle-layout`).

**The version is stated once, in `pyproject.toml`.** One file holds what the
project is — name, version, dependencies, entry points — rather than splitting
it across a manifest and a module that then have to be kept agreeing.
`__init__.__version__` reads it back from the installed metadata, so the two
cannot drift; a source tree that was never installed reports `0+unknown`,
because a wrong version sends a bug report to the wrong commit and an absent
one does not.

*It was the other way round*, dynamic from `__init__.py` via
`[tool.hatch.version]`, justified by `__version__` working uninstalled. That
justification was thin against a project whose own rule is *test against an
install*.

**One wart, and it bites at exactly the wrong moment.** An editable install
serves the metadata recorded when it was made, so right after a version bump
`-V` reports the *old* number until `pip install -e .` runs again. A release
that skips it ships binaries disagreeing with their own changelog.
`test_the_version_comes_from_pyproject_and_nowhere_else` fails when they
disagree and says which command fixes it.

That wart is the whole cost, and it has been weighed and accepted: a number
anyone can see in the file that states the rest of the project beats a
reinstall step during development. **`isabelle-layout` chose the other way**
(`dynamic = ["version"]`), so the two siblings differ here on purpose — do
not "align" them without re-deciding, and note that the argument does not
transfer, since only one of them is a library whose consumers read
`__version__`.

**Why `hatchling`, now that the version is static?** Honestly, not much — the
dynamic version was most of it. What is left is that
`[tool.hatch.build.targets.sdist] include` puts the sdist contents in
`pyproject.toml`; setuptools governs those from a separate `MANIFEST.in`, so
switching would *add* a file and split the packaging story across two, against
the same "one place" argument that moved the version. Not a strong reason,
just the standing one. Nothing here needs a build step, so if the backend ever
has to change, nothing depends on it beyond those two `[tool.hatch.build]`
tables.

```sh
python3 -m venv /tmp/v && /tmp/v/bin/pip install .   # or -e .
/tmp/v/bin/trajectory --help
/tmp/v/bin/trajectory --version
```

**Test against an install, not `PYTHONPATH=src`.** Three failures showed up
only under a real install and would have passed otherwise:
`trajectory._attempts()` loading `attempts.py` via `spec_from_file_location`
(no parent package, so its own relative import failed), a
`readme = "README.md"` that did not exist, and now `__version__`, which has no
metadata to read outside one.

### Releasing

`.github/workflows/release.yml`, kept **mechanism-identical** to
`isabelle-layout`'s and `isabelle-query`'s — only the release title differs —
so a fix to any of the three carries across without a diff to read.

**The release notes are the version-bump commit's message.** Not a style
preference: the workflow reads `repos/<repo>/commits/<tag>` and publishes that
commit's message as the Release body. So that one commit is written *for
users* — what changed since the last release — and everything about how the
work happened belongs in the commits around it.

```sh
git commit -m "0.4.0 — <headline>" -m "## Fixed …"
git tag -a v0.4.0 -m "isabelle-watchdog 0.4.0"
git push origin main
git push origin v0.4.0        # `git push` alone does not push tags
```

Four things that have each cost something, upstream or here:

- **Not the tag annotation.** `gh release create --notes-from-tag` reads the
  annotation and silently falls back to the commit message when the runner's
  local tag is lightweight, which mis-published `isabelle-query` v0.2.5.
  Dereferencing the tag to its commit is deterministic, so that is what the
  workflow does — which means the annotation is read by nothing.
- **The workflow must be in the tagged commit's tree.** For `on: push: tags`,
  Actions resolves the workflow from the pushed ref, not from the default
  branch. Commit it *before* the release it is meant to publish.
- **Never `--amend` a release commit that has been pushed.** It leaves the
  branch a sibling of the remote rather than a descendant, and the only ways
  out are a force-push or a reconciling merge. Re-tagging is fine — the
  workflow edits an existing Release rather than erroring — so a bad publish
  is repaired by a *new* commit and a moved tag, never by rewriting.
- **`pip install -e .` before tagging.** The editable-install wart above means
  `-V` reports the pre-bump number until you do, and
  `test_the_version_comes_from_pyproject_and_nowhere_else` is what catches it.
- **Rename `## [Unreleased]` in the same commit as the bump.** The notes point
  at `CHANGELOG.md`, so a version with no section there publishes a pointer to
  nothing — quietly, since the Release itself renders fine.
  `test_the_changelog_names_the_version_being_released` catches it.

**Two of those four are tests; the other two are prose, and that split is the
answer to "should this be a `make release`".** A check runs unprompted; a
target only helps someone who already remembered the procedure — who is
exactly the person who was not going to make the mistake. So anything
checkable moves into `tests/test_cli.py`, and what is left is genuinely
unsequenceable: the workflow-in-the-tagged-tree rule and `git push` not
pushing tags both fail by silence, on a machine no test is running on. A
Makefile would also be a second statement of this runbook, and this repo has
twice been bitten by an inventory beside the thing it inventories (the audits
catalogue, the attribution lists) — both since replaced by derivation.

PyPI upload is manual and separate (`python -m build`, `twine check --strict`,
`twine upload`), after the tag, so the tag names the tree that produced the
artefacts. `isabelle-layout`'s `docs/publishing.md` is the long-form runbook
for the parts that are once-per-person rather than once-per-project — account,
2FA, token scoping, the TestPyPI rehearsal.

### `-V` / `--version`

All three entry points, both spellings. Absent, they did more than say
nothing: `isabelle-watchdog -V` treated `-V` as *the command to supervise*, so
it resolved a log directory, **created a corpus** and recorded the failure as
an attempt — the silent-creation failure this file documents twice over,
reached by a typo. `--version` was quieter: the child is wrapped in `stdbuf`,
so it ran `stdbuf --version` and reported that program's version instead.

Hence the other half of the fix: **an unrecognised leading `-word` is a usage
error**, not a command name, exit 2, before anything is resolved or created.
A program genuinely named that way is what `--` is for, which the wrapper's
own docstring already said.

### Tests

pytest, in the ordinary way. It is a *test* dependency, not a runtime one:
nothing under `tests/` is installed, imported by the package, or present when a
build runs. Keeping the runtime list short protects the build; it was never a
reason to hand-roll a runner every contributor then has to learn.

```sh
pip install -e ".[test]"                # pulls isabelle-layout from PyPI
pip install ../isabelle-layout          # only to test an unreleased change to it
pytest -m "not slow and not isabelle"   # pure logic — seconds
pytest -m "not isabelle"                # + real subprocesses
pytest                                  # + a real isabelle build
```

Two markers, both `--strict`, so a typo in one marks nothing rather than
silently opting a slow test back in:

- **`slow`** — spawns real processes. On a Mac with a security agent in the
  exec path, `git` costs ~0.5 s per invocation and one capture makes ~25 of
  them, so these dominate the wall clock on that machine and barely register
  elsewhere.
- **`isabelle`** — needs a real Isabelle *and* a prebuilt HOL heap.

Three layers, deliberately, because each catches what the others cannot:

| layer | what it can decide |
|---|---|
| pure functions over text and records | every parse, verdict and statistic |
| the watchdog against a **fake** child (`sh -c` printing canned output) | the three kill conditions, the loop detector, battery scaling — in seconds, with no Isabelle |
| the watchdog against a **real** `isabelle build` | whether Isabelle still prints what the parsers assume |

The middle layer is why the suite is usable: supervision decides everything
from the text on a pipe and the clock, so a shell script emitting the right
lines at the right times exercises the real code path. It found two defects in
the read loop that a corpus never could — see the fixtures in
`tests/conftest.py`.

**A fake that counts calls is measuring the test machine.** The contention
tests drive a fake `ps` (`cpu_stub`, in the supervision test file) to hold a
duty cycle still. It used to advance its reported CPU a fixed step *per call*,
which makes the presented duty `step × calls-per-second` — and
calls-per-second is not 1, because each sample costs the sampler two `pgrep`s
and the stub itself, at 150–300 ms per exec on a Mac with a security agent in
the path. A nominal 1 s interval measured 2.3 s, so every duty in the group
arrived at 0.43× its nominal value: `duty 0.1` read 0.043, under `STALL_DUTY`,
and the *starved* build one test had set up was measured as a *stalled* one.
It failed 3/3 here and presumably passed wherever it was written, which is the
tell — the error is systematic in the machine's exec latency, and unbounded as
that grows. The stub now reports against the child's own `ps -o etime=`, so
the sampler's cost changes *when* samples land rather than what they say; what
is left is whole-second resolution, ±40% and bounded. The general form: **a
fixture that stands in for a measurement must not derive its answer from the
thing being measured.**

`test_isabelle_integration.py` is the third layer: a green build, a false lemma
(so a locus is extracted from genuine Isabelle output), and an axiom that
rewrites `f x → f (Suc x)` forever. The last is the one worth its ~2m45s — it
asserts `timeout_reason == "loop_progress"` and that the named line is the
looping `by`, so it fails if Isabelle ever stops re-emitting its progress
warning and the three coupled constants stop lining up. Skips cleanly without
Isabelle or a HOL heap; building HOL to run a test would cost more than the
test is worth.

Fixtures worth knowing about, all in `tests/conftest.py`:

- **`clean_env`** (autouse) clears every variable in the environment contract.
  The whole public API is environment variables, so a developer with
  `WATCHDOG_LOG_DIR` exported — that is, anyone using this on a real project —
  would otherwise run a different suite from CI.
- **`repo`** copies a template repository rather than running `git init`, for
  the exec-latency reason above.
- **`trajectory`** records five *real* attempts once per session — a tracked
  edit, an untracked theory beside files the allowlist must exclude, a
  mid-run commit, a no-op rebuild, and a prose-only edit. The integrity
  readers' claim is that regeneration is the ground truth, so they have to be
  tested against genuine payloads over a genuine object store; synthetic
  records would only confirm that the code agrees with itself.
- **`watchdog`** and **`stub_bin`** run the supervisor against a fake child,
  and put a fake `pmset` on `PATH` (the only way to reach the battery branch —
  no environment variable does).

Note that any test exercising a timeout runs `kill_tree`, which ends with
`pkill -TERM -f poly` as a safety net for orphaned Poly/ML. That will signal an
unrelated interactive Isabelle session on the same machine. It is the tool's
production behaviour, not something the tests add.

### Verifying a change

Both corpora survive and are the regression suite. There is no mocking to do —
run every subcommand against both, before and after, and diff:

- `~/projects/ndtht-trajectories/stac-wip/builds.jsonl` (695 records)
- `~/projects/trajectories/43sp/builds.jsonl` (40 records)

For the *write* path, that is now what the `trajectory` fixture does — `git
init` a scratch repo, edit theories, capture, then `check` and `replay` against
the result. It is the shape of test that caught the `PROJECT_DIR` bug, and no
amount of reading would have.

**Validating against a real project writes to its real corpus** unless you
say otherwise. Set `WATCHDOG_LOG_DIR` to a scratch directory and confirm with
`isabelle-build --where` before building; `--no-record` is the wrong tool,
since it switches off the recorder you came to test.
[`docs/working-on-the-tooling.md`](docs/working-on-the-tooling.md) has the
rest, including what to do when something does get into a corpus, and why
generating load needs cleanup that cannot be skipped.

## Corpora are separate repos

Data does not live with the tools. Each project symlinks its log dir's
`builds.jsonl` at a corpus repo (`~/projects/trajectories`,
`~/projects/ndtht-trajectories`). Keep this split: diffs are stored inline, so
a corpus is irreplaceable data rather than a derived cache, with a different
backup and sharing story from the tools.

## Known follow-up work

- `legacy_convert.py` was a one-shot migration off the git-chain
  prototype; kept as a record of how the initial dataset was produced, not on
  any live path.

## What stayed behind

`ndtht/bin/shape-vs-trajectory.py` and `probe-noproof.py` join trajectory data
to one project's Isabelle proof shapes (they shell out to `query` and glob
`t/<dir>`), so they belong to the application. The rule they illustrate is
general: **keep project-specific joining in separate scripts, out of the
readers.** Readers take their input path as an argument and hold repo-relative
paths as overridable defaults, never constants.
