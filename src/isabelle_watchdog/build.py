#!/usr/bin/env python3
"""isabelle-build — the recorded way to run the Isabelle build.

    isabelle-build                                 # no note; records null
    isabelle-build -m 'change: swap blast for auto; expect: ok'
    isabelle-build -m - < note.md                  # note on stdin
    isabelle-build --note-file notes/next.md
    isabelle-build --lint -m '...'                 # check the note, do not build
    isabelle-build --where                         # what would this build, and record where?
    isabelle-build --no-record                     # supervise, but record nothing
    isabelle-build -- -o quick_and_dirty           # extra isabelle arguments

Why a single command that carries its own note, rather than "write a file,
then build":

  - **One call, one record.**  The note and the attempt it explains cannot
    become separated, and a caller cannot do half the protocol.  Write-then-
    build is two steps with state between them; the failure mode is a build
    with no note (silent, and the whole point lost) or a pending note that
    attaches to some later attempt (worse — misattributed reasoning is
    indistinguishable from the real thing).
  - **`-m` is the shape every operator already knows.**  It is `git commit
    -m`.  Nothing has to be learned, and a small model reaches for it without
    being told twice.
  - **One permission, one audit line.**  In a harness that gates tool calls by
    command prefix, `bin/build *` is a single rule.  The file route needs a
    write permission into the log directory *as well*, and shows up in a
    transcript as two unrelated actions.

The file route is kept for genuinely long notes, where shell quoting stops
being pleasant: write $WATCHDOG_LOG_DIR/next-note.md and build with no -m.
Notes are optional in every form; the default is no note, recorded as null.

Recording is on by default and `--no-record` (or BUILD_RECORD=0) turns it off
for a project that wants only the supervision.  `--where` reports which corpus
this would record into, and why -- the question a project has when adopting
this, and one the tools can answer about themselves.

This wraps isabelle_watchdog.watchdog, which is where trajectory capture
actually happens, so any path through the watchdog is recorded whether or not
it came through here.  Running `isabelle build` directly is the one way to
lose an attempt: the sources are still captured by the next recorded build
(the diff is cumulative), but that attempt's outcome, timing and error loci
are gone.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from . import __version__
from . import corpus
from . import guard
from . import roots



def project_dir() -> Path:
    """The project being built: the git top level of the current directory.

    Not `__file__`-relative.  While this script lived in the application that
    happened to name the right thing; from its own repository it names the
    tooling, and every path below would be wrong in the same silent way the
    recorder's PROJECT_DIR was.
    """
    p = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    return Path(p.stdout.strip()) if p.returncode == 0 else Path.cwd()


# The session to build.  There is no defensible *constant* default -- it was
# "SPSlowdown" because this script was written inside 43sp -- but there is a
# defensible derivation; see `resolve_session`.
DEFAULT_SESSION = os.environ.get("BUILD_SESSION")
# Where the session's ROOT lives, relative to the project.  No longer defaulted
# to `.`: unset now means "derive it from the ROOT that declares the session",
# which is the same answer wherever the ROOT actually is.
DEFAULT_SESSION_DIR = os.environ.get("BUILD_SESSION_DIR")


# ROOT parsing is `isabelle-layout`'s, reached through `roots.py` and shared
# with `attempts.py`.  It was two regexes, one here and one there, and they
# disagreed: `session "HOL-Analysis"` built under that name and was attributed
# to `HOL`.  Neither survives; see `roots.py`.
#
# What stays here is *which* ROOTs to read, which is a question about this
# project rather than about ROOT syntax -- `root_files` asks git, deliberately
# (below), where `isabelle_layout.discover_roots` walks the filesystem.
sessions_in = roots.sessions_in


def root_files(project: Path) -> list[Path]:
    """ROOT files in the project, as *git* sees it, relative to `project`.

    Git rather than a filesystem walk, and for more than speed: `.git`,
    ignored build trees, virtualenvs and vendored AFP checkouts are exactly
    the places a stray ROOT lives, and "the files this project tracks or could
    track" is a definition already relied on everywhere else here (the
    recorder's pathspecs, `project_root`).  A walk would need a pruning list,
    and a pruning list is a guess that goes stale.

    `--others --exclude-standard` includes untracked-but-not-ignored files, so
    a project whose ROOT has not been committed yet still resolves -- which is
    the state a new project is in when it runs this for the first time.
    """
    p = subprocess.run(
        ["git", "-C", str(project), "ls-files", "--cached", "--others",
         "--exclude-standard", "--", "*ROOT"],
        capture_output=True, text=True)
    if p.returncode != 0:
        return []
    # `*` crosses directory separators in a git pathspec, so `*ROOT` also
    # matches `MY_ROOT` and `docs/ROOTS`; the basename is the actual test.
    return sorted({Path(l) for l in p.stdout.split("\n")
                   if l.strip() and Path(l).name == "ROOT"})


def resolve_session(project: Path, session: str | None,
                    session_dir: str | None) -> tuple[str, str]:
    """`(session, dir)` to build, deriving whatever was not stated.

    The ladder `resolve_log_dir` uses, for the other question a build needs
    answered.  Declarations win in the order they were made, and only the last
    rung guesses -- at which point it refuses to, unless the project leaves
    exactly one possibility:

      1. `--session` / `--dir` on the command line
      2. `$BUILD_SESSION` / `$BUILD_SESSION_DIR`
      3. the project's committed `.isabelle-watchdog` (`session:` / `dir:`)
      4. derived: one ROOT under the project declaring one session

    Rung 4 is what makes a per-project wrapper deletable.  43sp's
    `isabelle/ROOT` declares exactly `SPSlowdown` and nothing else, so
    `BUILD_SESSION` was carrying information the repository already stated;
    ndtht has ten ROOTs and cannot be derived, which is why this refuses
    rather than picking one.  **Several ROOTs, or several sessions, is an
    error** -- building the wrong session is a confusing Isabelle failure
    minutes later, and recording it as an attempt puts a build of the wrong
    thing into the corpus.

    The directory is derived independently: given a session name, the
    directory is wherever the ROOT that declares it lives.  That is strictly
    better than the `.` this used to default to, and it means a project only
    ever has to state the session.
    """
    marker = corpus.find_marker(project)
    if marker is not None:
        fields = corpus.read_marker(marker)
        session = session or fields["session"]
        session_dir = session_dir or fields["dir"]

    # `roots` are project-relative, because that is what `-d` wants and what
    # an error message should show.  Reading one means anchoring it to the
    # project: resolving it against the cwd works only when the operator
    # happens to be standing at the top level, which is the same class of bug
    # as every other entry in the table in CLAUDE.md.
    roots = root_files(project)
    if session is None:
        declared = [(r, s) for r in roots for s in sessions_in(project / r)]
        if len(declared) == 1:
            (root_file, session), = declared
            return session, session_dir or str(root_file.parent)
        raise ValueError(_no_session_message(project, declared))

    if session_dir is not None:
        return session, session_dir

    # The session was named; find the ROOT that declares it.  Falling back to
    # `.` when nothing does reproduces exactly what this command did before
    # any of this existed, so a project whose ROOT lives somewhere git cannot
    # see is no worse off than it was.
    holders = [r for r in roots if session in sessions_in(project / r)]
    if len(holders) == 1:
        return session, str(holders[0].parent)
    if len(holders) > 1:
        listed = "\n".join(f"    {r}" for r in holders)
        raise ValueError(
            f"session {session!r} is declared by more than one ROOT; say "
            f"which with --dir or `dir:`\n{listed}")
    return session, "."


def _no_session_message(project: Path, declared: list) -> str:
    """Why the session could not be derived, in terms of what was found."""
    if not declared:
        return ("no session to build, and no ROOT under this project declares "
                "one.\n  Pass --session, set $BUILD_SESSION, or add "
                f"`session:` to {corpus.MARKER_NAME}.")
    listed = "\n".join(f"    {s:<24} {r}" for r, s in declared)
    return (f"several sessions under {project}; say which to build.\n"
            f"  Pass --session, set $BUILD_SESSION, or add `session:` to "
            f"{corpus.MARKER_NAME}.\n{listed}")


def read_note(args) -> str | None:
    """The note from -m or --note-file, or None to fall back to the pending
    file (which build_record reads itself)."""
    if args.note_file:
        return sys.stdin.read() if args.note_file == "-" \
            else Path(args.note_file).read_text()
    if args.note:
        return sys.stdin.read() if args.note == "-" else args.note
    return None


# argparse prints `description` above the options, so only the synopsis
# belongs there -- the title and the usage block, which are the first two
# paragraphs of the module docstring.  Everything below them is design
# rationale for someone reading the source, and putting forty lines of it in
# `-h` buries the two things a new project actually needs to find.
SYNOPSIS = "\n\n".join(__doc__.split("\n\n")[:2])

# Printed under the options, where a new adopter looks.  The resolution ladder
# is the one thing a project has to understand before its first build, and the
# marker is the only part of it a project has to *do* -- so it is stated here
# rather than only in a README nobody has opened yet.
EPILOG = """\
where records go:
  $WATCHDOG_LOG_DIR if set.  Otherwise the tools look rather than assume:

    1. a committed `.isabelle-watchdog` at the project root -- its first
       non-blank, non-comment line names the log directory, relative to it
    2. an existing corpus under a known layout (t/logs, results/isabelle-logs)
    3. a new one at t/logs

  Readers resolve the same way, so `trajectory` lands where this wrote.
  `--where` reports the answer for this project, and which rule gave it.

  A new project wants the marker: discovery finds only a corpus that already
  exists, so it is silent about a fresh clone -- whose first build would
  otherwise mint one somewhere the project did not choose.

