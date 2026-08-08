# Working on the tooling

Practices for developing and validating `isabelle-watchdog` itself. Distinct
from using it: everything here is about the times you point the tooling at a
real project in order to test *the tooling*, which is exactly when it is
easiest to damage something that matters.

The tests cover the logic. What they cannot cover is the last step before
believing a change — running it against a real Isabelle build in a real
project — and that step writes to a real corpus unless you say otherwise.

## Never validate against a project's own corpus

A corpus is irreplaceable data: diffs are stored inline, so it is not a
derived cache that can be regenerated. A validation build appends to it like
any other build, and the record is indistinguishable from a real attempt
except by reading the note.

Point the log directory somewhere disposable, and check before you write:

```sh
cd ~/projects/myproject
export WATCHDOG_LOG_DIR=/tmp/scratch-log

isabelle-build --where          # confirm, THEN build
# project: /Users/me/projects/myproject
# log dir: /tmp/scratch-log
#     why: $WATCHDOG_LOG_DIR is set
# records: /tmp/scratch-log/builds.jsonl

isabelle-build -m 'diagnosis: ...; change: ...; expect: ...'
```

`$WATCHDOG_LOG_DIR` is the top tier of resolution — above a committed
`.isabelle-watchdog` marker — so this keeps working in a project that has
declared where its records go. That precedence exists for exactly this: the
operator at the command line outranks the project's default.

**`--no-record` is the wrong tool here.** It suppresses capture entirely, so
if the thing under test is the recorder — a new field, a changed diff base,
the contention block — you have switched off what you came to look at. The
scratch directory keeps the whole write path live and only moves where it
lands. Use `--no-record` when you want supervision *without* a dataset, not
when you want a dataset somewhere else.

**One caveat.** A fresh log directory has no `.last-attempt`, so the first
attempt re-baselines on `HEAD` instead of diffing against the previous
attempt. Irrelevant when testing supervision, timing or contention. It does
matter if you are testing incremental diffs or re-baselining, and then you
want to copy the project's real `.last-attempt` into the scratch directory
first — or accept that attempt 1 is a HEAD diff and start reading at 2.

### Why the built-in warning does not save you

The recorder prints `creating a new corpus at ...` when it mints one. Note
when that fires: on the **scratch** run, which was the safe one. Appending to
a project's real corpus is the normal case, so nothing is printed — the
warning is silent in precisely the situation that hurts.

That is correct behaviour and it is worth knowing the shape of it: the
warning protects against *writing somewhere unexpected*, not against
*writing to production on purpose by accident*. The check for the second one
is `--where`, and it only works if you run it.

## Generating load, and other long-lived side processes

Some validation needs the machine put under stress — contention scaling
cannot be exercised on an idle box. Two rules, both learned the hard way:

**Cleanup must be structural, not a trailing line.** This is not cleanup:

```sh
for i in $(seq 16); do (while :; do :; done) & done
isabelle-build -m '...' | tail -4
kill $(jobs -p)                 # never runs if anything above is interrupted
```

Sixteen busy loops orphaned to PID 1 and ran for eleven hours. Any
interruption — a tool timeout, a broken pipe from that `| tail` — skips the
last line, and a backgrounded subshell survives its parent. Prefer a
construct where cleanup is guaranteed by the language rather than by reaching
the end of a script:

```python
procs = [subprocess.Popen([...]) for _ in range(n)]
try:
    ...
finally:
    for p in procs:
        p.kill()
```

**Never redirect stderr away from a cleanup or a verification.** `kill -TERM
$PIDS 2>/dev/null` followed by `kill -0 $p 2>/dev/null` reported every
process gone while `ps` showed all sixteen running: the signals were being
refused and both errors were in the bin. A cleanup that cannot fail loudly is
a cleanup you cannot trust, and the check that would have caught it was
silenced by the same habit.

Verify by observation — `ps`, the load average — not by the exit status of
the command that was supposed to do the work.

## If something does get into a corpus

Do not reach for `git checkout` on the corpus file. Corpus repositories
accumulate records for a long time before anyone commits, so the working tree
routinely holds many uncommitted *genuine* records; reverting to `HEAD` to
drop three of your own can destroy dozens of theirs. Check first:

```sh
git show HEAD:43sp/builds.jsonl | grep -c .   # committed
grep -c . 43sp/builds.jsonl                   # working tree
```

Remove by `build_id` instead, and confirm the count matched before writing:

```python
DROP = {"20260807-223417-212", ...}
keep = [l for l in lines if json.loads(l)["build_id"] not in DROP]
assert len(lines) - len(keep) == len(DROP)    # a typo'd id must not pass silently
```

Then prove nothing else moved. Records carry diffs that are incremental
against the previous attempt, so removing one from the middle of a chain can
desynchronise every record after it:

```sh
trajectory check                              # no damaged / divergent / truncated
```

and diff every reader's output across the edit, against a copy of the file
taken beforehand. Only the counts you meant to change should change. When
three tail records with empty diffs were removed this way, `lengths`,
`episodes`, `flips` and `progress` were byte-identical — the classifier had
been excluding them from the statistics all along — and only `notes` and
`size` moved.

Back the file up before touching it, and say plainly what was removed. A
corpus whose history is quietly edited is worth less than one with a
documented deletion in it.
