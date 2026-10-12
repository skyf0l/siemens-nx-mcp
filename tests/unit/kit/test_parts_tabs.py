"""NX window tabs (kit.parts, the shared fake): every part NX MCP displays gets its own
tab and no call replaces the part of another tab (tabs probe, NX 2406).

``show`` is ``SetActiveDisplayRetainUndoMarks(AllowAdditional)``: a new tab, or the
part's tab activated, undo marks kept. ``present`` is the policy of nx_create_part,
nx_open_part and nx_activate_part: while the engineer's part is shown no agent part is
left active (the engineer tab guard); else, with two or more agent parts loaded, the tab
active before comes back unless the call asks for the part's tab (``display`` true).
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from nx_mcp.kit import parts, switching
from tests.kit import fake_nx
from tests.kit.kernel import services
from tests.kit.parts import with_parts


def setup():
    nx = fake_nx()
    with_parts(nx, services(nx).root)
    engineer = nx.session.Parts.Display  # the fixture's part: the engineer's tab
    services(nx).foreign.add(services(nx).part_key(engineer))
    tx = NS(session=nx.session, nx=nx.nx, uf=nx.uf, workspace=services(nx).workspace,
            services=services(nx))  # fmt: skip
    return nx, tx, engineer


def new(nx, name):
    builder = nx.session.Parts.FileNew()
    builder.NewFileName = str(nx.root / name)
    builder.Units = "mm"
    builder.MakeDisplayedPart = False
    return builder.Commit()


def test_show_opens_a_tab_per_part_and_reactivates_an_existing_one():
    nx, tx, engineer = setup()
    a, b = new(nx, "a.prt"), new(nx, "b.prt")
    mark = nx.session.SetUndoMark(1, "probe")
    parts.show(nx.nx, nx.session.Parts, a)
    parts.show(nx.nx, nx.session.Parts, b)
    assert parts.tabs(nx.session.Parts) == [b, a, engineer]
    parts.show(nx.nx, nx.session.Parts, engineer)  # its tab again, no second one
    assert parts.tabs(nx.session.Parts) == [engineer, b, a]
    assert nx.session.Parts.Display is engineer and nx.session.Parts.Work is engineer
    assert mark in nx.session.marks and {k for k, _ in nx.session.shown} == {"retain"}


def test_closing_a_tab_part_removes_its_tab_and_the_active_one_leaves_no_display():
    nx, tx, engineer = setup()
    a, b = new(nx, "a.prt"), new(nx, "b.prt")
    for part in (a, b):
        parts.show(nx.nx, nx.session.Parts, part)
    a.CloseRetainUndoMarks()
    assert parts.tabs(nx.session.Parts) == [b, engineer]
    b.CloseRetainUndoMarks()  # the active tab: NX has no display part until one is shown
    assert nx.session.Parts.Display is None and parts.tabs(nx.session.Parts) == [engineer]


def test_the_agents_only_part_keeps_its_tab_active_when_no_engineer_part_is_shown():
    nx, tx, engineer = setup()
    nx.session.Parts.Display = nx.session.Parts.Work = None  # NX shows no part
    a = new(nx, "a.prt")
    assert not parts.crowded(tx, a)  # the engineer's part never counts
    shown = parts.present(tx, a)
    assert shown.active and shown.work and shown.kept is None
    assert nx.session.Parts.Display is a and parts.tabs(nx.session.Parts)[0] is a


def test_the_engineer_tab_guard_never_leaves_an_agent_part_active():
    """bench3t: 12 of 12 creates passed display true and took the engineer's tab for
    5 min 19 s; the first agent part took it even without display."""
    nx, tx, engineer = setup()
    assert parts.engineer_tab(tx) is engineer
    a, b = new(nx, "a.prt"), new(nx, "b.prt")
    calls = len(nx.session.shown)
    for display in (None, False):  # the agent's only part, or not: background, no tab
        shown = parts.present(tx, a, display=display)
        assert not shown.active and not shown.work and shown.engineer and shown.kept is engineer
    assert len(nx.session.shown) == calls and parts.tabs(nx.session.Parts) == [engineer]
    shown = parts.present(tx, b, display=True)  # its own tab, behind the engineer's
    assert not shown.active and shown.engineer
    assert nx.session.Parts.Display is engineer and nx.session.Parts.Work is engineer
    assert parts.tabs(nx.session.Parts) == [engineer, b] and parts.has_tab(tx, b)
    note = parts.shown_note(tx, b, shown)
    assert note.startswith("The engineer's work.prt stays the active NX tab; b.prt is loaded "
                           "in its own NX tab behind it") and '"part": "b.prt"' in note  # fmt: skip
    assert "in the background" in parts.shown_note(tx, a, parts.present(tx, a))


def test_with_two_agent_parts_the_tab_active_before_comes_back():
    nx, tx, engineer = setup()
    mine = new(nx, "mine.prt")
    parts.show(nx.nx, nx.session.Parts, mine)  # an agent part is shown: no guard
    assert parts.engineer_tab(tx) is None
    a, b = new(nx, "a.prt"), new(nx, "b.prt")
    assert parts.crowded(tx, b)
    shown = parts.present(tx, b)
    assert not shown.active and shown.kept is mine and not shown.engineer
    assert nx.session.Parts.Display is mine and nx.session.Parts.Work is mine
    assert parts.tabs(nx.session.Parts) == [mine, b, engineer]  # b has its own tab
    assert parts.present(tx, a, display=True).active  # the call asks for it
    assert nx.session.Parts.Display is a and parts.tabs(nx.session.Parts)[:3] == [a, mine, b]
    calls = len(nx.session.shown)
    assert not parts.present(tx, b, display=False).active and len(nx.session.shown) == calls


def test_the_guard_leaves_the_engineers_work_component_alone():
    nx, tx, engineer = setup()
    component = new(nx, "c.prt")
    nx.attach(engineer, component)
    nx.session.Parts.SetWork(component)  # the engineer edits a component in context
    a, b = new(nx, "a.prt"), new(nx, "b.prt")
    assert not parts.present(tx, b, display=True).active
    assert nx.session.Parts.Display is engineer and nx.session.Parts.Work is component
    assert not parts.present(tx, a, display=False).work  # work too stays the engineer's
    assert nx.session.Parts.Work is component and a in nx.session.Parts


def test_a_display_bound_switch_works_in_the_targets_tab_and_gives_the_engineers_back():
    nx, tx, engineer = setup()
    a = new(nx, "a.prt")
    sw = switching.Switch(tx, a, "display").enter()
    assert nx.session.Parts.Display is a and parts.tabs(nx.session.Parts) == [a, engineer]
    assert sw.restore() is None
    assert nx.session.Parts.Display is engineer and nx.session.Parts.Work is engineer
    assert parts.tabs(nx.session.Parts) == [engineer, a]  # a keeps its tab
    again = switching.Switch(tx, a, "display").enter()  # the existing tab is re-activated
    assert parts.tabs(nx.session.Parts) == [a, engineer] and again.restore() is None
