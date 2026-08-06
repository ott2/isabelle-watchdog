# Changelog

Notable changes to `isabelle-watchdog`. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

One thing this project versions that most do not: the **record schema**. A
corpus is irreplaceable data read years after it was written, so a release
that changes what a record contains says so here explicitly, and
`trajectory check` will tell you whether a corpus written by an older version
still regenerates.

## [Unreleased]

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
