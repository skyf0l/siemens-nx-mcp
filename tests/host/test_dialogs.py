"""The modal-dialog safety net: detection logic, the call watch and the host report."""

from __future__ import annotations

import threading
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
    return NS(activity=Activity(tmp_path / "activity.log"), running_method="nx_activate_part",
              last_error=None, notice=None, _set_notice=lambda text: None)  # fmt: skip


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
    stub._answer_blocked_call = lambda d: interactive.InteractiveHost._answer_blocked_call(stub, d)

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
    if dev:  # answered at once, while the call still waits in NX
        (error,) = results
        assert error.code == "NX_MODAL_DIALOG_OPEN" and not released.is_set()
        assert error.details["mutation_outcome"] == "unknown"
        assert error.details["dialogs"][0]["title"] == "Error During Update"
    else:  # production: reported only; the caller waits (here: the bridge timeout)
        assert results[0].code == "NX_BRIDGE_UNAVAILABLE"
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
