"""The integrity readers: check, repair, replay, extract.

The claim these rest on is that a payload is exactly
`git diff --no-color -M <base> <tree>` for trees the record names, so it can
be regenerated and compared.  That is both the strongest available check and
the exact repair -- no inference about what was lost.  A test of it therefore
has to use a *real* corpus over a *real* object store, which the `trajectory`
fixture provides; synthetic records would only ever confirm that the code
agrees with itself.

The defect classes are then produced by damaging copies of that corpus in the
specific ways the wild produces: a payload through `.strip()`, a payload
overwritten, a payload lost, an object pruned.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from isabelle_watchdog import trajectory as T


# =========================================================== pure diff surgery

def test_index_abbreviation_drift_is_normalised_away():
    """`core.abbrev` defaults to `auto`, so object ids on an `index` line get
    longer as a repository grows.

    A payload recorded when the repo was smaller regenerates *differently*
    today, with no corruption whatever.  Comparing raw bytes would reclassify
    every such record as damaged and -- far worse -- invite a "repair" that
    rewrites healthy history.
    """
    old = "index 1234567..89abcde 100644\n@@ -1 +1 @@\n-a\n+b\n"
    new = "index 1234567ab..89abcdef0 100644\n@@ -1 +1 @@\n-a\n+b\n"
    assert T.normalise(old) == T.normalise(new)


def test_normalising_leaves_the_body_alone():
    """It must: the tail a repair appends is computed on normalised text, so
    anything it touched outside the header would be written back verbatim."""
    body = "@@ -1,3 +1,3 @@\n context\n-old\n+new\n \n"
    assert T.normalise(body) == body


def test_a_complete_hunk_is_not_short():
    assert T.hunk_shortfalls("@@ -1,2 +1,2 @@\n a\n-b\n+c\n") == []


def test_missing_trailing_context_is_short_by_equal_amounts():
    """The `.strip()` signature.  A blank source line is a context line
    holding a single space, so stripping eats whole lines from both sides
    equally -- which is what makes it repairable by inference."""
    (_, old, new), = T.hunk_shortfalls("@@ -1,4 +1,4 @@\n a\n-b\n+c\n")
    assert old == new == 1


def test_a_missing_changed_line_is_short_by_unequal_amounts():
    """`.strip()` cannot remove a `+` line without a matching `-`.  This is
    the class the tool refuses to repair, because guessing the content would
    be fabrication."""
    (_, old, new), = T.hunk_shortfalls("@@ -1,2 +1,4 @@\n a\n-b\n+c\n")
    assert (old, new) != (0, 0) and old != new


def test_paths_are_read_from_both_sides_of_a_patch():
    diff = ("--- a/thy/A.thy\n+++ b/thy/A.thy\n"
            "--- a/thy/Gone.thy\n+++ /dev/null\n")
    pre, post = T.paths_in_diff(diff)
    assert pre == {"thy/A.thy", "thy/Gone.thy"}
    assert post == {"thy/A.thy"}                # /dev/null is not a path


@pytest.mark.parametrize("name,key", [
    ("~/projects/p/isabelle/SP_Slowdown.thy", "SP_Slowdown"),
    ("SPSlowdown.SP_Slowdown", "SP_Slowdown"),      # a watchdog kill
    ("thy/Probe_A.thy", "Probe_A"),                 # a diff
])
def test_the_several_ways_a_theory_is_named_collapse_to_one_key(name, key):
    """A compile error cites a path, a watchdog kill cites a session-qualified
    name, a diff cites a repo-relative path.  All three must compare equal or
    every transition looks like a cross-file edit."""
    assert T.theory_key(name) == key


def test_loci_come_from_the_field_when_there_is_one():
    rec = {"error_loci": [["thy/A.thy", "12"]], "error_head": "ignored"}
    assert T.error_loci(rec) == [("A", 12)]


def test_loci_fall_back_to_parsing_the_error_head():
    """Older corpora predate the field and carry only the first two `***`
    lines, so roughly half their failures name no location at all -- the
    fallback is what makes them readable rather than lost."""
    rec = {"error_loci": None,
           "error_head": 'At command "by" (line 41 of "thy/A.thy")'}
    assert T.error_loci(rec) == [("A", 41)]


def test_a_record_naming_nowhere_yields_no_loci():
    assert T.error_loci({"error_loci": None, "error_head": "Failed"}) == []


def test_a_line_below_an_insertion_is_drift_corrected():
    """Without this, inserting anything above the error makes its line number
    grow, and "the error moved later in the file" fires on pure drift rather
    than on progress."""
    hunks = [(10, 1, 10, 4)]                    # 1 old line becomes 4
    assert T.map_line(hunks, 40) == 43
    assert T.map_line(hunks, 5) == 5            # above the edit: unmoved


def test_a_line_the_edit_covered_maps_to_nothing():
    """"The failing line was rewritten" is a different fact from "the failing
    line moved", and only the second is a line number."""
    assert T.map_line([(10, 3, 10, 3)], 11) is None


