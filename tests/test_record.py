"""Trajectory capture: what lands in the payload, and against what baseline.

Every assertion here is about a decision that has already gone wrong once and
cost data:

  the allowlist          tracked-only capture was blind while a new theory was
                         being authored -- 26 of 28 otherwise-inexplicable
                         fail -> ok flips in the first month.
  re-baselining          a corpus read as one flat chain desynchronises at the
                         first commit.
  the project directory  the recorder once diffed the *tooling* repository and
                         `check` certified every such record sound, because the
                         payload genuinely regenerated.

The trajectory these read is recorded for real -- genuine `git diff` output
against genuine tree objects -- because the readers' whole claim is that
regeneration is the ground truth, and checking a fiction would prove nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from conftest import capture, package_env
from helpers import Repo

pytestmark = pytest.mark.slow


# --------------------------------------------------------------- the allowlist

def test_an_edit_to_a_tracked_theory_is_the_payload(trajectory):
    rec = trajectory.records[0]
    assert "thy/Probe_A.thy" in rec["diff"]
    assert "+lemma easy:" in rec["diff"]


def test_a_theory_git_has_never_seen_is_captured_from_its_first_edit(trajectory):
    """The 2026-07-27 fix, and the reason capture is an allowlist over
    `git add -A` rather than `git add -u`.

    While a new theory is being authored, tracked-only staging sees nothing:
    the snapshot tree never moves and a whole fail -> fix run records as a
    sequence of empty diffs.  That is exactly the highest-value work to be
    blind during.
    """
    rec = trajectory.records[1]
    assert "thy/Probe_B.thy" in rec["diff"]
    assert "+theory Probe_B" in rec["diff"]


def test_scratch_files_stay_out_of_the_payload(trajectory):
    """The other direction, and the reason it is not just `git add -A`.

    Scratch scripts, draft memos and editor backups are not proof deltas.
    A corpus about proving must not accumulate files nobody has decided to
    keep yet.
    """
    diff = trajectory.records[1]["diff"]
    assert "scratch.py" not in diff
    assert "NOTES.md" not in diff


def test_a_non_source_change_is_named_even_though_it_is_not_shipped(trajectory):
    """The fact of the change, without its content.

    An attempt whose outcome flipped with an empty source diff is a puzzle,
    and "the Makefile's timeout budget changed" is the answer.  Names and
    line counts are a few dozen bytes and answer it; the patch is kilobytes
    and is already in the repository's history.
    """
    changed = {path: (add, dele)
               for path, add, dele in trajectory.records[1]["other_changed"]}
    assert changed == {"Makefile": (1, 1)}


def test_only_tracked_non_source_changes_are_reported(trajectory):
    """The boundary, stated so it is a decision rather than a surprise.

    `other_changed` compares two snapshots, and both stage untracked files by
    the same source allowlist -- so a file git has never seen and that is not
    source appears in neither.  For an Isabelle build that is narrow: the
    untracked things able to flip an outcome are `.thy` and `ROOT` files,
    which the allowlist already admits.  A brand-new untracked scratch script
    is not reported, and should not be.
    """
    rec = trajectory.records[1]
    named = {path for path, _a, _d in rec["other_changed"]}
    assert "scratch.py" not in named and "NOTES.md" not in named
    assert "scratch.py" not in rec["diff"]


def test_the_allowlist_admits_and_excludes_the_right_things(repo, logs):
    """The capture allowlist, both directions, in one attempt.

    This was `scripts/check-snapshot-untracked.sh`, which was never wired
    into anything, called `_snapshot_tree()` with an arity it no longer has,
    and probed one project's `t/base/...` paths.  A guard nobody runs is not
    a guard.
    """
    repo.write("thy/New_Theory.thy", "theory New_Theory imports Main begin end\n")
    repo.write("thy/extra/ROOT", "session Extra = HOL +\n  theories\n")
    repo.write("scratch-probe.py", "# throwaway\n")
    (logs / "junk.txt").write_text("gitignored\n")

    capture(repo.root, logs)
    diff = json.loads((logs / "builds.jsonl").read_text().splitlines()[-1])["diff"]

    assert "thy/New_Theory.thy" in diff, "an untracked theory must be captured"
    assert "thy/extra/ROOT" in diff, "an untracked session ROOT must be captured"
    assert "scratch-probe.py" not in diff, "a scratch script must stay out"
    assert "junk.txt" not in diff, "gitignored paths must stay out"


def test_the_gitignored_log_directory_never_enters_a_snapshot(trajectory):
    """The corpus lives inside the project's log directory.  Capturing it
    would make each attempt contain every previous attempt."""
    for rec in trajectory.records:
        assert "logs/" not in (rec["diff"] or "")


# ------------------------------------------------------------- the baseline

def test_diffs_are_incremental_not_cumulative(trajectory):
    """Attempt 1's payload is what attempt 1 changed, not everything since
    HEAD -- otherwise every trajectory grows quadratically and no attempt's
    own contribution is legible."""
    first, second = trajectory.records[0], trajectory.records[1]
    assert "Probe_A" in first["diff"] and "Probe_B" not in first["diff"]
    assert "Probe_B" in second["diff"] and "lemma easy" not in second["diff"]


def test_a_mid_run_commit_re_bases_the_payload(trajectory):
    """Attempt 2 committed everything, then made one small edit.

    Without re-baselining, the payload would be the diff against the *previous
    attempt's* tree -- which now differs from HEAD by everything that was just
    committed, so the committed content would leak back into the record.  Any
    consumer treating the corpus as one flat chain desynchronises here.
    """
    rec = trajectory.records[2]
    assert "by auto" in rec["diff"]
    assert "Probe_B" not in rec["diff"], "committed content leaked into the payload"
    assert rec["git_head"] != trajectory.records[1]["git_head"], "HEAD did move"


def test_a_rebuild_that_changed_nothing_records_an_empty_payload(trajectory):
    """Faithful, and *not* a defect: `check` calls this `empty-consistent`.

    The `head_dirty` flag is what keeps it distinguishable from a rebuild of
    a clean tree.
    """
    rec = trajectory.records[3]
    assert rec["diff"].strip() == ""
    assert rec["other_changed"] is None


def test_the_project_recorded_is_the_one_being_built(trajectory):
    """The instructive failure, guarded.

    `PROJECT_DIR` was `__file__`-relative, which named the *tooling* once the
    recorder moved into its own repository.  It did not error: it wrote a
    faithful, well-formed diff of the wrong repository, and `check` then
    certified every such record sound because the payload really does
    regenerate from the trees it names.
    """
    paths = [l.split()[-1] for l in trajectory.records[0]["diff"].splitlines()
             if l.startswith("diff --git ")]
    assert paths == ["b/thy/Probe_A.thy"]
    # And the tree object it claims to have snapshotted exists *here*.
    assert trajectory.repo.git("cat-file", "-t", trajectory.records[0]["tree"]) == "tree"


# ------------------------------------------------------------------ the record

def test_the_budgets_in_force_are_carried_verbatim(trajectory):
    assert trajectory.records[0]["limits"] == {"activity_timeout": 20,
                                               "wall_timeout": 40}


def test_a_note_is_parsed_but_kept_verbatim(trajectory):
    rec = trajectory.records[1]
    assert rec["note_fields"]["diagnosis"] == "Probe_B is not in ROOT"
    assert rec["note_predicted"] == "ok"
    assert rec["note_source"] == "env"
    assert rec["note"].startswith("diagnosis: Probe_B is not in ROOT")


def test_an_env_note_is_neither_stale_nor_post_hoc(trajectory):
    """`BUILD_NOTE` is set before the process starts, so the two integrity
    bits that qualify a *file* note do not apply and record as null rather
    than as a guess."""
    rec = trajectory.records[1]
    assert rec["note_pre_build"] is None and rec["note_age_s"] is None


def test_every_record_carries_the_same_instance_id(trajectory):
    """One working copy, one id -- that is what lets trajectories from
    parallel worktrees pool by union without colliding."""
    assert len({r["instance_id"] for r in trajectory.records}) == 1


def test_every_record_says_which_version_wrote_it(trajectory):
    """A corpus is read years after it was written, and a field's *meaning*
    can change under a release even when its shape does not -- 0.6.0's
    `contention` is the worked example.  Before this, the only thing telling
    the eras apart was `isabelle-watchdog -V` on the writing machine, which is
    not in the corpus and does not survive pooling several machines'
    (github.com/ott2/isabelle-watchdog#7).

    Compared against the installed metadata rather than a literal: a literal
    here would be a second statement of the version, which is the one thing
    `test_the_version_comes_from_pyproject_and_nowhere_else` exists to
    prevent.
    """
    from isabelle_watchdog import __version__
    assert {r["writer_version"] for r in trajectory.records} == {__version__}


def test_the_version_is_a_release_not_an_uninstalled_tree(trajectory):
    """`0+unknown` is the honest answer for a source tree that was never
    installed, and it must not be what a *test* sees: the suite runs against
    an install (CLAUDE.md), so seeing it here means the fixture captured from
    somewhere the package is not, and every other assertion about the record
    is then about the wrong code."""
    assert trajectory.records[0]["writer_version"] != "0+unknown"


def test_a_reader_shows_the_writer_without_being_taught_about_it(trajectory,
                                                                capsys):
    """`show` prints `rec.items()`, not a declared field list, so provenance
    added to the record reaches a reader with no reader change.  Worth pinning:
    the alternative -- a list of keys to display -- is the inventory-beside-the-
    thing failure this project has twice replaced with derivation."""
    from isabelle_watchdog import attempts
    attempts.cmd_show(trajectory.records, trajectory.records[0]["build_id"],
                      full=False)
    assert "writer_version" in capsys.readouterr().out


def test_records_are_one_json_object_per_line(trajectory):
    """The format is append-only and readable by `tail`; a payload containing
    a newline would break both."""
    for line in trajectory.corpus.read_text().splitlines():
        assert json.loads(line)["build_id"]


# ------------------------------------------------------------ the pending note

def test_a_note_file_is_consumed_by_the_attempt_it_was_written_for(repo, logs):
    """A note that survived its build would attach to a later attempt, and
    misattributed reasoning is indistinguishable from the real thing."""
    note = logs / "next-note.md"
    note.write_text("change: try the other lemma\nexpect: fail\n")
    capture(repo.root, logs, outcome="fail", exit_code=1)
    rec = json.loads((logs / "builds.jsonl").read_text().splitlines()[-1])
    assert rec["note_source"] == "file"
    assert rec["note_predicted"] == "fail"
    assert not note.exists(), "the pending note outlived its attempt"


def test_a_one_liner_overrides_a_stale_pending_note(repo, logs):
    """And leaves the pending note alone: it belongs to an attempt that has
    not happened yet."""
    note = logs / "next-note.md"
    note.write_text("change: something else entirely\n")
    capture(repo.root, logs, note="change: this one; expect: ok")
    rec = json.loads((logs / "builds.jsonl").read_text().splitlines()[-1])
    assert rec["note_source"] == "env"
    assert "this one" in rec["note"]
    assert note.exists(), "a superseded note must not be consumed"


def test_a_note_written_after_the_build_started_is_marked_as_a_summary(repo, logs):
    """`expect:` is only a prediction if it predates the outcome.

    The record stores whether it did rather than trusting anyone: the note's
    mtime against the build's start (now, less its elapsed time).  A note
    written during or after a 60-second build is a summary, and counting it
    in a calibration statistic would flatter the result.
    """
    note = logs / "next-note.md"
    note.write_text("expect: ok\n")               # mtime = now
    capture(repo.root, logs, elapsed_s=60.0)      # ...so the build began first
    rec = json.loads((logs / "builds.jsonl").read_text().splitlines()[-1])
    assert rec["note_pre_build"] is False


def test_a_genuine_prediction_is_marked_as_one_and_aged(repo, logs):
    """The other side, and `note_age_s` with it.

    A note left pending across an hour of further editing is technically
    pre-build and describes something else; only the age exposes that, so
    both fields are recorded rather than one verdict.
    """
    note = logs / "next-note.md"
    note.write_text("expect: ok\n")
    os.utime(note, (time.time() - 300, time.time() - 300))
    capture(repo.root, logs, elapsed_s=60.0)
    rec = json.loads((logs / "builds.jsonl").read_text().splitlines()[-1])
    assert rec["note_pre_build"] is True
    assert 200 < rec["note_age_s"] < 300, rec["note_age_s"]


# -------------------------------------------------------- never break a build

def test_a_project_with_no_matching_sources_still_records(tmp_path, logs, repo):
    """A pathspec matching nothing makes `git add` exit 128, and
    `--ignore-errors` does not cover it -- a project with no ROOTS file would
    otherwise lose every record to a fatal on that one pathspec."""
    capture(repo.root, logs, argv=["isabelle", "build", "X"],
            env={"BUILD_SOURCE_PATHSPECS": "*.nothing-matches-this"})
    rec = json.loads((logs / "builds.jsonl").read_text().splitlines()[-1])
    assert rec["outcome"] == "ok"


def test_the_very_first_theory_in_a_project_is_captured(tmp_path, logs):
    """No source file has been committed yet -- the start of a formalisation.

    Reported from a project whose every build printed `build-record: skipped
    (CalledProcessError: ... exit status 128.)`; `builds.jsonl` was never
    created and five attempts were lost, two of them the informative ones.

    `git add -u` stages *tracked* files only, so a pathspec matching nothing
    but untracked files is fatal to it -- and the filter guarding against
    that asked `ls-files --cached --others`, which counts the untracked ones.
    The filter passed the spec through and the command it protected died on
    it.  Every source pathspec is in that state before the first commit, so
    this was the first build of every new project, and there was no earlier
    record to notice the absence against.

    Deliberately not folded into the `repo` fixture, whose template commits a
    theory and a ROOT: with one tracked `.thy` anywhere the specs match, the
    bug is unreachable, which is exactly why it survived a suite that already
    tested untracked capture.
    """
    r = Repo(tmp_path / "fresh")
    r.write("README.md", "no theories yet\n")
    r.commit("initial")
    # Both source classes present, neither tracked.
    r.write("isabelle/ROOT", "session Laminar = HOL +\n  theories\n    A\n")
    r.write("isabelle/A.thy", "theory A\n  imports Main\nbegin\nend\n")

    capture(r.root, logs, argv=["isabelle", "build", "-d", "isabelle", "X"])

    rec = json.loads((logs / "builds.jsonl").read_text().splitlines()[-1])
    assert "isabelle/A.thy" in rec["diff"], "the first theory must be captured"
    assert "isabelle/ROOT" in rec["diff"], "so must the ROOT declaring it"
    assert "README.md" not in rec["diff"], "the allowlist still applies"


def test_a_tracked_source_change_survives_an_untracked_one_of_another_class(
        tmp_path, logs):
    """Pass 1 is filtered per-spec, so a class with tracked files must not be
    dropped because another class has none.

    The narrow fix -- skip pass 1 whenever anything is untracked -- would
    pass the test above and silently stop recording edits to committed
    theories, which is most of a corpus.
    """
    r = Repo(tmp_path / "mixed")
    r.write("thy/A.thy", "theory A\n  imports Main\nbegin\nend\n")
    r.commit("initial")                      # *.thy tracked, *ROOT is not
    r.write("thy/A.thy", "theory A\n  imports Main\nbegin\n(*edit*)\nend\n")
    r.write("thy/ROOT", "session S = HOL +\n  theories\n    A\n")

    capture(r.root, logs, argv=["isabelle", "build", "X"])

    diff = json.loads((logs / "builds.jsonl").read_text().splitlines()[-1])["diff"]
    assert "+(*edit*)" in diff, "a tracked theory's edit must still be captured"
    assert "thy/ROOT" in diff, "beside the untracked ROOT"


def test_a_chain_pointer_naming_a_vanished_tree_re_baselines(tmp_path, logs):
    """`.last-attempt` names throwaway tree objects that nothing references.

    They are unreachable by design, so `git gc --prune` may drop them, and a
    clone never receives them at all -- nothing points at them to fetch.  A
    project that committed the file (reasonably: it sits beside
    `builds.jsonl`, which certainly is data) would then hand every build a
    base its object store cannot reach.

    The cost of getting this wrong is not one record.  `git diff <gone>` is
    fatal, and the pointer is rewritten only after a record lands, so the
    same dead base is read again next time: the project captures nothing,
    permanently, having done nothing wrong but commit a file.
    """
    r = Repo(tmp_path / "cloned")
    r.write("thy/A.thy", "theory A\n  imports Main\nbegin\nend\n")
    r.commit("initial")
    logs.mkdir(parents=True, exist_ok=True)
    # The *commit* is present -- it was pushed, so a clone has it.  Only the
    # throwaway trees are absent, which is precisely the state that gets past
    # the `last_head == head` check and into the fatal diff.  A pointer whose
    # head is also bogus proves nothing: re-baselining already handles that.
    (logs / ".last-attempt").write_text(f"{'b' * 40}\t{r.head()}\t{'c' * 40}\n")

    r.write("thy/A.thy", "theory A\n  imports Main\nbegin\n(*edit*)\nend\n")
    capture(r.root, logs, argv=["isabelle", "build", "X"])

    rec = json.loads((logs / "builds.jsonl").read_text().splitlines()[-1])
    assert "+(*edit*)" in rec["diff"], "re-baselined on HEAD, not lost"
    # And the chain is healthy again, rather than dead for every later build.
    r.write("thy/A.thy", "theory A\n  imports Main\nbegin\n(*two*)\nend\n")
    capture(r.root, logs, argv=["isabelle", "build", "X"])
    nxt = json.loads((logs / "builds.jsonl").read_text().splitlines()[-1])
    # Against attempt 1 this is `-(*edit*)` / `+(*two*)`; against HEAD it
    # would be `+(*two*)` alone, since HEAD's A.thy has neither line.  The
    # removed line is what says the chain healed rather than staying stuck
    # on HEAD.
    assert "-(*edit*)" in nxt["diff"] and "+(*two*)" in nxt["diff"], \
        f"the next diff must be incremental again:\n{nxt['diff']}"


RECORD_ONCE = ("from isabelle_watchdog import record\n"
               "record.record(argv=[], outcome='ok', exit_code=0, "
               "timeout_reason='', elapsed_s=1.0, error_head='', "
               "log_name='x.log')\n"
               "print('returned normally')\n")


def _record_in(cwd, logs, **env):
    return subprocess.run([sys.executable, "-c", RECORD_ONCE], cwd=str(cwd),
                          env=package_env(WATCHDOG_LOG_DIR=str(logs), **env),
                          capture_output=True, text=True)


def test_capture_failure_is_a_warning_not_an_exception(tmp_path):
    """Called from somewhere with no repository at all.

    `record()` must return normally.  The watchdog calls it after the child
    has exited and before it returns the child's code, so an exception here
    would turn a green build red.
    """
    p = _record_in(tmp_path, tmp_path / "l")
    assert p.returncode == 0, p.stderr
    assert "returned normally" in p.stdout
    assert "NOT recorded" in p.stderr


# ------------------------------------------ what git has not been asked for
#
# Both of these are the state a formalisation starts in, which is where the
# attempts are worth most -- the same observation that made the untracked
# pathspec bug expensive.  Both were fatal a moment later and identically
# unhelpful: `git rev-parse HEAD` reports `fatal: not a git repository` in one
# and prints back the word `HEAD` in the other, either of them arriving
# wrapped in a CalledProcessError beside a green build.

def test_a_directory_that_is_not_a_repository_says_so_and_says_what_to_do(
        tmp_path):
    p = _record_in(tmp_path, tmp_path / "l")
    assert "NOT recorded" in p.stderr, p.stderr
    assert "is not a git repository" in p.stderr
    assert "git init" in p.stderr, "the fix must be in the message"
    assert "BUILD_RECORD=0" in p.stderr, \
        "and the way out for someone who wanted only supervision"
    assert "Traceback" not in p.stderr


def test_a_repository_with_no_commits_yet_says_capture_starts_at_the_first(
        tmp_path):
    """`git init` and no commit: distinct from no repository at all, and it
    gets a distinct message, because the remedy is different.

    Capture is not broken here -- a record anchors its diff to a public
    commit, which is what makes a corpus portable, so there is nothing to
    anchor to yet.  Saying that is worth more than reporting the fatal.
    """
    r = Repo(tmp_path / "fresh")            # init, deliberately no commit
    r.write("A.thy", "theory A imports Main begin end\n")
    logs = tmp_path / "l"
    p = _record_in(r.root, logs)
    assert p.returncode == 0, p.stderr
    assert "has no commits yet" in p.stderr, p.stderr
    assert "capture starts at the first one" in p.stderr
    assert "Traceback" not in p.stderr


def test_nothing_is_created_for_a_project_that_cannot_be_captured(tmp_path):
    """No `instance-id` for a corpus that will never exist.

    The identity is minted once and persisted, so creating one here would
    outlive the condition and attach a working copy to a corpus it never had.
    The check therefore runs before anything is written.
    """
    r = Repo(tmp_path / "fresh")
    logs = tmp_path / "l"
    _record_in(r.root, logs)
    assert not (logs / "instance-id").exists()
    assert not (logs / "builds.jsonl").exists(), \
        "an empty corpus reads as a project that has built nothing"


def test_capture_resumes_at_the_first_commit(tmp_path, logs):
    """The message promises "commit now and every attempt from there is
    captured".  That promise is the test."""
    r = Repo(tmp_path / "fresh")
    r.write("thy/A.thy", "theory A\n  imports Main\nbegin\nend\n")
    _record_in(r.root, logs)
    assert not (logs / "builds.jsonl").exists()

    r.commit("initial")
    r.write("thy/A.thy", "theory A\n  imports Main\nbegin\n(*edit*)\nend\n")
    capture(r.root, logs, argv=["isabelle", "build", "X"])

    rec = json.loads((logs / "builds.jsonl").read_text().splitlines()[-1])
    assert "+(*edit*)" in rec["diff"]


def test_a_failed_capture_says_the_attempt_was_lost(tmp_path):
    """The warning has to be readable as data loss by someone who did not
    write it.

    "skipped", beside a green `OK 1 theories`, was read downstream as a note
    about something optional; five attempts went before anyone worked out
    what it meant.  The consequence goes first, in the words of the thing
    that is gone.

    Provoked with an unwritable log directory -- a genuine unexpected
    failure, as opposed to the preconditions above, which have their own
    message because the operator can act on them.
    """
    r = Repo(tmp_path / "proj")
    r.write("thy/A.thy", "theory A imports Main begin end\n")
    r.commit("initial")
    blocked = tmp_path / "notadir"
    blocked.write_text("this is a file, so mkdir cannot make it a directory\n")

    p = _record_in(r.root, blocked / "logs")
    assert p.returncode == 0, "a broken capture still must not break a build"
    assert "returned normally" in p.stdout
    assert "build-record: FAILED" in p.stderr, p.stderr
    assert "NOT recorded" in p.stderr
    assert "cannot be reconstructed" in p.stderr
    assert p.stderr.index("NOT recorded") < p.stderr.index("cause:"), \
        "the consequence must precede the exception"


def test_gits_own_diagnosis_reaches_the_warning():
    """`CalledProcessError` prints the argv and the exit status and nothing
    else, so the line that named the fault was captured and then discarded.

    That is what made the reported failure unreadable: `returned non-zero
    exit status 128` says a command failed, `fatal: pathspec '*.thy' did not
    match any files` says which pathspec and why.
    """
    from isabelle_watchdog.record import GitFailed
    exc = GitFailed(128, ["git", "add", "-u", "--", "*.thy"], "",
                    "fatal: pathspec '*.thy' did not match any files\n")
    assert "did not match any files" in str(exc)
    assert "128" in str(exc)
    assert isinstance(exc, subprocess.CalledProcessError), \
        "callers catching CalledProcessError must keep working"
