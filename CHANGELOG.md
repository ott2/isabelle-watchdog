# Changelog

Notable changes to `isabelle-watchdog`. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

One thing this project versions that most do not: the **record schema**. A
corpus is irreplaceable data read years after it was written, so a release
that changes what a record contains says so here explicitly, and
`trajectory check` will tell you whether a corpus written by an older version
still regenerates.

## [Unreleased]

**No record-schema change.** No field was added, removed or given a new
meaning; what changed is when one of them is set.

### Fixed

- **A parallel build that was still progressing could be killed as a loop**
  ([#1](https://github.com/ott2/isabelle-watchdog/issues/1)). The detector's
  rule is N consecutive `command "X" running for ...s` warnings on one
  `(theory, line, command)` triple, but the counter only ever compared a
  warning against the previous *warning* — so everything printed in between
  was invisible to it. Isabelle builds a session's theories in parallel, so a
  merely slow `by` emits its warnings while the rest of the session visibly
  progresses, and three of those spread over 73 seconds with thirteen other
  theories starting between them read as a tactic spinning on one line.

  The reporter lost two full AFP entries to it: Isabelle discards a session's
  heap image when rebuilding, so a mid-rebuild kill leaves nothing to resume
  from.

  Any other output now resets the count, rather than an enumerated set of
  lines that count as progress — a list of what progress looks like is a
  guess that goes stale, and the two failures are not symmetric. A missed
  loop kill costs the difference between the loop budget and the wall budget,
  and the summary still names the line, because a wall kill reports
  `loop_key` too. A false loop kill destroys a partial build.

  **The kill is postponed, not surrendered.** It fires as soon as its claim
  becomes true: measured against a real looping `by` beside three theories
  building in parallel, the others finished at 5.6 s and the warnings then
  ran uninterrupted every 2 s for the rest of a 75 s capture — three of them
  land 4 s after the first, exactly as on a single-theory build. What the
  detector now answers is "is this command the only thing still happening",
  which is what a kill needs to know.

- **A note section can follow `. ` as well as `; `**
  ([#2](https://github.com/ott2/isabelle-watchdog/issues/2)). A prose
  diagnosis is a sentence and a sentence ends in a full stop, so
  `diagnosis: the floor lands mid-distribution. expect: ok` swallowed the
  prediction into the diagnosis — and `isabelle-build --lint` then reported
  "no `expect:`" about a note with `expect: ok` plainly in it.

  This is a data fix, not a formatting one. Every note that fails to parse
  silently *lowers* the measured prediction rate rather than erroring, so a
  parser stricter than the writing it accepts quietly edits a published
  statistic. A full stop needs the trailing space a semicolon does not,
  because it is also a decimal point and a filename separator: `19.5s. expect:`
  splits, `v1.2.ref:` does not.

  The raw `note` was always stored verbatim, so nothing was ever lost from a
  record — only from `note_fields`, and only for future builds. Existing
  corpora are untouched; re-parsing one would now find more sections in it.

- **The linter names the real cause when a key is mid-sentence.** A key that
  appears in the note but not in the parse now says so, instead of being
  reported as absent. "No `expect:`" about a note containing `expect: ok`
  reads as a broken linter, and the reasonable response — ignoring the
  warnings — costs the near-miss check next to it, which is the only thing
  standing between `expects:` and a section that silently does not exist.

- **The timeout summary keeps the session qualifier on a stuck theory**
  ([#3](https://github.com/ott2/isabelle-watchdog/issues/3)). `LOOP
  FSM_Tests.Util: …` rather than `LOOP Util: …`, in the `LOOP`, `TIMEOUT` and
  `STUCK` lines and in the record's `error_head`. Shortening assumed every
  supervised build targets one session — true of the build the operator meant
  to run, false of the one Isabelle planned, because a stale dependency heap
  silently adds its parent sessions. The reporter went looking for a `Util`
  that was not in their project.

  Unconditional rather than "qualify when it differs from the target session":
  the watchdog supervises an argv and has no reliable notion of a target. This
  changes `error_head`'s text for *new* timeout records; `error_loci` already
  carried the qualified name, and `attempts.theory_key` collapses either
  spelling to the same key, so nothing downstream reads differently.

## [0.3.1] — 2026-08-11

**No record-schema change.** Every reader that could open a 0.3.0 corpus can
open this one, and vice versa.

**One new runtime dependency**, `isabelle-layout` — the ROOT parser, split
out of `isabelle-query` for exactly this, declaring no dependencies of its
own. A patch number rather than a minor one because nothing existing breaks:
no field changed, no flag changed meaning, and `pip` supplies the dependency
without being asked.

### Added

- **The session to build is derived when a project leaves no doubt.** One
  ROOT under the project declaring one session is unambiguous: that session,
  and the ROOT's own directory as `-d`. So a single-session project now needs
  no configuration at all — `isabelle-build -m '...'` and nothing else — which
  is what makes a per-project wrapper deletable. 43sp's `isabelle/ROOT`
  declares `SPSlowdown` and nothing else, so `$BUILD_SESSION` had been
  carrying information the repository already stated.

  Several ROOTs, or several sessions, is an **error listing them** rather than
  a guess — the same ladder `resolve_log_dir()` uses. Building the wrong
  session is a confusing Isabelle failure minutes later, and recording it puts
  a build of the wrong thing into the corpus, which is worse. ndtht has ten
  ROOTs declaring thirteen sessions and keeps `$BUILD_SESSION`.

  ROOTs come from `git ls-files` rather than a filesystem walk: `.git`,
  ignored build trees, virtualenvs and vendored AFP checkouts are exactly
  where a stray ROOT lives, and a pruning list is a guess that goes stale.
  `--others` includes untracked files, so a project whose ROOT is not
  committed yet still resolves.
- **`session:` and `dir:` keys in `.isabelle-watchdog`**, for a project too
  ambiguous to derive but tired of exporting a variable. The bare first line
  still means the log directory, so markers written before this read
  identically; the bare line is now identified by *not* being a `key: value`,
  so the file can be written in whichever order reads best. A marker may
  declare only a session — that is a file with no opinion about where records
  go, not a parse failure — and an unrecognised key is an error, since
  `sessions:` for `session:` would otherwise be accepted and do nothing.
- **`isabelle-build --where` reports the session and its directory**, since
  with both derived, "which session does this project build" stopped being
  answerable by reading a Makefile.
- **`trajectory audit`**, making the validation suite reachable. The six
  audits — the modules that re-derive each published statistic a second way —
  were `python -m isabelle_watchdog.audits.<name>` and were named nowhere in
  `trajectory --help`, so finding one required already knowing it existed. A
  suite guarding numbers that get published, reachable only by the people who
  wrote it, is not reachable.

  `trajectory audit` lists them; `trajectory audit NAME [-i CORPUS] [...]`
  runs one, passing the rest of the argv through to the audit's own parser.
  Deliberately unlike every other subcommand, which take a positional corpus:
  an audit resolves its own corpus and fits its own attribution, so routing it
  through the shared one would do that work twice and could do it two ways.
- **`-V` / `--version` on all three commands**, both spellings, because
  someone who tries one and gets nothing concludes the tool has no version
  rather than trying the other.

### Changed

- **`isabelle-layout` is now a runtime dependency, and this package's ROOT
  grammar is gone.** `roots.py` was a regex that agreed with the reference
  parser; it is now a twenty-line adapter over
  `isabelle_layout.parse_root_sessions`, holding no regex and no notion of
  what a session name may contain.

  The no-runtime-dependencies rule was formed against `isabelle_query.common`
  — a parser reachable only by installing an 11k-line querying CLI, with that
  tool's release cadence and its userbase's constraints attached. That weight
  is what "anything it imports can break a build" was refusing.
  `isabelle-layout` is the same parser split out for this purpose and declares
  no dependencies of its own, so the transitive tree stays empty. Keeping a
  private copy would have been applying the proxy after the thing it stood for
  was fixed.

  Nor does the import land mid-build: `build.py` reaches it while deriving the
  session, *before* `isabelle build` is spawned, and `attempts.py` long after,
  reading a corpus. A failure there is configuration — loud, before anything
  runs — not the class `guard.py` swallows.

  Two things stayed local, and are all `roots.py` now is. A **fragment is not
  a file**: `attempts.py` reads the added and context lines of a hunk in a
  ROOT, and the public entry point takes a path, so `sessions_in_fragment`
  writes the fragment to a temporary ROOT and parses that (sixty hunks across
  both real corpora, so the cost is nothing; a text-taking entry point
  upstream would remove it). And **`<anon>` is not a name** — see *Fixed*.

  Reading a hunk as a unit rather than line by line is also more accurate than
  what it replaced: a `(* … *)` wholly inside the payload now hides what it
  encloses, where the line-wise reader saw through it.

  Required as `>=0.2.2` — a floor and no ceiling, which is what that package
  asks consumers for. An upper bound cannot tell "0.3 broke something" from
  "0.3 exists", since it is evaluated before anything runs, and it propagates
  to *our* consumers, who never chose it. 0.2.2 because that is the first
  release published to PyPI: a floor naming versions no index will ever serve
  reads as a weaker constraint than it is.

- **The version is stated in `pyproject.toml`**, not in
  `src/isabelle_watchdog/__init__.py` via `[tool.hatch.version]`. One file
  holds what the project is — name, version, dependencies, entry points —
  instead of splitting it across a manifest and a module that have to be kept
  agreeing. `__version__` reads it back from the installed metadata, so they
  cannot drift; a source tree that was never installed reports `0+unknown`,
  since a wrong version sends a bug report to the wrong commit and an absent
  one does not.

  The old arrangement was justified by `__version__` working uninstalled,
  which is thin against a project whose own rule is *test against an install*.
  Note the wart it trades for: an editable install serves the metadata
  recorded when it was made, so after a bump `-V` reports the old number until
  `pip install -e .` runs again. A test fails when those disagree and names
  the command.

- `$BUILD_SESSION_DIR` unset no longer means `.`. It means "wherever the ROOT
  declaring this session lives", which is the same answer in the case `.` was
  right and the correct one otherwise. If no ROOT git can see declares the
  named session, it still falls back to `.` — a project whose ROOT is outside
  git's view is no worse off than before.

### Fixed

- **Capture died on the first build of every new project, and said so in a
  way nobody could read.** A project whose theories are not yet committed —
  the start of any formalisation — hit `fatal: pathspec '*.thy' did not match
  any files` on every build. No `builds.jsonl` was ever created; `trajectory
  check` then reported `no corpus found`. A downstream project lost five
  attempts this way, two of them the informative ones, and they are not
  reconstructible: a hand-written record of an attempt nobody captured is
  fabricated data.

  `git add -u` stages **tracked** files only, so a pathspec matching nothing
  but untracked ones is fatal to it. The filter that exists to prevent
  exactly that (`_matching_pathspecs`, added when a project with no `ROOTS`
  file lost every record the same way) asked git for `ls-files --cached
  --others` — tracked *or* untracked. That is the right set for pass 2,
  `git add -A`, and the wrong one for pass 1: the filter passed the spec
  through and the command it was protecting died on it. Each pass is now
  filtered against what that pass can actually stage.

  Note where it hid. Every existing corpus was written by a project that
  already had a committed theory, so one tracked `.thy` anywhere made the
  bug unreachable — including in the fixture that tests untracked capture,
  which adds an untracked theory *beside* a tracked one. The failure needed
  a project with no committed sources at all, which is a state a corpus by
  definition contains no record of.

- **A repository with no commits, and a directory that is not a repository,
  each failed as a raw git error.** Both are states a formalisation starts
  in — the second is what `git init` leaves you in for as long as it takes to
  write the first theory — and both went through `git rev-parse HEAD`, which
  reports `fatal: not a git repository` in one and prints back the word
  `HEAD` in the other. Either arrived wrapped in a `CalledProcessError`
  beside a green build.

  They are now told apart before anything is created, and each says what is
  missing, why capture needs it, and the command that supplies it — the same
  class as `build.py`'s "no session to build": a precondition the operator
  can meet, not a failure. Capture still never blocks a build, and a project
  that has no corpus yet no longer acquires an `instance-id` for one.

  `isabelle-build --where` reports it too, which is the point at which it is
  most useful: that command exists to answer "what will adopting this do to
  my repo" *before* the first build, and "nothing will be recorded until you
  commit" is exactly that answer.

- **A failed capture said "skipped" beside a green build.** The warning
  reported the event and not the consequence:

      build-record: skipped (CalledProcessError: Command '['git', 'add',
      '-u', '--', '*.thy', '*ROOT']' returned non-zero exit status 128.)

  It was read as a note about something optional, which is what "skipped"
  means everywhere else in a build log. It now leads with what was lost:

      build-record: FAILED -- this attempt was NOT recorded.
        The build itself is unaffected.  The source changes will be picked up
        by the next recorded build (diffs are cumulative), but this attempt's
        outcome, timing and error loci are gone -- they cannot be
        reconstructed afterwards.
        cause: GitFailed: ... exit status 128. fatal: pathspec '*ROOT' did
        not match any files

  `run_guarded` takes the consequence as an argument, so a side task whose
  failure costs nothing irreplaceable — enriching an error message — still
  says "skipped". The distinction now carries information instead of being
  the only word available.

  And git's own diagnosis reaches the operator. `CalledProcessError.__str__`
  prints the argv and the exit status and nothing else, so the one line that
  identified the fault was captured and then dropped. `GitFailed` subclasses
  it — `except CalledProcessError` callers are unaffected — and appends what
  git said.

- **A `.last-attempt` naming an unreachable tree lost every later attempt,
  not one.** The chain pointer names throwaway git objects that nothing
  references, so `git gc --prune` may drop them and a clone never receives
  them. `git diff <gone> <tree>` is fatal — and since the pointer is
  rewritten only *after* a record lands, the same dead base is read again on
  the next build. A project that committed the file, which is a reasonable
  thing to do with something sitting beside `builds.jsonl`, would capture
  nothing at all in a fresh clone, silently and permanently.

  The base is now checked for reachability and falls back to `HEAD`'s tree,
  which is what the first attempt in a fresh log directory already does. One
  over-large diff, once, against a project that records nothing. README now
  says which files in a log directory are data (`builds.jsonl`, and only
  that) and which are local state.

- **A missing `isabelle-layout` reached the reader as a traceback.**
  `trajectory list` ended nine frames down at `ModuleNotFoundError: No module
  named 'isabelle_layout'`, from a CLI whose other failures list what they
  tried and how to fix it. Worse, `check` and the builds themselves do not
  route through the ROOT parser, so an absent dependency presents as "the
  writer works, the readers don't" — which reads like a corpus problem.

  It now exits 2 with the install line. The message is raised at the import
  in `roots.py`, so `build.py` inherits it too, and it names the editable
  install specifically: one made before this release declared the dependency
  keeps serving the metadata it was made with, so `pip show` reports no
  requirements and `pip install -e .` has to be re-run. An unrelated
  `ImportError` is re-raised rather than disguised as a missing package.

- **Two ROOT parsers in one package disagreed about session names.**
  `build.py` and `attempts.py` each had a regex, and `attempts.py`'s
  (`"?([A-Za-z0-9_']+)"?`) stopped at the first character outside that class
  *inside* the quotes: `session "HOL-Analysis"` was built under that name and
  attributed under `HOL`, a different real session. `"With.Dots-2"` truncated
  to `With`. A session built under one name and attributed under another is
  undetectable downstream. Both now share `roots.py`.
- **A session commented out across lines was read as real**, so a project
  with one live session and one `(* … *)`'d one was refused as ambiguous
  rather than derived. Isabelle's comments nest and its cartouches carry free
  text; both are now stripped before matching. `attempts.py` reads diff
  fragments, so it can only strip a comment whose `(*` was actually captured
  — one opened outside the hunk is invisible to anything.
- **The ROOT conformance table asserted behaviour Isabelle does not have.**
  Its eight cases were transcribed into `tests/test_roots.py` from a diff of
  one parser against another, with no Isabelle involved. Checked against a
  real `isabelle sessions -d` (Isabelle2025-2), **four of the eight are ROOTs
  `isabelle build` refuses**: `session "Probe (AFP)"` (a quoted name may not
  contain spaces), a bare `session With.Dots-2` (bare names take `.` but not
  `-`), two sessions sharing a directory, and an unterminated comment.

  So the fixture that justified unifying the parsers was partly fiction, and
  the docstring it supported described a build — `"Probe (AFP)"` built under
  that name — that could not have occurred. The defect was real: the valid
  spelling is `"HOL-Analysis"`, quoted, which the old regex truncated to
  `HOL`. The illustration was invented.

  That fixture is gone with the grammar it pinned. `tests/test_roots.py` is
  no longer a conformance suite: whether the parser matches Isabelle is
  checked in `isabelle-layout`, against a corpus it regenerates from a real
  `isabelle sessions -d`. Asserting a dependency's behaviour back at it would
  fail whenever it legitimately improved. What is tested here is the fragment
  seam and the spellings this project acts on.
- **A mistyped flag made `isabelle-watchdog` create a corpus.** An
  unrecognised leading `-word` fell through to the child as the command to
  supervise, so `isabelle-watchdog -V` ran a program named `-V`: it resolved a
  log directory, **created a corpus**, and recorded the failure as an attempt.
  Appending to the wrong file is loud; creating one is silent and looks
  exactly like a first build — and a typo was enough to do it.

  `--version` failed more quietly still. The child is wrapped in `stdbuf`, so
  it ran `stdbuf --version` and confidently reported *that* program's version.

  An unrecognised leading `-word` is now a usage error, exit 2, before
  anything is resolved or created; a program genuinely named that way is what
  `--` is for, which the wrapper's own docstring already said.

- **A nameless `session` stanza reached the attribution map as a name.**
  `isabelle-layout` calls a `session` keyword with nothing after it `<anon>`,
  and Isabelle rejects that input outright, so it is the *absence* of a name.
  `roots.py` drops it.

  Not hypothetical for the fragment reader: the tokeniser's identifier class
  excludes `#`, so a line like `# not a session` reduces to the bare keyword,
  and prose landing on a hunk boundary does that without trying. An
  `<anon> -> some/directory` entry in an attribution map is a name nothing
  can build.
- **`contention.cpu_time_s` reported the spawn baseline on a run too short to
  sample.** The first CPU sample is taken when the child is spawned, as the
  baseline the first duty cycle is measured against; it says what the tree had
  used a millisecond into the run, which is a claim about the machine and not
  about the build. Reading the sample list's tail published it as the run's
  CPU time, so a 3.4 s build recorded `cpu_time_s: 0.02` beside a correctly
  null `duty_cycle` — two fields describing one run, one of which could not be
  true. The field is now set only from an in-loop sample, so the baseline
  cannot reach a record; a run with nothing measured records `null`, which is
  what `duty_cycle` already did.

  *No schema change* — the key and its type are unchanged, and every reader
  already treated absent-or-null as unmeasured. Existing corpora keep whatever
  they recorded; the number was wrong only where `duty_cycle` was already
  `null`, which is exactly where a reader had been told not to trust it.
- **The audits package listed a module that has never existed.** Its docstring
  carried a hand-written inventory naming `loci`, so
  `python -m isabelle_watchdog.audits.loci` failed — those checks are in
  `tests/test_attribution.py`. `audits.catalogue()` now reads the directory
  (parsing each module's docstring rather than importing it, so one broken
  audit cannot hide the other five), and `trajectory audit` prints that. An
  inventory maintained beside the thing it inventories reads as authoritative
  and is the last thing anyone updates.
- **Pre-package script names in user-facing help.** `trajectory audit oneshot
  -h` opened `audit-1shot.py — …`, `python -m isabelle_watchdog.export`
  opened `trajectory-export.py — …`, and `trajectory.py`'s usage block listed
  `bin/trajectory.py check`. Every one names a file that is not installed, not
  on disk and not runnable, on the first line a reader sees. Each now names
  the command that actually reaches it, and a test asserts no retired name
  appears in any help output.

  The audits' `--help` also headed itself with the *dispatcher's* usage,
  because argparse derives `prog` from how the interpreter was launched rather
  than from `sys.argv[0]`. They now share one parser constructor
  (`audits.parser`), which sets `prog` and carries the `-i`/`--attribution`
  pair all six had separately.
- **`trajectory check` told every reader to widen an allowlist the recorder
  narrows on purpose.** Its closing line on `empty-blind` payloads was
  "Unrecoverable, and not a defect in the payload — widen the allowlist",
  which named a cause that applies to none of the records in either real
  corpus: all 46 of ndtht's blind payloads predate the 2026-07-27 capture fix,
  when `git add -u` could not see a theory that had never been committed. An
  instruction that cannot be followed is worse than none.

  `check` now dates them. Before the fix: the tracked-only gap, nothing to
  change, and these records cannot be repaired. After it: a path
  `$BUILD_SOURCE_PATHSPECS` does not admit, which is a decision about the
  project rather than a repair, with a pointer to `trajectory show`. The date
  is `corpus.UNTRACKED_CAPTURE_FIX`, which `audits/zerodiff.py` now shares
  instead of keeping a second copy that could come to disagree about which
  records are explained.

## [0.3.0] — 2026-08-07

Everything a project other than the two that grew this needs before adopting
it: where its records go is discoverable and declarable, capture is optional,
and the budgets no longer assume a machine nobody else has.

**Record-schema change, additive.** Two new keys: a top-level `contention`
object and `limits.load_factor_max`. Corpora written by `0.2.0` read
identically — every view treats an absent key as unmeasured, which is not the
same as zero — and readers' output on both existing corpora is byte-for-byte
unchanged.

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
- **`BATTERY_FACTOR` had never applied off macOS.** Detection was `pmset`
  only, so the scaling was silently inert on exactly the laptops it was
  written for. Linux now reads `/sys/class/power_supply` — a file read, no
  subprocess. A desktop with no mains supply still answers "unknown", because
  "no battery" and "on battery" must not be confused.

### Added

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