# ================================================= against a recorded corpus

@pytest.fixture
def corpus(trajectory):
    """A private, mutable copy of the recorded records."""
    return copy.deepcopy(trajectory.records)


def verdicts(records, trajectory):
    return {v["i"]: v["class"] for v in T.classify(records, trajectory.root)}



# ------------------------------------------------------------- the base rule

@pytest.mark.slow
def test_each_payload_is_anchored_where_the_recorder_anchored_it(corpus, trajectory):
    """The rule, applied in reverse: previous attempt's tree, except across a
    commit, where it re-bases on the new HEAD.

    A consumer that treats the corpus as one flat chain desynchronises at the
    first commit -- and this corpus has one, between records 1 and 2.
    """
    bases = T.resolve_bases(corpus, trajectory.root)
    # Within a run, the base is the previous attempt's snapshot.
    assert bases[1] == corpus[0]["tree"]
    assert bases[3] == corpus[2]["tree"]
    # Across the commit, it is the new HEAD's tree -- not record 1's snapshot.
    head_tree = trajectory.repo.git("rev-parse", f"{corpus[2]['git_head']}^{{tree}}")
    assert bases[2] == head_tree != corpus[1]["tree"]


# --------------------------------------------------------------- healthy corpus

@pytest.mark.slow
def test_a_freshly_recorded_corpus_is_entirely_sound(corpus, trajectory):
    """The baseline every other case is a deviation from.  If this ever
    fails, the recorder and the reader have stopped agreeing on what a
    payload is, and no other verdict here means anything."""
    got = verdicts(corpus, trajectory)
    assert set(got.values()) <= {"sound", "empty-consistent"}, got
    assert got[3] == "empty-consistent"          # the rebuild that changed nothing


@pytest.mark.slow
def test_check_reports_and_changes_nothing(corpus, trajectory, capsys):
    before = json.dumps(corpus)
    assert T.cmd_check(corpus, trajectory.root, SimpleNamespace()) == 0
    assert json.dumps(corpus) == before, "check must be read-only"
    assert "5 records:" in capsys.readouterr().out


# ------------------------------------------------------------- damaged corpora

@pytest.mark.slow
def test_a_payload_through_strip_is_truncated_and_exactly_repairable(corpus,
                                                                     trajectory):
    """The known historical bug, and the one class that repairs to the byte.

    `.strip()` eats the trailing newline and any trailing run of
    whitespace-only context lines.  The repair appends *only* the lost tail,
    so the record keeps its original index abbreviations and every
    historically accurate byte.
    """
    original = corpus[0]["diff"]
    corpus[0]["diff"] = original.strip()
    assert original != corpus[0]["diff"], "the fixture must have a strippable tail"

    (v,) = [v for v in T.classify(corpus, trajectory.root) if v["i"] == 0]
    assert v["class"] == "truncated"
    assert v["repair"] == original


@pytest.mark.slow
def test_an_overwritten_payload_is_divergent_not_truncated(corpus, trajectory):
    """Repairable, but reported separately: it means something other than the
    known `.strip()` bug, and a corpus should never look healthier than the
    evidence supports."""
    corpus[0]["diff"] = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
    assert verdicts(corpus, trajectory)[0] == "divergent"


@pytest.mark.slow
def test_a_lost_payload_whose_trees_survive_is_fully_recoverable(corpus, trajectory):
    corpus[0]["diff"] = ""
    assert verdicts(corpus, trajectory)[0] == "empty-recoverable"


@pytest.mark.slow
def test_an_empty_payload_across_an_outcome_flip_is_blind_not_broken(corpus,
                                                                     trajectory):
    """The two axes, separated.

    A prover is deterministic on identical sources, so an outcome that
    flipped between fail and ok while the snapshot did not move means the
    edit was to a file outside the allowlist.  The payload is *faithful*; the
    capture is what failed, and no amount of regeneration can recover content
    that was never seen.
    """
    corpus[3]["outcome"] = "fail"                # record 4 is ok, snapshot unmoved
    corpus[4]["diff"] = ""
    corpus[4]["tree"] = corpus[3]["tree"]
    assert verdicts(corpus, trajectory)[4] == "empty-blind"


