# Logging design for `isabelle-build.py`

**Status:** Design sketch, 2026-05-18.  Not yet implemented; review
before coding.

## 1. Context

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
small `t/generic/Timing.thy` and applied at proof sites that need
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
  `t/generic/Timing.thy` — Isabelle theory defining `timed_by` and
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
`t/generic/Timing.thy`:

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
whether a new `t/generic/Timing.thy` is needed.

### 9.2 Layer-2 ML-decorator spike (only if §9.1 picks (b))

If §9.1 says (a) doesn't work: write the minimal `timed_by`
method_setup in `t/generic/Timing.thy`, apply at one proof site
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
  (b) wins at §9.1, the same `t/generic/Timing.thy` is the natural
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

## 12. References

- `bin/isabelle-watchdog.py` — existing watchdog, frozen during
  parallel development.
- `Makefile` — `WATCHDOG_TIMEOUT=20`, `WALL_TIMEOUT=40`,
  `REPETITION_THRESHOLD=3` (env-overridable).
- `insights/71.md` — large-but-trivial cost model gap; motivates
  per-`by` granularity ambition.
- `.claude/memory/feedback_no_buildclean_reflex.md` — `make build`
  TIMEOUT treated as cost-regression signal; this design makes the
  signal first-class.
- `feedback_small_increments.md` — the sorry-skeleton dance that
  per-`by` data aims to make less often necessary.
- Josef Urban, `isa_algtop1` — source of the `by100` ML snippet
  reproduced in §11; Layer 3 follow-up.
