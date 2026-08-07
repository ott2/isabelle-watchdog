#!/usr/bin/env python3
"""isabelle-build — the recorded way to run the Isabelle build.

    isabelle-build                                 # no note; records null
    isabelle-build -m 'change: swap blast for auto; expect: ok'
    isabelle-build -m - < note.md                  # note on stdin
    isabelle-build --note-file notes/next.md
    isabelle-build --lint -m '...'                 # check the note, do not build
    isabelle-build --where                         # where would this record to?
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

from . import corpus
from . import guard



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


# The session to build.  There is no defensible default -- it was
# "SPSlowdown" because this script was written inside 43sp -- so it is
# required, from $BUILD_SESSION or --session.  A wrong session name is a
# confusing Isabelle error several seconds later; a missing one should be a
# clear message now.
DEFAULT_SESSION = os.environ.get("BUILD_SESSION")
# Where the session's ROOT lives, relative to the project.  `.` suits a
# project whose ROOT is at the top; 43sp used `isabelle/`, ndtht used `t/`.
DEFAULT_SESSION_DIR = os.environ.get("BUILD_SESSION_DIR", ".")


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

    $ cat .isabelle-watchdog
    # the log directory, relative to this file
    results/isabelle-logs

environment:
  BUILD_SESSION / BUILD_SESSION_DIR   session to build, and where its ROOT is
  BUILD_NOTE / BUILD_NOTE_FILE        note text / pending-note path
  BUILD_RECORD                        capture on/off (default: on; --no-record)
  BUILD_SOURCE_PATHSPECS              what counts as source (*.thy *ROOT *ROOTS)
  WATCHDOG_TIMEOUT / WALL_TIMEOUT     the kill budgets, in seconds
  BATTERY_FACTOR                      scales the budgets on battery (1.0 = off)

  `isabelle-watchdog --help` documents the supervision side in full.
"""


def report_where(project: Path) -> int:
    """Answer "which corpus would this build record into, and why".

    A tool that resolves a path by four rules should be able to say which one
    fired.  The alternative is an operator reasoning about it from the source,
    which is how a wrong answer stays believed.
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
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=SYNOPSIS, epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
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
                    help="Isabelle session to build "
                         "(default: $BUILD_SESSION; required)")
    ap.add_argument("--dir", default=DEFAULT_SESSION_DIR,
                    help="directory holding the session ROOT, relative to the "
                         "project (default: $BUILD_SESSION_DIR, else '.')")
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
        return report_where(project)

    if not args.session and not args.lint:
        print("no session to build: pass --session, or set $BUILD_SESSION.\n"
              "(There is no default -- it would only ever be right for one "
              "project.)", file=sys.stderr)
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
           "-d", args.dir, "-v", *args.rest, args.session]
    return subprocess.run(cmd, cwd=project, env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
