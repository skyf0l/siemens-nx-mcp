"""Panel modes, UI handoff and scheduler failures; Win32 calls are explicit seams."""

import ctypes
import json
import os
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from nx_mcp import interactive
from nx_mcp.bridge import BridgeDescriptor
from nx_mcp.core import kernel
from nx_mcp.core.errors import NXError
from nx_mcp.interactive import ControlPanel, InteractiveHost
from tests.kit.objects import Body, Collection

BODIES = {"kind": "body"}  # a catalogue read


@pytest.fixture
def host(rig, tmp_path, monkeypatch):
    lock = {"value": 0}
    enabled = {"value": True}
    ui = NS(
        AskLockStatus=lambda: int(lock["value"] > 0),
        CanOpenPart=lambda: not lock["value"],
        LockAccess=Mock(side_effect=lambda: lock.update(value=lock["value"] + 1)),
        UnlockAccess=Mock(side_effect=lambda: lock.update(value=max(0, lock["value"] - 1))),
    )
    rig.nx.UI = NS(Status=NS(Lock=1))
    h = InteractiveHost.__new__(InteractiveHost)
    h.__dict__.update(
        thread=threading.get_ident(),
        mode="ask",
        window_disabled=False,
        ui=ui,
        nx=rig.nx,
        session=rig.session,
        services=rig.e,
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
        busy=False,
        stopped=False,
        stop_requested=False,
        requested_mode=None,
        stop_file=tmp_path / "stop",
        state_dir=tmp_path,
        root=tmp_path,
        panel=NS(update=Mock(), close=Mock(), wake=Mock()),
        dispatcher=NS(
            drain=Mock(),
            stop=Mock(),
            drain_where=Mock(return_value=0),
            waiting=Mock(return_value=[]),
            reject_where=Mock(return_value=0),
        ),
        server=NS(stop=Mock()),
        timer=7,
        _flash_nx=Mock(),
    )
    h.user = NS(
        EnableWindow=Mock(side_effect=lambda _, v: enabled.update(value=bool(v))),
        IsWindowEnabled=lambda _: enabled["value"],
        KillTimer=Mock(),
    )
    h.lock, h.enabled = lock, enabled  # test handles
    rig.e.host = h  # nx_get_status reaches the panel through the kernel
    monkeypatch.setenv("NX_MCP_DEV", "1")  # the nx_test_* development tools
    monkeypatch.setattr(
        ctypes, "windll", NS(kernel32=NS(GetCurrentThreadId=lambda: 123)), raising=False
    )
    h.descriptor = BridgeDescriptor.create(12345, "test")
    h.descriptor_path = tmp_path / "bridge.json"
    h.descriptor.write(h.descriptor_path)
    return h


class _Queue:
    """Real MainThreadDispatcher queue semantics around the fake host's execute."""

    def __init__(self, host):
        from nx_mcp.bridge import MainThreadDispatcher, _PendingBridgeCall

        self.dispatcher = MainThreadDispatcher(host.execute)
        self.make = _PendingBridgeCall
        host.dispatcher = self.dispatcher

    def submit(self, method, params=None):
        pending = self.make(method, params or {})
        self.dispatcher._calls.put(pending)
        return pending


def _queue(host, mode):
    queue = _Queue(host)
    host.mode = mode
    return queue


def _busy(host, value=True):
    host.simulated_busy = value


# -- Calls -------------------------------------------------------------------


def test_calls_run_only_through_the_scheduler_and_refresh_failure_is_a_warning(host, rig):
    with pytest.raises(NXError, match="NX MCP panel"):
        host.execute("nx_list_objects", BODIES)  # not taken for this call
    host.mode, host._light = "auto", True  # as the scheduler runs an Auto step
    rig.e.dev_tools["nx_test_fail"] = Mock(side_effect=RuntimeError("native error"))
    with pytest.raises(NXError):
        host.execute("nx_test_fail", {})
    rig.part.ModelingViews.WorkView.UpdateDisplay.side_effect = RuntimeError("refresh")
    result = host.execute("nx_list_objects", BODIES)
    assert not any("View refresh" in w for w in result["warnings"])
    rig.e.dev_tools["nx_test_edit"] = lambda: {}
    edited = host.execute("nx_test_edit", {})
    assert any("View refresh" in w for w in edited["warnings"])
    rig.part.DrawingSheets = Collection()
    rig.part.DrawingSheets.CurrentDrawingSheet = object()
    drawing = host.execute("nx_test_edit", {})
    assert not any("View refresh" in w for w in drawing["warnings"])
    host.ui.LockAccess.assert_not_called()  # no mode ever locks NX's UI
    host.ui.UnlockAccess.assert_not_called()
    assert host.mode == "auto"


