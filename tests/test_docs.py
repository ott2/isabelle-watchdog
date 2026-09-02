"""The design record is cited by section number, so the numbers have to hold.

`docs/logging-design.md` predates most of the package and is deliberately not
maintained against it -- its own preamble says so.  What *is* maintained is the
numbering: a dozen comments across `record.py`, `attempts.py`, `corpus.py`,
`watchdog.py` and the audits send a reader to a section for reasoning the code
cannot state itself, and a citation that resolves to nothing sends them
looking for a passage that is not there.

That is the half of the drift a test can see.  Whether §13.1 still *says* what
its citer thinks it says is not checkable, and pretending otherwise would be
the inventory-beside-the-thing-it-inventories failure this repository has
already replaced twice.  So this checks the arrow, not the target: every
section named by the code exists.

Nothing here checks the reverse -- that every section is cited.  Half of the
document is a superseded plan, and a test demanding it be cited would be an
argument for deleting the history, which is the one thing the document is for.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DESIGN = "docs/logging-design.md"

# `## 12. Trajectory axis`, `### 12.4 Episode shape` -- the trailing dot is
# present on top-level headings and absent on subsections, so it is optional.
HEADING = re.compile(r"(?m)^\#{2,4}\s+(\d+(?:\.\d+)*)\.?\s")

# Two spellings reach the same place.  The explicit one names the file and may
# carry `§`, `§§` or nothing at all (`audits/lengths.py` writes
# "logging-design.md 13.2"); the bare one is just `§13.2.1`, which every
# numeric `§` in this package means.
CITATION = re.compile(r"logging-design\.md\s*§{0,2}\s*(\d+(?:\.\d+)*)"
                      r"|§(\d+(?:\.\d+)*)")


def _design_text() -> str:
    p = ROOT / DESIGN
    if not p.exists():
        pytest.skip(f"no {DESIGN} beside the tests")   # installed from a wheel
    return p.read_text()


def _design_sections() -> set[str]:
    return set(HEADING.findall(_design_text()))


def _citations() -> dict[str, list[str]]:
    """Section -> the files citing it, over the package only.

    Not over `tests/`, and not for tidiness: this file discusses §12.3.2 by
    name -- the dangling citation that prompted it -- so a scan including the
    tests reports its own prose as the defect.  Citations that matter are in
    the package anyway; a test that cites a section is talking about the code
    that cites it.
    """
    found: dict[str, list[str]] = {}
    for py in sorted((ROOT / "src").rglob("*.py")):
        for explicit, bare in CITATION.findall(py.read_text()):
            found.setdefault(explicit or bare, []).append(
                str(py.relative_to(ROOT)))
    return found


def test_every_cited_section_of_the_design_record_exists():
    """A citation is a promise that a passage is there to read.

    `watchdog.py` cited §12.3.2 for the error head, and §12.3 has no numbered
    sub-subsections -- the `.2` was the numbered *list item* within it.  The
    passage was real; the coordinates were not, which is the worse failure of
    the two, because a reader who cannot find §12.3.2 concludes the reasoning
    was never written down.
    """
    sections = _design_sections()
    assert sections, "no numbered sections found -- has the heading style changed?"

    dangling = {sec: sorted(set(files))
                for sec, files in _citations().items() if sec not in sections}
    assert not dangling, "\n".join(
        f"  §{sec} is cited by {', '.join(files)} but {DESIGN} has no such "
        f"heading" for sec, files in sorted(dangling.items()))


def test_the_design_record_says_it_is_not_documentation():
    """The preamble is the whole repair, so it is the thing to guard.

    Half the document describes a cost axis that was never built, and §5's
    schema shares three field names with a real record and omits `diff`
    entirely -- so a reader who takes it for documentation writes a reader
    that parses nothing.  Scoping the claim is what makes the rest safe to
    keep, exactly as a null role is what makes a `-b` build's record safe to
    read.
    """
    text = _design_text()
    head = text[:text.index("\n## ")]
    assert "not maintained against the code" in head
    assert "§5" in head and "record.py" in head, (
        "the preamble no longer points past the superseded schema")
