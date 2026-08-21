"""Every subcommand runs, and the failures fail the way they promise.

Thirteen verbs over one file format, assembled from two scripts written in
two projects.  A smoke test over all of them is worth more than it looks:
several are thin adapters onto `attempts.py` whose signatures predate the
parser, and the historical failure mode there is not a wrong answer but a
`TypeError` nobody hit until someone tried that verb on that corpus.

The two failures checked at the foot are the ones a reader meets first, and
both used to be silent: a corpus that could not be resolved, and a corpus
that resolved to nothing.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import package_env

pytestmark = pytest.mark.slow

ALL_COMMANDS = ("check", "repair", "replay", "extract",
                "list", "show", "episodes", "notes", "classify",
                "lengths", "size", "progress", "flips", "audit")

# Every name a user ever typed to reach this code before it was a package.
# They survived into `--help` as the first line a reader sees: `trajectory
# audit oneshot -h` opened "audit-1shot.py — …", naming a file that is not
# installed, not on disk, and not runnable.
RETIRED_SCRIPT_NAMES = (
    "audit-1shot.py", "audit-attribution.py", "audit-timeouts.py",
    "audit-zerodiff.py", "recount-lengths.py", "oneshot-significance.py",
    "trajectory-export.py", "bin/trajectory.py", "bin/attempts.py",
)


def run(args, cwd, corpus=None, **env):
    argv = [sys.executable, "-m", "isabelle_watchdog.trajectory", *args]
    if corpus is not None:
        env.setdefault("TRAJECTORY_CORPUS", str(corpus))
    return subprocess.run(argv, cwd=str(cwd), env=package_env(**env),
                          capture_output=True, text=True)


@pytest.mark.parametrize("cmd", [
    ["check"], ["repair"], ["replay"], ["list"], ["episodes"], ["notes"],
    ["lengths"], ["lengths", "--fit"], ["lengths", "--by-project"],
    ["size"], ["progress"], ["flips"], ["list", "--all"],
    ["episodes", "--diffs"], ["notes", "--loci"],
])
def test_a_subcommand_runs_against_a_real_corpus(trajectory, cmd, tmp_path):
    p = run(cmd, cwd=trajectory.root, corpus=trajectory.corpus)
    assert p.returncode == 0, f"{cmd}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip(), f"{cmd} printed nothing"


def test_show_and_classify_take_a_build_id(trajectory):
    build_id = trajectory.records[0]["build_id"]
    for cmd in (["show", build_id], ["classify", build_id, "-v"]):
        p = run(cmd, cwd=trajectory.root, corpus=trajectory.corpus)
        assert p.returncode == 0, f"{cmd}\n{p.stdout}\n{p.stderr}"
        assert build_id in p.stdout or "code" in p.stdout


def test_extract_writes_a_snapshot(trajectory, tmp_path):
    dest = tmp_path / "snapshot"
    p = run(["extract", "1", str(dest)], cwd=trajectory.root,
            corpus=trajectory.corpus)
    assert p.returncode == 0, p.stderr
    assert (dest / "thy" / "Probe_A.thy").exists()


def test_every_documented_subcommand_is_reachable(trajectory):
    """The grouped epilog is the command list, so a verb that fell out of the
    parser would still be advertised there."""
    p = run(["--help"], cwd=trajectory.root, corpus=trajectory.corpus)
    assert p.returncode == 0
    for name in ALL_COMMANDS:
        assert name in p.stdout, f"{name} is not in the help"
    for group in ("integrity", "reading", "measuring", "auditing"):
        assert group in p.stdout


# ---------------------------------------------------------------------- audits
#
# The audits guard the statistics that get published, and were reachable only
# as `python -m isabelle_watchdog.audits.<name>` -- which is to say reachable
# only by someone who already knew they were there, since nothing in `--help`
# said so.  A validation suite nobody can find is one nobody runs.

def test_every_audit_on_disk_is_listed(trajectory):
    """The catalogue is derived, so it cannot drift from the directory.

    It drifted once: the package docstring carried a hand-written inventory
    naming a `loci` module that never existed here (those checks are
    `test_attribution.py`).  An inventory beside the thing it inventories
    reads as authoritative and is the last thing anyone updates.
    """
    from isabelle_watchdog import audits
    on_disk = {p.stem for p in
               (Path(audits.__file__).parent).glob("*.py")
               if not p.stem.startswith("_")}
    assert {name for name, _ in audits.catalogue()} == on_disk

    p = run(["audit"], cwd=trajectory.root, corpus=trajectory.corpus)
    assert p.returncode == 0, p.stderr
    for name in on_disk:
        assert name in p.stdout, f"{name} is on disk but not listed"


def test_an_audit_runs_through_the_dispatcher(trajectory):
    """Flags reach the audit's own parser, and the corpus it reads is the one
    it resolves for itself -- `-i`, not the positional every other verb takes."""
    p = run(["audit", "zerodiff", "-i", str(trajectory.corpus)],
            cwd=trajectory.root)
    assert p.returncode == 0, f"{p.stdout}\n{p.stderr}"
    assert p.stdout.strip()


def test_an_unknown_audit_names_the_ones_that_exist(trajectory):
    p = run(["audit", "nosuch"], cwd=trajectory.root, corpus=trajectory.corpus)
    assert p.returncode == 2
    assert "no audit named" in p.stderr and "zerodiff" in p.stderr


@pytest.mark.parametrize("name", ["oneshot", "zerodiff", "significance"])
def test_an_audits_own_help_quotes_a_command_that_works(trajectory, name):
    """argparse takes `prog` from how the interpreter was launched, not from
    `sys.argv[0]`, so an audit reached through the dispatcher used to head its
    help with the *dispatcher's* usage -- a command that does not accept the
    flags printed under it."""
    p = run(["audit", name, "-h"], cwd=trajectory.root, corpus=trajectory.corpus)
    assert p.returncode == 0
    assert f"usage: trajectory audit {name}" in p.stdout


def test_no_help_text_names_a_script_that_no_longer_exists(trajectory):
    """These names predate the package.  A first line naming an uninstallable
    file is the worst place to spend a reader's first guess."""
    helps = [run(["--help"], cwd=trajectory.root, corpus=trajectory.corpus),
             run(["audit"], cwd=trajectory.root, corpus=trajectory.corpus)]
    helps += [run(["audit", name, "-h"], cwd=trajectory.root,
                  corpus=trajectory.corpus)
              for name, _ in _catalogue()]
    for p in helps:
        for retired in RETIRED_SCRIPT_NAMES:
            assert retired not in p.stdout, f"{retired} in:\n{p.stdout}"


