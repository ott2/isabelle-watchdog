# Changelog

Notable changes to `isabelle-watchdog`. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

One thing this project versions that most do not: the **record schema**. A
corpus is irreplaceable data read years after it was written, so a release
that changes what a record contains says so here explicitly, and
`trajectory check` will tell you whether a corpus written by an older version
still regenerates.

## [Unreleased]

**Record-schema change, additive.** Two new keys: a top-level `contention`
object and `limits.load_factor_max`. Older records read identically — every
view treats an absent key as unmeasured, which is not the same as zero — and
readers' output on both existing corpora is byte-for-byte unchanged.

### Added

- **Budgets adapt to a contended machine, measured rather than predicted.**
  Battery and load look like one problem and are two. Throttling changes how
  much work a CPU-second *buys*, which no accounting can see afterwards —
  hence `BATTERY_FACTOR`, assumed in advance. Contention changes how many
  CPU-seconds you *get* per wall-second, and a descheduled process accrues no
  CPU time at all, so it needs no prediction and no benchmark.

  The watchdog samples its process tree's CPU time (`ps`, every
  `CPU_SAMPLE_INTERVAL`, a fraction of a percent of a build) and derives a
  **duty cycle**: CPU-seconds per wall-second. Dimensionless, so 1.0 is a
  whole core on any machine and no threshold here is fitted to the machine it
  was written on. Three regimes — *stalled* (no CPU: killed on time, because
  `1/duty` is unbounded and the naive rule would hand a deadlock four times
  its deadline), *starved* (budgets × `1/duty`, capped by `LOAD_FACTOR_MAX`),
  and *running* (a core or more: killed on time). That last one is what the
  design turns on — a proof that got genuinely more expensive burns CPU at
  full rate, so it is never mistaken for a starved one and the
  cost-regression signal the tight wall budget carries survives intact.
  `LOAD_FACTOR_MAX=1.0` disables the measurement, `ps` calls included.

  Load average was tried first and rejected on measurement: its 60 s time
  constant is longer than the whole 40 s budget it would govern — a workload
  already 1.27× slower after 5 s still read as idle — and on a heterogeneous
  CPU, scheduler migration between performance and efficiency cores swamps
  what signal remains, with no correct denominator.
- **`contention` in every record**: `cpu_time_s`, `duty_cycle`, `verdict`,
  `load_factor_applied`. The observations, not just the derived factor — the
  policy above them will change and these will not, and without them "that
  timeout was a hard proof" and "that timeout was a busy laptop" are
  indistinguishable in the corpus.
- **A timeout summary says which.** `used no CPU — a hang, not a busy
  machine`, or `machine contended — this build got 0.31 of a core`. The
  stalled case earns its line: it is the one verdict where nothing was
  scaled, so nothing else in the output would hint at it.
- **Battery detection on Linux**, via `/sys/class/power_supply`. `pmset` is
  macOS-only, so `BATTERY_FACTOR` had simply never applied anywhere else —
  not broken, but silently inert on exactly the machines it was written for.
  A desktop with no mains supply still answers "unknown", because "no
  battery" and "on battery" must not be confused.

### Fixed

- **A writer with nothing configured created a corpus instead of finding
  one.** With `$WATCHDOG_LOG_DIR` unset, all three writers fell straight to a
  built-in `t/logs` default. In a project whose corpus is the other known
  layout — 43sp's is `results/isabelle-logs`, named by a Makefile variable, so
  any build run outside make had no variable — that minted a *second* corpus:
  new instance id, empty history, and every record in it perfectly valid.
  Appending to the wrong file is loud; creating the wrong file is silent and
  looks exactly like a first build, and `trajectory check` calls both halves
  sound because each is. `corpus.resolve_log_dir()` now looks before it
  creates, using the same tiers the readers use, and the default is reached
  only by a project that genuinely has no corpus.

  The reader's half of this was fixed in 0.2.0 — readers honour
  `$WATCHDOG_LOG_DIR` so they land where the writer wrote. That left the
  writer guessing, which is the worse half: a reader that guesses reports the
  wrong number, a writer that guesses makes one.
- **The watchdog placed `last-build.log` relative to `__file__`.** That named
  the project only while the script lived inside it; installed, it named
  `site-packages/t/logs`. The recorder's copy of this bug was fixed during
  consolidation and this one was missed, because it misplaces a log file
  rather than a payload — nothing errors and no record ever looks wrong.
  `export.py`'s defaults had it too.

