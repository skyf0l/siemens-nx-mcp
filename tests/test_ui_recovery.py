"""UI handoff and scheduler failures; Win32 calls are explicit seams."""

import ctypes
import os
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from nx_mcp import interactive
from nx_mcp.bridge import BridgeDescriptor
from nx_mcp.interactive import ControlPanel, InteractiveHost
from nx_mcp.runtime import NXToolError
from tests.fakes import Body, Collection

pytestmark = pytest.mark.fake_nx


@pytest.fixture
def host(rig, tmp_path, monkeypatch):
    lock = {"value": 0}
    enabled = {"value": True}
    ui = NS(
        AskLockStatus=lambda: lock["value"],
        CanOpenPart=lambda: not lock["value"],
        LockAccess=lambda: lock.update(value=1),
        UnlockAccess=lambda: lock.update(value=0),
    )
    rig.nx.UI = NS(Status=NS(Lock=1))
    h = InteractiveHost.__new__(InteractiveHost)
    h.__dict__.update(
        thread=threading.get_ident(),
        mode="manual",
        owns_lock=False,
        window_disabled=False,
        ui=ui,
        nx=rig.nx,
        session=rig.session,
        executor=rig.e,
        main_hwnd=1,
        native_thread=123,
        ticks=0,
        completed=0,
        running_method=None,
        operation_started=None,
        last_duration_seconds=None,
        last_method=None,
        last_error=None,
        started=time.time(),
        auto_start=False,
        busy=False,
        stopped=False,
        stop_requested=False,
        requested_mode=None,
        stop_file=tmp_path / "stop",
        state_dir=tmp_path,
        panel=NS(update=Mock(), close=Mock()),
        dispatcher=NS(
            drain=Mock(),
            stop=Mock(),
            drain_where=Mock(return_value=0),
            waiting=Mock(return_value=[]),
            reject_where=Mock(return_value=0),
        ),
        server=NS(stop=Mock()),
        timer=7,
    )
    h.user = NS(
        EnableWindow=lambda _, v: enabled.update(value=v),
        IsWindowEnabled=lambda _: enabled["value"],
        KillTimer=Mock(),
    )
    monkeypatch.setattr(
        ctypes, "windll", NS(kernel32=NS(GetCurrentThreadId=lambda: 123)), raising=False
    )
    h.descriptor = BridgeDescriptor.create(12345, "test")
    h.descriptor_path = tmp_path / "bridge.json"
    h.descriptor.write(h.descriptor_path)
    return h


def test_manual_handoff_invalidates_references_and_checkpoints(host, rig):
    body = Body()
    rig.part.Bodies.append(body)
    ref = rig.ref(body)
    rig.e._checkpoint()
    assert host.control("agent")["actual_ui_lock"]
    host.control("manual")
    assert not host.owns_lock and host.status()["nx_window_input_enabled"]
    assert not rig.e._checkpoints
    with pytest.raises(NXToolError):
        rig.e.objects.resolve(ref)
    with pytest.raises(NXToolError):
        host.control("bad")
    host.ui.CanOpenPart = lambda: False
    with pytest.raises(NXToolError, match="dialog"):
        host.control("agent")
    host.thread = -1
    with pytest.raises(RuntimeError):
        host.control("manual")
    with pytest.raises(RuntimeError):
        host.execute("nx_status", {})