def _blind(corpus, timestamp):
    """The same damage as above, dated.

    Flipping record 3 makes it blind as well as record 4 -- it is the fixture's
    no-op rebuild, so its payload is already empty and its outcome now differs
    from record 2's.  Both get the timestamp: the point here is the era split,
    and a stray record from the other era would test the wrong thing.
    """
    corpus[3]["outcome"] = "fail"
    corpus[4]["diff"] = ""
    corpus[4]["tree"] = corpus[3]["tree"]
    corpus[3]["timestamp"] = corpus[4]["timestamp"] = timestamp
    return corpus


@pytest.mark.slow
def test_a_blind_payload_from_the_tracked_only_era_asks_for_nothing(corpus,
                                                                    trajectory,
                                                                    capsys):
    """`check` used to close by telling every reader to "widen the allowlist",
    which was wrong twice over: it contradicts a narrowing the recorder makes
    deliberately, and on both real corpora the entire population predates the
    2026-07-27 capture fix, where there is no allowlist question to answer --
    a theory being authored was invisible until its first commit.

    An instruction that cannot be followed is worse than none: it sends a
    reader to change a setting that was never the cause.
    """
    T.cmd_check(_blind(corpus, "2026-01-01T12:00:00"), trajectory.root,
                SimpleNamespace())
    out = capsys.readouterr().out
    assert "2 predate 2026-07-27" in out
    assert "Nothing to change" in out
    assert "BUILD_SOURCE_PATHSPECS" not in out


@pytest.mark.slow
def test_a_blind_payload_recorded_since_is_a_question_about_this_project(
        corpus, trajectory, capsys):
    """After the fix the allowlist genuinely is the remaining explanation --
    but it is narrow on purpose, so widening it is a decision rather than a
    repair, and the message says which files to look at first."""
    T.cmd_check(_blind(corpus, "2026-08-01T12:00:00"), trajectory.root,
                SimpleNamespace())
    out = capsys.readouterr().out
    assert "BUILD_SOURCE_PATHSPECS" in out
    assert "predate" not in out


@pytest.mark.slow
def test_a_timeout_flip_is_not_read_as_blindness(corpus, trajectory):
    """A timeout is wall-clock dependent and so can differ on identical
    input -- the one flip that is not evidence of a missed edit."""
    corpus[3]["outcome"] = "timeout"
    corpus[4]["diff"] = ""
    corpus[4]["tree"] = corpus[3]["tree"]
    assert verdicts(corpus, trajectory)[4] == "empty-consistent"


@pytest.mark.slow
def test_a_pruned_object_leaves_a_payload_unverified_never_sound(corpus,
                                                                 trajectory):
    """Deliberately not called sound.

    Hunk accounting cannot see a lost `+`/`-` line that leaves the counts
    balanced, so with the objects gone the payload can only be judged against
    itself -- and the tool says so rather than certifying it.
    """
    corpus[0]["tree"] = "0" * 40                 # an object that never existed
    assert verdicts(corpus, trajectory)[0] == "unverified"


@pytest.mark.slow
def test_an_unequal_shortfall_with_no_objects_is_damaged(corpus, trajectory):
    corpus[0]["tree"] = "0" * 40
    corpus[0]["diff"] = "@@ -1,2 +1,9 @@\n a\n-b\n+c\n"
    assert verdicts(corpus, trajectory)[0] == "damaged"


# ------------------------------------------------------------------- repairing

def write_corpus(path: Path, records) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


@pytest.mark.slow
def test_a_dry_run_reports_without_writing(corpus, trajectory, tmp_path, capsys):
    corpus[0]["diff"] = corpus[0]["diff"].strip()
    path = write_corpus(tmp_path / "builds.jsonl", corpus)
    before = path.read_text()
    args = SimpleNamespace(corpus=str(path), apply=False, heuristic=False,
                           backup=False)
    assert T.cmd_repair(copy.deepcopy(corpus), trajectory.root, args) == 0
    assert path.read_text() == before
    assert "dry run" in capsys.readouterr().out


@pytest.mark.slow
def test_repair_restores_the_payload_and_says_it_did(corpus, trajectory,
                                                     tmp_path):
    """A repaired record is annotated, because a corpus in which repaired and
    original payloads are indistinguishable cannot be audited afterwards."""
    original = corpus[0]["diff"]
    damaged = copy.deepcopy(corpus)
    damaged[0]["diff"] = original.strip()
    path = write_corpus(tmp_path / "builds.jsonl", damaged)

    args = SimpleNamespace(corpus=str(path), apply=True, heuristic=False,
                           backup=False)
    assert T.cmd_repair(damaged, trajectory.root, args) == 0

    written = [json.loads(l) for l in path.read_text().splitlines()]
    assert written[0]["diff"] == original
    assert written[0]["diff_repaired"] == T.NOTE_REGENERATED
    # ...and the repaired corpus now checks clean.
    assert verdicts(written, trajectory)[0] == "sound"


