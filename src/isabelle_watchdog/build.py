#!/usr/bin/env python3
"""bin/build — the recorded way to run the Isabelle build.

    bin/build                                          # no note; records null
    bin/build -m 'change: swap blast for auto; expect: ok'
    bin/build -m - < note.md                           # note on stdin
    bin/build --note-file notes/next.md
    bin/build --lint -m '...'                          # check the note, do not build
    bin/build -- -o quick_and_dirty                    # extra isabelle arguments

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

This wraps bin/isabelle-watchdog.py, which is where trajectory capture
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


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-m", "--note", metavar="TEXT",
                    help="reasoning for this attempt; '-' reads stdin. "
                         "Sections: diagnosis: / change: / expect: / ref:, "
                         "separable by '; '. Optional.")
    ap.add_argument("--note-file", metavar="PATH",
                    help="read the note from a file ('-' for stdin)")
    ap.add_argument("--lint", action="store_true",
                    help="report on the note's format and exit without building")
    ap.add_argument("--session", default=DEFAULT_SESSION,
                    help="Isabelle session to build "
                         "(default: $BUILD_SESSION; required)")
    ap.add_argument("--dir", default=DEFAULT_SESSION_DIR,
                    help="directory holding the session ROOT, relative to the "
                         "project (default: $BUILD_SESSION_DIR, else '.')")
    ap.add_argument("rest", nargs="*", metavar="...",
                    help="extra arguments passed through to isabelle build")
    args = ap.parse_args()

    if not args.session and not args.lint:
        print("no session to build: pass --session, or set $BUILD_SESSION.\n"
              "(There is no default -- it would only ever be right for one "
              "project.)", file=sys.stderr)
        return 2

    project = project_dir()
    # The entry point owns the log location rather than inheriting it.  It was
    # a Makefile's to export, which meant `bin/build` run directly quietly
    # recorded into the recorder's built-in default -- a second corpus, a
    # second instance id, and a build that looks unrecorded because the
    # records are somewhere else.  Configuration belongs to the command, not
    # to one of its wrappers.
    log_dir = os.environ.get("WATCHDOG_LOG_DIR") or str(project / "t" / "logs")
    os.environ.setdefault("WATCHDOG_LOG_DIR", log_dir)

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

    env = dict(os.environ)
    env["WATCHDOG_LOG_DIR"] = log_dir
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
