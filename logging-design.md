# Build-record design: cost + trajectory logging

**Status:** Trajectory-axis MVP implemented 2026-05-27 as a prototype
(`bin/build_record.py` + the `bin/isabelle-watchdog.py` hook, read by
`bin/attempts.py`); see §14.  The prototype proved the value of capture
and scoped the real design; on 2026-06-19 it was superseded by the
**portable per-episode patch-file** model (§16): each attempt records its
incremental `diff`, episodes are materialised as self-contained files
anchored on public commits, and the prototype's git-ref-chain store is
retired (the initial data converted by `bin/convert-legacy-trajectory.py`).
Cost axis (§§2–11) designed 2026-05-18, not yet implemented.  Trajectory
axis merged in 2026-05-27 from the former `attempt-capture-design.md`.

## 1. Context

This is the design for what the build wrapper records on **every**
`make build` invocation, keyed by a `build_id`.  There are two axes,
and the pipeline is one:

- **Trajectory** (§§12–15) — *which formulations were tried, which
  failed and how, which finally worked.*  The proof-search
  progression.  This is the higher-resolution, more principled view
  of "what is being logged," and the reason the pipeline is worth
  building; it also yields a dataset of independent research interest
  (§12.1).
- **Cost** (§§2–11, below) — *how slow is what worked* (per-build /
  per-theory / per-`by` timing, kill diagnosis, regime-change
  detection).  Valuable for diagnosis and regression-watching, but on
  its own it was never quite worth pursuing — it resolves cost, not
  trajectory.

Both come from the same per-invocation wrapper and share `build_id`;
the cost-axis sections that follow stand, reframed as one axis of the
unified record.  The original cost-axis context follows.

The existing `bin/isabelle-watchdog.py` has been invoked thousands of
times in this project across attempts 2 and 3.  Each invocation
overwrites `t/logs/last-build.log`; no cumulative history is kept.
The terminal one-line summary (`OK 7 theories 23s ...`, `STUCK ...
80% no output for 20s`, `LOOP repeated 3x ...`) is shown to the user
and then discarded.

Costs of having no structured history fall into **two distinct
audiences**:

- **Active in-session diagnosis** — when a build times out, the
  question that needs answering is *"which `by` or `apply` invocation
  is the slow one?"*, at sub-theory granularity.  The current TIMEOUT
  output identifies the theory; localising further requires the
  sorry-skeleton binary-search dance documented in
  `feedback_no_buildclean_reflex.md`.  Lived experience says this is
  the most frequent and most painful gap.
- **Post-hoc trend monitoring** — "is theory T usually this slow, or
  did it just regress?", "which theories are growing fastest in build
  time?", "did this commit push the build past the ~270s prompt-cache
  TTL (a 10×+ cost amplifier on the next session turn)?".  Cumulative
  per-build / per-theory data answers these.

These two audiences want data at *different granularities*:
diagnosis needs per-`by`, trend monitoring needs per-theory.  A
design that addresses only one is half-useful.  In particular, a
per-theory data layer would not by itself have helped the last
TIMEOUT-driven session — the theory-level fact "AlphabetEnlargement_
Classical.thy regressed to 38s" only restates the timeout output in
different words; localising the slow `by` still requires the
sorry-skeleton dance.  Per-`by` instrumentation is the higher-leverage
primitive, not a future-work afterthought.

Costs of having no kill-time disambiguation: we cannot tell
loop-style timeouts from slow-proof-style timeouts any better than
the watchdog's three coarse `timeout_reason` buckets.  This is
orthogonal to granularity and lives at the build-level.

The existing watchdog is battle-tested across thousands of invocations
of varied failure modes (infinite loops, slow proofs, error paths,
document builds, graph builds).  It is not to be refactored under
load.

## 2. Goals

The design organises around **three layers**, ordered by leverage
from highest to lowest:

### Layer 2 — Proof observability (per-`by` timing)

Capture per-tactic-invocation timing for every `by` / `apply` /
`proof` step in the build.  Each record carries `(build_id, theory,
source_line, command_kind, tactic_head, elapsed_ms)`.  Mechanism gate
in §9.1: either parse Isabelle's `command_timing=true` output if it
produces source-annotated per-`by` records at acceptable overhead, or
fall back to ML-level tactic decorators (`timed_by`) defined in a
small `t/base/Timing.thy` and applied at proof sites that need
diagnosis.  Listed first because it pays off in active diagnosis —
the case where the project burns the most session time today.

### Layer 1 — Build observability (per-build, per-theory)

Cumulative JSONL records of every build invocation (build-level
metadata, exit code, kill diagnosis, cache-burn flag, etc.) and one
record per `(build_id, theory)` for per-theory elapsed/cpu timing
parsed from each `Timing T (N threads, X.Xs elapsed, Y.Ys cpu)` line.
This is the layer that answers trend questions and supports the
regime-change detector at theory granularity.  Important for
post-hoc analysis but does not by itself help with active
diagnosis.

### Layer 3 — Proof enforcement (deferred)