def _catalogue():
    from isabelle_watchdog import audits
    return audits.catalogue()


def test_repo_is_only_offered_where_it_does_something(trajectory):
    """`--repo` on a view that never opens the repository would imply it did
    something -- the fault that left `check` silently defaulting to the tool's
    own directory."""
    needs = run(["check", "--help"], cwd=trajectory.root, corpus=trajectory.corpus)
    does_not = run(["notes", "--help"], cwd=trajectory.root,
                   corpus=trajectory.corpus)
    assert "--repo" in needs.stdout
    assert "--repo" not in does_not.stdout


# ------------------------------------------------------------------- failures

def test_an_unresolvable_corpus_is_reported_not_guessed(repo):
    p = run(["list"], cwd=repo.root)
    assert p.returncode == 2
    assert "no corpus found" in p.stderr


def test_an_empty_corpus_says_so(repo, tmp_path):
    empty = tmp_path / "builds.jsonl"
    empty.write_text("")
    p = run(["list"], cwd=repo.root, corpus=empty)
    assert p.returncode == 1
    assert "no attempts recorded yet" in p.stderr


def test_a_named_attribution_file_must_exist(trajectory, tmp_path):
    """A typo must not read as "no overrides": absence is what a rename looks
    like, and an oversight should not read as a decision."""
    p = run(["lengths", "--attribution", str(tmp_path / "nope.json")],
            cwd=trajectory.root, corpus=trajectory.corpus)
    assert p.returncode == 2
    assert "FAIL" in p.stderr


def test_an_unknown_subcommand_is_rejected(repo):
    p = run(["frobnicate"], cwd=repo.root)
    assert p.returncode == 2


# The reading views parse ROOT files, so they need `isabelle-layout`; `check`
# and the builds themselves do not.  An absent dependency therefore presents
# as "the writer works, the readers don't", which reads like a corpus problem
# rather than an install problem -- and it arrived as a nine-frame traceback
# ending in the module name, from a CLI whose other failures list their fixes.
BLOCK_LAYOUT = """
import sys
class Absent:
    def find_spec(self, name, path=None, target=None):
        if name == "isabelle_layout" or name.startswith("isabelle_layout."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None
sys.meta_path.insert(0, Absent())
sys.modules.pop("isabelle_layout", None)
from isabelle_watchdog.trajectory import main
sys.argv = ["trajectory", "list"]
sys.exit(main())
"""


