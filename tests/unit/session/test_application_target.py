"""nx_activate_application with two parts loaded: NX runs one application per session,
entered for the displayed part, so a ``part`` naming a part that is not displayed is
refused before NX is touched (no display switch, no license query, no application
switch); naming the displayed part acts there as before."""

from __future__ import annotations

from pathlib import Path

from tests.kit import fake_nx, run
from tests.kit.kernel import services
from tests.kit.objects import Part


class App:
    """``Session.ApplicationName`` and ``ApplicationSwitchImmediate`` of the fake."""

    def __init__(self, nx, name="UG_APP_MODELING"):
        self.nx = nx
        nx.session.ApplicationName = name
        nx.session.ApplicationSwitchImmediate = self.switch
        self.calls: list[str] = []

    def switch(self, name):
        self.calls.append(name)
        self.nx.session.ApplicationName = name


def two():
    """``a.prt`` displayed and work, ``b.prt`` loaded in the background (both the agent's)."""
    nx = fake_nx()
    root = Path(services(nx).root)
    nx.part.FullPath = str(root / "a.prt")
    parts = nx.session.Parts
    work, display = parts.Work, parts.Display
    b = Part(nx.session, root / "b.prt")
    parts.Work, parts.Display = work, display
    return nx, nx.part, b


def licensed(nx, answer=True):
    asked: list[str] = []
    nx.session.LicenseManager = type(
        "LM", (), {"CheckPresence": staticmethod(lambda f: asked.append(f) or answer)}
    )()
    return asked


def test_a_part_that_is_not_displayed_is_refused_before_nx():
    nx, a, b = two()
    app = App(nx)
    asked = licensed(nx)
    bad = run(nx, "nx_activate_application", application="sheet_metal", part="b")
    assert bad.error["code"] == "NX_WRONG_PART_CONTEXT", bad
    assert bad.mutation_outcome == "not_started" and bad.error["field"] == "/part"
    assert "b.prt" in bad.error["message"] and "a.prt" in bad.error["message"]
    assert "nx_activate_part" in bad.error["hint"]
    # Nothing moved: no application switch, no license query, a.prt still shown and work.
    assert app.calls == [] and asked == []
    parts = nx.session.Parts
    assert parts.Display is a and parts.Work is a
    assert nx.session.ApplicationName == "UG_APP_MODELING"


def test_the_displayed_part_named_by_part_switches_as_before():
    nx, a, _b = two()
    app = App(nx)
    licensed(nx)
    ok = run(nx, "nx_activate_application", application="sheet_metal", part="a")
    assert ok.ok, ok
    assert ok.data == {"application": "sheet_metal", "previous": "modeling", "changed": True}
    assert app.calls == ["UG_APP_SBSM"] and nx.session.Parts.Display is a
    assert nx.session.ApplicationName == "UG_APP_SBSM"  # the switch stays after the call


def test_without_part_the_agents_displayed_part_is_the_target():
    """NX enters an application for the displayed part only: with two loaded parts and no
    part, the call acts on the displayed part when it is the agent's."""
    nx, a, _b = two()
    app = App(nx)
    licensed(nx)
    ok = run(nx, "nx_activate_application", application="sheet_metal")
    assert ok.ok and app.calls == ["UG_APP_SBSM"], ok
    assert nx.session.Parts.Display is a


def test_without_part_the_engineers_displayed_part_is_never_taken():
    nx, a, _b = two()
    app = App(nx)
    licensed(nx)
    svc = services(nx)
    owns = svc.owns
    svc.owns = lambda part: part is not a and owns(part)  # the engineer's displayed part
    bad = run(nx, "nx_activate_application", application="sheet_metal")
    assert not bad.ok and app.calls == [], bad
    assert bad.error["code"] in ("NX_WRONG_PART_CONTEXT", "NX_AMBIGUOUS_PART")