# -- Mode changes -----------------------------------------------------------


def test_mode_changes_keep_references_without_edits_and_only_two_modes_exist(host, rig):
    body = Body()
    rig.part.Bodies.append(body)
    host.mode, host._light = "auto", True  # as the scheduler runs an Auto step
    ref = rig.ref(body)
    host.execute("nx_list_objects", BODIES)
    rig.e.checkpoints["cp_" + "0" * 32] = {"mark": 1, "part_id": "p", "index": 0}
    host.set_mode("ask")
    host.set_mode("auto")
    host.execute("nx_list_objects", BODIES)  # the engineer changed nothing in between
    assert rig.e.objects.resolve(ref) is body and rig.e.checkpoints
    assert interactive.MODES == ("auto", "ask")
    for gone in ("agent", "bad"):
        with pytest.raises(NXError):
            host.set_mode(gone)  # Agent drives no longer exists
    host.set_mode("manual")  # the old name of Ask me first
    assert host.mode == "ask" and host.status()["modes"] == ["auto", "ask"]
    host.thread = -1
    with pytest.raises(RuntimeError):
        host.set_mode("ask")
    with pytest.raises(RuntimeError):
        host.execute("nx_get_status", {})


def test_engineer_change_between_calls_invalidates_references_once(host, rig, monkeypatch):
    monkeypatch.setattr(kernel, "_gate_evidence", lambda *a: None)
    body = Body()
    rig.part.Bodies.append(body)
    model = {"marks": 5}
    host.model_fingerprint = lambda: (model["marks"],)
    host.mode, host._light = "auto", True
    ref = rig.ref(body)
    rig.e.dev_tools["nx_test_add"] = lambda: rig.part.Bodies.append(Body()) or {}
    rig.run("nx_test_add", {})  # an agent operation nx_undo_operation could undo
    host.execute("nx_list_objects", BODIES)
    checkpoint = rig.run("nx_create_checkpoint", {})["data"]["checkpoint_id"]
    host._engineer_had_nx = True  # NX was released after the call
    model["marks"] = 6  # the engineer added a feature (an undo mark)
    host.execute("nx_list_objects", BODIES)
    # References are re-validated, not dropped: the body still exists, so its ref works.
    assert rig.e.objects.resolve(ref) is body
    assert rig.e.checkpoints[checkpoint]["expired"] == "engineer_change"
    # The history stays, ended by an engineer barrier that no undo or restore crosses.
    assert [h.get("engineer") for h in rig.e.history] == [None, True]
    line = host.history_lines(8)[1]
    assert line.startswith("✎  ") and "You changed the model — agent will re-read" in line
    log = (host.state_dir / "activity.log").read_text(encoding="utf-8")
    assert "ENGINEER  You changed the model" in log
    ref = rig.ref(body)
    host._engineer_had_nx = False  # no call gave NX back in between
    model["marks"] = 7
    host.execute("nx_list_objects", BODIES)
    assert rig.e.objects.resolve(ref) is body
    # Neither undo nor restore reaches back across the engineer's edit.
    with pytest.raises(NXError) as undo:
        host.execute("nx_undo_operation", {})
    with pytest.raises(NXError) as restore:
        host.execute("nx_restore_checkpoint", {"checkpoint_id": checkpoint})
    assert (undo.value.code, restore.value.code) == ("NX_NOT_FOUND", "NX_STALE_CHECKPOINT")
    assert len(rig.part.Bodies) == 2


def test_model_fingerprint_reads_undo_marks_and_parts(host, rig):
    rig.nx.Session.MarkVisibility.AnyVisibility = 9
    rig.session.GetNewestUndoMark = lambda visibility: 41 if visibility == 9 else None
    rig.nx.UF.Undo = NS(UserVisibility=NS(ANY_VIS=2))
    rig.uf.Undo = NS(AskNumberOfMarks=lambda visibility: 3)
    newest, count, loaded, work, display = host.model_fingerprint()
    assert (newest, count) == (41, 3) and loaded and work
    host.session, host.nx, host._uf = NS(), NS(), None  # every probe fails, never raises
    assert host.model_fingerprint() is None


def test_unknown_fingerprint_counts_as_a_change(host, rig):
    host.model_fingerprint = lambda: None
    host._remember_model()
    host._engineer_had_nx = True
    assert host._check_engineer_change()