### Added

- **Trajectory capture can be turned off**: `--no-record` on both
  `isabelle-build` and `isabelle-watchdog`, or `BUILD_RECORD=0` for a project
  that never wants it. On by default, because the capture is the reason the
  supervision was written — but the supervision is useful alone (killing a
  looping tactic and naming its line needs no dataset), and a project that
  only wants that should not accumulate records it will never read. The check
  lives in the watchdog rather than inside `record()`, so a declined capture
  skips the module entirely rather than importing it to do nothing.
  An unrecognised `$BUILD_RECORD` is an error: read as *on*, a misspelt "off"
  quietly collects the data someone declined.
- **`isabelle-build --where`** — reports the resolved corpus, which of the
  four rules chose it, and whether capture is on, without building. A tool
  that resolves a path by four rules should be able to say which one fired;
  the alternative is deducing it from the source, which is how a wrong answer
  stays believed.
- **`isabelle-watchdog --help`.** There was none: `--help` was taken as the
  command to supervise, so the one entry point named after the package ran
  when asked for help. Its text now covers the resolution ladder, the marker
  and the capture switch, and only *leading* flags are the wrapper's — `env`,
  `nice` and `timeout` draw the line in the same place, and anywhere else
  means silently eating an option meant for `isabelle build`.
- **`.isabelle-watchdog`**, a committed project marker naming the log
  directory — first non-blank, non-comment line, relative to the marker. Same
  file shape and same search as `.isabelle-query`, deliberately: a project
  already carrying one marker should not learn a second convention. This is
  the tier discovery cannot reach, since discovery can only find a corpus that
  already exists and so says nothing about a fresh clone, or about a layout
  the tool has never seen. The search stops at the project root, unlike
  `.isabelle-query`'s unbounded walk: projects are routinely nested, and
  overshooting here would pool two repositories' trajectories into one corpus.
  A marker that names nothing is an error rather than a no-op.
- **One line to stderr when a corpus is created.** Resolution only sees
  layouts it knows and markers that were committed, so a project keeping
  records somewhere else entirely still gets a fresh one minted. "creating"
  where the operator expected "appending" is the whole of the bug, and it
  fires once per corpus rather than once per build.

### Changed

- Two discovered corpora now make a *writer* refuse, before the build starts,
  rather than silently picking one. This is not the failure `guard.py` exists
  to swallow — a capture that breaks is instrumentation failing and the build
  must survive it, whereas this is a configuration error decided before
  anything runs, with the fix in the message. Guessing would split an
  irreplaceable dataset in a way nothing downstream can detect.
- `$TRAJECTORY_CORPUS` remains reader-only, now explicitly: honouring it in
  `resolve_log_dir()` would mean that pointing a view at someone else's
  dataset silently redirects your next build's records into it.
- The watchdog publishes its resolved log directory into the environment
  before importing the recorder, so the two writers in one run share one
  answer rather than deriving two that agree by construction.
- `trajectory-export` resolves its input through `corpus.resolve()` instead of
  a module-level default, and `--out` defaults to `trajectories/` beside the
  corpus it actually read.

## [0.2.0] — 2026-08-06

The first version meant to be installed by the projects it records rather than
vendored into them.

**No record-schema change.** Corpora written by `0.1.0.dev0` read identically:
every reader's output on both existing corpora is byte-for-byte unchanged.

### Fixed

- **The watchdog lost output after a burst.** `select()` polled the raw file
  descriptor while `readline()` read through a buffered reader, which pulls a
  whole chunk into userspace and returns one line — leaving the rest stranded
  where `select()` could not see it. A child that printed four lines and went
  quiet had exactly one of them logged, parsed, or matched against anything.
  Lines drained at EOF, so builds that *exited* were unaffected; the case it
  broke was a burst followed by a hang, which is exactly how an error block
  and the line-naming progress warnings arrive.
- **The budgets were unenforceable against a build that kept talking.** The
  wall, loop and activity checks ran only in the branch taken when `select()`
  timed out, so a child emitting more than a line per second kept the pipe
  permanently ready and was never measured — `while :; do echo tick; done`
  ran unbounded under a three-second wall. Continuous output is what a
  parallel `isabelle build -v` looks like.
