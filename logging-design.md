# Logging design for `isabelle-build.py`

**Status:** Design sketch, 2026-05-13.  Not yet implemented; review before
coding.

## 1. Context

The existing `bin/isabelle-watchdog.py` has been invoked roughly 20k times
in this project.  Each invocation overwrites `t/logs/last-build.log`; no
cumulative history is kept.  The terminal one-line summary (`OK 7 theories
23s ...`, `STUCK ... 80% no output for 20s`, `LOOP repeated 3x ...`) is
shown to the user and then discarded.

Costs of having no history:

- We cannot answer "is theory T usually this slow, or did it just regress?"
- We cannot tell loop-style timeouts from slow-proof-style timeouts apart
  any better than the watchdog's three coarse `timeout_reason` buckets.
- We cannot detect that a new commit pushed a build past the ~270s point
  at which the Claude prompt cache (5-minute TTL) gets wiped, which is at
  least an order of magnitude cost amplifier on the next session turn.
- We cannot answer questions like "which theories are growing fastest in
  build time as the project progresses?"

The existing watchdog is battle-tested across 20k invocations of varied
failure modes (infinite loops, slow proofs, error paths, document builds,
graph builds).  It is not to be refactored under load.

## 2. Goals

- A cumulative per-build record across all invocations, structured.
- Per-theory timing history sufficient to detect cost regressions.
- Per-`by` (per-command) timing data *if* the Isabelle option that
  produces it has acceptable (≤5%) overhead; without that, per-theory is
  the granularity we accept.
- A heuristic that distinguishes loops from slow proofs at kill time,
  using process-state observation in addition to the existing
  `timeout_reason` field.
- A small analyser script that converts the raw data into actionable
  summaries, so the data has a consumer from day 1.
- Parallel deployment alongside the existing watchdog until trust is
  earned, then a clean swap.

## 3. Non-goals (for now)

- **by100-style bottom-up enforcement.**  Deferred for now;
  complementary not alternative to this measurement layer (see §11 for
  the ML implementation and calibration notes).  Urban's setting is
  "small-and-trivial" proofs; NDTHT is "large-but-trivial"
  (insights/71), so an NDTHT per-step budget would be ≫100ms —
  calibrating from data is one of the things the logging layer will
  help with.
- **Archiving full Isabelle stdout across builds.**  Current verbose
  output (`isabelle build -v`) is repetitive (progress ticks), and the
  structured per-theory timing is sufficient for trend analysis.  We
  keep the latest invocation's full stdout in `last-build.log` for the
  terminal-tail experience; we do not archive prior invocations.
- **Modifying `bin/isabelle-watchdog.py` during the parallel period.**
- **Modifying the `Makefile` during the parallel period.**  Targets
  `build`, `buildclean`, `graphs`, `pdf-full` keep invoking the existing
  watchdog.

## 4. Architecture

### 4.1 Script

- **New:** `bin/isabelle-build.py`.
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
                                     default: on if verified ≤5% overhead
    [--snapshot-on-kill/--no-snapshot]
                                     default: on
    -- <isabelle build args...>
```

### 4.3 Files produced

Under `--log-dir` (default `t/logs2/`):

- `builds.jsonl` — one record appended per invocation.  Cumulative.
- `timings.jsonl` — one record appended per `(build_id, theory)`
  derived from each `Timing T (N threads, X.Xs elapsed, Y.Ys cpu)`
  line that the build emits at theory completion.
- `commands.jsonl` — one record appended per `(build_id, theory,
  command)`.  Only populated when `--command-timing` is active.
- `last-build.log` — the current invocation's full stdout, overwritten
  each run.  Same role as in the existing watchdog.

No `archive/` subdirectory; no historical full-stdout retention.

### 4.4 `build_id` format

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

- `cache_burn_risk`: `wall_elapsed_s > 270`.  5-minute prompt-cache TTL
  threshold; flags successful-but-expensive builds too.

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

One row per `Timing T (...)` line in the build output.  Existing watchdog
regex captures all the fields already; new script just emits a row
instead of discarding all but the last.

### 5.3 `commands.jsonl` (conditional)

```json
{
  "build_id": "20260513T103045-12345",
  "theory": "NDTHT.AlphabetEnlargement",
  "line": 1247,
  "command_kind": "by",
  "elapsed_ms": 287
}
```

Only populated if `--command-timing` is active.  Parser shape depends on
the exact format `-o command_timing=true` produces (to be verified —
see §8.1).  `command_kind` distinguishes `by`, `apply`, `proof`,
`lemma`, etc. — whatever the Isabelle option labels them.

## 6. Kill diagnosis heuristic

Computed at kill time from `timeout_reason`, the `kill_snapshot`, and
recent progress ticks.

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

In `bin/build-stats.py`, per theory:

- Compute median and MAD over the theory's last 20 successful runs.
- Flag the latest run if
  `elapsed_s > median + 3·MAD`  **AND**  `elapsed_s > 1.5 × median`.
- Skip if the theory has fewer than 5 prior runs (insufficient
  baseline).

The double-guard prevents false positives on theories with very low
absolute variance (where 3·MAD might be tiny seconds).

`bin/isabelle-build.py` calls into the detector on successful
completion.  When a flag fires, it adds a one-line warning to the
normal OK summary:

```
OK  7 theories  23s elapsed  41s cpu
  ⚠ NDTHT.AlphabetEnlargement: 12.3s (was median 4.1s over last 20)
  log: t/logs2/last-build.log