def test_tick_modes_status_persistence_and_exception_handoff(host):
    host.requested_mode = "auto"
    host.tick()
    assert host.mode == "auto"
    assert json.loads((host.state_dir / "ui-mode.json").read_text())["mode"] == "auto"
    host.requested_mode = "ask"
    host.ticks = 9
    host.tick()
    assert host.mode == "ask" and (host.state_dir / "ui-state.json").is_file()
    assert json.loads((host.state_dir / "ui-state.json").read_text())["mode"] == "ask"
    host.requested_mode = "bad"
    host.tick()
    assert "mode must" in host.last_error and host.mode == "ask"
    host.set_mode("auto")
    host.window_disabled, host.enabled["value"] = True, False  # a call had taken NX
    host.dispatcher.drain.side_effect = RuntimeError("dispatcher")
    host.dispatcher.drain_where.side_effect = RuntimeError("dispatcher")
    host.tick()
    assert not host.busy and "dispatcher" in host.last_error
    assert host.mode == "auto" and host.enabled["value"] and not host.window_disabled
    host.ui.LockAccess.assert_not_called()
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


def test_mode_is_remembered_for_this_nx_process_only(tmp_path, monkeypatch):
    def init(self, *args):
        self.mode, self.stopped, self.state_dir = "ask", False, tmp_path
        self.status = lambda: {"mode": self.mode}
        self.services = NS(dev_tools={})

    monkeypatch.setattr(interactive, "_host", None)
    monkeypatch.setattr(interactive, "_retired", [])
    monkeypatch.setattr(InteractiveHost, "__init__", init)
    assert interactive.start("w", "d", True, dev=True)["mode"] == "auto"  # development
    interactive._host.stopped = True
    (tmp_path / "ui-mode.json").write_text(json.dumps({"pid": os.getpid(), "mode": "ask"}))
    assert interactive.start("w", "d", True, dev=True)["mode"] == "ask"  # a dev reload
    interactive._host.stopped = True
    (tmp_path / "ui-mode.json").write_text(json.dumps({"pid": -1, "mode": "ask"}))
    assert interactive.start("w", "d", True, dev=False)["mode"] == "auto"  # production
    interactive._host.stopped = True
    (tmp_path / "ui-mode.json").write_text(json.dumps({"pid": os.getpid(), "mode": "agent"}))
    assert interactive.start("w", "d", True)["mode"] == "auto"  # a removed mode


# -- Auto -------------------------------------------------------------------


def test_auto_runs_each_call_with_the_light_take(host, rig):
    queue = _queue(host, "auto")
    seen = {}

    def edit():
        seen.update(enabled=host.enabled["value"], lock=host.lock["value"])
        return {}

    rig.e.dev_tools["nx_test_edit"] = edit
    pending = queue.submit("nx_test_edit", {"intent": "Add the boss"})
    host.tick()
    assert pending.error is None and seen == {"enabled": False, "lock": 0}
    assert host.enabled["value"] and not host.window_disabled
    host.ui.LockAccess.assert_not_called()
    # The agent's next health read (a worker-thread snapshot) already sees NX released.
    assert host._snapshot["mode"] == "auto" and host._snapshot["nx_window_input_enabled"]
    host.panel.wake.assert_called()  # tick again at once for a queued follow-up


def test_auto_waits_while_the_engineer_is_busy_then_runs(host, rig):
    queue = _queue(host, "auto")
    rig.e.dev_tools["nx_test_edit"] = lambda: {}
    pending = queue.submit("nx_test_edit", {"intent": "Round the edges"})
    _busy(host)
    host.tick()
    assert not pending.complete.is_set() and host.busy_wait is pending
    pending.seen_at -= 12
    text, title, mode, card = host.panel_state()
    assert text.startswith("⏸  Waiting until you finish your command (12 s): Test edit")
    assert "“Round the edges”" in text.splitlines()[1]
    assert title == "NX MCP — Auto · agent waiting for you" and card is None
    assert host.status()["activity"] == "waiting_for_engineer_command"
    _busy(host, False)
    host.tick()
    assert pending.complete.is_set() and pending.error is None and host.busy_wait is None


def test_auto_busy_wait_times_out_as_not_started(host, rig):
    queue = _queue(host, "auto")
    rig.e.dev_tools["nx_test_edit"] = Mock(return_value={})
    pending = queue.submit("nx_test_edit")
    _busy(host)
    host.tick()
    pending.seen_at -= interactive.BUSY_WAIT_SECONDS
    host.tick()
    assert pending.error.code == "NX_UI_BUSY" and pending.error.retryable
    assert pending.error.details["mutation_outcome"] == "not_started"
    assert "Retry later" in pending.error.message
    rig.e.dev_tools["nx_test_edit"].assert_not_called()
    assert "busy in NX for 2 minutes" in host.panel_text()


