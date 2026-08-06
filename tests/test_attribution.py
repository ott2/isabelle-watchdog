"""Which development a trajectory belongs to, derived rather than declared.

Four signals, all of them already in the corpus:

  what is a development?        a directory holding a `.thy`
  which target is which?        `session` lines in captured ROOT diffs, else
                                which directory a build's edits touched
  is this even our work?        the `-d` load path
  are two directories one?      a `.thy` that changed directory

The last two replaced hand-maintained lists.  Neither of the two real corpora
needs a declaration file any more, and the tests below build their
attributions from explicit inputs rather than importing a table from the
package -- there is no such table, and a test that needed one would be
re-asserting the bug.

Also covers the `error_loci` path end to end, which only fires on a *broken*
build and so cannot be validated by running a suite green.  The error text
here is verbatim from a real corpus.
"""
from __future__ import annotations

import json

import pytest

from helpers import diff_of, make_record, rename_of
from isabelle_watchdog import attempts as A
from isabelle_watchdog import watchdog as W


def rec(diff: str = "", **kw) -> dict:
    return make_record(diff=diff, command=kw.pop("command", []),
                       error_loci=kw.pop("error_loci", []), **kw)


def thy_diff(path: str) -> str:
    return diff_of(path, ['lemma x: "True" by simp'])


# ================================================ error loci -> a development

@pytest.fixture
def ndtht_shaped():
    """An attribution of ndtht's shape, stated rather than assumed.

    `HOAU_Spike` is deliberately absent: it was built *against* the tree's
    sessions rather than being one of them, so it must attribute to nothing.
    Absence is what that looks like now -- the old code needed an explicit
    `None` entry, because there a missing key and a declared exclusion
    behaved differently.
    """
    return A.use_attribution(A.Attribution(
        dirs={"t/ae", "t/ar", "t/art", "t/base", "t/ntr"},
        targets={"Alphabet_Enlargement": "t/ae",
                 "Multitape_Alphabet_Reduction": "t/ar",
                 "Nondeterministic_Tape_Reduction": "t/ntr"},
        aliases={}))


# Verbatim shapes from the corpus: a compile error names a path, and the same
# locus is reported once per elaborated obligation.
CORPUS_LINES = [
    "*** Failed to finish proof:",
    "*** goal (1 subgoal):",
    '*** At command "by" (line 1441 of "~/projects/ndtht/t/ar/AlphabetReduction.thy")',
    '*** Undefined constant: "time_cleanup"',
    '*** At command "lemma" (line 41 of "~/projects/ndtht/t/ae/Wrap_Convention.thy")',
    '*** At command "by" (line 1441 of "~/projects/ndtht/t/ar/AlphabetReduction.thy")',
]


def test_loci_extracted_from_real_error_text_attribute(ndtht_shaped):
    loci = W._error_loci(CORPUS_LINES)
    assert len(loci) == 2, f"dedup failed: {loci}"
    assert [ndtht_shaped.locus_dir(t) for t, _ in loci] == ["ar", "ae"]


@pytest.mark.parametrize("theory,want", [
    ("Alphabet_Enlargement.EncodingWrap_WF", "ae"),
    ("Multitape_Alphabet_Reduction.AlphabetReduction", "ar"),
    ("Nondeterministic_Tape_Reduction.TapeReduce", "ntr"),
    ("HOAU_Spike.Anti_Unify", None),
])
def test_a_watchdog_kill_names_its_theory_session_qualified(ndtht_shaped,
                                                            theory, want):
    """The two shapes come from different sides of the watchdog and only one
    of them is a path: a compile error gives a file, a kill gives Isabelle's
    session-qualified name.  Both have to attribute."""
    assert ndtht_shaped.locus_dir(theory) == want


def test_an_episode_with_no_diff_still_attributes_and_counts(ndtht_shaped):
    """The case the whole ladder exists for: a failure that named a line and
    a capture that saw nothing."""
    thy = "~/projects/ndtht/t/ar/AlphabetReduction.thy"
    ep = [rec(outcome="fail", error_head="Failed to finish proof:",
              error_loci=[[thy, "1441"]]),
          rec(outcome="ok")]
    assert ndtht_shaped.project(ep) == "ar"
    assert A.proof_bearing(ep) is True
    assert A.attempt_length(ep) == 2