what gets built:
  --session / --dir if given, else $BUILD_SESSION / $BUILD_SESSION_DIR, else
  `session:` / `dir:` in the marker, else derived: one ROOT under the project
  declaring one session is unambiguous, and its directory is where it was
  found.  Several ROOTs or several sessions is an error listing them, not a
  guess -- building the wrong session records an attempt against the wrong
  thing.  A project with a single session need configure neither.

    $ cat .isabelle-watchdog
    # the log directory, relative to this file
    results/isabelle-logs
    # optional, for a project too ambiguous to derive
    session: SPSlowdown
    dir: isabelle

environment:
  BUILD_SESSION / BUILD_SESSION_DIR   session to build, and where its ROOT is
  BUILD_NOTE / BUILD_NOTE_FILE        note text / pending-note path
  BUILD_RECORD                        capture on/off (default: on; --no-record)
  BUILD_SOURCE_PATHSPECS              what counts as source (*.thy *ROOT *ROOTS)
  WATCHDOG_TIMEOUT / WALL_TIMEOUT     the kill budgets, in seconds
  BATTERY_FACTOR                      scales the budgets on battery (1.0 = off)

  `isabelle-watchdog --help` documents the supervision side in full.
"""


def report_where(project: Path, session: str | None = None,
                 session_dir: str | None = None) -> int:
    """Answer "what would this build do, and why".

    A tool that resolves two questions by four rules each should be able to
    say which rule fired for each.  The alternative is an operator reasoning
    about it from the source, which is how a wrong answer stays believed --
    and with the session derived rather than stated, "which session does this
    project build" is no longer answerable by reading a Makefile.
    """
    marker = corpus.find_marker(project)
    try:
        recording = guard.capture_enabled()
        log_dir = corpus.resolve_log_dir(project, recording=recording)
    except (ValueError, corpus.CorpusError) as exc:
        print(f"build: {exc}", file=sys.stderr)
        return 2

    if os.environ.get(corpus.ENV_LOG_DIR):
        why = f"${corpus.ENV_LOG_DIR} is set"
    elif marker is not None:
        why = f"declared by {marker}"
    elif log_dir == project / corpus.DEFAULT_LAYOUT \
            and not (log_dir / corpus.BASENAME).exists():
        why = ("no corpus found and nothing declared -- this is the default, "
               f"and a build here would create it.\n         Commit a "
               f"{corpus.MARKER_NAME} to choose somewhere else.")
    else:
        why = "an existing corpus was found here"

    print(f"project: {project}")
    print(f"log dir: {log_dir}")
    print(f"    why: {why}")
    # Named separately from the directory, because with capture off the
    # directory is still used -- last-build.log goes there -- and printing a
    # corpus path that nothing will write would be the same species of
    # confident wrong answer this whole ladder exists to stop.
    print(f"records: {log_dir / corpus.BASENAME}" if recording else
          f"records: none -- capture is off (${guard.ENV_RECORD})")

    # The other half of "what would this do".  Reported even when it fails,
    # and *after* the corpus lines, so a project that cannot derive its
    # session still learns where its records would go -- the two questions
    # are independent and one being unanswerable should not hide the other.
    try:
        name, where = resolve_session(project, session, session_dir)
    except ValueError as exc:
        print(f"session: unresolved -- {exc}")
        return 2
    print(f"session: {name}")
    print(f"     in: {where}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=SYNOPSIS, epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-V", "--version", action="version",
                    version=f"isabelle-build (isabelle-watchdog) {__version__}")
    ap.add_argument("-m", "--note", metavar="TEXT",
                    help="reasoning for this attempt; '-' reads stdin. "
                         "Sections: diagnosis: / change: / expect: / ref:, "
                         "separable by '; '. Optional.")
    ap.add_argument("--note-file", metavar="PATH",
                    help="read the note from a file ('-' for stdin)")
    ap.add_argument("--lint", action="store_true",
                    help="report on the note's format and exit without building")
    ap.add_argument("--where", action="store_true",
                    help="report which corpus this would record into, and "
                         "which rule decided, then exit without building")
    ap.add_argument("--no-record", dest="record", action="store_false",
                    default=None,
                    help="supervise the build but capture no trajectory "
                         "(default: capture; also $BUILD_RECORD=0)")
    ap.add_argument("--record", dest="record", action="store_true",
                    help=argparse.SUPPRESS)   # the explicit form of the default
    ap.add_argument("--session", default=DEFAULT_SESSION,
                    help="Isabelle session to build (default: $BUILD_SESSION, "
                         "else `session:` in the marker, else derived from a "
                         "single ROOT declaring a single session)")
    ap.add_argument("--dir", default=DEFAULT_SESSION_DIR,
                    help="directory holding the session ROOT, relative to the "
                         "project (default: $BUILD_SESSION_DIR, else `dir:` "
                         "in the marker, else where that ROOT was found)")
    ap.add_argument("rest", nargs="*", metavar="...",
                    help="extra arguments passed through to isabelle build")
    args = ap.parse_args()

    # Before anything reads it -- including `record`, which resolves at import
    # time below, and the child, which inherits this environment.  A flag is
    # the same setting said on the command line, so it is written where the
    # setting lives rather than threaded separately.
    if args.record is not None:
        os.environ[guard.ENV_RECORD] = "1" if args.record else "0"

    project = project_dir()
    if args.where:
        return report_where(project, args.session, args.dir)

    # Lint asks about a note, not about a build, so it must not require a
    # session -- and must not pay for the ROOT scan that resolving one costs.
    session = session_dir = None
    if not args.lint:
        try:
            session, session_dir = resolve_session(project, args.session,
                                                   args.dir)
        except ValueError as exc:
            print(f"build: {exc}", file=sys.stderr)
            return 2

    # The entry point owns the log location rather than inheriting it.  It was
    # a Makefile's to export, which meant `bin/build` run directly quietly
    # recorded into the recorder's built-in default -- a second corpus, a
    # second instance id, and a build that looks unrecorded because the
    # records are somewhere else.  Configuration belongs to the command, not
    # to one of its wrappers.
    #
    # Owning it is not the same as inventing it: the built-in default was
    # still a guess, and in 43sp -- whose Makefile is exactly the wrapper this
    # paragraph is about -- it guessed wrong.  corpus.resolve_log_dir() looks
    # for the project's real corpus first and only creates one where there is
    # none.  Resolved once here and exported, so every layer below inherits
    # this answer rather than deriving its own.
    try:
        log_dir = str(corpus.resolve_log_dir(project,
                                             recording=guard.capture_enabled()))
    except (ValueError, corpus.CorpusError) as exc:
        print(f"build: {exc}", file=sys.stderr)
        return 2
    os.environ[corpus.ENV_LOG_DIR] = log_dir

    from .record import lint_note, NOTE_FILE

    note = read_note(args)
    if note is None and args.lint:
        note = NOTE_FILE.read_text() if NOTE_FILE.exists() else ""

    if args.lint:
        if not (note or "").strip():
            print("no note to lint", file=sys.stderr)
            return 2
        complaints = lint_note(note)
        for c in complaints:
            print(f"note: {c}", file=sys.stderr)
        if not complaints:
            print("note: ok")
        return 1 if complaints else 0

    # Already carries WATCHDOG_LOG_DIR: it was set above, before `record` was
    # imported, because that module reads it at import time.
    env = dict(os.environ)
    if note is not None:
        env["BUILD_NOTE"] = note
        # A note given here supersedes whatever was pending, and the pending
        # file must not then be consumed by this build: it belongs to an
        # attempt that has not happened yet.
        for c in lint_note(note):
            print(f"note: {c}", file=sys.stderr)

    # `-m` rather than a path to the script: the watchdog is a module in this
    # package now, and asking Python to resolve it means the subprocess uses
    # the same installed copy as this process rather than whatever sits next
    # to __file__.  The subprocess boundary itself stays -- the watchdog
    # installs signal handlers and reaps a process tree, which is not
    # something to run inside a caller's interpreter.
    cmd = [sys.executable, "-m", "isabelle_watchdog.watchdog", "isabelle", "build",
           "-d", session_dir, "-v", *args.rest, session]
    return subprocess.run(cmd, cwd=project, env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