@pytest.mark.slow
def test_repair_leaves_a_backup_when_git_cannot(corpus, trajectory, tmp_path):
    """A corpus normally lives in its own git repository, which is a better
    backup than a `.bak`.  Outside one there is no safety net, so it writes
    the file."""
    corpus[0]["diff"] = corpus[0]["diff"].strip()
    path = write_corpus(tmp_path / "builds.jsonl", corpus)
    args = SimpleNamespace(corpus=str(path), apply=True, heuristic=False,
                           backup=False)
    T.cmd_repair(corpus, trajectory.root, args)
    assert (tmp_path / "builds.jsonl.bak").exists()


@pytest.mark.slow
def test_repair_writes_through_a_symlink_to_the_real_file(corpus, trajectory,
                                                          tmp_path):
    """A corpus is usually a symlink into a separate trajectory repository.

    Writing through the link naively would replace the *link* with a regular
    file and orphan the real corpus -- losing irreplaceable data in the name
    of repairing it.
    """
    real = write_corpus(tmp_path / "real.jsonl", corpus)
    link = tmp_path / "builds.jsonl"
    link.symlink_to(real)
    corpus[0]["diff"] = corpus[0]["diff"].strip()

    args = SimpleNamespace(corpus=str(link), apply=True, heuristic=False,
                           backup=False)
    T.cmd_repair(corpus, trajectory.root, args)
    assert link.is_symlink(), "the symlink was replaced by a regular file"
    assert "diff_repaired" in real.read_text()


@pytest.mark.slow
def test_nothing_to_repair_is_not_an_error(corpus, trajectory, tmp_path, capsys):
    args = SimpleNamespace(corpus=str(tmp_path / "x.jsonl"), apply=True,
                           heuristic=False, backup=False)
    assert T.cmd_repair(corpus, trajectory.root, args) == 0
    assert "nothing to repair" in capsys.readouterr().out


# -------------------------------------------------------------------- replaying

@pytest.mark.slow
def test_every_payload_reconstructs_the_blobs_it_claims(corpus, trajectory,
                                                        capsys):
    """An independent route: apply the patches rather than re-diffing.

    It does not assume the payload equals a regeneration, so it catches a
    class regeneration cannot -- a payload that compares equal to a *wrong*
    pair of trees.  Verification is per file because `git archive` honours
    `export-ignore` and `git add -A` honours `.gitignore`, so re-hashing a
    whole materialised tree is unreliable.
    """
    args = SimpleNamespace(start=None, stop=None)
    assert T.cmd_replay(corpus, trajectory.root, args) == 0
    assert "0 failed" in capsys.readouterr().out


@pytest.mark.slow
def test_replay_fails_loudly_on_a_payload_that_does_not_apply(corpus, trajectory,
                                                              capsys):
    corpus[0]["diff"] = corpus[0]["diff"].replace("+lemma easy:", "+lemma NOPE:")
    args = SimpleNamespace(start=None, stop=None)
    assert T.cmd_replay(corpus, trajectory.root, args) == 1
    assert "blob mismatch" in capsys.readouterr().out


# ------------------------------------------------------------------ extracting

@pytest.mark.slow
def test_extract_materialises_the_sources_of_one_attempt(corpus, trajectory,
                                                          tmp_path):
    """Written out from `ls-tree` + `cat-file` rather than `git archive`,
    which would silently drop anything marked `export-ignore`."""
    dest = tmp_path / "out"
    args = SimpleNamespace(n="1", dest=str(dest))
    assert T.cmd_extract(corpus, trajectory.root, args) == 0
    assert (dest / "thy" / "Probe_A.thy").exists()
    # Record 1 is the attempt that introduced the second theory.
    assert (dest / "thy" / "Probe_B.thy").exists()


@pytest.mark.slow
def test_extract_declines_when_the_snapshot_is_gone(corpus, trajectory, tmp_path,
                                                    capsys):
    corpus[0]["tree"] = None
    args = SimpleNamespace(n="0", dest=str(tmp_path / "out"))
    assert T.cmd_extract(corpus, trajectory.root, args) == 1
    assert "no surviving tree object" in capsys.readouterr().err