# ============================================ what counts as a development

@pytest.mark.parametrize("paths,want", [
    (["t/ae/A.thy", "t/ntr/B.thy"], {"t/ae", "t/ntr"}),      # ndtht
    (["isabelle/SP.thy", "isabelle/S3.thy"], {"isabelle"}),  # 43sp, flat
    (["src/theories/X.thy"], {"src/theories"}),
])
def test_the_directories_holding_theories_are_the_developments(paths, want):
    """The whole of the generalisation.  ndtht's `t/x` and 43sp's flat
    `isabelle/` fall out of one rule, and neither is named anywhere in the
    package.

    This used to be `^t/([A-Za-z0-9_-]+)/` -- ndtht's layout, in a tool meant
    to read any project's corpus.  Anywhere else it matched nothing, and the
    consequence was not "unlabelled" but *wrong*: everything fell through to
    'tooling', which reads as "no theory was touched".
    """
    assert A.session_dirs([rec(thy_diff(p)) for p in paths]) == want


def test_a_directory_that_never_holds_a_theory_is_not_one():
    """Deriving the set also *bounds* attribution, which the `t/` prefix used
    to do.  Without a bound, "the parent directory of a changed code file"
    would make a session out of `bin/`."""
    at = A.Attribution.learn([rec(thy_diff("t/ae/A.thy")),
                              rec(thy_diff("bin/tool.py"))])
    assert at.path_dir("t/ae/A.thy") == "ae"
    assert at.path_dir("bin/tool.py") is None


# ================================================== which target is which

ROOT_DIFF = ("diff --git a/t/ar/ROOT b/t/ar/ROOT\n--- a/t/ar/ROOT\n"
             "+++ b/t/ar/ROOT\n@@ -0,0 +1 @@\n"
             "+session Multitape_Alphabet_Reduction = Multitape_TM_Substrate +\n")


@pytest.fixture
def learned():
    return A.Attribution.learn([
        rec(thy_diff("t/ar/A.thy")),                      # makes t/ar a session
        rec(ROOT_DIFF),                                    # ROOT declares the name
        rec(thy_diff("t/ntr/B.thy"),                       # co-occurrence only
            command=["isabelle", "build", "-d", "t",
                     "Nondeterministic_Tape_Reduction"]),
    ])


def test_a_root_declares_its_own_session_name(learned):
    """Isabelle's own declaration, captured in a diff the corpus already
    holds."""
    assert learned.targets["Multitape_Alphabet_Reduction"] == "t/ar"


def test_co_occurrence_names_a_target_no_root_diff_captured(learned):
    """Which directory a build's edits touched -- with a dominance margin,
    since you can build X while editing Y."""
    assert learned.targets["Nondeterministic_Tape_Reduction"] == "t/ntr"


def test_the_root_route_reaches_builds_that_captured_no_diff(learned):
    """Co-occurrence cannot see a target that only ever appears on builds
    with no captured diff -- which is exactly how two ndtht trajectories lost
    their label when the ROOT route was missing."""
    diffless = rec(command=["isabelle", "build", "-d", "t",
                            "Multitape_Alphabet_Reduction"])
    assert learned.command_dir(diffless) == "ar"


# ================================================== is this even our work?

@pytest.fixture
def in_tree():
    return A.Attribution.learn([rec(thy_diff("t/ae/A.thy")),
                                rec(thy_diff("t/ntr/B.thy"))])


def test_a_build_loading_from_outside_the_tree_is_not_this_project(in_tree):
    """ndtht's HOAU_Spike was a preliminary investigation scoped against
    these theories for convenience and later split out.  It used to need a
    config entry; all eight of its builds pass `-d scratch/hoau`, which
    reaches no session directory, while every in-project build passes `-d t`,
    which reaches all of them."""
    outside = rec(command=["isabelle", "build", "-d", "scratch/hoau", "Spike"])
    assert in_tree.loaded_outside(outside) is True
    assert in_tree.command_dir(outside) is None


