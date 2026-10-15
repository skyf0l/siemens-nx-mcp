"""The modal-dialog safety net: detection logic, the call watch and the host report."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace as NS

import pytest

from nx_mcp import interactive
from nx_mcp.activity import Activity
from nx_mcp.core.errors import NXError
from nx_mcp.host import dialogs

PID, OTHER = 7740, 1234
MAIN = 1116010
TEXT = ("Features added in the Modeling environment may not be properly depicted in a flat "
        "pattern.\nSome Modeling operations may make the sheet metal part unusable.\n\n")  # fmt: skip


def sheet_metal_warning(hwnd=35721366, **changes):
    window = {
        "hwnd": hwnd, "pid": PID, "cls": "#32770", "title": "Sheet Metal",
        "owner": MAIN, "owner_pid": PID, "texts": (TEXT,),
        "buttons": ("&OK", "X", "&Cancel", "Don't display this message again"),
    }  # fmt: skip
    window.update(changes)
    return dialogs.Window(**window)


def test_the_nx_sheet_metal_warning_is_a_blocking_dialog():
    (found,) = dialogs.blocking([sheet_metal_warning()], PID)
    assert found.title == "Sheet Metal" and found.text.startswith("Features added")
    assert found.buttons == ("OK", "Cancel", "Don't display this message again")
    assert found.describe().startswith("“Sheet Metal”: Features added in the Modeling")
    assert found.row()["buttons"] == ["OK", "Cancel", "Don't display this message again"]


@pytest.mark.parametrize(
    "changes",
    [
        {"pid": OTHER},  # another program
        {"cls": "NxMcpPanel"},  # not a dialog (the NX MCP panel)
        {"visible": False},
        {"enabled": False},  # disabled under another modal dialog
        {"owner": 0, "owner_pid": 0, "title": "Work in Progress"},  # NX's progress box
        {"owner_pid": OTHER},  # owned by another program's window
    ],
)
def test_other_windows_are_not_blocking_dialogs(changes):
    assert dialogs.blocking([sheet_metal_warning(**changes)], PID) == []


def test_new_dialogs_compare_window_handles():
    old, new = dialogs.Dialog(1, "A"), dialogs.Dialog(2, "B")
    assert dialogs.new_dialogs([old], [old, new]) == [new]
    assert dialogs.new_dialogs([old, new], [new]) == []


def test_scan_never_fails_and_uses_the_given_enumerator():
    assert dialogs.scan(PID, windows=lambda pid: [sheet_metal_warning()])[0].title == "Sheet Metal"

    def broken(pid):
        raise OSError("EnumWindows failed")

    assert dialogs.scan(PID, windows=broken) == []


def test_messages_tell_who_answers():
    found = [dialogs.Dialog(1, "Sheet Metal", "Features added")]
    assert "NX and every call wait" in dialogs.message(found, still_open=True)
    assert "never clicks" in dialogs.message(found, still_open=True)
    assert "someone answered it" in dialogs.message(found, still_open=False)


def test_the_watch_reports_each_new_dialog_once():
    before = [dialogs.Dialog(1, "Old")]
    screens = [[before[0]], [before[0], dialogs.Dialog(2, "New")], [dialogs.Dialog(2, "New")]]
    heard = []
    watch = dialogs.Watch(before, on_new=heard.append, scanner=lambda: screens.pop(0))
    watch.poll()
    watch.poll()
    assert [d.title for d in watch.stop()] == ["New"]  # stop() polls the last screen
    assert [d.title for d in heard] == ["New"]


def test_the_watch_thread_sees_a_dialog_while_the_call_blocks():
    shown = threading.Event()
    seen = threading.Event()
    warning = dialogs.Dialog(9, "Sheet Metal")

    def scanner():
        return [warning] if shown.is_set() else []

    watch = dialogs.Watch([], on_new=lambda d: seen.set(), scanner=scanner, interval=0.01)
    watch.start()
    shown.set()  # the call is blocked in NX now
    assert seen.wait(2)
    shown.clear()  # someone answered it
    assert watch.stop() == [warning]


# ---------------------------------------------------------------------------
# The host (interactive.execute's helpers)


def host(tmp_path):
    """An InteractiveHost without NX: just what the dialog helpers read."""
    h = object.__new__(interactive.InteractiveHost)
    h._activity = Activity(tmp_path / "activity.log")
    h.running_method, h.current_part, h.last_error, h.notice = "nx_activate_part", None, None, None
    h._snapshot = None
    h.dispatcher = NS(reject_where=lambda *a: 0, fail_running=lambda error: False)
    h._set_notice = lambda text: None
    return h


def test_a_call_is_refused_while_a_dialog_blocks_nx(tmp_path, monkeypatch):
    monkeypatch.setattr(dialogs, "scan", lambda *a, **k: [dialogs.Dialog(5, "Sheet Metal", TEXT)])
    with pytest.raises(NXError) as raised:
        interactive.InteractiveHost._watch_dialogs(host(tmp_path), refuse=True)
    error = raised.value
    assert error.code == "NX_MODAL_DIALOG_OPEN"
    assert error.details["mutation_outcome"] == "not_started"
    assert error.details["dialogs"][0]["title"] == "Sheet Metal"


def test_a_dialog_during_a_call_is_logged_and_reported(tmp_path, monkeypatch):
    stub = host(tmp_path)
    warning = dialogs.Dialog(5, "Sheet Metal", TEXT)
    interactive.InteractiveHost._dialog_appeared(stub, warning)
    log = (tmp_path / "activity.log").read_text(encoding="utf-8")
    assert "NX DIALOG  NX waits for an answer to a modal dialog" in log
    assert "“Sheet Metal”: Features added" in log
    assert "nx_activate_part" in log
    assert stub.activity.recent(1)[0].startswith("⚠")

    # Still open after the call: the result names it; nothing is clicked.
    monkeypatch.setattr(dialogs, "scan", lambda *a, **k: [warning])
    result = {"ok": True, "warnings": []}
    interactive.InteractiveHost._report_dialogs(stub, result, [warning])
    (reported,) = result["warnings"]
    assert reported["code"] == "NX_MODAL_DIALOG" and "NX and every call wait" in reported["message"]

    # Answered during a failed call: the error carries it.
    monkeypatch.setattr(dialogs, "scan", lambda *a, **k: [])
    envelope = {"ok": False, "error": {}}
    error = NXError("NX_ERROR", "failed", details={"envelope": envelope})
    interactive.InteractiveHost._report_dialogs(stub, None, [warning], error=error)
    assert error.details["modal_dialogs"][0]["open"] is False
    assert "someone answered it" in envelope["warnings"][0]["message"]


def test_no_dialog_no_report(tmp_path):
    result = {"ok": True, "warnings": []}
    interactive.InteractiveHost._report_dialogs(host(tmp_path), result, [])
    assert result["warnings"] == []


@pytest.mark.parametrize("dev", [True, False])
def test_dev_mode_answers_a_call_blocked_by_a_new_dialog_at_once(tmp_path, dev):
    from nx_mcp.bridge import MainThreadDispatcher

    released, results = threading.Event(), []
    stub = host(tmp_path)
    stub.dev = dev

    def blocked(method, params):  # NX's UI thread inside the dialog's modal loop
        released.wait(5)
        return {"status": "success"}

    stub.dispatcher = MainThreadDispatcher(blocked, timeout=0.5)
    stub.dispatcher.explain = stub._explain_wait

    def call():
        try:
            results.append(stub.dispatcher.call("nx_dev_python", {}))
        except NXError as error:
            results.append(error)

    caller = threading.Thread(target=call)
    caller.start()
    runner = threading.Thread(target=lambda: stub.dispatcher.drain(timeout=1))
    runner.start()
    while stub.dispatcher._running is None:
        threading.Event().wait(0.01)
    interactive.InteractiveHost._dialog_appeared(stub, dialogs.Dialog(5, "Error During Update"))
    caller.join(2)
    (error,) = results
    # dev: answered at once, while the call still waits in NX; production: at the run
    # timeout, but with the dialog named instead of "the bridge is not running"
    assert error.code == "NX_MODAL_DIALOG_OPEN" and not released.is_set()
    assert error.details["mutation_outcome"] == "unknown"
    assert error.details["dialogs"][0]["title"] == "Error During Update"
    assert error.details["blocked_call"]["tool"] == "nx_activate_part"
    assert "“Error During Update”" in error.message and "never clicks" in error.message
    released.set()
    runner.join(2)
    assert len(results) == 1 and stub.dispatcher._running is None  # late result dropped


def test_a_modeless_box_left_open_does_not_block():
    # A modal dialog disables its owner; a modeless #32770 box leaves NX enabled.
    modeless = sheet_metal_warning(owner_enabled=True)
    assert dialogs.blocking([modeless], PID) == []
    assert dialogs.scan(PID, windows=lambda pid: [modeless], texts=lambda h: ("", (), ())) == []


def test_an_owner_the_host_disabled_needs_a_modal_frame():
    # The host disabled NX's main window itself for a call, which proves nothing.
    plain = sheet_metal_warning(modal_frame=False)
    assert dialogs.blocking([plain], PID, host_disabled={MAIN}) == []
    assert dialogs.blocking([plain], PID) != []  # NX disabled it: modal
    assert dialogs.blocking([sheet_metal_warning()], PID, host_disabled={MAIN}) != []


def test_texts_are_read_only_for_confirmed_modal_dialogs():
    windows = [
        sheet_metal_warning(hwnd=1, title="", texts=(), buttons=()),  # modal: read
        sheet_metal_warning(hwnd=2, owner=0, owner_pid=0),  # "Work in Progress"
        sheet_metal_warning(hwnd=3, owner_enabled=True),  # modeless
        sheet_metal_warning(hwnd=4, enabled=False),  # under another dialog
    ]
    read = []

    def texts(hwnd):
        read.append(hwnd)
        return "Error During Update", ("Update failed",), ("&OK",)

    (found,) = dialogs.scan(PID, windows=lambda pid: windows, texts=texts)
    assert read == [1]
    assert (found.title, found.text, found.buttons) == ("Error During Update", "Update failed",
                                                        ("OK",))  # fmt: skip


def test_the_watch_never_notifies_after_stop():
    heard = []
    watch = dialogs.Watch([], on_new=heard.append, scanner=lambda: [dialogs.Dialog(3, "Late")])
    assert [d.title for d in watch.stop()] == ["Late"]  # reported in the result...
    watch.poll()
    assert heard == []  # ...but never answers a call that has ended (or the next one)


# ---------------------------------------------------------------------------
# NX's message boxes (live NX 2406, 2026-10-12): "Edit Sketch" blocked NX 34 minutes
# unnoticed: it is owned by a hidden, enabled "Information" #32770 window and has no
# modal frame; the modal loop disables NX's main window (the root owner) instead.

INFO = 19270994  # the hidden "Information" owner


def edit_sketch_box(**changes):
    window = {
        "hwnd": 659272, "pid": PID, "cls": "#32770", "title": "Edit Sketch",
        "owner": INFO, "owner_pid": PID, "owner_enabled": True, "owner_visible": False,
        "root_owner": MAIN, "root_enabled": False, "modal_frame": False, "active": True,
        "texts": ("The edit causes curves or dimensions to be deleted.\n",), "buttons": ("&OK",),
    }  # fmt: skip
    window.update(changes)
    return dialogs.Window(**window)


def test_an_nx_message_box_with_a_hidden_owner_blocks_nx():
    # during an agent call the host disabled NX's main window itself: the box is the
    # active window of NX's UI thread, which proves the modal loop
    (found,) = dialogs.blocking([edit_sketch_box()], PID, host_disabled={MAIN})
    assert found.title == "Edit Sketch" and found.buttons == ("OK",)
    assert found.text.startswith("The edit causes curves")
    assert dialogs.blocking([edit_sketch_box(active=False)], PID) != []  # NX disabled it
    # the version before this fix: the enabled hidden owner read as "modeless"
    assert dialogs.is_modal(edit_sketch_box(owner_visible=True), PID, {MAIN}) is False


@pytest.mark.parametrize(
    "changes",
    [
        {"root_enabled": True},  # NX's main window takes input: not modal
        {"active": False},  # host-disabled root, no modal frame, not active: no proof
    ],
)
def test_a_hidden_owner_box_without_proof_is_not_blocking(changes):
    assert dialogs.blocking([edit_sketch_box(**changes)], PID, host_disabled={MAIN}) == []


def test_a_scan_from_outside_nx_needs_proof_for_every_owner():
    # the sidecar cannot know which window the host disabled
    assert dialogs.blocking([edit_sketch_box()], PID, dialogs.ANY_OWNER) != []
    assert dialogs.blocking([edit_sketch_box(active=False)], PID, dialogs.ANY_OWNER) == []
    plain = sheet_metal_warning(modal_frame=False)
    assert dialogs.blocking([plain], PID, dialogs.ANY_OWNER) == []
    assert dialogs.blocking([sheet_metal_warning()], PID, dialogs.ANY_OWNER) != []


def test_the_watch_hears_when_a_dialog_closes():
    box = dialogs.Dialog(5, "Edit Sketch")
    screens = [[box], [box], []]
    new, gone = [], []
    watch = dialogs.Watch([], on_new=new.append, on_gone=gone.append,
                          scanner=lambda: screens.pop(0))  # fmt: skip
    watch.poll()
    watch.poll()
    assert new == [box] and gone == []
    watch.poll()
    assert gone == [box]


def test_the_blocked_message_names_the_dialog_the_call_and_the_part():
    row = {"title": "Edit Sketch", "text": "The edit causes curves or dimensions to be deleted.\n",
           "tool": "nx_create_sketch_constraints", "part": "sketch_1_b.prt", "since": 0}  # fmt: skip
    text = dialogs.blocked_message({**row, "since": time.time() - 34}, outcome="not_started")
    assert text.startswith("NX is blocked by its modal dialog “Edit Sketch”: The edit causes")
    assert "during nx_create_sketch_constraints in sketch_1_b.prt" in text and "34 s ago" in text
    assert text.endswith("This call did not run.")
    assert "unknown" in dialogs.blocked_message({"title": "X"}, outcome="unknown")
    assert "opened" not in dialogs.blocked_message({"title": "X"}, outcome="unknown")


def test_a_blocking_dialog_answers_every_waiting_caller_at_once(tmp_path):
    """Queued calls are answered when the dialog appears, new calls are refused before
    they queue, the status read names the dialog, and the answer frees NX again."""
    from nx_mcp.bridge import MainThreadDispatcher

    released, results = threading.Event(), {}
    stub = host(tmp_path)
    stub.dev = False
    stub.current_part = "sketch_1_b.prt"
    stub.running_method = "nx_create_sketch_constraints"
    stub._snapshot = {"running_method": "nx_create_sketch_constraints", "mode": "auto",
                      "activity": "running"}  # fmt: skip
    stub.dispatcher = MainThreadDispatcher(lambda m, p: released.wait(5) and {}, timeout=5,
                                           queue_timeout=5)  # fmt: skip
    stub.dispatcher.explain = stub._explain_wait

    def call(name):
        try:
            results[name] = stub.dispatcher.call(name, {})
        except NXError as error:
            results[name] = error

    first = threading.Thread(target=call, args=("nx_create_sketch_constraints",))
    first.start()
    runner = threading.Thread(target=lambda: stub.dispatcher.drain(timeout=1))
    runner.start()
    while stub.dispatcher._running is None:
        threading.Event().wait(0.01)
    queued = threading.Thread(target=call, args=("nx_extrude",))
    queued.start()
    while not stub.dispatcher.waiting():
        threading.Event().wait(0.01)
    box = dialogs.Dialog(7, "Edit Sketch", "The edit causes curves or dimensions to be deleted.")
    stub._dialog_appeared(box)
    queued.join(2)
    error = results["nx_extrude"]  # answered at once, not after the 135 s queue wait
    assert error.code == "NX_MODAL_DIALOG_OPEN"
    assert error.details["mutation_outcome"] == "not_started"
    assert error.details["blocked_call"] == {"tool": "nx_create_sketch_constraints",
                                             "part": "sketch_1_b.prt", "seconds": 0}  # fmt: skip
    assert "“Edit Sketch”" in error.message and error.category == "engineer_control"
    with pytest.raises(NXError) as refused:  # a new call never queues
        stub.dispatch("nx_list_parts", {})
    assert refused.value.code == "NX_MODAL_DIALOG_OPEN"
    assert stub._snapshot["blocked_by_dialog"]["title"] == "Edit Sketch"
    stub._dialog_gone(box)  # the engineer answered it
    assert stub.blocked_by is None and stub._snapshot["blocked_by_dialog"] is None
    released.set()
    first.join(2)
    runner.join(2)
    assert results["nx_create_sketch_constraints"] == {}


def test_the_status_read_names_the_blocking_dialog(tmp_path, monkeypatch):
    from nx_mcp.domains.session import status

    stub = host(tmp_path)
    stub.blocked_by = {"title": "Edit Sketch", "text": "curves deleted", "buttons": ["OK"],
                       "hwnd": 7, "tool": "nx_create_sketch_constraints", "part": "a.prt",
                       "since": time.time() - 5}  # fmt: skip
    stub._snapshot = {"running_method": "nx_create_sketch_constraints", "mode": "auto",
                      "activity": "running"}  # fmt: skip
    stub._queue_state = lambda: {"waiting": 0, "oldest_seconds": None}
    stub.services = NS(workspace=NS(root=tmp_path), build="2406.9160", store=None)
    monkeypatch.setattr(status, "licenses", lambda: [], raising=False)
    envelope = stub.dispatch("nx_get_status", {"include": ["control"]})
    control = envelope["data"]["control"]
    assert control["activity"] == "blocked_by_dialog" and control["busy"] is True
    row = control["blocked_by_dialog"]
    assert row["title"] == "Edit Sketch" and row["tool"] == "nx_create_sketch_constraints"
    assert "hwnd" not in row and row["seconds"] >= 5
    assert isinstance(row["since"], str) and row["since"].endswith("Z")  # UTC ISO 8601
    (warning,) = envelope["warnings"]
    assert warning["code"] == "NX_MODAL_DIALOG" and "“Edit Sketch”" in warning["message"]


@pytest.mark.parametrize(
    ("title", "words"),
    [
        ("Large Sketch Performance", "at most 50"),
        ("Error During Update", "choose Undo"),
        ("Sheet Metal", "nx_activate_application"),
        ("License Not Available", "license"),
        ("Heartbeat Failure", "restart NX"),
    ],
)
def test_known_dialogs_have_their_own_hint(title, words):
    what, hint = dialogs.known(title)
    assert what and words in hint
    assert dialogs.hint_for(title.upper(), "default") == hint  # any case
    assert dialogs.hint_for("Some Other Box", "default") == "default"
    message = dialogs.message([dialogs.Dialog(9, title)], still_open=True)
    assert hint in message


def test_a_known_dialog_without_text_is_explained_in_the_blocked_message():
    row = {"title": "Large Sketch Performance", "tool": "nx_create_sketch_curves"}
    text = dialogs.blocked_message(row, outcome="unknown")
    assert "“Large Sketch Performance” (NX asks whether to keep shading" in text


def test_the_watchdog_alerts_after_the_delay_then_repeats():
    box = dialogs.Dialog(5, "Large Sketch Performance")
    now = [100.0]
    open_now = [box]
    alerts = []
    watch = dialogs.Watch(
        [], scanner=lambda: list(open_now), on_stuck=lambda d, s: alerts.append((d.hwnd, s)),
        alert_after=20, alert_repeat=60, clock=lambda: now[0],
    )  # fmt: skip
    watch.poll()  # appears
    now[0] = 119.0
    watch.poll()
    assert alerts == []
    now[0] = 120.0
    watch.poll()
    assert alerts == [(5, 20.0)]
    now[0] = 150.0
    watch.poll()
    assert len(alerts) == 1  # not again before the repeat delay
    now[0] = 180.0
    watch.poll()
    assert alerts[-1] == (5, 80.0)
    open_now.clear()  # answered
    now[0] = 400.0
    watch.poll()
    assert len(alerts) == 2


def test_the_watchdog_never_alerts_for_a_dialog_open_before_the_call():
    box = dialogs.Dialog(5, "Error During Update")
    alerts = []
    now = [0.0]
    watch = dialogs.Watch([box], scanner=lambda: [box], on_stuck=lambda d, s: alerts.append(d),
                          alert_after=1, clock=lambda: now[0])  # fmt: skip
    watch.poll()
    now[0] = 100.0
    watch.poll()
    assert alerts == []


def test_a_stuck_dialog_alerts_the_engineer_in_the_panel(tmp_path):
    stub = host(tmp_path)
    posted, flashed = [], []
    stub.panel = NS(alert=lambda: posted.append(True))
    stub._flash_nx = lambda: flashed.append(True)
    stub.current_started_at, stub.current_summary, stub.current_intent = time.time(), "Sketch", None
    box = dialogs.Dialog(5, "Large Sketch Performance")
    interactive.InteractiveHost._dialog_appeared(stub, box)
    assert posted  # the panel names the dialog at once
    stub.blocked_by["since"] = time.time() - 30
    interactive.InteractiveHost._dialog_stuck(stub, box, 30.0)
    assert flashed and len(posted) == 2
    log = (tmp_path / "activity.log").read_text(encoding="utf-8")
    assert "NX has waited 30 s for your answer" in log and "agent steps are blocked" in log
    line = stub.dialog_alert()
    assert line.startswith("⚠") and "“Large Sketch Performance”" in line and "30 s" in line
    stub.mode, stub.panel_rows = "auto", 8
    assert stub.panel_text().splitlines()[0].startswith(line[:60])  # clipped to the panel
    stub._dialog_gone(box)
    assert stub.dialog_alert() is None and len(posted) == 3


def test_reads_fail_fast_in_auto_but_wait_for_the_card_in_ask_me_first(tmp_path):
    from nx_mcp.bridge import QUEUE_TIMEOUT, READ_QUEUE_TIMEOUT

    stub = host(tmp_path)
    stub.mode = "auto"
    assert stub._queue_wait("nx_list_parts") == READ_QUEUE_TIMEOUT
    assert stub._queue_wait("nx_create_extrude") == QUEUE_TIMEOUT
    stub.mode = "ask"  # the engineer answers each call on the card: the full time
    assert stub._queue_wait("nx_list_parts") == QUEUE_TIMEOUT