def test_operation_failure_relocks_ui_and_refresh_failure_is_warning(host, rig):
    with pytest.raises(NXToolError, match="Give control to agent"):
        host.execute("nx_list_bodies", {})
    host.execute("nx_ui_control", {"mode": "agent"})
    assert host.execute("nx_status", {})["ui"]["mode"] == "agent"
    rig.e._handlers["nx_test_fail"] = Mock(side_effect=RuntimeError("native error"))
    with pytest.raises(NXToolError):
        host.execute("nx_test_fail", {})
    assert host.ui.AskLockStatus() == 1
    rig.part.ModelingViews.WorkView.UpdateDisplay.side_effect = RuntimeError("refresh")
    result = host.execute("nx_list_bodies", {})
    assert not any("View refresh" in w for w in result["warnings"])
    rig.e._handlers["nx_test_edit"] = lambda: {}
    edited = host.execute("nx_test_edit", {})
    assert any("View refresh" in w for w in edited["warnings"])
    rig.part.DrawingSheets = Collection()
    rig.part.DrawingSheets.CurrentDrawingSheet = object()
    drawing = host.execute("nx_test_edit", {})
    assert not any("View refresh" in w for w in drawing["warnings"])
    host.ui.LockAccess = Mock(side_effect=RuntimeError("lock failure"))
    host.execute("nx_list_bodies", {})
    assert host.mode == "manual" and "Cannot restore" in host.last_error


def test_tick_modes_status_persistence_and_exception_handoff(host):
    host.auto_start = True
    host.tick()
    assert host.mode == "agent" and not host.auto_start
    host.requested_mode = "manual"
    host.ticks = 9
    host.tick()
    assert host.mode == "manual" and (host.state_dir / "ui-state.json").is_file()
    host.requested_mode = "bad"
    host.tick()
    assert "mode must" in host.last_error
    host.auto_start = True
    host.ui.CanOpenPart = lambda: False
    host.tick()
    assert host.auto_start
    host.dispatcher.drain.side_effect = RuntimeError("dispatcher")
    host.dispatcher.drain_where.side_effect = RuntimeError("dispatcher")
    host.tick()
    assert not host.busy and host.last_error == "dispatcher"
    host.busy = True
    n = host.ticks
    host.tick()
    assert host.ticks == n
    host.busy = False
    host.stop_requested = True
    host.tick()
    assert host.stopped and not host.descriptor_path.exists()
    host.user.KillTimer.assert_called_once()
    host.server.stop.assert_called_once()


def test_stop_does_not_delete_another_session_descriptor(host):
    replacement = BridgeDescriptor.create(12346, "other")
    replacement.write(host.descriptor_path)
    host.stop()
    assert host.descriptor_path.exists()
    host.descriptor_path.write_text("malformed")
    host.stop()


def test_panel_commands_and_cleanup(host):
    panel = ControlPanel.__new__(ControlPanel)
    panel.host = host
    panel.user = NS(
        DefWindowProcW=Mock(return_value=9),
        SetWindowTextW=Mock(),
        UpdateWindow=Mock(),
        DestroyWindow=Mock(),
        UnregisterClassW=Mock(),
    )
    panel.label = 2
    panel.hwnd = 3
    panel._class = NS(lpszClassName="class")
    host.decline_requested = False
    panel._message(1, 0x111, 104, 0)
    assert host.decline_requested
    for current, requested in [("agent", "manual"), ("manual", "agent")]:
        host.mode, host.requested_mode = current, None
        assert panel._message(1, 0x111, 101, 0) == 0 and host.requested_mode == requested
        host.mode = requested
        panel._message(1, 0x111, 101, 0)  # A second click before the tick must not flip back.
        assert host.requested_mode == requested
    host.requested_mode = None
    panel._message(1, 0x111, 103, 0)
    assert host.stop_requested
    host.stop_requested = False
    panel._message(1, 0x10, 0, 0)
    assert host.stop_requested
    assert panel._message(1, 999, 0, 0) == 9
    assert panel._message(1, 0x111, None, 0) == 9 and host.last_error
    panel.update("hello")
    panel.close()
    panel.user.DestroyWindow.assert_called_once_with(3)


def test_main_window_selection_requires_own_visible_window(monkeypatch):
    user = NS(
        EnumWindows=Mock(),
        GetWindowThreadProcessId=Mock(),
        GetWindowRect=Mock(),
        IsWindowVisible=Mock(return_value=True),
    )
    user.EnumWindows.side_effect = lambda fn, _: [fn(h, 0) for h in (1, 2, 3)]
    user.GetWindowThreadProcessId.side_effect = lambda h, p: setattr(
        p._obj, "value", os.getpid() if h != 3 else os.getpid() + 1
    )

    def rect(h, p):
        p._obj.right = h * 100
        p._obj.bottom = 100

    user.GetWindowRect.side_effect = rect
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: user, raising=False)
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", lambda *a: lambda fn: fn, raising=False)
    assert interactive.nx_main_window() == 2
    user.IsWindowVisible.return_value = False
    with pytest.raises(RuntimeError, match="visible"):
        interactive.nx_main_window()


