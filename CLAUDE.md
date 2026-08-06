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
        trajectory.py          the single reader CLI     -> trajectory
        attempts.py            its reading/measuring views (a module, not a CLI)
        build.py               note-carrying entry point -> isabelle-build
        export.py  legacy_convert.py
        audits/                validation suite for the readers' statistics
    tests/                     pytest; conftest.py holds the fixtures
    docs/logging-design.md     the design doc the code cites by section

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

### 1. `watchdog.py` — process supervision

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
| `WATCHDOG_LOG_DIR` | resolved, see below | where records go; read by watchdog, recorder *and* readers |
| `BUILD_SOURCE_PATHSPECS` | `*.thy *ROOT *ROOTS` | what counts as source |
| `BUILD_SESSION` / `BUILD_SESSION_DIR` | — / `.` | session to build, and where its ROOT is |
| `BUILD_NOTE` / `BUILD_NOTE_FILE` | — | note text / pending-note path |
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

## Commands

```sh
# build (from the project being built, not from here)
BUILD_SESSION=MySession isabelle-build -m 'diagnosis: X; change: Y; expect: ok'
isabelle-build --lint -m '...'         # check the note, do not build
isabelle-build -- -o quick_and_dirty   # extra args to isabelle build

# read the corpus — CORPUS is optional everywhere
trajectory --help                      # all thirteen, grouped
trajectory check                       # regenerate every payload, compare
trajectory repair --apply [--heuristic]
trajectory replay [--from N] [--to N]
trajectory extract N DEST              # materialise attempt N's sources
trajectory lengths --fit --by-project
trajectory classify BUILD_ID -v
```

`audits/` is the validation suite for the readers: each module interrogates one
measurement decision (is the 1-shot rate measuring search or bookkeeping? is a
timeout a proof event or load?) and re-derives the quantity a second way, so
agreement is evidence rather than tautology. Run one with
`python -m isabelle_watchdog.audits.<name> -i CORPUS`.

### Packaging

`hatchling`, src-layout, version single-sourced from
`src/isabelle_watchdog/__init__.py` (`[tool.hatch.version]`). No runtime
dependencies, deliberately: this runs beside a build, so anything it imports is
something that can break one.

```sh
python3 -m venv /tmp/v && /tmp/v/bin/pip install .   # or -e .
/tmp/v/bin/trajectory --help
```

**Test against an install, not `PYTHONPATH=src`.** Two failures showed up only
under a real install and would have passed otherwise: `trajectory._attempts()`
loading `attempts.py` via `spec_from_file_location` (no parent package, so its
own relative import failed), and a `readme = "README.md"` that did not exist.

### Tests

pytest, in the ordinary way. It is a *test* dependency, not a runtime one:
nothing under `tests/` is installed, imported by the package, or present when a
build runs. The no-dependencies rule protects the build; it is not a reason to
hand-roll a runner every contributor then has to learn.

```sh
pip install -e ".[test]"
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
