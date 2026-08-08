"""Audits of the corpus tooling's own measurement decisions.

Not tests of the code -- tests of the *statistics*.  Each module interrogates
one judgement call the readers make, on real corpus data, and reports whether
it holds.  They exist because the numbers these tools produce get published,
and a filter that quietly changes what it counts is invisible in the output.
Each one re-derives its quantity a second way rather than re-running the same
code path, so agreement is evidence rather than tautology.

    trajectory audit              # what is here, and what each one asks
    trajectory audit oneshot -i CORPUS

**The catalogue is derived, not declared.**  This docstring used to carry the
list, and it drifted: it named a `loci` module that has never existed here --
those checks are `tests/test_attribution.py`, which exercises the `error_loci`
path end to end against synthetic Isabelle text.  A hand-maintained inventory
beside the thing it inventories reads as authoritative and is the last thing
anyone updates, which is the same fault this package documents everywhere
else in a different guise.  `catalogue()` asks the directory instead.
"""
from __future__ import annotations

import argparse
import ast
from pathlib import Path


def parser(module: str, doc: str) -> argparse.ArgumentParser:
    """The parser every audit starts from: `-i CORPUS` and `--attribution`.

    `prog` is set rather than left to argparse, which derives it from how the
    interpreter was launched and not from `sys.argv[0]`.  An audit reached
    through `trajectory audit oneshot` would otherwise head its own `--help`
    with the *dispatcher's* usage line -- a command that does not accept the
    flags printed underneath it.
    """
    ap = argparse.ArgumentParser(
        prog=f"trajectory audit {module.rpartition('.')[2]}",
        description=doc.strip().splitlines()[0])
    ap.add_argument("-i", "--input", metavar="CORPUS", default=None,
                    help="builds.jsonl to read (default: $TRAJECTORY_CORPUS, "
                         "else the corpus for the current project)")
    ap.add_argument("--attribution", metavar="FILE", default=None,
                    help="JSON of attribution facts this corpus cannot show "
                         "(default: $TRAJECTORY_ATTRIBUTION)")
    return ap


def catalogue() -> list[tuple[str, str]]:
    """`(name, one-line gloss)` for every audit, read from the directory.

    The gloss is each module's docstring first line with its own `name --`
    prefix removed, so the listing here and `--help` there cannot disagree.

    Parsed rather than imported: a module that fails at import time should
    not stop `trajectory audit` from listing the other five and naming the
    one that broke.  Reading a docstring is also not a reason to execute a
    file.
    """
    out = []
    for path in sorted(Path(__file__).parent.glob("*.py")):
        if path.stem.startswith("_"):
            continue
        try:
            doc = ast.get_docstring(ast.parse(path.read_text())) or ""
        except (OSError, SyntaxError, ValueError):
            doc = ""
        head = doc.strip().splitlines()[0] if doc.strip() else "(unreadable)"
        for dash in (" — ", " -- "):
            _, sep, gloss = head.partition(dash)
            if sep:
                head = gloss
                break
        out.append((path.stem, head.strip()))
    return out