def test_start_reuses_live_host_and_cleans_partial_initialization(monkeypatch, host):
    monkeypatch.setattr(interactive, "_host", host)
    monkeypatch.setattr(interactive, "_retired", [])
    assert interactive.start("unused", "unused")["pid"] == os.getpid()
    host.stopped = True
    resources = NS(user=NS(KillTimer=Mock()), server=NS(stop=Mock()), panel=NS(close=Mock()))

    def fail(self, *_):
        self.timer = 2
        self.user = resources.user
        self.server = resources.server
        self.panel = resources.panel
        raise RuntimeError("initialization failed")

    monkeypatch.setattr(InteractiveHost, "__init__", fail)
    with pytest.raises(RuntimeError):
        interactive.start("unused", "unused")
    resources.server.stop.assert_called_once()
    resources.panel.close.assert_called_once()
    assert len(interactive._retired) == 2


def test_running_snapshot_is_readable_without_ui_queue_or_nx_calls(host, rig):
    host.control("agent")
    observed = []

    def operation():
        def reader():
            observed.append(host.dispatch("nx_ui_control", {"mode": "status"}))

        thread = threading.Thread(target=reader)
        thread.start()
        thread.join(1)
        assert not thread.is_alive()
        state = __import__("json").loads((host.state_dir / "ui-state.json").read_text())
        assert state["running_method"] == "nx_test_busy"
        text = host.panel.update.call_args.args[0]
        assert "● Agent working" in text and "▶ Test busy" in text and "Started " in text
        assert host.panel.update.call_args.args[1] == "NX MCP — Agent working"
        return {}

    rig.e._handlers["nx_test_busy"] = operation
    host.execute("nx_test_busy", {})
    assert observed[0]["activity"] == "running"
    assert observed[0]["snapshot_only"] is True
    assert observed[0]["operation_elapsed_seconds"] >= 0
    host.dispatcher.drain.assert_not_called()
    assert host.status()["activity"] == "reserved_idle"
    assert host.status()["last_duration_seconds"] >= 0
    assert "NX input is locked" in host.panel_text()
    assert not host.status()["nx_window_input_enabled"]


def test_diagnostic_write_failure_does_not_fail_committed_operation(host, rig):
    host.control("agent")
    host.publish = Mock(side_effect=OSError("disk full"))
    result = host.execute("nx_list_bodies", {})
    assert result["status"] == "success"
    assert host.running_method is None and host.operation_started is None
    assert host.owns_lock


def test_panel_unchanged_text_does_not_repaint(host):
    panel = ControlPanel.__new__(ControlPanel)
    panel.user = NS(SetWindowTextW=Mock(), UpdateWindow=Mock())
    panel.label, panel.hwnd = 2, 3
    panel.update("Idle")
    panel.update("Idle")
    panel.user.SetWindowTextW.assert_called_once()
    assert panel.user.UpdateWindow.call_count == 2


def test_toggle_caption_and_title_follow_who_has_control(host):
    host.mode = "manual"
    text, title, button, enabled, decline = host.panel_state()
    assert text.startswith("● You have control") and title == "NX MCP — You have control"
    assert (button, enabled, decline) == ("Give control to agent", True, False)
    host.handover_error = "Close the open NX dialog, then give control to the agent again."
    assert "Close the open NX dialog" in host.panel_state()[0]
    host.mode = "agent"
    text, title, button, _, _ = host.panel_state()
    assert text.startswith("● Agent has control") and title == "NX MCP — Agent has control"
    assert button == "Take control back"
    host.running_method = "nx_extrude"
    assert host.panel_state()[2:] == ("Take control back after this step", True, False)
    host.requested_mode = "manual"
    assert host.panel_state()[2:] == ("Handing over…", False, False)