def test_busy_signals_cover_dialogs_locks_menus_and_mouse(host):
    assert not host.engineer_busy()
    host.lock["value"] = 1  # an NX command holds the UI lock
    assert host.busy_signals()["ui_locked"] and host.busy_signals()["command_active"]
    host.lock["value"] = 0
    host.enabled["value"] = False  # a modal dialog disabled NX's main window
    assert host.busy_signals()["modal_dialog"]
    host.window_disabled = True  # ...unless we disabled it ourselves
    assert not host.engineer_busy()
    host.window_disabled, host.enabled["value"] = False, True
    host.user.GetAsyncKeyState = lambda key: -32768 if key == 1 else 0
    assert host.busy_signals()["mouse_button_down"]
    host.user.GetAsyncKeyState = lambda key: 0
    host._gui_thread_busy = lambda: True  # menu, move/size or mouse capture
    assert host.busy_signals()["menu_or_drag"]


def test_auto_reads_never_wait_and_leave_a_busy_engineers_window_alone(host):
    queue = _queue(host, "auto")
    _busy(host)
    host.enabled["value"] = False  # a modal dialog is open
    pending = queue.submit("nx_list_objects", BODIES)
    host.tick()
    assert pending.complete.is_set() and pending.error is None
    host.user.EnableWindow.assert_not_called()  # never re-enabled under the dialog


def test_auto_never_shows_a_card_not_even_for_a_long_step(host, rig, monkeypatch):
    from nx_mcp.host import dispatch

    monkeypatch.setattr(
        dispatch, "cost_of", lambda method: "long" if method == "nx_test_long" else "light"
    )
    queue = _queue(host, "auto")
    seen = []

    def long_step():
        seen.append(host.panel_text().splitlines()[0])
        return {}

    rig.e.dev_tools["nx_test_long"] = long_step
    rig.e.dev_tools["nx_test_edit"] = Mock(return_value={})
    first = queue.submit("nx_test_long", {"intent": "Export the STEP"})
    after = queue.submit("nx_test_edit")
    read = queue.submit("nx_list_objects", BODIES)
    for _ in range(3):
        host.tick()
        assert host.waiting_count == 0 and host.panel_state()[3] is None  # never a card
    assert first.error is None and after.error is None and read.error is None
    assert seen[0].endswith("Test long — running, NX may pause")  # it simply runs
    host._flash_nx.assert_not_called()
    assert host.mode == "auto" and host.enabled["value"]


def test_auto_idle_and_running_lines(host):
    host.mode = "auto"
    text, title, mode, card = host.panel_state()
    assert text.startswith("·  Auto — you work in NX, agent steps run between your commands")
    assert (title, mode, card) == ("NX MCP — Auto", "auto", None)
    host.running_method, host.current_started_at = "nx_extrude", time.time()
    assert host.panel_state()[1] == "NX MCP — Auto · agent step running"
    assert "— running, NX may pause" in host.panel_text().splitlines()[0]


# -- Ask me first -----------------------------------------------------------


def test_ask_run_once_runs_only_the_waiting_steps_and_stays(host, monkeypatch):
    queue = _queue(host, "ask")
    status = queue.submit("nx_get_status")
    held = queue.submit("nx_list_objects", {**BODIES, "intent": "Check the boss body"})
    host.tick()
    assert status.complete.is_set() and not held.complete.is_set()  # status never waits
    host.tick()
    text, title, mode, card = host.panel_state()
    assert title == "NX MCP — Agent is waiting" and card == "ask" and mode == "ask"
    assert "Agent wants to: List body objects" in text
    assert "“Check the boss body”  (agent's note)" in text and "Answer within" in text
    host._flash_nx.assert_called_once()
    assert host.status()["waiting_for_engineer"] == 1
    host.answer_requested = "run_once"
    host.tick()
    assert held.complete.is_set() and held.error is None and host.mode == "ask"
    assert host.waiting_count == 0 and host.enabled["value"]
    later = queue.submit("nx_list_objects", BODIES)
    host.tick()
    assert not later.complete.is_set() and host.waiting_count == 1  # asks again


def test_ask_run_and_switch_to_auto(host):
    queue = _queue(host, "ask")
    held = queue.submit("nx_list_objects", BODIES)
    host.tick()
    host.answer_requested = "run_auto"
    host.tick()
    assert held.error is None and host.mode == "auto"
    assert json.loads((host.state_dir / "ui-mode.json").read_text())["mode"] == "auto"
    later = queue.submit("nx_list_objects", BODIES)
    host.tick()
    assert later.error is None and host.waiting_count == 0  # Auto: no card


