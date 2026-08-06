# isabelle-watchdog

A build watchdog for Isabelle, and the build-trajectory corpus it records.

Two things that ship together because one calls the other:

- **The watchdog** supervises an `isabelle build` and kills it on a stalled
  stdout, a wall-clock budget, or a tactic looping on a single line — and in
  that last case it *names the line*, which is the difference between "the
  build hung" and "`by (auto simp: …)` at `AlphabetReduction.thy:1488`".
- **The recorder** appends one JSON line per attempt to a corpus: the outcome,
  the budgets that were in force, the error loci, the reasoning you wrote
  beforehand, and the incremental diff of the sources. Over time that is a
  record of how a proof was actually found, as opposed to how it reads once
  finished.

```sh
pip install isabelle-watchdog
```

## Use

```sh
# supervise a build and record the attempt
BUILD_SESSION=MySession isabelle-build -m 'diagnosis: the induction is too weak;
                                           change: generalise over the tape index;
                                           expect: ok'

# or call the watchdog directly around any command
isabelle-watchdog isabelle build -d t MySession

# read the corpus
trajectory --help          # thirteen views, grouped by the question they answer
trajectory lengths --fit   # how many attempts did each proof take?
trajectory notes           # what did you predict, and were you right?
trajectory check           # is every recorded diff still intact?
```

## Why record a build at all

A finished proof tells you where you ended up. It does not tell you how many
attempts it took, which of them made progress, or what you believed at the
time — and those are the questions worth asking if you want to know whether a
development was hard, or whether a tool helped.

Three design choices follow from wanting that record to be trustworthy:

**The diff is the payload, stored as text.** Each record carries its own
incremental diff inline, anchored to a public commit. A corpus is therefore
portable: you can read it, and reconstruct any attempt's sources from it,
without the original git object store. An earlier prototype chained snapshots
on `refs/attempts/*` and was unshareable for exactly that reason.

**A prediction, recorded before the outcome.** Notes take four keys —
`diagnosis:`, `change:`, `expect:`, `ref:`. `expect:` is the one worth the
trouble: it is the only field in a build corpus that scores itself. Because
that only holds if the note predates the build, the record stores whether it
did (`note_pre_build`) rather than assuming.

**The corpus can prove its own integrity.** Every payload is exactly
`git diff --no-color -M <base> <tree>` for trees the record names, so
`trajectory check` regenerates and compares each one. Where objects survive,
that is both the strongest available check and the exact repair — no inference
about what was lost. Defects that cannot be repaired without fabricating
content are reported and left alone.

## Configuration

Everything is environment variables, so the tooling composes with whatever
build system a project already has.

| variable | default | what |
|---|---|---|
| `WATCHDOG_TIMEOUT` | `20` | kill after N seconds of stalled stdout |
| `WALL_TIMEOUT` | `40` | absolute wall-clock cap |
| `BATTERY_FACTOR` | `2.0` | scale the budgets on battery power (macOS); `1.0` disables |
| `LOOP_PROGRESS_THRESHOLD` | `3` | consecutive same-line warnings before a loop kill |
| `BUILD_PROGRESS_THRESHOLD` | `15` | passed to Isabelle as `-o build_progress_threshold` |
| `WATCHDOG_LOG_DIR` | `<project>/t/logs` | where records go |
| `BUILD_SOURCE_PATHSPECS` | `*.thy *ROOT *ROOTS` | what counts as source |
| `BUILD_SESSION` | — | session to build (`isabelle-build`) |
| `TRAJECTORY_CORPUS` | — | read a specific corpus |
| `TRAJECTORY_ATTRIBUTION` | — | attribution facts a corpus cannot show |

The wall timeout is deliberately tight. A build that hits it is either looping
or has become measurably more expensive, and both are worth knowing about;
raising the budget to make a red build go green trades a fast, specific failure
for a slow, vague one.

On battery the budgets are scaled rather than bypassed, so a
battery-throttled-but-fine build stops tripping while a genuine cost regression
still does. The loop-detection threshold is scaled too — without that, a slow
but healthy command crosses the unscaled threshold and gets killed as a loop
while the scaled budgets still have room.

## Requirements

Python 3.10+, `git`, and no other runtime dependencies. This runs beside a
build, and anything it depends on is something that can break one.

## Status

Alpha. The record schema is still moving; `trajectory check` will tell you if a
corpus written by an older version has drifted.

The design is documented at length in [`docs/logging-design.md`](docs/logging-design.md),
which the code comments cite by section number.

## Development

```sh
pip install -e ".[test]"
pytest -m "not slow and not isabelle"   # pure logic — seconds
pytest -m "not isabelle"                # + real subprocesses
pytest                                  # + a real isabelle build
```

pytest is a test dependency only — nothing under `tests/` is installed or
imported by the package. The `isabelle` marker covers the end-to-end test,
which needs a real Isabelle and a prebuilt HOL heap and skips cleanly without
them.

## Licence

MIT.