- **`$TRAJECTORY_CORPUS` did not override anything.** Documented as "read a
  specific corpus, ignoring the above" and implemented as one candidate among
  several, so the one situation it exists for — pointing a reader at a pooled
  corpus while standing in a project that has its own — reported "several
  corpora found" and refused to read either. A named corpus now wins outright;
  only *discovered* layouts can be ambiguous.
- **Two routes to one corpus read as a conflict.** A project setting
  `WATCHDOG_LOG_DIR` to one of the known layouts — which 43sp's Makefile does
  — reached the same file twice and made every reader unusable there.
  Candidates are now deduplicated by resolved path, which also covers the
  normal case of a corpus symlinked in from a separate repository.
- **Attribution named one project's layout.** The session-directory pattern
  was `^t/([A-Za-z0-9_-]+)/` — ndtht's tree, in a tool meant to read any
  project's corpus. Elsewhere it matched nothing, and the result was not
  "unlabelled" but *wrong*: every trajectory fell through to `tooling`, which
  reads as "no theory was touched". The whole 43sp corpus was labelled that
  way; 16 of its 19 trajectories now attribute correctly, and ndtht's
  labelling is unchanged.

### Added

- **A pytest suite: 257 tests, in three tiers.**

  ```sh
  pip install -e ".[test]"
  pytest -m "not slow and not isabelle"   # 167 tests, pure logic
  pytest -m "not isabelle"                # 250 tests, + real subprocesses
  pytest                                  # 257 tests, + a real isabelle build
  ```

  Covering the supervision loop, capture, corpus resolution, the integrity
  readers, the code-vs-doc classifier, notes and lint, the `isabelle-build`
  entry point, and all thirteen CLI subcommands. The middle tier runs the
  watchdog against a *fake* child — `sh -c` printing canned Isabelle output —
  which exercises every kill condition in seconds with no Isabelle installed,
  and is what surfaced the two read-loop defects above.
- **An end-to-end test against a real `isabelle build`**: a green build, a
  false lemma so a locus is extracted from genuine Isabelle output, and an
  axiom that rewrites `f x → f (Suc x)` forever. The last asserts
  `timeout_reason == "loop_progress"` and that the named line is the looping
  `by`, so it fails if Isabelle ever stops re-emitting its progress warning
  and the three coupled constants stop lining up. Skips cleanly without
  Isabelle or a prebuilt HOL heap.
- **Out-of-tree builds and split sessions derive themselves.** A build whose
  `-d` load path reaches no session directory is building something defined
  elsewhere; two directories are one development when a `.thy` moved between
  them, which the recorder captures as a rename because it diffs with `-M`.
  Together these replaced the hand-maintained exclusion and alias lists —
  **neither existing corpus now needs a declaration file at all.**
- **`--attribution FILE` / `$TRAJECTORY_ATTRIBUTION`** for facts a corpus
  genuinely cannot show. Named by the caller, never discovered beside the
  data: a conventional sidecar path would make overriding anything require
  write access to the dataset, and would let a file nobody passed on the
  command line change published statistics. A named file must exist, and an
  unrecognised key is an error.
- A `test` extra (`pip install -e ".[test]"`). There are still no runtime
  dependencies.

### Changed

- Attribution is derived from the corpus on four signals already in it —
  directories holding a `.thy`, `session` lines in captured ROOT diffs, the
  `-d` load path, and theory renames — rather than from tables inside the
  package.
- `-d` paths are matched component-wise from any suffix, so an absolute load
  path still matches a repo-relative session directory. Three 43sp records use
  the absolute form and were being read as out-of-tree.
- `--attribution` is offered only by the subcommands that attribute, rather
  than by five that mostly ignored it.

### Removed

- `tests/run.sh` — pytest runs the suite.
- `scripts/check-snapshot-untracked.sh` — never wired into anything, called
  `_snapshot_tree()` with an arity it no longer had, and probed one project's
  `t/base/…` paths. Its two checks (build-relevant source in; scratch and
  gitignored out) are now a real capture in `tests/test_record.py`.

## [0.1.0.dev0] — 2026-08-04

Initial packaging of the watchdog and the trajectory tooling as
`isabelle-watchdog`, consolidated with history from the two application
projects that grew it.
