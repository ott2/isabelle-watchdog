# Changelog

Notable changes to `isabelle-watchdog`. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

One thing this project versions that most do not: the **record schema**. A
corpus is irreplaceable data read years after it was written, so a release
that changes what a record contains says so here explicitly, and
`trajectory check` will tell you whether a corpus written by an older version
still regenerates.

## [Unreleased]

### Changed

**The docs said what the package does, not which command does which part.**

The README opened by dividing it into "the watchdog" and "the recorder" — an
accurate split of the *work*, and a misleading one for the *commands*, since
the recorder runs inside the watchdog. The usage block then offered the two as
alternatives ("or call the watchdog directly") with nothing said about what
the second one costs, which invites the reading that it skips capture. What it
actually skips is the note and the derived session; it records either way.

That layering was stated correctly in exactly one place — `build.py`'s module
docstring — and `SYNOPSIS` truncates the docstring to its first two paragraphs
before argparse sees it. Rightly so: forty lines of rationale in `-h` buries
what a new project needs to find. But it meant the one correct statement of
how the commands relate reached no user-facing surface at all.

It is now in three, worded to agree: the README (*Three commands, one stack*),
a `what this runs:` section in `isabelle-build --help`, and the top of
`isabelle-watchdog --help`, which held the only previous mention — "records
the attempt to a build-trajectory corpus", in the help text of the command a
new adopter reads second. All three also name the one path that genuinely
loses an attempt, which the README had never mentioned: running `isabelle
build` yourself. The sources reach the next recorded build, since diffs are
cumulative; the outcome, timing and error loci do not.

Documentation only — no behaviour changed.

**`docs/logging-design.md` now says what it is.**

It is cited by section number from `record.py`, `attempts.py`, `corpus.py`,
`watchdog.py` and three of the audits, for reasoning the code cannot state
itself — why an episode ends at a success rather than a commit, why
tracked-only capture was a data-quality failure. That half earns its keep.

The other half is a plan from before the implementation, and one section of it
had gone from stale to wrong. §5 presents a `builds.jsonl` schema; three of its
twenty-one fields still exist, `timeout_reason` is spelled `"repetition"` where
the code emits `"loop_progress"`, and it never mentions `diff` — the payload
the entire design now rests on. Someone writing a reader from it would produce
one that parses nothing. §§2–11 design a cost axis that was never built:
`build-stats.py` appears there six times and nowhere in the package.

The repair is to scope the claim rather than to split the document, since
deciding today which sections are live is the inventory-beside-the-thing-it-
inventories failure this project has already replaced twice. A dated preamble
now states that the document is a design record, not documentation, and that
where it disagrees with the code or with a real record it loses. The README's
pointer said "the design is documented at length", which sent a new adopter to
a plan; it now says what the file is for.

**Section numbers are load-bearing, and now checked.** `tests/test_docs.py`
asserts that every `§N` the package cites resolves to a heading. It found one
already: `watchdog.py` cited §12.3.2 for the error head, where §12.3 has no
numbered sub-subsections — the `.2` was a list item within it. The passage was
real and the coordinates were not, which is the worse of the two failures,
because a reader who cannot find §12.3.2 concludes the reasoning was never
written down. Nothing checks the reverse, that every section is cited: half
the document is superseded plan, and a test demanding otherwise would be an
argument for deleting the history the document exists to hold.

## [0.6.2] — 2026-08-28

### Fixed

**0.6.1 leaked a looping Poly/ML instead of killing it**

0.6.1 replaced `kill_tree`'s machine-wide `pkill -TERM -f poly` with a kill of
the child's process group. Removing the pattern match was right; doing the
group *instead of* the descendant walk was not. Isabelle's launcher calls
`setsid()` on every bash process it starts — `contrib/bash_process-*/
bash_process.c`, whose opening comment is *"Bash process with separate process
group id"* — so its Poly/ML is never in our process group at all. A killed
build left an ML still burning a core five minutes later.