def test_not_now_declines_only_the_shown_request(host):
    queue = _queue(host, "ask")
    first = queue.submit("nx_set_camera")
    second = queue.submit("nx_list_objects", BODIES)
    host.tick()
    text = host.panel_text()
    assert host.waiting_count == 2 and "for this step (1 more waiting)" in text
    assert "Fit the view" in text and "List body objects" not in text  # one call per card
    host.answer_requested = "decline"
    host.tick()
    assert first.complete.is_set() and first.error.code == "NX_CONTROL_DECLINED"
    assert first.error.details["mutation_outcome"] == "not_started"
    assert not second.complete.is_set() and host.waiting_count == 1  # now on the card
    assert "List body objects" in host.panel_text() and host.mode == "ask"
    host.answer_requested = "decline"
    host.tick()
    assert second.error.code == "NX_CONTROL_DECLINED" and host.waiting_count == 0
    assert "You declined" in host.panel_text()


def test_unanswered_request_expires_without_running(host):
    queue = _queue(host, "ask")
    pending = queue.submit("nx_set_camera")
    host.tick()
    host.waiting_since -= 61
    host.tick()
    assert pending.error.code == "NX_CONTROL_PAUSED" and pending.error.retryable
    assert "expired after 60 s" in host.panel_text()


def test_ask_shows_a_card_for_every_call_but_the_status_read(host, rig):
    rig.e.dev_tools["nx_test_edit"] = Mock(return_value={})
    for method, params in (
        ("nx_test_edit", {}),  # a model step
        ("nx_set_camera", {}),  # a view step
        ("nx_create_part", {"path": "x.prt"}),  # a file step
        ("nx_list_objects", BODIES),  # a read
    ):
        queue = _queue(host, "ask")
        pending = queue.submit(method, params)
        host.tick()
        assert not pending.complete.is_set() and host.waiting_count == 1, method
        assert host.panel_state()[3] == "ask", method  # Run once, Run & switch, Not now
        host.answer_requested = "decline"
        host.tick()
        assert pending.error.code == "NX_CONTROL_DECLINED" and host.waiting_count == 0
    rig.e.dev_tools["nx_test_edit"].assert_not_called()
    status = queue.submit("nx_get_status")
    host.tick()
    assert status.complete.is_set() and host.waiting_count == 0  # never waits


def test_switching_to_ask_pauses_after_the_current_step(host, rig):
    queue = _queue(host, "auto")
    rig.e.dev_tools["nx_test_edit"] = lambda: {}

    def first_step():
        host.requested_mode = "ask"  # the engineer clicks during the step
        return {}

    rig.e.dev_tools["nx_test_first"] = first_step
    first = queue.submit("nx_test_first")
    second = queue.submit("nx_test_edit")
    host.tick()
    assert first.error is None and not second.complete.is_set()
    host.tick()
    assert host.mode == "ask" and not second.complete.is_set() and host.waiting_count == 1


# -- Panel ------------------------------------------------------------------


def test_titles_follow_mode_and_state(host):
    expected = {"auto": "NX MCP — Auto", "ask": "NX MCP — Ask me first"}
    for mode, title in expected.items():
        host.mode = mode
        assert host.panel_state()[1:] == (title, mode, None)
    host.mode = "auto"
    assert host.panel_text().startswith("·  Auto — you work in NX")
    host.mode = "ask"
    assert host.panel_text().startswith("·  Ask me first — agent steps wait for your OK")
    host.dev = True
    assert host.panel_state()[1] == "NX MCP DEV — Ask me first"
    host.requested_mode = "auto"  # the click shows at once
    assert host.panel_state()[2] == "auto"


def test_panel_commands_and_cleanup(host):
    panel = ControlPanel.__new__(ControlPanel)
    panel.host = host
    panel.user = NS(
        DefWindowProcW=Mock(return_value=9),
        SetWindowTextW=Mock(),
        UpdateWindow=Mock(),
        DestroyWindow=Mock(),
        UnregisterClassW=Mock(),
        PostMessageW=Mock(),
    )
    panel.label = 2
    panel.hwnd = 3
    panel._class = NS(lpszClassName="class")
    assert interactive.ID_MODES == {111: "auto", 113: "ask"}  # two radio buttons
    for ident, mode in ((111, "auto"), (113, "ask")):
        assert panel._message(1, 0x111, ident, 0) == 0 and host.requested_mode == mode
    host.requested_mode = None
    panel._message(1, 0x111, 112, 0)  # the removed Agent drives button
    assert host.requested_mode is None
    for ident, answer in ((107, "run_once"), (108, "run_auto"), (104, "decline")):
        panel._message(1, 0x111, ident, 0)
        assert host.answer_requested == answer
    host.requested_mode = None
    panel._message(1, 0x111, 103, 0)
    assert host.stop_requested
    host.stop_requested = False
    panel._message(1, 0x10, 0, 0)
    assert host.stop_requested
    host.stop_requested = False
    host.bridge_ready = Mock()
    panel._message(1, interactive.WM_BRIDGE_ACCEPT, 0, 0)
    host.bridge_ready.assert_called_once()
    host.tick = Mock()
    panel._message(1, interactive.WM_HOST_WAKE, 0, 0)
    host.tick.assert_called_once()
    panel.wake()
    panel.user.PostMessageW.assert_called_once_with(3, interactive.WM_HOST_WAKE, 0, 0)
    assert panel._message(1, 999, 0, 0) == 9
    assert panel._message(1, 0x111, None, 0) == 9 and host.last_error
    panel.close()
    panel.user.DestroyWindow.assert_called_once_with(3)


