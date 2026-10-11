"""NX undo marks across loads and display switches (kit.parts, the shared fake).

NX 2406 (measured live): a plain ``SetDisplay`` drops every undo mark when a live mark
is older than the first display of the shown or hidden part, and so do a displaying
``FileNew``, ``Part.Save`` and a plain ``Close``; ``OpenBase``, ``FileNew`` with
``MakeDisplayedPart`` false, ``SetActiveDisplayRetainUndoMarks`` and
``CloseRetainUndoMarks`` keep them; NX keeps about 181 marks for the session.
"""

from __future__ import annotations

import re
from pathlib import Path

import nx_mcp
from nx_mcp.kit import parts
from tests.kit import fake_nx
from tests.kit.kernel import services
from tests.kit.parts import FileNew, with_parts

SRC = Path(nx_mcp.__file__).parent
# Display switches and loads that drop NX's undo marks (kit.parts.show replaces them all).
DROPPING = re.compile(
    r"\.(SetDisplay|SetActiveDisplay|OpenBaseDisplay|OpenActiveDisplay|OpenDisplay|NewDisplay|"
    r"NewBaseDisplay)\("
)


def _sources():
    for path in sorted(SRC.rglob("*.py")):
        yield path.relative_to(SRC).as_posix(), path.read_text(encoding="utf-8")


def test_no_display_switch_or_load_drops_the_undo_marks():
    """One display switch (kit.parts.show, retaining the marks); every FileNew is made
    without display."""
    offenders = [name for name, text in _sources() if DROPPING.search(text)]
    assert offenders == []
    retaining = [name for name, text in _sources() if "SetActiveDisplayRetainUndoMarks(" in text]
    assert retaining == ["kit/parts.py"]
    displaying = [
        name
        for name, text in _sources()
        if ".FileNew()" in text and "MakeDisplayedPart = False" not in text
    ]
    assert displaying == []


def nx_with_files():
    nx = fake_nx()
    with_parts(nx, services(nx).root)
    return nx


def mark(nx):
    return nx.session.SetUndoMark(1, "probe")


def new(nx, name, displayed):
    builder = nx.session.Parts.FileNew()
    builder.NewFileName = str(nx.root / name)
    builder.Units = "mm"
    builder.MakeDisplayedPart = displayed
    return builder.Commit()


def test_fake_loads_without_display_keep_the_marks_and_the_shown_parts():
    nx = nx_with_files()
    work = nx.session.Parts.Work
    m = mark(nx)
    b = new(nx, "b.prt", displayed=False)
    assert nx.session.marks.keys() == {m}
    assert nx.session.Parts.Work is work and nx.session.Parts.Display is work
    b.Save()  # NX drops every mark on a save
    assert not nx.session.marks
    b.CloseRetainUndoMarks()
    m = mark(nx)
    opened, _ = nx.session.Parts.OpenBase(b.FullPath)
    assert opened in nx.session.Parts and m in nx.session.marks
    assert nx.session.Parts.Display is work and nx.session.Parts.Work is work
    assert FileNew(nx).MakeDisplayedPart is True  # NX's default displays the new part
    new(nx, "c.prt", displayed=True)  # and drops every mark
    assert not nx.session.marks


def test_fake_plain_display_drops_marks_older_than_the_parts_first_display():
    nx = nx_with_files()
    a = nx.session.Parts.Display  # displayed before every mark
    b = new(nx, "b.prt", displayed=False)
    m = mark(nx)
    nx.session.Parts.SetActiveDisplayRetainUndoMarks(b, 0, 0)
    assert m in nx.session.marks and nx.session.Parts.Work is b
    nx.session.Parts.SetDisplay(a, False, False)  # b was first shown after m
    assert not nx.session.marks
    m = mark(nx)
    nx.session.Parts.SetDisplay(b, False, False)  # both shown before m: kept
    assert m in nx.session.marks
    c = new(nx, "c.prt", displayed=False)
    nx.session.Parts.SetDisplay(c, False, False)  # c shown for the first time after m
    assert not nx.session.marks
    assert [kind for kind, _ in nx.session.shown] == ["retain", "plain", "plain", "plain"]


def test_fake_close_drops_the_marks_unless_retained_and_evicts_the_oldest():
    nx = nx_with_files()
    b, c = new(nx, "b.prt", displayed=False), new(nx, "c.prt", displayed=False)
    m = mark(nx)
    b.CloseRetainUndoMarks()
    assert m in nx.session.marks and b not in nx.session.Parts
    c.Close()
    assert not nx.session.marks
    nx.session.mark_cap = 3
    first, *rest = [mark(nx) for _ in range(5)]
    assert first not in nx.session.marks and sorted(nx.session.marks) == rest[-3:]


def test_activate_keeps_the_marks_where_a_plain_display_switch_drops_them():
    nx = nx_with_files()
    a = nx.session.Parts.Display
    b = new(nx, "b.prt", displayed=False)
    m = mark(nx)
    parts.activate(nx.tx, b)
    parts.activate(nx.tx, a)
    assert m in nx.session.marks
    assert nx.session.shown == [("retain", b), ("retain", a)]
    assert nx.session.Parts.Work is a and nx.session.Parts.Display is a