def test_a_missing_isabelle_layout_is_reported_not_traced(trajectory):
    p = subprocess.run([sys.executable, "-c", BLOCK_LAYOUT],
                       cwd=str(trajectory.root),
                       env=package_env(TRAJECTORY_CORPUS=str(trajectory.corpus)),
                       capture_output=True, text=True)
    assert p.returncode == 2, f"{p.stdout}\n{p.stderr}"
    assert "Traceback" not in p.stderr, p.stderr
    assert "pip install isabelle-layout" in p.stderr
    assert "pip install -e ." in p.stderr, \
        "an editable install predating the dependency keeps stale metadata"


def test_an_unrelated_import_error_is_not_disguised_as_the_dependency(trajectory):
    """Catching every ImportError here would report a typo inside
    `attempts.py` as a missing package, sending the reader to pip for a
    problem pip cannot fix."""
    code = BLOCK_LAYOUT.replace('"isabelle_layout"', '"json"') \
                       .replace('"isabelle_layout."', '"json."')
    p = subprocess.run([sys.executable, "-c", code], cwd=str(trajectory.root),
                       env=package_env(TRAJECTORY_CORPUS=str(trajectory.corpus)),
                       capture_output=True, text=True)
    assert "pip install isabelle-layout" not in p.stderr, p.stderr


def _source_file(name: str) -> Path:
    """A file from the source tree beside the tests, or skip.

    Installed from a wheel there is no source tree, and the two release checks
    below are about the tree a release is cut from rather than about an
    install.
    """
    p = Path(__file__).resolve().parent.parent / name
    if not p.exists():
        pytest.skip(f"no {name} beside the tests")
    return p


def _declared_version() -> str:
    """The version `pyproject.toml` states -- the single statement of it."""
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"',
                  _source_file("pyproject.toml").read_text())
    assert m, "pyproject.toml states no version"
    return m.group(1)


def test_the_version_comes_from_pyproject_and_nowhere_else():
    """One statement of what this project *is*, in the file that states the
    rest of it.  `__version__` reads it back from the installed metadata, so
    the two cannot drift -- unless someone reintroduces a literal here, which
    is what this catches.

    It also catches a stale environment, and that is worth a moment: an
    editable install keeps serving the version recorded when it was made, so
    right after a bump `-V` reports the *old* one until `pip install -e .` is
    run again.  A release that skips that ships a binary disagreeing with its
    own changelog.
    """
    from isabelle_watchdog import __version__
    declared = _declared_version()
    assert __version__ == declared, (
        f"installed metadata says {__version__}, pyproject says "
        f"{declared} -- re-run `pip install -e .`")


def test_the_changelog_names_the_version_being_released():
    """A release publishes the version-bump commit's message, and that message
    points at CHANGELOG.md -- so a version with no section there ships a
    pointer to nothing.

    The failure is quiet and public: `pip install -U` fetches the new code,
    the Release renders correctly, and only a reader who follows the link
    finds that the file stops at the previous version.  By then the tag is
    pushed, and repairing it means a new commit and a moved tag.

    Notes accumulating under `## [Unreleased]` are exactly right until the
    bump.  What this catches is bumping the version and leaving them there --
    which is the same slip as the stale editable install above, and lands in
    the same five minutes.

    It is also the *only* release hazard a test can see.  The rest of the
    runbook -- the workflow must live in the tagged commit's tree, and
    `git push` alone does not push tags -- fails by silence, on a machine no
    test is running on.  That asymmetry is why this is a test and not a
    Makefile: a check runs unprompted, where a target only helps someone who
    already remembered the procedure.
    """
    declared = _declared_version()
    text = _source_file("CHANGELOG.md").read_text()
    assert f"## [{declared}]" in text, (
        f"pyproject declares {declared}, but CHANGELOG.md has no "
        f"`## [{declared}]` section -- rename the `## [Unreleased]` heading, "
        f"or add one, before tagging v{declared}")


@pytest.mark.parametrize("flag", ["-V", "--version"])
def test_the_version_answers_before_the_required_subcommand(repo, flag):
    """COMMAND is `required=True`, so the obvious reading is that `trajectory
    -V` complains about a missing subcommand -- which is what it did.

    argparse runs a `version` action the moment it sees the flag, before the
    required check, and this pins that: it is behaviour of argparse rather
    than of anything written here, so it should fail loudly if it changes.
    """
    p = run([flag], cwd=repo.root)
    assert p.returncode == 0, p.stderr
    assert "isabelle-watchdog" in p.stdout