def test_panel_update_sets_title_and_toggle(host):
    panel = ControlPanel.__new__(ControlPanel)
    panel.user = NS(
        SetWindowTextW=Mock(), EnableWindow=Mock(), UpdateWindow=Mock(), ShowWindow=Mock()
    )
    panel.label, panel.hwnd, panel.toggle, panel.decline, panel.open_log = 2, 3, 4, 5, 6
    panel.history = 7
    panel.update("text", "NX MCP — Agent is waiting", "Give control to agent", True, True)
    shown = [c.args for c in panel.user.ShowWindow.call_args_list]
    assert (5, 5) in shown and (6, 0) in shown  # "Not now" replaces "Open log".
    panel.update("text", "NX MCP — You have control", "Handing over…", False)
    calls = [c.args for c in panel.user.SetWindowTextW.call_args_list]
    assert (3, "NX MCP — You have control") in calls and (4, "Handing over…") in calls
    assert panel.user.EnableWindow.call_args_list[-1].args == (4, False)
    assert panel.user.ShowWindow.call_args_list[-2:][0].args == (5, 0)
    panel.update("t", "x", "y", True, False, "Recent agent activity")
    assert (7, "Recent agent activity") in [
        c.args for c in panel.user.SetWindowTextW.call_args_list
    ]


class _Queue:
    """Real MainThreadDispatcher queue semantics around the fake host's execute."""

    def __init__(self, host):
        from nx_mcp.bridge import MainThreadDispatcher, _PendingBridgeCall

        self.dispatcher = MainThreadDispatcher(host.execute)
        self.make = _PendingBridgeCall

    def submit(self, method, params=None):
        pending = self.make(method, params or {})
        self.dispatcher._calls.put(pending)
        return pending


def _waiting_host(host, monkeypatch):
    queue = _Queue(host)
    host.dispatcher = queue.dispatcher
    host.mode = "manual"
    host._flash_nx = Mock()
    host.root = host.state_dir
    host.executor._units = lambda: "mm"
    return queue


def test_request_waits_for_engineer_and_runs_after_handover(host, monkeypatch):
    queue = _waiting_host(host, monkeypatch)
    status = queue.submit("nx_status")
    held = queue.submit("nx_list_bodies", {"intent": "Check the boss body"})
    host.tick()
    assert status.complete.is_set() and not held.complete.is_set()  # status is never held
    host.tick()
    text, title, button, _, decline = host.panel_state()
    assert title == "NX MCP — Agent is waiting" and decline and button == "Give control to agent"
    assert "Agent wants to: List bodies" in text
    assert "“Check the boss body”  (agent's note)" in text and "Answer within" in text
    host._flash_nx.assert_called_once()
    assert host.status()["waiting_for_engineer"] == 1
    host.requested_mode = "agent"
    host.tick()
    assert host.mode == "agent" and held.complete.is_set() and held.error is None
    assert host.waiting_count == 0


def test_not_now_declines_every_waiting_request(host, monkeypatch):
    queue = _waiting_host(host, monkeypatch)
    first = queue.submit("nx_fit_view")
    second = queue.submit("nx_ui_control", {"mode": "agent"})  # agent asking for control
    host.tick()
    assert host.waiting_count == 2 and "(+1 more)" in host.panel_text()
    host.decline_requested = True
    host.tick()
    for pending in (first, second):
        assert pending.complete.is_set() and pending.error.code == "NX_UI_DECLINED"
        assert pending.error.details["mutation_outcome"] == "not_started"
    assert host.mode == "manual" and "You declined" in host.panel_text()


def test_unanswered_request_expires_without_running(host, monkeypatch):
    queue = _waiting_host(host, monkeypatch)
    pending = queue.submit("nx_fit_view")
    host.tick()
    host.waiting_since -= 61
    host.tick()
    assert pending.error.code == "NX_UI_PAUSED" and pending.error.retryable
    assert "expired after 60 s" in host.panel_text()


