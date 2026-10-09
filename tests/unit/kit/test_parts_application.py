"""kit.parts never lets NX open its modal "Sheet Metal" application warning.

The fake mirrors what NX 2406 did live: NX remembers per part the application last
entered with ApplicationSwitchImmediate while it was displayed (Modeling for a new
part). From no display part, SetDisplay enters the part's remembered application;
closing any part re-enters the display part's remembered application. Entering
Modeling with sheet-metal features opens the dialog. Changing the display part
while an application runs keeps that application.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.parts_files import _saving
from nx_mcp.kit import parts

MODELING, SHEET_METAL, NO_PART = "UG_APP_MODELING", "UG_APP_SBSM", "UG_APP_NOPART"


class Features(list):
    SheetmetalManager = None


class Part:
    def __init__(self, nx, tag, sheet_metal=False):
        self.nx, self.Tag = nx, tag
        self.Features = Features([NS(FeatureType="SB_TAB")] if sheet_metal else [])
        self.Bodies = []

    def Close(self, *_):
        self.nx.close(self)


class PartList(NS):
    def __iter__(self):
        return iter(list(self.loaded))


class FakeNX:
    def __init__(self):
        self.ApplicationName = NO_PART
        self.remembered: dict[int, str] = {}
        self.dialogs: list[str] = []
        self.switches: list[str] = []
        self.loaded: list[Part] = []
        self.Parts = PartList(Display=None, Work=None, SetDisplay=self.set_display,
                              SetWork=self.set_work, FileNew=self.file_new,
                              loaded=self.loaded)  # fmt: skip
        self.tags = iter(range(100, 200))

    def part(self, sheet_metal=False) -> Part:
        made = Part(self, next(self.tags), sheet_metal)
        self.loaded.append(made)
        return made

    def enter(self, application, part):
        if application == MODELING and parts.has_sheet_metal(part):
            self.dialogs.append("Sheet Metal")  # NX would block here
        self.ApplicationName = application

    def set_display(self, part, *_):
        if self.Parts.Display is None:
            self.enter(self.remembered.get(part.Tag, MODELING), part)
        self.Parts.Display = self.Parts.Work = part
        return None, NS(Dispose=lambda: None)

    def set_work(self, part):
        self.Parts.Work = part

    def ApplicationSwitchImmediate(self, name):
        if self.Parts.Display is None:
            raise RuntimeError("Cannot enter the specified application since a part is required.")
        self.switches.append(name)
        self.remembered[self.Parts.Display.Tag] = name
        self.ApplicationName = name

    def file_new(self):
        nx = self

        class Builder:
            def Commit(self):
                self.made = nx.part()
                nx.Parts.Display = nx.Parts.Work = self.made
                nx.ApplicationName = MODELING
                return self.made

            def Destroy(self):
                pass

        return Builder()

    def close(self, part):
        self.loaded.remove(part)
        display = self.Parts.Display
        if display is part:
            self.Parts.Display = self.Parts.Work = None
            self.ApplicationName = NO_PART
        elif display is not None:
            self.enter(self.remembered.get(display.Tag, MODELING), display)


@pytest.fixture
def nx():
    return FakeNX()


def tx_of(nx, tmp_path):
    enums = NS(
        Part=NS(Units=NS(Millimeters=1)),
        DisplayPartOption=NS(AllowAdditional=1),
        BasePart=NS(CloseWholeTree=NS(FalseValue=False), CloseModified=NS(CloseModified=1)),
    )
    services = NS(part_key=lambda p: p.Tag, forget_parts=lambda keys: None)
    return NS(session=nx, nx=enums, workspace=NS(root=tmp_path), services=services,
              ref=lambda p, kind: {"id": f"p:{p.Tag}"})  # fmt: skip


def test_the_fake_shows_the_nx_trigger(nx):
    """Displaying a sheet-metal part from no display part opens the dialog in NX."""
    bent = nx.part(sheet_metal=True)  # made while NX ran Sheet Metal: remembers Modeling
    nx.set_display(bent)
    assert nx.dialogs == ["Sheet Metal"]


def test_a_sheet_metal_part_is_displayed_from_no_part_without_the_dialog(nx, tmp_path):
    bent = nx.part(sheet_metal=True)
    parts.activate(tx_of(nx, tmp_path), bent)
    assert nx.dialogs == []
    assert nx.Parts.Display is bent and nx.Parts.Work is bent
    assert nx.ApplicationName == SHEET_METAL and nx.remembered[bent.Tag] == SHEET_METAL
    assert nx.loaded == [bent]  # the scratch part is closed again
    # Recorded: from no display part again, NX now enters Sheet Metal by itself.
    nx.close(bent)
    nx.loaded.append(bent)
    nx.set_display(bent)
    assert nx.dialogs == [] and nx.ApplicationName == SHEET_METAL


def test_a_plain_part_is_displayed_directly(nx, tmp_path):
    plain = nx.part()
    parts.activate(tx_of(nx, tmp_path), plain)
    assert nx.Parts.Display is plain and nx.ApplicationName == MODELING
    assert nx.loaded == [plain] and nx.switches == []  # no scratch part, no switch


def test_display_changes_in_sheet_metal_record_it(nx, tmp_path):
    plain, bent = nx.part(), nx.part(sheet_metal=True)
    nx.set_display(plain)
    nx.ApplicationSwitchImmediate(SHEET_METAL)
    parts.activate(tx_of(nx, tmp_path), bent)
    assert nx.dialogs == [] and nx.remembered[bent.Tag] == SHEET_METAL


def test_closing_another_part_keeps_the_displayed_sheet_metal_part_in_sheet_metal(nx, tmp_path):
    plain, bent = nx.part(), nx.part(sheet_metal=True)
    nx.set_display(plain)
    nx.ApplicationSwitchImmediate(SHEET_METAL)
    nx.set_display(bent)  # shown in Sheet Metal, still remembers Modeling
    _saving.close_part(tx_of(nx, tmp_path), plain, save=False)
    assert nx.dialogs == [] and nx.ApplicationName == SHEET_METAL
    assert nx.loaded == [bent]


def test_without_the_record_a_close_would_open_the_dialog(nx):
    plain, bent = nx.part(), nx.part(sheet_metal=True)
    nx.set_display(plain)
    nx.ApplicationSwitchImmediate(SHEET_METAL)
    nx.set_display(bent)
    nx.close(plain)
    assert nx.dialogs == ["Sheet Metal"]


def test_keep_sheet_metal_only_acts_in_sheet_metal_on_sheet_metal_parts(nx, tmp_path):
    tx = tx_of(nx, tmp_path)
    plain, bent = nx.part(), nx.part(sheet_metal=True)
    nx.set_display(bent)  # Modeling (the engineer answered the dialog)
    nx.dialogs.clear()
    parts.keep_sheet_metal(tx)
    nx.Parts.Display = plain
    nx.ApplicationName = SHEET_METAL
    parts.keep_sheet_metal(tx)
    assert nx.switches == []
    nx.Parts.Display = bent
    parts.keep_sheet_metal(tx)
    assert nx.switches == [SHEET_METAL]