def _panel():
    panel = ControlPanel.__new__(ControlPanel)
    panel.user = NS(
        SetWindowTextW=Mock(),
        UpdateWindow=Mock(),
        ShowWindow=Mock(),
        SendMessageW=Mock(),
    )
    panel.label, panel.hwnd = 2, 3
    panel.radios = {"auto": 11, "ask": 13}
    panel.run_once, panel.run_auto, panel.decline, panel.open_log = 4, 5, 6, 7
    return panel


def test_panel_unchanged_text_does_not_repaint():
    panel = _panel()
    panel.update("Idle")
    panel.update("Idle")
    panel.user.SetWindowTextW.assert_called_once()
    assert panel.user.UpdateWindow.call_count == 2


def test_panel_update_sets_title_radios_and_card_buttons():
    panel = _panel()
    panel.update("text", "NX MCP — Agent is waiting", "ask", "ask")
    shown = dict(c.args for c in panel.user.ShowWindow.call_args_list)
    assert shown == {4: 5, 5: 5, 6: 5, 7: 0}  # the card's three answers replace Open log
    checks = {c.args[0]: c.args[2] for c in panel.user.SendMessageW.call_args_list}
    assert checks == {11: 0, 13: 1}
    panel.user.ShowWindow.reset_mock()
    panel.update("▶  14:32:05  Extrude\n✓  14:32:01  Sketch", "NX MCP — Auto", "auto", None)
    shown = dict(c.args for c in panel.user.ShowWindow.call_args_list)
    assert shown == {4: 0, 5: 0, 6: 0, 7: 5}
    calls = [c.args for c in panel.user.SetWindowTextW.call_args_list]
    assert (3, "NX MCP — Auto") in calls
    assert (2, "▶  14:32:05  Extrude\n✓  14:32:01  Sketch") in calls


# -- Recovery guarantees (unchanged) ----------------------------------------


def test_stop_does_not_delete_another_session_descriptor(host):
    replacement = BridgeDescriptor.create(12346, "other")
    replacement.write(host.descriptor_path)
    host.stop()
    assert host.descriptor_path.exists()
    host.descriptor_path.write_text("malformed")
    host.stop()


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
    host.mode, host._light = "auto", True  # as the scheduler runs an Auto step
    host.window_disabled, host.enabled["value"] = True, False  # NX taken for this call
    observed = []

    def operation():
        def reader():
            observed.append(host.dispatch("nx_get_status", {"include": ["control", "operation"]}))

        thread = threading.Thread(target=reader)
        thread.start()
        thread.join(1)
        assert not thread.is_alive()
        state = json.loads((host.state_dir / "ui-state.json").read_text())
        assert state["running_method"] == "nx_test_busy"
        text = host.panel.update.call_args.args[0]
        # The running step is the top line of the history; the title says the mode.
        top = text.splitlines()[0]
        assert top.startswith("▶  ") and top.endswith("Test busy — running, NX may pause")
        assert host.panel.update.call_args.args[1] == "NX MCP — Auto · agent step running"
        return {}

    rig.e.dev_tools["nx_test_busy"] = operation
    host.execute("nx_test_busy", {"operation_id": "op_busy_0001"})
    status = observed[0]  # nx_get_status from the snapshot, never queued
    assert status["ok"] and status["mutation_outcome"] == "not_applicable"
    assert status["data"]["operation"]["tool"] == "nx_test_busy"
    assert status["data"]["operation"]["operation_id"] == "op_busy_0001"  # solo: shown
    assert status["data"]["control"] == {
        "mode": "auto",
        "mode_label": "Auto",
        "busy": False,
        "waiting": 0,
        "activity": "running",
        "running": "nx_test_busy",
        "queue": {"waiting": 0, "oldest_seconds": None},
    }
    host.dispatcher.drain.assert_not_called()
    assert host.status()["activity"] == "idle"
    assert host.status()["last_duration_seconds"] >= 0
    idle, *history = host.panel_text().splitlines()
    assert idle.startswith("·  Auto — ") and history[0].startswith("✓ ")


