"""No NX prompt during an agent call: update failures undo, listing output is returned."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from nx_mcp import interactive
from nx_mcp.core.errors import NXError
from nx_mcp.host import dispatch, quiet
from nx_mcp.host.services import NXServices
from nx_mcp.workspace import Workspace
from tests.host.test_services import FakeSession

pytestmark = pytest.mark.fake_nx

FAILURE = NS(NoOption="NoOption", Undo="Undo", Accept="Accept", AcceptAll="AcceptAll")
DEVICE = NS(Window="Window", File="File", FileAndWindow="FileAndWindow", NotSet="NotSet")


class UpdateManager:
    def __init__(self):
        self.action = FAILURE.NoOption  # the engineer's setting: NX asks
        self.seen = []

    def GetDefaultUpdateFailureAction(self):
        return self.action

    def SetDefaultUpdateFailureAction(self, action):
        self.action = action


class ListingWindow:
    """NX's listing window: writes go to the Information window or (buffered) a file."""

    def __init__(self):
        self.Device, self.path, self.IsOpen = DEVICE.Window, "", False
        self.window, self.buffer = [], []  # lines shown in NX / not yet in the file

    def SelectDevice(self, device, name):
        if self.path and self.buffer:
            Path(self.path).write_text("﻿" + "".join(self.buffer), encoding="utf-8")
        self.Device, self.path, self.buffer, self.IsOpen = device, name, [], False

    def Open(self):
        self.IsOpen = True

    def Close(self):
        self.IsOpen = False

    def WriteLine(self, text):
        if self.Device in (DEVICE.Window, DEVICE.FileAndWindow):
            self.window.append(text)  # the "Information" window pops up in NX
        if self.Device in (DEVICE.File, DEVICE.FileAndWindow):
            self.buffer.append(text + "\n")


def services(tmp_path):
    session = FakeSession()
    session.UpdateManager, session.ListingWindow = UpdateManager(), ListingWindow()
    nx = NS(Session=NS(MarkVisibility=NS(Visible=1, Invisible=0)), UpdateFailureOption=FAILURE,
            ListingWindowDeviceType=DEVICE)  # fmt: skip
    return NXServices(session, nx, "test", Workspace(tmp_path / "ws"))


@pytest.mark.parametrize("fails", [False, True])
def test_update_failures_undo_during_a_call_and_the_engineers_setting_returns(tmp_path, fails):
    s = services(tmp_path)
    manager = s.session.UpdateManager
    manager.action = FAILURE.AcceptAll  # whatever the engineer chose

    def tool():
        manager.seen.append(manager.action)
        if fails:
            raise RuntimeError("update failed")
        return {}

    s.dev_tools["nx_dev_python"] = tool
    if fails:
        with pytest.raises(NXError) as error:
            dispatch.execute(s, "nx_dev_python", {})
        assert error.value.details["mutation_outcome"] == "rolled_back"
    else:
        dispatch.execute(s, "nx_dev_python", {})
    assert manager.seen == [FAILURE.Undo]  # never asks, never accepts a failure
    assert manager.action == FAILURE.AcceptAll


def test_nx_dev_python_output_never_opens_the_listing_window(tmp_path):
    s = services(tmp_path)
    lw = s.session.ListingWindow
    host = NS(nx=s.nx, session=s.session, services=s)
    host.session.Parts.Work = None
    s.dev_tools["nx_dev_python"] = lambda code: interactive.InteractiveHost._dev_python(host, code)
    code = (
        "import sys\n"
        "print('from print')\n"
        "sys.stderr.write('from stderr' + chr(10))\n"
        "session.ListingWindow.Open()\n"
        "session.ListingWindow.WriteLine('from the listing window')\n"
    )
    result = dispatch.execute(s, "nx_dev_python", {"code": code})
    assert result["stdout"] == "from print\nfrom stderr\nfrom the listing window\n"
    assert lw.window == [] and lw.Device == DEVICE.Window and not lw.IsOpen
    assert not list((Path(s.workspace.root) / ".nx-mcp").glob("listing-*"))


def test_listing_output_of_a_failed_call_is_in_the_error(tmp_path):
    s = services(tmp_path)

    def tool():
        s.session.ListingWindow.WriteLine("partial output")
        raise RuntimeError("boom")

    s.dev_tools["nx_dev_python"] = tool
    with pytest.raises(NXError) as error:
        dispatch.execute(s, "nx_dev_python", {})
    assert error.value.details["listing"] == "partial output\n"
    assert s.session.ListingWindow.window == []


def test_a_nested_guard_changes_nothing(tmp_path):
    s = services(tmp_path)
    manager, lw = s.session.UpdateManager, s.session.ListingWindow
    with s.quiet() as outer:
        with s.quiet() as inner:
            lw.WriteLine("inner")
        assert manager.action == FAILURE.Undo and lw.Device == DEVICE.File
        assert inner.listing == ""
    assert outer.listing == "inner\n" and manager.action == FAILURE.NoOption
    assert quiet._active == 0


def test_services_without_the_apis_run_unguarded(tmp_path):
    s = NXServices(FakeSession(), NS(Session=NS(MarkVisibility=NS(Visible=1, Invisible=0))),
                   "test", Workspace(tmp_path / "ws"))  # fmt: skip
    s.dev_tools["nx_dev_python"] = lambda: {"done": True}
    assert dispatch.execute(s, "nx_dev_python", {})["done"]