```

When no flag fires, the terminal output is feature-comparable to the
existing watchdog.

## 8. Day-1 analyser

`bin/build-stats.py` with no args prints:

1. **Recent builds** (last 20): count by `timeout_reason`, wall p50 /
   p90 / p99.
2. **Top theories** by mean elapsed (last 20 successful builds): name,
   mean, trend vs. prior 20.
3. **Recent cache-burners**: builds with `wall_elapsed_s > 270`, with
   timestamp, short SHA, slowest theory.
4. **Regressions**: any rows currently flagged by the regime-change
   detector.

Subcommands deferred until we have real data:

- `theory <name>` — history for one theory.
- `diagnose <build_id>` — single build, full record.
- `tail [N]` — last N records pretty-printed.

## 9. Verification before adoption

Two gates to pass before the design is committed in code.

### 9.1 `command_timing` overhead

Run the same `make build` invocation against `t/` with and without
`-o command_timing=true` (exact option spelling to be verified against
Isabelle docs).  ≥5 runs each.  If median overhead ≤5%, enable by
default.  If >5%, leave it off.  Per-`by` data is the headline feature
of the design — losing it would be a real loss — but a build that costs
significantly more in wall time defeats the cache-cost goal.

This verification must happen *after* the other proof-working instance
is parked: it requires running `isabelle build` multiple times back to
back.

### 9.2 Behavioural parity with the existing watchdog

On a corpus of representative invocations (success, activity timeout,
wall timeout, repetition kill, build error), the new script's terminal
output should be at least feature-comparable to the existing one.  New
fields are additions, not regressions of the user-facing display.

## 10. Migration plan

1. Land `bin/isabelle-build.py` and `bin/build-stats.py` alongside the
   existing watchdog.  No Makefile change.  Invocation manual.
2. Accumulate data for ≥1 week of normal use.  Validate parser
   correctness on real invocations.
3. Tune the regime-change detector thresholds based on real data.
4. Add a Makefile target (`make build-new` or `USE_NEW_WATCHDOG=1`
   switch) for opt-in.
5. Swap as default once the new script is the obviously-better
   experience.  Rename `t/logs2/` → `t/logs/` (after archiving the
   existing log if anything in it matters).  Retire or fallback the old
   watchdog.

Steps 1–3 are the substantive ones; 4–5 are mechanical once trust is
earned.

## 11. Open questions / future work

- **`-v` verbosity in the new script.**  The existing watchdog uses
  `isabelle build -v` (verbose), which produces noisy progress ticks
  that the user does not find informative.  Open question: drop `-v`
  in the new script's default invocation?  Trade-off: fewer ticks means
  less per-theory progress info for the stuck/loop heuristic, but the
  per-theory `Timing` lines are emitted regardless of verbose mode.
  Provisionally: keep `-v` for parity, revisit after step 2 of
  migration.
- **by100-style enforcement.**  Discussed with Josef Urban; his ML
  implementation from `isa_algtop1` is:

  ```isabelle
  method_setup by100 =
    \<open>
      Method.text_closure >> (fn text => fn ctxt => fn facts =>
        let
          val limit = Time.fromMilliseconds 100
          fun tac st = timed_seq "by100" limit (method_evaluate text ctxt facts st)
        in
          SIMPLE_METHOD tac facts
        end)
    \<close>
    "apply a proof method with 100ms timeout per result step"
  ```

  Note the granularity: `timed_seq` applies the budget *per result
  step* of the method's lazy `Seq.seq`, not as a total wall budget for
  the whole `by` invocation.  Methods that yield multiple alternatives
  (`metis` exploring strategies, `presburger`, etc.) get the 100ms
  budget separately for each alternative; deterministic methods
  (`simp`, `auto`) hit it once.  Practical effect: a "500ms per step"
  budget in NDTHT might allow 1–2s of total wall time for proofs that
  exercise method alternatives.

  When/if adopted, the per-step budget needs calibration from real
  NDTHT data (which the logging layer produces).  Probably 500ms–1s
  for NDTHT vs. 100ms for Urban's ATP-shaped work.
- **SQLite upgrade.**  If JSONL querying gets painful (estimated
  tipping point ~100k rows in `commands.jsonl`, which would take
  several months to accumulate), switch storage backend.  Day-1
  analyser stays JSONL.
- **Cross-build storage hygiene.**  At 20k invocations × ~500 B/record,
  `builds.jsonl` is ~10 MB and stays flat.  `commands.jsonl` is the
  growth risk.  Gzip-rotate per quarter if/when needed; not
  pre-engineered.

## 12. References

- `bin/isabelle-watchdog.py` — existing watchdog, frozen during parallel
  development.
- `Makefile` — `WATCHDOG_TIMEOUT=20`, `WALL_TIMEOUT=40`,
  `REPETITION_THRESHOLD=3` (env-overridable).
- `insights/71.md` — large-but-trivial cost model gap; motivates per-`by`
  granularity ambition.
- `.claude/memory/feedback_no_buildclean_reflex.md` — `make build`
  TIMEOUT treated as cost-regression signal; this design makes the
  signal first-class.
- Josef Urban, `isa_algtop1` — source of the `by100` ML snippet
  reproduced in §11; deferred follow-up.