Parentage is what binds it, through the JVM, and the walk follows that. So all
three nets now run, because no one of them is a superset of another:

| escape route | descendant walk | process group | cwd sweep |
|---|---|---|---|
| `setsid`, parent alive — *what Isabelle does* | **finds** | misses | misses |
| orphaned into our group | misses | **finds** | misses |
| `setsid` *and* orphaned — an ML outliving its JVM | misses | misses | **finds** |

The walk also has to **enumerate before anything is signalled**, since it
follows parent links and the first kill starts breaking them.

**Nothing is left to leak under automation.** The third net is the scoped
replacement for `pkill`: it asks where an orphan is *working* rather than what
it is *called*, so it claims ours and never a neighbour's. Two filters keep it
honest — the orphan set sampled at spawn (an idle desktop has hundreds, so
"parentless" is no filter at all), and the directory the operator launched
from, which is what Isabelle's ML runs inside. On Linux it reads
`/proc/<pid>/cwd` and spawns nothing; elsewhere it is a single `lsof`.

Verified against a real `isabelle build` this time, not only a synthetic
child: the integration layer's looping-`by` test now leaves nothing behind,
and three unrelated builds running concurrently on the same machine finished
untouched.

## [0.6.1] — 2026-08-28

A patch bump, and the contrast with 0.6.0 is the point: the record schema only
*gains* a key here. No existing field changed shape or meaning, and every
reader that uses `.get` is unaffected, so records either side of this release
stay directly comparable — which is exactly what was not true of 0.6.0, and
why that one took a minor bump.

### Fixed

**A watchdog kill no longer signals every Poly/ML on the machine**

`kill_tree` ended with `pkill -TERM -f poly`, a safety net for *orphaned*
Poly/ML that also matched every process on the machine whose command line
contained "poly". One machine hosting several Isabelle projects is the
ordinary case, and this project's own test suite killed three builds of
another mid-proof — recorded as `fail`, with no Isabelle error, at a duty
cycle over a whole core. The expensive part was not the three records but the
two attempts their operator then spent diagnosing the interference as a fault
in their own theories.

The net was reachable without the pattern all along. **Orphaning changes a
process's parent, not its process group**, so the escapees the tree walk
cannot see — it follows `pgrep -P`, which is parentage — are still in the
group the child leads. The supervised command is now spawned with
`start_new_session=True`, and one `os.killpg` reaps this build's tree, orphans
included, and nothing else. `pkill` is gone.

Two consequences worth knowing about:

- `signal_group` verifies the pid leads its own group before signalling, and
  falls back to the per-pid walk otherwise. `os.killpg` on a non-leader
  signals whatever group it is in — for an ordinarily-spawned child, the
  watchdog's own — so being wrong there is catastrophic rather than merely
  ineffective.
- **Ctrl-C is now forwarded explicitly.** A new session has no controlling
  terminal, so the keystroke reaches the watchdog rather than the child. It
  is passed to the child's group and nothing else happens, which reproduces
  the old behaviour exactly: the build dies, and the attempt is still
  recorded. An abandoned build is still an attempt, and its note is the part
  that cannot be reconstructed.

### Added

**Records say which version wrote them**

A new third key, `writer_version`, from the installed package metadata:

```json
{"build_id": "20260827-234352-063", "instance_id": "fe086617a56ab674",
 "writer_version": "0.6.0", "timestamp": "2026-08-27T23:43:52", …}
```

0.6.0 is the reason it is needed. Its `contention.duty_cycle` and `.verdict`
mean something materially different from the same fields written before it,
while the *shape* of a record did not change — and that release's own notes
had to say "`isabelle-watchdog -V` on the writing machine is the only thing
that distinguishes them". That is not in the corpus, and it does not survive
pooling records from several machines.

**The package version, not a schema number.** A hand-bumped `schema_version`
is an inventory beside the thing it inventories, free to drift; this one is
read from the single `version` in `pyproject.toml` and cannot. It
over-discriminates — a release that changes nothing about records still bumps
it — which costs a reader nothing, since `>= "0.6.0"` answers correctly either
way and the changelog says which releases actually mattered.

