"""The engineer tab guard (kit.parts.present): while the displayed part
is the engineer's (loaded by the engineer, not created, opened or granted to the agent),
nx_create_part, nx_open_part and nx_activate_part never leave an agent part active and
never change the work part, whatever ``display`` and ``work`` say; the reply says where
the part is (``NX_NOTE``) and calls reach it by ``part``. Without an engineer part
shown, the tab policy is as before.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from nx_mcp.core import catalogue
from nx_mcp.domains.parts_files import parts as tools
from tests.kit.kernel import services
from tests.unit.parts_files.support import call
from tests.unit.parts_files.test_close_multipart import BLOCK


@pytest.fixture
def engineer(nx):
    """The fixture's part is the engineer's: displayed, work part, never the agent's."""
    services(nx).foreign.add(services(nx).part_key(nx.part))
    return nx.part


@pytest.fixture(autouse=True)
def _tools(monkeypatch):
    catalogue.load()
    monkeypatch.setitem(catalogue._REGISTRY, BLOCK.name, BLOCK)


def notes(result) -> list[str]:
    return [w["message"] for w in result.warnings if w["code"] == "NX_NOTE"]


def kept(nx, engineer) -> bool:
    parts = nx.session.Parts
    return parts.Display is engineer and parts.Work is engineer


def test_create_never_takes_the_engineers_tab_even_with_display_true(nx, engineer):
    first = call(nx, tools.nx_create_part, path="g/a.prt")  # the agent's only part
    assert first.ok and not first.data["display"] and not first.data["work"], first
    assert kept(nx, engineer) and nx.session.displayed() == [engineer]  # no tab
    (note,) = notes(first)
    assert note.startswith("The engineer's work.prt stays the active NX tab; a.prt is loaded "
                           'in the background. Calls reach it with "part": "a.prt"')  # fmt: skip
    asked = call(nx, tools.nx_create_part, path="g/b.prt", display=True)
    assert asked.ok and not asked.data["display"] and not asked.data["work"], asked
    assert kept(nx, engineer) and nx.session.displayed() == [engineer, asked_part(nx, "b")]
    assert any("in its own NX tab behind it" in n for n in notes(asked)), asked


def asked_part(nx, name):
    return next(p for p in nx.session.Parts if p.Name == name)


def test_a_ref_less_call_reaches_the_agents_only_part_not_the_engineers(nx, engineer):
    assert call(nx, tools.nx_create_part, path="g/a.prt").ok
    made = call(nx, NS(spec=BLOCK))  # no part key, no refs: the agent's only part
    assert made.ok and made.part["file"] == "a.prt", made
    assert len(asked_part(nx, "a").Features) == 1 and not engineer.Features
    assert kept(nx, engineer)  # the kernel's switch came back


def test_open_without_display_works_beside_the_engineer(nx, root, engineer):
    """nx_open_part(display=false, work=true) is not refused NX_WRONG_PART_CONTEXT: a
    read-only inspector need not take the engineer's tab."""
    part = nx.new_part("g/c.prt")
    part.Save()
    part.Close()
    nx.session.Parts.Display = nx.session.Parts.Work = engineer
    for display in (False, None, True):
        opened = call(nx, tools.nx_open_part, path="g/c.prt", work=True, display=display)
        assert opened.ok and not opened.data["display"] and not opened.data["work"], opened
        assert kept(nx, engineer) and any('"part": "c.prt"' in n for n in notes(opened))
    assert nx.session.displayed() == [engineer, asked_part(nx, "c")]  # display true: a tab


def test_activate_gives_a_tab_behind_the_engineers_and_says_so(nx, engineer):
    """nx_activate_part with its defaults does not reply work false, display false and
    nothing else (a wasted call)."""
    assert call(nx, tools.nx_create_part, path="g/a.prt").ok
    shown = call(nx, tools.nx_activate_part, part="a")
    assert shown.ok and not shown.data["display"] and not shown.data["work"], shown
    assert kept(nx, engineer) and nx.session.displayed() == [engineer, asked_part(nx, "a")]
    (note,) = notes(shown)
    assert "in its own NX tab behind it" in note and '"part": "a.prt"' in note
    for args in ({"display": True}, {"display": False}, {"work": False, "display": True}):
        again = call(nx, tools.nx_activate_part, part="a", **args)
        assert again.ok and not again.data["display"] and kept(nx, engineer), again


def test_an_engineers_part_the_agent_opens_again_stays_as_it_is(nx):
    eng = nx.new_part("e/eng.prt")
    eng.Save()
    services(nx).foreign.add(services(nx).part_key(eng))
    nx.session.Parts.Display = nx.session.Parts.Work = eng
    again = call(nx, tools.nx_open_part, path="e/eng.prt")
    assert again.ok and again.data["already_loaded"] and again.data["display"], again
    assert kept(nx, eng) and notes(again) == [
        "eng.prt is already loaded as the engineer's part: it stays as it is (no tab or "
        'work part change). Read it with "part": "eng.prt"; to change it, save a copy of '
        "your own"
    ]


def test_without_an_engineer_part_shown_the_tab_policy_is_as_before(nx):
    nx.session.Parts.remove(nx.part)  # solo, NX shows nothing
    nx.session.Parts.Display = nx.session.Parts.Work = None
    first = call(nx, tools.nx_create_part, path="g/a.prt")
    assert first.ok and first.data["display"] and first.data["work"] and not notes(first)
    second = call(nx, tools.nx_create_part, path="g/b.prt")  # two agent parts: not shown
    assert second.ok and not second.data["display"], second
    other = asked_part(nx, "b")
    shown = call(nx, tools.nx_activate_part, part="b", display=True)  # an agent's own tab
    assert shown.ok and shown.data["display"] and nx.session.Parts.Display is other