def test_a_build_loading_from_inside_the_tree_is(in_tree):
    assert in_tree.loaded_outside(
        rec(command=["isabelle", "build", "-d", "t", "Some_Session"])) is False


def test_no_load_path_is_no_opinion(in_tree):
    """Silence must not be read as a verdict."""
    assert in_tree.loaded_outside(rec(command=["isabelle", "build", "X"])) is False


def test_an_absolute_load_path_still_matches_a_relative_session_dir(in_tree):
    """A `-d` may be absolute while session directories are repo-relative --
    three 43sp records use the absolute form, and comparing strings rather
    than path components read them as out-of-tree and dropped two
    trajectories."""
    absolute = rec(command=["isabelle", "build", "-d",
                            "/Users/x/projects/proj/t", "Some_Session"])
    assert in_tree.loaded_outside(absolute) is False


def test_a_genuinely_different_absolute_path_does_not_match(in_tree):
    assert in_tree.loaded_outside(
        rec(command=["isabelle", "build", "-d", "/other/proj/spikes", "S"])) is True


# ============================================ are two directories one project?

def test_a_session_split_for_build_speed_and_merged_back_is_one_development():
    """A directory that exists only to make builds faster is not a second
    development.

    ndtht's `t/aem` was the settled half of `t/ae`, split out so the active
    half re-checked quickly and later merged back.  The split and the merge
    are edits, and because the recorder diffs with `-M` they are recorded as
    renames -- so the corpus says so without anyone declaring it.
    """
    at = A.Attribution.learn([
        rec(thy_diff("t/ae/Settled.thy")),
        rec(rename_of("t/ae/Settled.thy", "t/aem/Settled.thy")),
        rec(thy_diff("t/ae/Active.thy")),
        rec(rename_of("t/aem/Settled.thy", "t/ae/Settled.thy")),
    ])
    assert at.aliases.get("aem") == "ae"
    assert at.path_dir("t/aem/Settled.thy") == "ae", "records from the split era"
    assert at.path_dir("t/ae/Active.thy") == "ae"


def test_a_split_that_is_still_live_resolves_to_the_new_home():
    """The last move wins: the work lives in the new directory now, so that
    is the development's name."""
    at = A.Attribution.learn([rec(thy_diff("t/old/A.thy")),
                              rec(rename_of("t/old/A.thy", "t/new/A.thy"))])
    assert at.aliases.get("old") == "new"


# ============================================================ the escape hatch

def write(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj))
    return p


def test_overrides_are_read_from_a_path_the_caller_names(tmp_path):
    """Named, not discovered beside the corpus.

    A conventional sidecar path sounds convenient and is not: it makes
    overriding anything require *write access to the data*, so trying an
    alternative attribution -- or reading a corpus someone sent you -- means
    editing a dataset.  It also lets a file nobody passed on the command line
    change published statistics.
    """
    p = write(tmp_path, "attr.json", {"_note": "ignored",
                                      "targets": {"Spike": None},
                                      "aliases": {"aem": "ae"}})
    got = A.load_overrides(p)
    assert got["targets"] == {"Spike": None}
    assert got["aliases"] == {"aem": "ae"}


def test_nothing_named_means_no_overrides():
    """The right default for a project with no renames and no out-of-tree
    sessions -- which is both of the real corpora."""
    assert A.load_overrides(None) == {}


def test_the_environment_is_the_other_channel(tmp_path, monkeypatch):
    p = write(tmp_path, "attr.json", {"targets": {"Spike": None}})
    monkeypatch.setenv(A.ENV_ATTRIBUTION, str(p))
    assert A.load_overrides(None)["targets"] == {"Spike": None}


def test_a_named_file_that_is_not_there_is_an_error(tmp_path):
    """A typo must not read as "no overrides": absence is what a rename looks
    like, and an oversight should not read as a decision."""
    with pytest.raises(Exception):
        A.load_overrides(tmp_path / "absent.json")


def test_a_misspelled_key_is_an_error_not_a_no_op(tmp_path):
    """`target` for `targets` would otherwise be accepted and do nothing."""
    p = write(tmp_path, "typo.json", {"target": {"Spike": None}})
    with pytest.raises(Exception):
        A.load_overrides(p)