No migration, and none is possible: absence means "written before this
release" and nothing finer, which is precisely why the field is worth adding
before the next era rather than after. `0+unknown` means the writer was an
uninstalled source tree. Readers need no change to display it — `trajectory
show` prints the record's own keys rather than a declared list.

**Compare it with `corpus.writer_at_least(rec, "0.6.0")`, not `>=` on the
strings.** `"0.10.0" >= "0.6.0"` is False, because "1" sorts before "6" — so
the obvious filter works until the minor number reaches two digits and then
silently starts dropping the *newest* records, which is the half an era
question is usually about. It reports fewer records rather than raising, which
is what makes it expensive. Absent, null and `0+unknown` all read as *cannot
confirm* and are excluded, so a reader is never told "yes" by a record that
does not know.

It does **not** retire `trajectory check`'s 2026-07-27 date. That separates
two causes of `empty-blind`, and the capture fix it names landed before
`0.1.0.dev0`, so no released version distinguishes it and no future record can
fall on the wrong side of it.

Thanks to the reporter of
[#7](https://github.com/ott2/isabelle-watchdog/issues/7).

## [0.6.0] — 2026-08-27

A minor bump rather than a patch, on the record-schema rule above: `duty_cycle`
and `verdict` now mean something materially different, so `contention` written
before and after this release is not directly comparable. Nothing about the
*shape* of a record changed, and no reader consumes `contention` yet, so no
corpus needs repairing — but a future one must not pool the two eras without
knowing which is which. `isabelle-watchdog -V` on the writing machine is the
only thing that distinguishes them.

### Fixed

**A build using a whole core was reported as `stalled` — "used no CPU"**

`ps` reports the processes that exist *now*, so summing the process tree gave
a CPU total that **fell** whenever a child exited — and Isabelle finishing a
session's `poly` worker is exactly that, several times a build. The duty cycle
differentiates that total, so a departure arrived as a negative delta, was
clamped to zero by `max(0.0, …)`, and was published as `stalled`: the most
confident verdict the policy has, on builds that had used 35.9 CPU-seconds in
40.6 s of wall clock. The tool contradicted itself inside one record:

```json
"contention": {"cpu_time_s": 35.88, "duty_cycle": 0.0, "verdict": "stalled"}
```

The sampler now accounts per pid (`ps -o pid=,time=`) and keeps every process
the tree has held, so a worker that exits keeps the seconds it spent. That is
the correct accounting rather than a workaround — those seconds were really
used, by this build — and it makes `cpu_time_s` mean the cumulative total its
own comment had always claimed.

Two smaller changes follow from it. `duty_cycle` returns `null` on a falling
total instead of zero: a broken measurement must not arrive as a verdict, and
`unknown` grants no extension either, so the conservative behaviour survives
without the false diagnosis. And pid reuse is guarded so it can only
over-count, which reads as `running` and grants nothing.

The report's own suggestion — suppress `stalled` when cumulative CPU is a high
fraction of elapsed time — was deliberately not taken. It treats the symptom
and blinds the one case the window measurement exists for: a build that ran
flat out and *then* hung has a healthy cumulative and a dead window. The
window is right; the arithmetic under it was not.

**A `stalled` timeout claimed more than the measurement supports**

`used no CPU — a hang, not a busy machine` made three claims where the tool
can support one: it measures a *window*, "a hang" is an inference, and "not a
busy machine" rules out an alternative it never tested. Beside the record
above it read as a broken tool, and it directed the reader away from
`last-build.log`, which had the answer. Now:

```
TIMEOUT  40s wall clock exceeded  (no CPU in the last 15s, 27.73s of CPU in 40s wall — possibly a hang)
```

The summary also now quotes the same dict the record keeps, rather than
re-deriving its own gloss from the same variables, so the message and the
record cannot disagree again.

### Added

**A timeout says when the budget went before any session started**

Isabelle spends its first seconds starting a JVM, loading the session graph
and verifying ancestor shasums, announcing nothing until a session actually
begins. A build that reached its target 19.9 s into a 40 s budget was measured
against half the budget its operator set, and nothing said so — the existing
"rebuilt from source first" note is about *whose* session ate the clock, and
is correctly silent when the answer is nobody's.

```
TIMEOUT  40s wall clock exceeded  (19.9s of the 40s budget went before
         Nondeterministic_Time_Hierarchy started — Isabelle startup, not proof time)
```

Reported above a quarter of the budget, and deliberately a report rather than
a correction: starting the wall clock at the first session would leave the
startup phase unsupervised, which is where a hang is least visible. No new
record field — `sessions[0].started_s` already carries it. Roles are not
consulted, so this survives `-b`.

Thanks to the reporter of
[#6](https://github.com/ott2/isabelle-watchdog/issues/6), whose two records
carried `cpu_time_s` and `duty_cycle` side by side; the contradiction was
visible only because both observations are stored.

## [0.5.1] — 2026-08-24

### Added

**A build now says when it is about to leave a heap cold**

Isabelle stores a session's heap only if something *in the same run* descends
from it. `isabelle-build` names one session, so that session is the leaf of
its own plan and stores nothing — and the next build of a descendant finds no
heap, declares the ancestor out of date and re-elaborates it from source. On a
project with a chain of sessions that is paid on every hop, and it presents as
a timeout in a theory you do not own, which is exactly the symptom #4 was
about.

```
note: this build stores no heap for Multitape_TM_Substrate; Multitape_Alphabet_Enlargement,
      Multitape_Alphabet_Reduction descend from it and will re-elaborate it from source.
  Pass `-- -b` to store one, if you build those too.
```

Derived from the project's ROOT graph, printed before the build and by
`isabelle-build --where`, and silent once `-b` is passed or once a descendant
is being built alongside its ancestor — in both cases because the premise has
become false, not because the warning was suppressed.

### Fixed

**`-b` made the `sessions` field name your own session as a dependency**

`role` is read from Isabelle's `Building` / `Running` verb, which is its own
statement of `store_heap`. `-b` sets `store_heap` globally
(`build_process.scala:1165`), so every session reads `Building` and the verb
stops discriminating. The session you asked for was then recorded
`"role": "dependency"`, and a timeout inside it was reported as *"the budget
went on rebuilding dependency X, not on the session you asked for"* — naming
the session you did ask for as one you did not.

Under `-b` the role is now `null` and that note falls silent. Which sessions
were elaborated, and when, is recorded as before.

**`audits/significance` named a consuming project's sessions** (#5)

Its summary line — shown in `trajectory audit` and in `trajectory audit
significance -h` — read *"how much does the pre-ntr/ntr 1-shot gap survive?"*.
`ntr` is a session in one project that uses this package, not a concept in it,
and it was the only one of the six audits not asking a general question about
a measurement. It now reads *"is a 1-shot gap between two session groups real,
or resampling noise?"*.

The report body had the same defect in two `print`s, where it was worse: the
leave-one-day-out table labelled its numbers `ntr` regardless of which group
`--b` had actually selected. Those now use the selected group's name, as the
rest of the report already did.

**`-R` and `-N` were parsed as taking a value**

Both are boolean, so `isabelle build -R -d t MySession` had its `-d` consumed
as `-R`'s argument and the session name was never found — which silently cost
the full error text from `isabelle build_log -H Error` on a failing build. The
option table is now transcribed from `build.scala` and applied with Isabelle's
own grammar, including bundled flags (`-bv`) and attached values (`-dbase`).

### Record schema

`sessions[].role` may now be `null`, meaning the session was elaborated but
Isabelle's output could not say whose it was (`-b`). `"dependency"` and
`"target"` are unchanged, and a record written by 0.5.0 needs no migration.

## [0.5.0] — 2026-08-23

### Fixed

**A wall-clock timeout claimed a loop the detector had not found** (#4)

The watchdog keeps the last `command "X" running for Ns (line Y of theory Z)`
warning it saw, so that a wall or activity kill can still name a line. That is
a *locus*, and the loop verdict is a separate thing — the count of consecutive
such warnings, which any other output resets. The wall-timeout summary printed
the locus with the verdict's word anyway:

```
TIMEOUT  20s wall clock exceeded (looping on Multitape_Alphabet_Enlargement.AlphabetEnlargement_OutputWF line 444 — "by" running for 15.0s)
```

Nothing there was looping. The message now matches the activity kill's
wording, which has always been careful about this:

```
TIMEOUT  20s wall clock exceeded (last: "by" at Multitape_Alphabet_Enlargement.AlphabetEnlargement_OutputWF line 444, 15.0s)
```

The `log:` line above it moves the same way, `stuck at` → `last at`, on a wall
kill only. A loop kill and an activity kill both mean nothing else was
happening and keep `stuck at`; a wall kill means only that the clock ran out.
`LOOP` still says looping, because it is the one kill that measured it.

### Added

**The summary says when the budget went on a dependency** (#4)

Isabelle re-elaborates every out-of-date ancestor from source before reaching
the session you asked for, so a wall budget can be spent entirely on other
people's proofs — and the old summary would then name a line in a theory you
have never opened. The report was 55 s of dependency compilation inside a 20 s
budget:

```
TIMEOUT  20s wall clock exceeded (last: "by" at Multitape_Alphabet_Enlargement.AlphabetEnlargement_OutputWF line 444, 15.0s)
    (the budget went on rebuilding dependency Multitape_Alphabet_Enlargement, not on the session you asked for)
```

and, when the clock did reach your session, `rebuilt from source first: …`.

Nothing is guessed and nothing had to be plumbed in. Isabelle announces every
session it elaborates, and the verb is a fact about the build graph rather
than a turn of phrase: `Building X ...` when X's heap is stored, which happens
exactly when something else in the build depends on it, and `Running X ...`
when it does not. So the dependency/target split is Isabelle's own answer,
read off the pipe. It is absent, rather than guessed, when the output was not
verbose enough to say — `isabelle-build` always passes `-v`.

### Record schema

**One field added: `sessions`.** A list of `{name, role, started_s}` in start
order, `role` being `"dependency"` or `"target"` per the above.

This is the third confound in the same family as `limits` and `contention`: a
build that timed out re-elaborating an out-of-date ancestor and one that timed
out on a proof that got harder were, until now, identical records — and
`trajectory audit timeouts`, whose entire question is telling those two apart,
had no way to derive the difference. It also records what was rebuilt on a
*successful* build, which is where the same slowdown shows up as elapsed time
rather than as a kill.

`[]` and `null` are different claims: `[]` means nothing was rebuilt from
source, `null` means the output never said. Readers ignore unknown keys, so an
older reader opens a newer corpus unchanged, and `trajectory check` is
unaffected — the field is not part of a payload.

## [0.4.0] — 2026-08-21

Three fixes, all reported from a real formalisation running 0.3.1 against the
AFP, and all in the same place: a rule that was right about the build someone
meant to run and wrong about the build that actually happened.

**No record-schema change.** No field was added, removed or given a new
meaning, and every reader that could open a 0.3.1 corpus can open this one.
Two things about *content* are worth knowing before you upgrade, both
affecting new records only and neither rewriting an old one:

- `error_head` now names the stuck theory session-qualified on a timeout
  (`FSM_Tests.Util`, not `Util`). `error_loci` already did, and
  `attempts.theory_key` collapses either spelling to the same key.
- `note_fields` will find sections in notes where 0.3.1 found one long
  `diagnosis:`. The raw `note` was always stored verbatim, so this recovers
  structure rather than adding it.

A minor number rather than a patch: nothing breaks, but the loop detector
decides whether builds get killed and its rule has changed, which is not
something to discover from a version that says "bug fixes".

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