def test_diagnostic_write_failure_does_not_fail_committed_operation(host, rig):
    host.mode, host._light = "auto", True
    host.publish = Mock(side_effect=OSError("disk full"))
    result = host.execute("nx_list_objects", BODIES)
    assert result["ok"]
    assert host.running_method is None and host.operation_started is None


def test_history_and_idle_text_follow_real_operations(host, rig):
    host.mode, host._light = "auto", True
    rig.e.dev_tools["nx_test_make"] = lambda: {
        "status": "success",
        "feature": {"name": "EXTRUDE(2)"},
    }

    def fail():
        raise RuntimeError("native builder failed")

    rig.e.dev_tools["nx_test_fail"] = fail
    host.execute("nx_test_make", {"intent": "Add the boss"})
    host.execute("nx_list_objects", BODIES)
    with pytest.raises(NXError):
        host.execute("nx_test_fail", {})
    # One list, newest first, under the idle line; no separate status block.
    idle, *history = host.panel_text().splitlines()
    assert idle.startswith("·  Auto — ") and host.current_summary is None
    assert history[0].startswith("✗ ") and "model unchanged" in history[0]
    assert "1 read-only or view step (model unchanged)" in history[1]
    assert history[2].startswith("✓ ") and "Test make → EXTRUDE(2)" in history[2]
    host.set_mode("ask")
    idle, *history = host.panel_text().splitlines()
    assert idle.startswith("·  Ask me first — ") and history[0].startswith("✗ ")
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


def test_notice_gives_way_to_the_idle_line(host):
    host.mode = "auto"
    host._set_notice("You declined the agent's request.")
    assert host.panel_text().startswith("You declined")
    host.notice_until = time.monotonic() - 1
    assert host.panel_text().startswith("·  Auto — ")


def test_fresh_nx_without_a_part_refuses_mutations_instead_of_freezing(host, rig):
    from types import SimpleNamespace as NS2

    host.mode, host._light = "auto", True
    host.driver = "journal"  # Only the resident journal host starves NX's main loop.
    real_session = host.session
    host.session = NS2(ApplicationName="UG_APP_NOPART", Parts=[])
    assert not host._nx_application_ready()
    with pytest.raises(NXError) as error:
        host.execute("nx_create_part", {"path": "x.prt"})
    assert error.value.code == "NX_NO_WORK_PART"
    assert "File > New" in error.value.message
    assert error.value.details["mutation_outcome"] == "not_started"
    host.driver = "startup"  # The startup app runs between NX commands: no refusal.
    with pytest.raises(Exception) as other:
        host.execute("nx_create_part", {"path": "x.prt"})
    assert getattr(other.value, "code", None) != "NX_NO_WORK_PART"
    host.session = NS2(ApplicationName="UG_APP_MODELING", Parts=[object()])
    assert host._nx_application_ready()
    host.session = NS2()  # A failed probe never blocks work.
    assert host._nx_application_ready()
    host.session = real_session


def test_light_release_never_raises_and_reenables_nx(host, rig):
    queue = _queue(host, "auto")
    rig.e.dev_tools["nx_test_fail"] = Mock(side_effect=RuntimeError("native error"))
    pending = queue.submit("nx_test_fail")
    host.tick()
    assert pending.error is not None and host.enabled["value"] and not host.window_disabled
    host.user.EnableWindow.side_effect = RuntimeError("no window")
    host.window_disabled = True
    host._release_to_engineer()  # records, never raises
    assert "Cannot re-enable the NX window" in host.last_error


def test_stop_gives_nx_back_and_tears_down_even_if_steps_fail(host, monkeypatch):
    host.window_disabled, host.enabled["value"] = True, False  # stopped during a call
    host.dispatcher.stop.side_effect = RuntimeError("dispatcher failure")
    host.stop()
    assert host.stopped and host.enabled["value"] and not host.window_disabled
    host.user.KillTimer.assert_called_once_with(None, 7)
    host.server.stop.assert_called_once()
    host.panel.close.assert_called_once()
    assert "dispatcher failure" in host.last_error
    assert not host.descriptor_path.exists()
    host.ui.LockAccess.assert_not_called()
    host.ui.UnlockAccess.assert_not_called()