def test_handing_control_back_never_waits(host, monkeypatch):
    queue = _waiting_host(host, monkeypatch)
    pending = queue.submit("nx_ui_control", {"mode": "manual"})
    host.tick()
    assert pending.complete.is_set() and pending.error is None and host.waiting_count == 0


def test_history_idle_text_and_recap_follow_real_operations(host, rig):
    host.control("agent")
    rig.e._handlers["nx_test_make"] = lambda: {
        "status": "success",
        "feature": {"name": "EXTRUDE(2)"},
    }

    def fail():
        raise RuntimeError("native builder failed")

    rig.e._handlers["nx_test_fail"] = fail
    host.execute("nx_test_make", {"intent": "Add the boss"})
    host.execute("nx_list_bodies", {})
    with pytest.raises(NXToolError):
        host.execute("nx_test_fail", {})
    history = host.history_text().splitlines()
    assert history[0] == "Recent agent activity"
    assert history[1].startswith("✓ ") and "Test make → EXTRUDE(2)" in history[1]
    assert "1 read-only or view step (model unchanged)" in history[2]
    assert history[3].startswith("✗ ") and "model unchanged" in history[3]
    assert "Idle since" in host.panel_text() and host.current_summary is None
    host.control("manual")
    assert "Agent's last session: 1 change, 1 failed" in host.panel_text()
    log = (host.state_dir / "activity.log").read_text(encoding="utf-8")
    assert "note: “Add the boss”" in log and log.count("\n") == 3


def test_open_log_button(host, monkeypatch):
    opened = []
    monkeypatch.setattr("os.startfile", opened.append, raising=False)
    host._open_log()
    assert host.notice == "No agent activity logged yet." and not opened
    host.activity.log_path.write_text("x", encoding="utf-8")
    host.open_log_requested = True
    host.tick()
    assert opened == [host.activity.log_path] and not host.open_log_requested


def test_engineer_take_back_is_lasting_but_agent_handback_is_held(host):
    host.control("agent")
    host.execute("nx_ui_control", {"mode": "manual"})  # The agent hands control back.
    assert host.mode == "manual" and host.auto_start and host.hold_auto_start
    host.tick()
    assert host.mode == "manual"  # This host never takes control back by itself.
    # The development loop restores agent control on reload in this case.
    assert host.mode == "agent" or host.auto_start
    host.requested_mode = "agent"  # The engineer gives control again.
    host.tick()
    assert host.mode == "agent" and not host.auto_start and not host.hold_auto_start
    host.requested_mode = "manual"  # The engineer takes control back.
    host.tick()
    assert host.mode == "manual" and not host.auto_start
    assert not (host.mode == "agent" or host.auto_start)  # A reload keeps the engineer in control.


def test_fresh_nx_without_a_part_refuses_mutations_instead_of_freezing(host, rig):
    from types import SimpleNamespace as NS2

    host.control("agent")
    host.driver = "journal"  # Only the resident journal host starves NX's main loop.
    real_session = host.session
    host.session = NS2(ApplicationName="UG_APP_NOPART", Parts=[])
    assert not host._nx_application_ready()
    with pytest.raises(NXToolError) as error:
        host.execute("nx_create_part", {"path": "x.prt"})
    assert error.value.code == "NX_NO_PART_OPEN"
    assert "File > New" in error.value.message
    assert error.value.details["mutation_outcome"] == "not_started"
    host.driver = "startup"  # The startup app runs between NX commands: no refusal.
    with pytest.raises(Exception) as other:
        host.execute("nx_create_part", {"path": "x.prt"})
    assert getattr(other.value, "code", None) != "NX_NO_PART_OPEN"
    host.session = NS2(ApplicationName="UG_APP_MODELING", Parts=[object()])
    assert host._nx_application_ready()
    host.session = NS2()  # A failed probe never blocks work.
    assert host._nx_application_ready()
    host.session = real_session