`by100`-style per-`by` budget enforcement that *fails* the build on
slow tactics instead of letting them eat 20s before the wall budget
fires.  Distinct from Layer 2 (which observes); this layer prevents.
Calibration of the per-step budget needs real NDTHT timing data —
exactly what Layer 2 produces — so this layer is genuinely downstream
and stays deferred (Urban's ML snippet is preserved verbatim in §11).

### Supporting goals (cross-layer)

- A kill-time heuristic that distinguishes loops from slow proofs
  using process-state observation in addition to the existing
  `timeout_reason` field.  Build-level; see §6.
- A small analyser script (`bin/build-stats.py`) that converts the
  raw data into actionable summaries, so the data has a consumer
  from day 1.  Includes proof-observability queries ("slowest `by`
  invocations in last build / by theory") alongside build-observability
  queries.
- A regime-change detector at *both* granularities: per-theory
  ("AlphabetEnlargement_Classical is 3× its 20-run median today") and
  per-`by` ("the `by metis` at line 2479 used to take 0.4s, today
  it takes 27s").  The per-`by` detector is the killer feature for
  active diagnosis — it localises the regression to the line of
  source the user wants to look at.
- Parallel deployment alongside the existing watchdog until trust
  is earned, then a clean swap.

## 3. Non-goals (for now)

- **Layer-3 enforcement.**  Layer 2 lands first; Layer 3 waits for
  the calibration data Layer 2 produces.  See §11 for the ML
  snippet and calibration notes.
- **Archiving full Isabelle stdout across builds.**  Current verbose
  output (`isabelle build -v`) is repetitive (progress ticks), and
  the structured per-theory + per-`by` timing is sufficient.  We
  keep the latest invocation's full stdout in `last-build.log` for
  the terminal-tail experience; we do not archive prior
  invocations.
- **Modifying `bin/isabelle-watchdog.py` during the parallel
  period.**
- **Modifying the `Makefile` during the parallel period.**  Targets
  `build`, `buildclean`, `graphs`, `pdf-full` keep invoking the
  existing watchdog.

## 4. Architecture

### 4.1 Scripts and theories

- **New:** `bin/isabelle-build.py` — Layer 1 wrapper; also the
  parser path for Layer 2 if §9.1's mechanism gate picks
  `command_timing=true`.
- **New:** `bin/build-stats.py` — analyser (§8).
- **New (if §9.1's mechanism gate picks ML decorators):**
  `t/base/Timing.thy` — Isabelle theory defining `timed_by` and
  related method decorators (§4.4).
- **Existing:** `bin/isabelle-watchdog.py` — frozen during parallel
  development.  Same path, same behaviour, no changes.
- **Initial use:** new script invoked manually for testing.  Makefile
  integration is a later step.

### 4.2 CLI

```
bin/isabelle-build.py
    [--log-dir DIR]                  default: t/logs2
    [--build-id ID]                  default: YYYYMMDDTHHMMSS-<pid>
    [--activity-timeout N]           default: 20  (match existing)
    [--wall-timeout N]               default: 40  (match existing)
    [--repetition-threshold N]       default: 3   (match existing)
    [--command-timing/--no-command-timing]
                                     default: on iff §9.1 picks mechanism (a)
    [--snapshot-on-kill/--no-snapshot]
                                     default: on
    -- <isabelle build args...>
```

### 4.3 Files produced

Under `--log-dir` (default `t/logs2/`):

- `builds.jsonl` — Layer 1: one record appended per invocation.
  Cumulative.
- `timings.jsonl` — Layer 1: one record appended per `(build_id,
  theory)` derived from each `Timing T (N threads, X.Xs elapsed,
  Y.Ys cpu)` line the build emits at theory completion.
- `commands.jsonl` — Layer 2: one record appended per `(build_id,
  theory, source_line, command_kind)` for every observed `by` /
  `apply` / `proof` step.  Populated whether the source mechanism
  is `command_timing` parsing (a) or ML decorators (b); the
  consumer doesn't care.
- `last-build.log` — current invocation's full stdout, overwritten
  each run.  Same role as in the existing watchdog.

No `archive/` subdirectory; no historical full-stdout retention.

### 4.4 Layer-2 mechanism options

The §9.1 spike decides between two mechanisms.  Both produce the
same `commands.jsonl` schema (§5.3); downstream analyser code is
identical.

**Option (a) — Parse `command_timing=true` output.**  Isabelle has
an option to emit per-command timing.  The spike verifies (i) it
produces per-`by` records with source-line annotations, and (ii)
the build-wide overhead is acceptable (≤5% target; up to ~10% may
be tolerable given the leverage).  If yes,
`bin/isabelle-build.py` parses these records out of build stdout
into `commands.jsonl`.  *Source code unchanged.*

**Option (b) — ML tactic decorators.**  Define in
`t/base/Timing.thy`:

```isabelle
method_setup timed_by =
  \<open>
    Method.text_closure >> (fn text => fn ctxt => fn facts =>
      let
        fun tac st =
          let val t0 = Time.now ()
              val res = method_evaluate text ctxt facts st
              val elapsed = Time.toMilliseconds
                              (Time.- (Time.now (), t0))
              val _ = log_command ctxt elapsed
          in res end
      in
        SIMPLE_METHOD tac facts
      end)
  \<close>
  "time a proof method, log per-step elapsed_ms"
```

Plus `log_command` writes to a sidecar file (e.g.
`t/logs2/ml-commands.jsonl`), which `bin/isabelle-build.py` merges
into `commands.jsonl` after the build completes.  Usage:
opportunistically replace `by metis` → `timed_by metis` at proof
sites that need diagnosis, leaving the rest of the proof code
unchanged.  *Source-code opt-in*, so the instrumentation is
targeted rather than global — and overhead applies only at
instrumented sites by definition.

Foundation overlap with Layer 3 (§11): the same Timing.thy is the
natural home for `by100` enforcement when that lands, since the
two methods share most of their ML plumbing.

### 4.5 `build_id` format

`YYYYMMDDTHHMMSS-<pid>`.  Sortable lexicographically by time, unique
modulo same-second-same-PID collisions (which the script avoids by
deferring start-time stamping until immediately before the subprocess
fork).  No UUID dependency.

## 5. Schema

### 5.1 `builds.jsonl`

```json
{
  "build_id": "20260513T103045-12345",
  "started_at": "2026-05-13T10:30:45Z",
  "finished_at": "2026-05-13T10:31:12Z",
  "wall_elapsed_s": 27.3,
  "git_sha": "f00e543",
  "git_dirty": false,
  "argv": ["isabelle", "build", "-v", "-D", "t", "NDTHT"],
  "log_name": "last-build.log",
  "budgets": {
    "activity_timeout": 20,
    "wall_timeout": 40,
    "repetition_threshold": 3
  },
  "exit_code": 0,
  "timeout_reason": null,
  "isabelle_elapsed_s": 23.1,
  "isabelle_cpu_s": 41.2,
  "n_theories": 7,
  "first_error": null,
  "last_in_flight_theory": null,
  "last_in_flight_pct": null,
  "cache_burn_risk": false,
  "kill_diagnosis": null,
  "kill_snapshot": null,
  "tail_lines": []
}
```

Fields populated only on failure / timeout:

- `timeout_reason`: `"activity" | "wall" | "repetition" | null`
- `first_error`: text of first `***` line (truncated to 200 chars)
- `last_in_flight_theory`, `last_in_flight_pct`: from the existing
  `THEORY_PROGRESS_RE` in the watchdog
- `kill_diagnosis`: derived field, see §6
- `kill_snapshot`: see §6
- `tail_lines`: last 20 stripped stdout lines before kill

Always-derived:

- `cache_burn_risk`: `wall_elapsed_s > 270`.  5-minute prompt-cache
  TTL threshold; flags successful-but-expensive builds too.

Power normalisation (always present, from `bin/isabelle-watchdog.py`):

- `power`: `"battery" | "ac" | "unknown"` (battery detected via
  `pmset -g ps`, macOS only; `"unknown"` elsewhere).
- `battery_factor`: the factor the watchdog multiplied its activity and
  wall budgets by — `> 1.0` only when on battery (default `2.0`, env
  `BATTERY_FACTOR`).
- `elapsed_s_ac`: `elapsed_s / battery_factor`, i.e. the wall time
  normalised to AC-equivalent seconds.  Equals `elapsed_s` on AC /
  unknown.  Compare *this* field across runs so battery throttling
  (~2x) does not masquerade as a cost regression; raw `elapsed_s`
  stays for the true wall time.

### 5.2 `timings.jsonl`

```json
{
  "build_id": "20260513T103045-12345",
  "theory": "NDTHT.AlphabetEnlargement",
  "elapsed_s": 12.3,
  "cpu_s": 18.7,
  "threads": 1
}
```

One row per `Timing T (...)` line in the build output.  Existing
watchdog regex captures all the fields already; new script just emits
a row instead of discarding all but the last.

### 5.3 `commands.jsonl`

```json
{
  "build_id": "20260513T103045-12345",
  "theory": "NDTHT.AlphabetEnlargement",
  "source_line": 1247,
  "command_kind": "by",
  "tactic_head": "metis",
  "elapsed_ms": 287,
  "source": "command_timing"
}
```

`source_line` is the Isabelle source line of the `by` / `apply` /
`proof` keyword.  `tactic_head` is the first keyword of the tactic
expression (e.g. `simp`, `blast`, `metis`, `auto`) — useful for
slicing slow-`by` reports by which tactic is the culprit.  `source`
indicates which mechanism produced the row (`"command_timing"` or
`"ml_decorator"`); mostly for debugging mixed-instrumentation
transitions, not load-bearing.

## 6. Kill diagnosis heuristic

Computed at kill time from `timeout_reason`, the `kill_snapshot`,
and recent progress ticks.

### 6.1 Snapshot

`psutil` is used.  Before `kill_tree` fires, the new script enumerates
all descendants of the build subprocess, filters for `poly` (the SML
runtime running Isabelle), and captures each:

```json
{
  "pid": 12348,
  "cpu_pct": 99.2,
  "rss_mb": 412.5,
  "num_threads": 4,
  "create_time": "2026-05-13T10:30:46Z"
}
```

`cpu_pct` is the headline field for the heuristic.  `psutil` requires
priming (`cpu_percent(None)` then sleep ≥0.1s then `cpu_percent(None)`),
so the snapshot path is ~0.2s slower than current kill — acceptable.

### 6.2 Diagnosis table

| `timeout_reason` | poly CPU%   | progress in last 10s | `kill_diagnosis` |
|------------------|-------------|----------------------|------------------|
| repetition       | —           | —                    | `"loop"`         |
| activity         | high (>80)  | none                 | `"slow"`         |
| activity         | low (<10)   | none                 | `"stuck"`        |
| wall             | —           | yes                  | `"slow"`         |
| wall             | —           | no                   | `"loop"`         |
| else             | —           | —                    | `null`           |

The heuristic is approximate and exposed as a derived field for
analysis; nothing in the build path branches on it.

## 7. Regime-change detector

Two granularities; both run on every successful build's completion.

### 7.1 Per-theory

In `bin/build-stats.py`, per theory:

- Compute median and MAD over the theory's last 20 successful runs.
- Flag the latest run if
  `elapsed_s > median + 3·MAD`  **AND**  `elapsed_s > 1.5 × median`.
- Skip if the theory has fewer than 5 prior runs (insufficient
  baseline).

The double-guard prevents false positives on theories with very low
absolute variance (where 3·MAD might be tiny seconds).

### 7.2 Per-`by`

Same shape, keyed by `(theory, source_line, command_kind)`:

- Compute median and MAD over the triple's last 20 successful
  appearances.
- Flag the latest run if
  `elapsed_ms > median + 3·MAD`  **AND**  `elapsed_ms > 2 ×
  median`  **AND**  `elapsed_ms > 100` (absolute floor: sub-100ms
  regressions aren't actionable).
- Skip if the triple has fewer than 5 prior runs.

`bin/isabelle-build.py` calls both detectors on successful
completion.  When per-`by` flags fire, they take precedence in the
summary output because they localise to the line of source the user
actually wants to look at:

```
OK  7 theories  38s elapsed  56s cpu
  ⚠ NDTHT.AlphabetEnlargement.thy:2479
      `by metis` took 27.0s (was median 0.4s over last 20)
  ⚠ NDTHT.AlphabetEnlargement: 38.0s total (was median 14s over last 20)
  log: t/logs2/last-build.log
```

When no flag fires, the terminal output is feature-comparable to
the existing watchdog.

## 8. Day-1 analyser

`bin/build-stats.py` with no args prints:

1. **Recent builds** (last 20): count by `timeout_reason`, wall p50 /
   p90 / p99.
2. **Top theories** by mean elapsed (last 20 successful builds): name,
   mean, trend vs. prior 20.
3. **Top `by` invocations** by mean `elapsed_ms` (last 20 successful
   builds): theory, line, tactic head, mean, trend vs. prior 20.
   *The active-diagnosis surface.*
4. **Recent cache-burners**: builds with `wall_elapsed_s > 270`, with
   timestamp, short SHA, slowest theory.
5. **Regressions**: any rows flagged by either regime-change detector.

Subcommands (added as data warrants):

- `theory <name>` — history for one theory.
- `slow-by [theory]` — top N slowest `by` invocations in the most
  recent build, or aggregated over last N.  Optional `theory`
  positional restricts.  *This is the query that pays off the
  five-day session.*
- `diagnose <build_id>` — single build, full record + per-`by` rows.
- `tail [N]` — last N records pretty-printed.

## 9. Verification before adoption

Three gates to pass before the design is committed in code.

### 9.1 Layer-2 mechanism spike (`command_timing`)

The highest-leverage gate in the design — decides Layer 2's
mechanism.  Run `make build` against `t/` with and without
`-o command_timing=true` (exact option spelling to be verified
against Isabelle docs).  ≥5 runs each.  Verify:

- **Output format.**  Does Isabelle's `command_timing` emit
  per-`by` records with `(theory, source_line, command_kind,
  elapsed_ms)` granularity, or only aggregate counts?  If the
  output is per-command with source-line annotations, mechanism
  (a) is viable.  If the output is theory-aggregated or untagged,
  fall back to mechanism (b) (ML decorators).
- **Overhead.**  Compare wall_elapsed_s with/without; ≤5% ideal,
  ≤10% acceptable as an always-on default.  If overhead is much
  worse than 10%, the option is unusable as always-on; in that
  case either disable by default (with opt-in for diagnosis
  sessions) or switch to mechanism (b).

This spike is short (≤30 minutes) and should run before any
implementation work on Layer 2; the choice between (a) and (b)
determines whether `bin/isabelle-build.py` does stdout parsing or
whether a new `t/base/Timing.thy` is needed.

### 9.2 Layer-2 ML-decorator spike (only if §9.1 picks (b))

If §9.1 says (a) doesn't work: write the minimal `timed_by`
method_setup in `t/base/Timing.thy`, apply at one proof site
(e.g. one `by` in `AlphabetEnlargement_Classical.thy`), verify the
sidecar log captures per-step elapsed_ms.  No overhead test needed
(the decorator is opt-in per call site).

### 9.3 Behavioural parity with the existing watchdog

On a corpus of representative invocations (success, activity
timeout, wall timeout, repetition kill, build error), the new
script's terminal output should be at least feature-comparable to
the existing one.  New fields are additions, not regressions of the
user-facing display.

## 10. Migration plan

1. **Mechanism spike** (§9.1, §9.2).  Decides Layer 2's mechanism.
   Output: a one-page note pinning (a) or (b) plus the overhead
   measurement.
2. **Land `bin/isabelle-build.py` + `bin/build-stats.py`** alongside
   the existing watchdog.  Output to `t/logs2/`.  Includes whichever
   Layer-2 path the spike chose.  No Makefile change yet; invocation
   manual.
3. **Accumulate data** for ≥1 week of normal use.  Validate parser
   correctness on real invocations.  Per-theory baseline (Layer 1)
   is useful immediately after the first dozen builds; per-`by`
   regime-change detection (Layer 2) is fully useful once each
   `(theory, line, command_kind)` triple has ≥5 occurrences.
4. **Tune detectors.**  Per-theory thresholds (§7.1) and per-`by`
   thresholds (§7.2) based on real distribution shapes.
5. **Add Makefile target** (`make build-new` or
   `USE_NEW_WATCHDOG=1` switch) for opt-in.
6. **Swap as default** once the new script is the obviously-better
   experience.  Rename `t/logs2/` → `t/logs/` (after archiving the
   existing log if anything in it matters).  Retire or fallback the
   old watchdog.

Steps 1–4 are the substantive ones; 5–6 are mechanical once trust
is earned.

## 11. Open questions / future work

- **`-v` verbosity in the new script.**  The existing watchdog uses
  `isabelle build -v` (verbose), which produces noisy progress ticks
  that the user does not find informative.  Open question: drop
  `-v` in the new script's default invocation?  Trade-off: fewer
  ticks means less per-theory progress info for the stuck/loop
  heuristic, but the per-theory `Timing` lines are emitted regardless
  of verbose mode.  Provisionally: keep `-v` for parity, revisit
  after step 3 of migration.

- **Layer-3 enforcement (`by100`-style).**  Once Layer 2 has produced
  ≥several hundred per-`by` records across normal builds, the
  distribution of `elapsed_ms` per tactic head can be characterised.
  Pick a budget at the 95th or 99th percentile of normal-case `by`s
  and define `by100` (or `by500`, or `by_ndtht`) as the timed
  alternative that *fails* slow tactics at build time.  Urban's ML
  snippet is the foundation; the per-step budget needs calibration
  from real data.  Verbatim snippet for reference:

  ```isabelle
  method_setup by100 =
    \<open>
      Method.text_closure >> (fn text => fn ctxt => fn facts =>
        let
          val limit = Time.fromMilliseconds 100
          fun tac st = timed_seq "by100" limit
                                 (method_evaluate text ctxt facts st)
        in
          SIMPLE_METHOD tac facts
        end)
    \<close>
    "apply a proof method with 100ms timeout per result step"
  ```

  Note the granularity: `timed_seq` applies the budget *per result
  step* of the method's lazy `Seq.seq`, not as a total wall budget
  for the whole `by` invocation.  Methods that yield multiple
  alternatives (`metis` exploring strategies, `presburger`, etc.)
  get the 100ms budget separately for each alternative; deterministic
  methods (`simp`, `auto`) hit it once.  Practical effect: a "500ms
  per step" budget in NDTHT might allow 1–2s of total wall time for
  proofs that exercise method alternatives.

  Foundation overlap with Layer 2's ML-decorator path: if mechanism
  (b) wins at §9.1, the same `t/base/Timing.thy` is the natural
  home for both `timed_by` (observation) and `by100` (enforcement);
  the two methods share most of their plumbing, so Layer-3
  implementation is mostly already in scope by the end of Layer-2
  work.

- **SQLite upgrade.**  If JSONL querying gets painful (estimated
  tipping point ~100k rows in `commands.jsonl`, which would take
  several months to accumulate at current build rates), switch
  storage backend.  Day-1 analyser stays JSONL.

- **Cross-build storage hygiene.**  At thousands of invocations ×
  ~500 B/record, `builds.jsonl` is on the order of single MB and
  stays flat.  `commands.jsonl` is the growth risk (10×–100× more
  rows per build).  Gzip-rotate per quarter if/when needed; not
  pre-engineered.

## 12. Trajectory axis — the proof-search progression

Merged 2026-05-27 from the former `attempt-capture-design.md`.  The
cost axis above answers "how slow is what worked"; this axis answers
"what was tried, what failed and how, what finally worked" — the
resolution the cost-only design lacked.  Both are recorded by the
same per-invocation wrapper, keyed by `build_id`: two axes of one
pipeline, not two pipelines.

### 12.1 Why

A committed proof is survivorship-biased — only what worked survives.
Per the AE author (2026-05-27), the effort went into *switching
between formulations* until one went through without taking too long;
`metis` was usually tried-and-failed, then replaced by a restructured
case analysis.  None of that trajectory survives: git keeps only
build-passing states (failures are edited away before any commit),
memory is lossy, and the Claude Code session transcripts that held
the edit sequence rotate out of `~/.claude`.

Capturing it has standalone value: an observational record of how a
large formal proof actually develops under Claude Code is of interest
in its own right — to proof-engineering researchers, tool builders,
and anyone studying human+AI formalisation — *before* any use of the
data to improve proof search.  The suggester payoff (§15) is a bonus
on top of a dataset that is interesting simply as a record.

### 12.2 Unit of capture

An *attempt* = one **build**: a prover invocation on changed proof
text that returns a real verdict.  Editing below the build (trying
phrasings before running it) is scratchpad thinking, not an attempt —
nothing is known until a build runs.

### 12.3 Mechanism

1. **Snapshot-on-build to a parallel git ref.**  The wrapper commits
   the working tree to `refs/attempts/<branch>` per invocation —
   failures included — tagged with the Layer-1 `build_id`, outcome,
   elapsed, sorry-count, and the watchdog's error/timeout summary.
   Main history stays clean; the ref is never pushed; git blob-dedup
   keeps thousands of snapshots cheap.  The `builds.jsonl` record
   (§5.1) gains an `attempt_tree` field = the snapshot's git object
   id, linking the cost and trajectory axes by `build_id`.
2. **Semantic label is free.**  The watchdog already summarises each
   failure (error head / `timeout_reason`, §6); fold that into the
   record.  An episode then reads as a run of *(diff, error)* failing
   attempts closed by a *(diff, builds OK)* success — no manual
   annotation, which never reliably happens anyway.
3. **Attribution to lemma.**  Diff consecutive snapshots and
   attribute the changed span to the enclosing entry via `query`
   entry-spans, joining outcomes from `builds.jsonl` → per-lemma
   attempt sequences.

### 12.4 Episode shape

The target record is the development episode:

    new code added → build fails → local fix → build fails →
    local fix → build succeeds → commitl

a run of failing attempts on one goal, each a small diff with its
error, terminated by the **success** that closes the goal.  The episode
boundary is the success (`outcome == ok`), **not** a commit: we
occasionally commit a *failing* state mid-flight as a rewind point for a
hard trajectory, so an intermediate commit is just an attempt that
happened to be committed — recorded and flagged (`committed_midflight`),
not a boundary.  `bin/trajectory-export.py` (§16) does this
segmentation; §14 specifies the richer per-lemma extractor.

### 12.5 What the git-only mechanism does not capture

Snapshotting source-on-build is attractive because it has no moving
parts beyond a ref and a wrapper — but git is a *state* store, not an
*event* store, and that boundary fixes what is recoverable.

Structural losses (the mechanism cannot recover these):

- **Intent.**  The diff records *what* changed, not the *hypothesis*
  it tested.  The watchdog summary (§12.3.2) labels the outcome, not
  the rationale; a post-hoc git walk infers "after error E, change X"
  but never "X *because* the author believed Y".
- **Ordering within one inter-build diff.**  Several conceptually
  distinct edits between two builds collapse to a single diff with no
  sequence.  §13's locality discipline is the workaround: keep each
  inter-build diff to one hypothesis so the collapse stays lossless.
- **Attribution on failing snapshots.**  §12.3.3 attributes a span
  via the `query` tokeniser, but failing builds often snapshot
  syntactically broken source the tokeniser cannot parse — so
  lemma-attribution is least reliable on exactly the failure states
  that carry the diagnostic value.

Default losses, cheaply closable by capturing a little more alongside
the source snapshot:

- **Non-source determinants.**  Heap warmth, Isabelle version,
  ROOT/session config and environment can decide a verdict the source
  text alone cannot explain or reproduce.  Stamp version + config into
  the `builds.jsonl` record (or the ref).
- **Rich tool feedback.**  Folding in only the error *head* (§12.3.2)
  drops the failed goal state, sledgehammer suggestion list and
  per-command timing — the most useful "why a method failed" detail,
  which lives in the build log, not the tree.  Persist the log if that
  detail is wanted.
- **Portability.**  `refs/attempts/...` is local-only, never pushed
  (§12.3.1) and `git gc`-prunable, so a normal clone/push carries none
  of the dataset.  The standalone-value motivation in §12.1 therefore
  requires an explicit ref push or export step — the dataset does not
  travel with the repository by default.

Ergonomic, not an information loss: raw refs are not a queryable
index, so every analysis re-walks and re-diffs (`bin/episodes.py`'s
reason to exist, §14) until episodes are materialised.

### 12.6 Event-native capture: a reason-carrying edit gate

The §12.5 structural losses (intent, ordering, broken-state
attribution) are unrecoverable *because* git records state, not
events.  The fix is to capture the event where it happens.  Route
every theory edit through a tool — the analogue of Claude Code's
`Edit`, with a **required `reason` field** — that appends a
structured record to an in-repo append-only log
(`history/edits.jsonl` or similar): file, target span, old→new text,
the reason, timestamp, and the `build_id` the edit precedes.  The
build verdict still attaches at build time, so the edit log and the
build outcome join by `build_id` exactly as the two axes already do.

This recovers, by construction, the losses the git-only mechanism
forfeits:

- **Intent** (#1) — the `reason` field is the rationale, captured at
  the moment of the edit rather than inferred from a diff later.
- **Ordering** (#2) — each edit is its own record, so the sequence
  within an inter-build interval survives instead of collapsing into
  one diff.
- **Failing-state attribution** (#3) — the tool knows the target span
  as it writes, so no re-parse of (possibly broken) source is needed
  to attribute the change to a lemma.
- **Portability** (#6) — an in-repo log travels with a normal clone,
  unlike a never-pushed `refs/attempts/` ref.

It is more robust as well as more informative: an append-only log is
not `git gc`-prunable and does not depend on ref machinery.

What it costs, and why it does not replace the snapshot:

- **Bypass / authorship.**  The git-snapshot mechanism is
  authorship-agnostic — it captures a human's external-editor edits
  too.  A gate only sees edits routed through it; edits made outside
  the tool are invisible.  So the gate needs a discipline (or a
  pre-build reconciliation that diffs the tree against the last
  snapshot and emits a synthetic "unattributed" edit record) to stay
  honest.
- **The build is still the unit of truth.**  An edit record is a
  *proposed* change; only a build assigns a verdict.  The gate
  supplies the event stream *between* snapshots; the snapshot (or at
  minimum `builds.jsonl`) still anchors the verdict.

So the two mechanisms are **complementary, not alternatives**: the
snapshot is the verdict-bearing state record at each build, the edit
gate is the intent-bearing event stream between builds.  Together
they close the state-vs-event gap §12.5 identifies — which is why the
recommended direction is to keep snapshot-on-build as the durable
verdict spine and add the reason-carrying gate as the event layer,
rather than hardening the fragile ref mechanism in isolation.

## 13. Change locality (data quality)

Episodes are most informative when each attempt is one attributable
change: a hyperlocal repair (a few lines at one site until the
failure clears) gives a clean cause→effect signal.  A sweeping edit
— `linarith`→`simp` at a dozen sites plus `metis`→`auto` elsewhere in
the same build — folds many hypotheses into one pass/fail, so that
episode says little about which change mattered.

This is *noise, not poisoning*: imperfect locality lowers one
episode's signal density; it does not corrupt the dataset, and
chasing perfect locality is not worth the cost.  Two cheap responses
keep the data honest without demanding perfection:

- **Discipline (soft).**  Prefer one hyperlocal single-hypothesis
  change per build; auto-loaded as
  `.claude/memory/feedback_hyperlocal_repair.md` so it is actually
  remembered.  A preference, not a purity rule.
- **Tooling.**  Record each attempt's *diff scope* (files / hunks /
  lines touched).  Analysis can down-weight or filter low-locality
  episodes, so the occasional sweep costs only itself.

### 13.1 Delta class: code vs doc-only (delivered)

The first diff-scope cut, in `bin/attempts.py`: classify each attempt's
delta as **code** (a proof or a statement moved), **doc** (only prose —
a `text` block, a `\<comment>`, a section heading, an ML comment, a
`.md` memo, `document/`, or pure re-indentation) or **none** (no
tracked-file change at all).  Every view filters to code by default;
`--all` restores the raw sequence.

Why it matters: a prose edit builds green first time by construction,
so counting doc deltas inflates the one-shot-correct rate and flattens
the trajectory histogram.  On the 693-attempt corpus, 100 of 333 closed
episodes are doc-only and drop out entirely.

The classifier is heuristic but auditable (`attempts.py classify -v`
prints the evidence).  Per changed `.thy` line it computes a *code
projection* — the line with prose spans, document-command keywords and
whitespace removed — then compares the multiset of removed projections
against the added ones.  Equality means nothing but prose moved, even
when the touched lines are code-bearing (appending a `\<comment>` to a
`by` line, retitling a `section`).  Prose state is carried across a hunk
by a small state machine over `(* *)`, `\<open>`/`\<close>` and the
document commands; since a hunk starts mid-file the entry state is
seeded from git's `@@ … @@` context line, with two escapes — retry on an
unmatched close token, and resync on a column-0 Isabelle command (which
cannot occur inside a cartouche).

**Computed in the reader, not recorded by the writer.**  A `diff_scope`
field written at capture time would freeze the classifier's first
version into the corpus; computing it on read lets an improved
classifier apply retroactively to every attempt already banked.  The
diff itself is the durable payload (§16); everything derived from it
stays derived.

*Finding (2026-07-27) — the untracked-theory blind spot.*  Filtering on
delta class exposed 28 fail→ok transitions whose delta is **empty**:
identical tree, identical HEAD, identical target, opposite verdict.
Flaky builds were the obvious reading and the wrong one.  The error
heads are hard deterministic errors (`Outer syntax error`, `Undefined
constant`, `Type unification failed`) that cannot flip green on
identical content, elapsed times are within a second of the following
success, and **26 of the 28 name a theory file that did not exist at
`git_head`**.

The cause was in capture, not in the build: `_snapshot_tree()` staged
`git add -u`, which sees tracked files only.  While a new theory is
being authored — before its first `git add` — every edit is invisible,
the snapshot tree never moves, and a whole fail→fix run records as
empty diffs.  The blind spot was therefore worst exactly where the
data is most valuable: the construction of a new theory from scratch.

Fixed 2026-07-27, in two staging passes: `git add -u` for every tracked
file (unchanged behaviour), then `git add -A` restricted to
`UNTRACKED_PATHSPECS` — `*.thy`, `ROOT`, `ROOTS`.  An **allowlist, not a
bare `git add -A`**: `.gitignore` is the wrong filter here, because it
answers "should this be committed?" while the question is "is this a
proof delta?".  A scratch script, a draft memo or an editor backup
passes the first test and fails the second, and a dataset should not
absorb files nobody has yet decided to keep.  Widening capture is
itself a scope decision — take the narrowest widening that covers the
defect.  `bin/check-snapshot-untracked.sh` guards both directions
(theory and session `ROOT` in; scratch and gitignored out).

Two consequences that outlive the fix.  Records from before it under-
report new-theory episodes and cannot be repaired retrospectively — the
content was never captured.  And a *zero-byte* delta remains a distinct
class from a doc-only one: it means "no change was seen", which is a
claim about the recorder, not about the attempt.  Counting trajectory
length over code deltas drops both, which is the conservative choice.

### 13.1.1 Reading a timeout: what the budgets were tuned for

Wall budgets were kept **as short as possible** on purpose — long enough
to let a proof fail and report why, short enough not to sit through a
diverging tactic.  They were retuned per session over time.  The cost of
that choice is that under heavy system load (not battery — the watchdog
already scales for battery) a build that would have gone green could be
killed instead, so a timeout count is part environmental and is *not*
interchangeable with a failure count.

Two consequences for anyone reading the numbers.  The worst of this
predates trajectory recording: the budgets had largely settled by the
time capture started, and the mean timeout in the corpus is ~35s, which
is the intended regime.  And the exposure is not uniform — `t/ar` is the
session to watch, being the only one where `activity` timeouts (18)
outnumber `wall` (7), the signature of a budget trimmed too close rather
than of a proof diverging.

`bin/audit-timeouts.py` splits the reasons and quantifies the rest.
`loop_progress` is genuine divergence and belongs with the failures (its
*per-attempt* rate rises with trajectory length — 1.1% in short runs
against 7.6% in long ones — and it clusters within a run, neither of
which an environmental artefact would do).  `wall` and `activity` carry
no such signature and are the load-sensitive pair.  The dynamic tables
therefore carry a `timeouts` column: it says how much of a row's failure
count is exposed to this, and the pooled rows show the exposure is
essentially equal on both sides of the pre-NTR/NTR split (46 timeouts
over 344 runs against 12 over 92), which is why it does not disturb the
contrast.

### 13.1.2 Length counts recorded builds, not captured diffs (delivered)

Trajectory length was originally the count of **code-class records** — a
conservative choice against doc-only noise, and wrong for the same reason
§13.1 was: a zero-byte delta is a claim about the recorder, not about the
attempt.  While a theory was untracked its edits produced empty diffs, so
a run that failed six times and then went green counted as **length 1, a
one-shot**.

`attempts.is_attempt` states the rule that replaces it.  A record counts
unless it is a *no-op rebuild* — a green with no code delta that did not
follow a failure.  Everything else did work:

- a **failure** is an attempt whether or not its diff survived; something
  was built and it did not compile;
- a **green after a failure** is the repair that closed the run, likewise;
- a **green after a green** with nothing recorded is a re-run of an
  unchanged tree, and is the only category that is not an attempt.

`bin/audit-zerodiff.py` measures the population this recovers.  Empty
diffs are 259 of 1360 records (19.0%, all pre-fix): 124 failures, 56
greens closing a failed run, and 79 no-op rebuilds.  So 180 of 259 are
real events.  Under the old counting, 35 closed episodes were dropped
entirely (116 attempts, lengths to 15) and 36 more were shortened — **23
of which scored as one-shot despite containing failures**.  That last
group is the reason this is a correctness fix and not a preference: a
missing episode is a hole, but a multi-attempt search recorded as a
first-time success is a wrong value in the headline statistic.

Attribution follows.  A diffless episode has no path to attribute by, but
an Isabelle error head carries the file it failed in, so `project()` falls
back to `attempts.error_dirs` — recovering 23 of the 35.  Diff paths stay
authoritative where they exist, since a build can fail in a dependency it
did not edit.

Effect on the published rates (pooled, attempt scope): pre-NTR 64.6% of
395 runs, NTR 35.4% of 96, a 29.2-point gap at p ~ 1e-7.  Every session
falls, AE most (77.9% -> 59.8%, it held the most untracked-theory work);
the contrast is unmoved.  The `blind%` column changes meaning with the
fix — those runs are now counted correctly, so what is lost is the *diff
content*, leaving them usable for rates but not for per-lemma analysis.

### 13.2 Attribution scope: proof-bearing trajectories (delivered)

The second cut, and a different kind of one.  §13.1 asks whether a
*delta* is substantive; this asks whether a *trajectory* is about the
thing being measured.  A trajectory reaches a session by path — the
`t/<sess>/` prefix of the files its code deltas touch — and
`classify_file` deliberately treats an unrecognised suffix as code so
nothing is hidden by accident.  Composing the two books a bare
`t/<sess>/ROOT` edit against that session as a code attempt, and a ROOT
edit builds green by construction, so it enters the histogram as a free
one-shot.  Nothing else in the pipeline separates "the edit was right
first time" from "there was no proof edit to get wrong".

`bin/shape-vs-trajectory.py` therefore counts only trajectories
containing a `.thy` change that passes §13.1's own code test — a
strictening in *scope*, not in kind.  `bin/audit-1shot.py` is the
standing check: it reports both scopes side by side, plus the dropped
runs, so the effect of the filter stays visible rather than becoming
invisible policy.

On the pooled corpus the effect is uneven and largest for AE (21 of 104
trajectories dropped, 88.5% -> 85.5%; NTR 4 of 96, 44.8% -> 42.4%),
which is why the share is reported per session as `noproof%` rather than
applied silently.  AE's exposure is structural: ~30 small theories means
the most ROOT churn.  The contrast the table exists to show survives at
roughly forty points, so this is a **reporting choice made explicit**,
not a correction to a wrong number.

The audit's other half came back empty.  Non-theory records *inside*
proof-bearing trajectories do not lengthen them — the proof-edit-only
count matches the proof-bearing count in every session — so only the
no-proof-trajectory half of the suspected defect is real.

*Attribution is by path, so a renamed directory needs declaring.*
`project()` reads the session off the `t/<dir>/` prefix — chosen because
session *names* were renamed twice while the directories were stable.
A directory rename defeats it, and one happened: `t/aem` was `t/ae` split
into a stable and an active session for build performance, later folded
back.  `attempts.SESSION_ALIASES` declares `aem -> ae`; without it, five
trajectories sat under their own label and two more counted as `mixed`
(one of them 69 attempts long).

The name class matters too.  It was `[A-Za-z]+`, which cannot match
`t/scratch-nae/` — and an unmatched path does not produce an unlabelled
trajectory, it produces a **`tooling`** one, meaning "no theory touched",
the opposite of the truth.  23 runs of real proof search were filed that
way.  Widened to `[A-Za-z0-9_-]+`.

The test for folding a directory in is whether its work **graduated into
the session**, which the git log settles; the directory's name proves
nothing either way.  `t/scratch-nae` reads like a spike and is not one —
it was the `[nae-prove]` reverse-arm toolkit, developed in a staging tree
and graduated by `68f95df`, and its lemmas sit in
`t/ae/AlphabetEnlargement_Reverse.thy` today (`ae_ss5_window_ofs_agree`
among them).  It is AE work: 23 runs, 65.2% one-shot.

`t/scratch` is the contrast that makes the rule concrete and stays out.
`NDTHT_Scratch` was the `[substrate-value-arity]` fork-(a) *decision
prototype* — it measured whether a `'k`-typed result transfers across a
fixed-arity isomorphism, answered the question, and was retired as spent
(`6f4d1fd`); nothing graduated and it no longer builds.  Search that
settles a design question is not search that built a session, and
counting it would mix two different activities in one rate.

*Interaction with §13.1's blind spot, and a counting defect it exposes.*
The two exposures are not independent, and on pre-fix data the overlap
dominates.  `bin/probe-noproof.py` prints the evidence per run: **20 of
AE's 21 dropped runs register a theory in a `ROOT`, and all 20 of those
theories were absent at the baseline commit** — authored while untracked,
so only the `ROOT` edit that registered them was captured.  Almost none of
what the filter drops is bookkeeping.

The 20 split cleanly, and in opposite directions:

- **10 contain failures whose error heads name the very theory being
  registered** — `EncodingWrap_TransposeLanguage` runs undefined constant
  → type unification → three failed proofs at line 127 → timeout → green,
  eight builds.  That is real proof search.
- **10 are a single green build**: the theory was written and compiled
  first time on first inclusion.  That is real one-shot signal, and
  dropping it *understates* the rate.

The first group also exposes a defect in the length metric itself.  Length
counts **code-class records**, and a zero-byte delta is class `none`, so
every one of those multi-attempt runs scored as length **1** — an
eight-attempt search recorded as a one-shot.  All 21 dropped runs were
length 1 under that metric, which is why removing them lowers AE's rate
despite half of them being genuine one-shots.

The diffs are unrecoverable, but **the attempt count and the error heads
are not** — they were recorded.  `bin/recount-lengths.py` compares three
scopes; counting every recorded build for runs that are real work (a
`.thy` edit *or* a `ROOT` registering a new theory) gives AE 69.9% of 103
and NTR 38.5% of 96, against 85.5%/42.4% under the proof scope.  The
anti-correlation survives, but **the AE–NTR gap closes from 43 points to
31**: the capture gap was inflating the easier session more, so correcting
it narrows the gap rather than widening it.  An earlier reading here had
that direction backwards.

### 13.2.1 The attribution ladder, and bounding each route to its reach

The two routes above left 12 multi-attempt trajectories attributed to
nothing at all — every one of them 0% one-shot, so their absence flattered
the headline.  They are the residue of both routes failing at once: no
captured diff, and an error head that names no file.  Seven are bare wall
timeouts (`wall timeout (40s wall)`), where the watchdog killed the build
before Isabelle reported where it was.

A third signal survives in every record: **the build target on the command
line**.  It is genuinely weaker than the other two — you can build AE while
editing base, so the target says what was *run*, not what was worked on —
which is why it goes last, after both stronger routes have declined.  For a
timed-out build with nothing else recorded it is the only signal there is.

It needs the historical session names mapped, which is exactly why
attribution was by path in the first place: `NDTHT_AR` →
`Alphabet_Reduction` → `Multitape_Alphabet_Reduction` are all `ar`.  The map
is **derived, not remembered** — every `(session, directory)` pairing that
has ever appeared in a committed `t/*/ROOT`, from `git log --all` (the
recipe is in the `SESSION_TARGETS` comment).  That turned up 18 pairings for
19 distinct invocations in the corpus, so the vocabulary is closed and the
map is checkable rather than a guess.  An explicit `-d t/<dir>` overrides
the name, because the corpus contains a case where they disagree and the
command is right: the ten `-d t -d t/scratch-ar NDTHT_ScratchAR` runs built
a staging tree that was never committed, so no ROOT records it.

**The map is an allowlist, and that is the load-bearing part.**  A target
that is not a `t/` session must yield *no* attribution rather than a guess.
The HOAU spike is the case that forces it: it built `HOAU_Spike` from
`-d scratch/hoau` *against the tree's existing sessions*, so the session it
runs against is not the work it is about.  Being absent from the map, it
declines, and its 2 trajectories stay unattributed — which is the correct
answer, not a residual failure.  They are the only 2 left of the 12.

*Bound each route to what it can actually support.*  A fourth signal exists
and is deliberately given a narrower job.  The watchdog's own error heads
name a theory by **base** name with the line it was elaborating —
`loop_progress: "by" line 190 of EncodingWrap_WF` — and 50 records carry
that and no path.  It reads like an attribution route and cannot be one:
11 base names have lived in more than one session directory across the
tree's re-layouts (`AlphabetReduction` in `t/generic`, `t/base` and `t/ar`),
and the record carries no era the tool can disambiguate with.  So it feeds
the *proof-bearing* test only, where it is decisive, and attribution falls
through to the command.  Evidence can be conclusive about one question and
useless about another; the reach has to be set per question, not per source.

*The proof-bearing test asks the wrong question, and was widened.*  §13.2
justifies the filter as keeping **free greens** out — a bare ROOT edit that
builds green by construction and has no proof to get wrong.  The test it
applied was "is a theory named?", which is a proxy.  The question the
justification actually implies is *could this trajectory have failed for a
proof reason?*  So a **timeout** now qualifies on its own: build furniture
cannot time out — registering a theory in a ROOT does not take 40 seconds,
and a build the watchdog had to kill was demonstrably deep in elaboration.
It is also the one kind of evidence that cannot reintroduce the bias being
guarded against, since a timeout is by definition not a green.

*A phantom session, found on the way.*  `project()` iterated the paths of
every **record** it judged code-class, rather than the paths that were
themselves code.  A record is code-class if any one file is, so one run that
edited `bin/isabelle-watchdog.py` and `t/document/glossary.tex` together
booked `t/document/` — the shared LaTeX include directory, not a session —
as a session of one trajectory.  Filtering per file removes it.  The same
per-file discipline stops the target route from overriding a diff that did
speak: if paths were recorded and none was under `t/`, route 1 *succeeded*
and said "not ours", and deferring to the target would relabel 9 tooling
runs as proof search.

Effect on the published rates: pre-NTR **63.3%** of 406 against NTR
**35.4%** of 96 — a 27.9-point gap, z = 4.98, p = 6e-07, and a day-clustered
bootstrap interval of +0.152 to +0.378 with no replicate reversing the sign
(`bin/oneshot-significance.py`).  Discounting timeouts still *widens* it,
to 31.8 points, so the load confound continues to run the wrong way for the
objection.

*The dependence was the recorder's, not the work's.*  A naive
two-proportion test assumes each trajectory is an independent draw, which
is exactly the assumption to doubt here — work happened in bursts, so a
hard afternoon on one lemma should show up as many correlated
observations.  Resampling whole **days** rather than trajectories measures
it, and the design effect now comes back at **1.1**: within-day dependence
is not inflating the naive interval.  It used to.  Swap `attempts.is_attempt`
back for the old count-the-captured-deltas rule, holding everything else
fixed, and the design effect is **2.4** — because untracked-theory work on
a given day was scoring as one-shot *together*, manufacturing exactly the
clustering the bootstrap was built to detect.  The correlation was an
artefact of the instrument, and fixing the instrument removed it; no
modelling choice would have.

What resampling cannot settle is stated rather than smoothed over:
pre-NTR pools four unlike developments (.545 to .688), and NTR is the
*later* one, so nothing here separates "tape reduction is harder" from
"that week was different".  Elapsed days is an **outcome** of difficulty
here, not a nuisance variable — the earlier sessions ran long because they
kept hitting problems — so conditioning on it would subtract part of the
effect being measured.  Leave-one-day-out on NTR's five days is the cheap
check against a single bad afternoon carrying the result; the gap survives
every drop.

### 13.2.2 Record the loci, not more prose — the cheap half of the record

The ladder above is entirely retrospective repair: three routes, two of
them fallbacks, reconstructing from surviving metadata what a discarded
field would have said outright.  That is the second time on this dataset
(§13.1 was the first), and both times the field was available at capture
time and cost nothing.  So the forward question is what else to record.

The storage budget answers it, and not in the direction intuition
suggests.  Over the 1360-record pooled corpus:

| field | share | mean/record |
|---|---|---|
| `diff` | 95.4% | 9562 B |
| everything else combined | 4.6% | ~460 B |
| `error_head` | **0.5%** | 55 B (98 B over the 697 non-green) |

Error capture is ~175× cheaper than the diff it sits beside, and the head
is capped where it has never needed to be: the longest in the corpus is
287 B and only 3% reach 200.  **There is no reason to be sparing with the
cheap half of the record.**

But "record more error text" is the wrong move.  The specific thing that
would have made two of the three rungs unnecessary is *structured* and the
watchdog **already computes it**: `_error_loci` extracts every
`*** At command "X" (line N of "T")` marker for the FAIL summary, and then
throws it away.  What survived instead was `_first_error` — the first two
`***` lines, which for a failed proof are truncated goal text.  That is why
210 failing records name no file at all.

So `error_loci` is now a field: every `(theory, line)` the build reported,
whole.  Three details are load-bearing:

- **Keep the theory unshortened.**  `_error_loci` used to strip the path
  for display, which was right while the terminal was the only consumer
  and wrong the moment the loci went into the record — the `t/<sess>/`
  prefix *is* the attribution.
- **Two shapes, deliberately.**  A compile error yields a path; a watchdog
  kill yields Isabelle's session-qualified `Alphabet_Enlargement.EncodingWrap_WF`.
  The qualifier is the session, so recording it whole repairs the exact
  ambiguity that barred base names from attributing at all (§13.2.1).
  `attempts._locus_dir` reads both; `error_dirs` prefers the structured
  field and falls back to scraping the head, so the two eras read alike.
- **Its absence is now informative.**  A record with no loci had no
  reported locus — a bare wall timeout killed before Isabelle said where
  it was.  That is the case, and now the *only* case, the build-target
  rung exists to cover.

`bin/check-loci.py` is the standing check, and it has to be a synthetic
one: this branch fires only on a broken build, so a green suite never
exercises it and rotated logs make it unverifiable after the fact.

Not adopted: persisting whole build logs (§12.5's "rich tool feedback"
gap — the goal state and sledgehammer suggestions).  That is the expensive
half again, at diff-like sizes, and it answers *why a method failed*
rather than *what this trajectory was*.  The loci are the part the
trajectory dataset needs.

## 14. Episode-extraction tool

`bin/episodes.py` (sibling of the cost-axis `bin/build-stats.py`,
§8) walks `refs/attempts/...` and the `builds.jsonl` outcomes to
reconstruct episodes:

- Segment the snapshot sequence into episodes: a maximal run of
  failing builds ended by a success, bounded by the `commitl` that
  lands the result on `main` (joined via `git_sha`).
- Per attempt in an episode: the diff from the previous snapshot, its
  outcome + error summary, and its diff scope (§13).
- Attribute the episode to the lemma(s) whose entry-span the diffs
  touch (via `query`), so episodes are queryable per lemma.
- Emit per-episode JSON (the dataset record) plus a human summary
  ("lemma L: 5 attempts, 4 metis/linarith failures, closed by case
  split; 12 min wall").

MVP: the snapshot ref + outcome alone stops the bleeding; the
extractor and per-lemma attribution are a later analysis layer over
accumulated data.

**Prototype (2026-05-27) — superseded by §16 (2026-06-19).**  The
prototype `bin/build_record.py`, called from `bin/isabelle-watchdog.py`
after every build, snapshotted tracked-file deltas (`git add -u`, seeded
from HEAD — §12.6 phantom-deletion note) to a chained git ref
(`refs/attempts/<branch>`) and appended an outcome record to
`t/logs/builds.jsonl`.  Used in anger across 667 attempts, it did its job
— it *scoped* the real design (§16): the git ref chain is the wrong store
for sharing, the per-attempt diff is the payload, and episodes end at a
success.  Capture stays fully guarded (it can never change a build's exit
code), but `bin/build_record.py` now records the incremental `diff`
directly and `bin/trajectory-export.py` materialises per-episode files;
the prototype's chain is converted once by
`bin/convert-legacy-trajectory.py`.  `bin/attempts.py` remains the flat
reader.  Remaining analysis layer (per-lemma `query` attribution,
diff-scope) tracked under `[attempt-capture]`; the file-shipping
build-out under `[trajectory-pool]` / `[trajectory-federate]`.

## 15. Payoff

Attributable *(goal context, change, verdict)* episodes are the
substrate for next-step heuristics or a trained suggester: given a
failing goal and its error, propose the change most likely to clear
it — replacing today's expensive pattern of trying a few approaches
then falling back to a big case analysis (almost always works, but
verbose, higher-effort, error-prone).  Per §12.1, the dataset is
valuable as an observational record even if the suggester is never
built.

## 16. Portable per-episode trajectory files (and how they agglomerate)

§§12–15 describe capture in *one* working copy.  In practice the dataset
is produced by *many*: parallel worktrees on one machine (the `stac/*`
workflow) and independent clones on other machines, by other people
running their own LLM-assisted proof efforts.  The two efforts a
tape-reduction push would split into — deterministic Hennie–Stearns vs.
the Book–Greibach–Wegbreit construction — touch disjoint directories and
sessions, so their trajectories are distinct; yet we want them, and every
collaborator's, to merge into one dataset.  This section is the
isolate-then-agglomerate answer to §12.5 loss #6 (the dataset does not
travel by default).

The store is **not** git objects.  The prototype chained working-tree
snapshots on `refs/attempts/*` (§14); that proved out the value of
capture but is the wrong store for *sharing and agglomeration* — a local
ref is never pushed, is `git gc`-prunable, does not travel with a clone,
and needs the whole object store to interpret a diff.  Git's strengths
(content-addressing, dedup, full-tree checkout) answer questions we are
not asking; its weakness, transport, is exactly our goal.  What we want
to analyse is the *diff*: which change was tried and what happened.  So an
episode is stored as a **self-contained, portable file** anchored on
public commits, with the diffs inline.

### 16.1 The unit: an episode file

An *episode* is a maximal run of attempts ending in a **success**
(`outcome == ok`); a trailing run with no success is *open*.  The
boundary is the success, **not** a commit — we sometimes commit a failing
state mid-flight as a rewind point, so an intermediate commit is just an
attempt that happened to be committed (§12.4), flagged
`committed_midflight`, never a boundary.  `bin/trajectory-export.py`
materialises one file per episode:

    { instance_id, branch,
      baseline:  <git_head of the first attempt>,    # a public commit
      attempts: [ { build_id, outcome, git_head, committed_midflight,
                    elapsed_s, error_head, diff } ... ],   # incremental
      closed_by: { build_id, git_head } | null,
      open: bool }

The two anchors (`baseline`, `closed_by`) are real commits on `main`, so
the file is interpretable by anyone with the repo — full file context is
recoverable by fetching the baseline and applying the patches.  The
failed attempts in between, never worth publishing as commits, live as
inline diffs.  Consistency check: an episode's net change should equal
`git diff baseline closed_by` (both public), the failed attempts being
the extra diffs that did not make it in.

### 16.2 Incremental diffs, re-baselined on commit

Each attempt's `diff` is the **incremental** change it introduced — the
training signal "the change tried in response to the last error" — vs the
previous attempt's tree.  The exception is a mid-flight commit: if HEAD
moved since the previous attempt, the diff is re-baselined on the new
HEAD's tree, so *committed* content is excluded (it is recoverable from
the commit) and the attempt's diff stays the small uncommitted edit.
Both `bin/build_record.py` (going forward) and
`bin/convert-legacy-trajectory.py` (the migration, §16.4) apply this same
rule.  Measured on the initial data: median diff 58 lines, p90 342; only
a handful of big-refactor steps exceed 2000 lines, each flagged by its
size and `committed_midflight`.

`bin/build_record.py` writes a tree object only to *compute* the diff
(its id is recorded as an integrity / no-op-rebuild anchor); no commit
chain is retained.

### 16.3 Identity and agglomeration

Each record / episode carries a stable **`instance_id`** — minted once as
64-bit random hex (UUID-grade: collision-free with no central registry,
the property that lets disconnected parties merge without coordinating),
persisted in gitignored `t/logs/instance-id` — plus `hostname`,
`contributor` (`git config user.email`), and `origin_url`.  The global
key is the pair `(instance_id, build_id)`.

Agglomeration is then a plain **file union**: one file per episode, each
single-writer, so combining many instances' / contributors' trajectories
never hits an append conflict — the move a CRDT makes, here with nothing
fancier than a path namespace (`<instance_id>/<episode>.json`).  No `git
fetch`, no namespaced refs, no object-store reachability to reason about.

### 16.4 The initial dataset: a one-shot conversion

The prototype's git-chain capture is brought into this format **once** by
`bin/convert-legacy-trajectory.py` — kept thereafter only as a record (in
git history) of how the initial dataset was produced.  Reading the legacy
chain and the diff-less `builds.jsonl` read-only, it reconstructs the
incremental diffs (the §16.2 re-baseline rule), stamps `instance_id` +
provenance (`backfilled:true`, since provenance is reconstructed from the
one checkout / machine, not captured), and writes a diff-bearing log that
`bin/trajectory-export.py` turns into episode files.  Measured: 667
attempts → 331 episodes (330 closed, 1 open), 21 mid-flight commits
flagged.  It mutates nothing — the legacy chain is read-only input, left
as-is, and can be `git gc`-ed once converted.  For a canonical run the
minted `instance_id` is written to the checkout's `t/logs/instance-id` so
the checkout's *future* builds share one identity across the
prototype→new boundary.

### 16.5 Federation: ship files (planned, `[trajectory-pool]` / `[trajectory-federate]`)

Going to many machines / people is just *moving files*, matching the
fork+PR model collaborators already use:

- **A dedicated trajectory repo is the hub** (`../ndtht-trajectory`), not
  the proof repo — keeping the dataset's volume and churn out of the
  thing people clone, and giving it a home that survives `git gc` /
  clone / push (closing §12.5 loss #6).
- **Contributor-namespaced files.**  Each contributor's episode files
  land under `<contributor>/<instance_id>/…`; adding a contributor is one
  directory — no shared write access, no ref plumbing, the same trust
  boundary as a PR.  Because every file is single-writer and ids are
  collide-free, the union across contributors is conflict-free.
- **Publishing is opt-in.**  A trajectory exposes someone's proof-search
  process, false starts and all; publishing is a deliberate copy, per
  contributor and ideally per episode — never automatic (a consent
  matter as much as noise control).  A portable file is trivially yours
  to share or withhold, with no local-only-ref caveat.

**Reading a pooled log: segment chronologically, not per instance.**  A
concatenated `builds.jsonl` interleaves records from several working
copies, so the obvious defensive move is to split episodes by
`instance_id`.  That is wrong for how worktrees are actually used here —
sequentially.  In the main + `stac/wip` pool the seam shows the *same*
session failing at the *same* `by` line on either side (main's last
attempt and the worktree's first, four days apart): one repair, two
working copies, and splitting by instance would cut that trajectory in
half.  The assumption that fails is *concurrent* instances, whose
records genuinely interleave; `attempts.py interleaving()` measures the
excess over the n-1 switches a sequential handoff costs, and the views
warn when it is non-zero rather than silently mis-segmenting.

`[trajectory-pool]` (gather a machine's per-instance files) and
`[trajectory-federate]` (the hub repo + opt-in publish) are the remaining
build-out; the per-episode format they move is in place.

## 17. References

- `bin/isabelle-watchdog.py` — existing watchdog, frozen during
  parallel development.
- `Makefile` — `WATCHDOG_TIMEOUT=20`, `WALL_TIMEOUT=40`,
  `REPETITION_THRESHOLD=3` (env-overridable).
- `insights/71.md` — large-but-trivial cost model gap; motivates
  per-`by` granularity ambition.
- `.claude/memory/feedback_no_buildclean_reflex.md` — `make build`
  TIMEOUT treated as cost-regression signal; this design makes the
  signal first-class.
- `insights/104.md` — committed proofs are survivorship-biased;
  capture the trajectory at the prover-invocation layer (trajectory
  axis, §12).
- `.claude/memory/feedback_hyperlocal_repair.md` — change-locality
  discipline that keeps episodes attributable (§13).
- `query` entry-spans — the trajectory attribution primitive
  (§12.3, §14).
- `proving-loop-design.md` — the warm-session discovery loop that
  will also feed this per-invocation pipeline.
- Provenance: this document absorbs the former
  `attempt-capture-design.md` (merged 2026-05-27).
- `feedback_small_increments.md` — the sorry-skeleton dance that
  per-`by` data aims to make less often necessary.
- Josef Urban, `isa_algtop1` — source of the `by100` ML snippet
  reproduced in §11; Layer 3 follow-up.