def test_grant_checkbox_shows_for_the_engineers_part_and_toggles(host, rig, monkeypatch):

    monkeypatch.delenv("NX_MCP_DEV", raising=False)
    assert host.grant_state() is None  # the rig part is the agent's own
    rig.e.owned_parts.clear()
    assert host.grant_state() == {"name": "test", "granted": False}
    panel = ControlPanel.__new__(ControlPanel)
    panel.host = host
    panel.user = NS(
        SetWindowTextW=Mock(), SendMessageW=Mock(), ShowWindow=Mock(), DefWindowProcW=Mock()
    )
    panel.grant = 9
    panel.show_grant(host.grant_state())
    panel.user.SetWindowTextW.assert_called_once_with(9, "Allow agent to change test")
    panel.user.SendMessageW.assert_called_once_with(9, 0xF1, 0, 0)  # unchecked
    panel.user.ShowWindow.assert_called_once_with(9, 5)
    panel.show_grant(host.grant_state())  # unchanged: no repaint
    assert panel.user.ShowWindow.call_count == 1
    # The engineer clicks it; the next tick grants the work part.
    assert panel._message(1, 0x111, 106, 0) == 0 and host.grant_toggle_requested
    host.panel = panel
    panel.update = Mock()
    panel.wake = Mock()
    host.tick()
    assert not host.grant_toggle_requested
    assert rig.e.granted(rig.part)
    assert panel.user.SendMessageW.call_args.args == (9, 0xF1, 1, 0)  # now checked
    assert "You allowed the agent to change test" in host.notice
    host.grant_toggle_requested = True
    host.tick()
    assert not rig.e.owns(rig.part)
    monkeypatch.setenv("NX_MCP_DEV", "1")
    host.tick()
    assert panel.user.ShowWindow.call_args.args == (9, 0)  # hidden in development


def test_light_catalogue_steps_do_not_warn_that_nx_may_pause(host):
    host.running_method = "nx_list_objects"
    host.current_started_at = time.time()
    assert host.panel_text().splitlines()[0].endswith("nx_list_objects — running")
    host.running_method = "nx_create_extrude"  # heavy catalogue steps keep the warning
    assert host.panel_text().splitlines()[0].endswith("— running, NX may pause")


# -- Modal NX dialogs (host/dialogs.py) ----------------------------------------


def _dialog_open(monkeypatch, is_open):
    from nx_mcp.host import dialogs

    warning = dialogs.Dialog(5, "Sheet Metal", "Features added in the Modeling environment")
    monkeypatch.setattr(dialogs, "scan", lambda *a, **k: [warning] if is_open() else [])


def test_auto_refuses_a_step_under_a_dialog_that_opened_after_the_busy_check(
    host, rig, monkeypatch
):
    # The busy probes saw nothing (the dialog opened in between, or its owner is not
    # NX's main window); the scan after the host took NX refuses the step.
    _dialog_open(monkeypatch, lambda: True)
    queue = _queue(host, "auto")
    rig.e.dev_tools["nx_test_edit"] = Mock(return_value={})
    edit = queue.submit("nx_test_edit", {})
    view = queue.submit("nx_set_camera", {})
    read = queue.submit("nx_list_objects", BODIES)
    for _ in range(3):
        host.tick()
    for refused in (edit, view):
        assert refused.error.code == "NX_MODAL_DIALOG_OPEN"
        assert refused.error.details["mutation_outcome"] == "not_started"
    rig.e.dev_tools["nx_test_edit"].assert_not_called()
    assert read.error is None  # reads are never refused
    assert host.enabled["value"] and not host.window_disabled  # NX given back
    assert host.waiting_count == 0  # Auto never shows a card


def test_auto_waits_for_an_engineers_dialog_then_runs_and_reads_answer(host, rig, monkeypatch):
    dialog = {"open": True}
    _dialog_open(monkeypatch, lambda: dialog["open"])
    queue = _queue(host, "auto")
    host.enabled["value"] = False  # the engineer's modal dialog disables NX's main window
    rig.e.dev_tools["nx_test_edit"] = Mock(return_value={})
    edit = queue.submit("nx_test_edit", {"intent": "Round the edges"})
    read = queue.submit("nx_list_objects", BODIES)
    host.tick()
    host.tick()
    assert read.complete.is_set() and read.error is None  # answered at once
    assert not edit.complete.is_set() and host.busy_wait is edit  # waits, not refused
    dialog["open"], host.enabled["value"] = False, True  # the engineer closed it
    host.tick()
    assert edit.complete.is_set() and edit.error is None
    rig.e.dev_tools["nx_test_edit"].assert_called_once()
