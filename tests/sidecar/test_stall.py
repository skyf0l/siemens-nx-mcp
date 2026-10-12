"""Why NX did not answer, said truthfully: a modal dialog, NX down, or NX busy.

Live 2026-10-12: a modal NX "Edit Sketch" box blocked NX for 34 minutes and every queued
caller read "NX or the bridge is not running" although NX and the bridge ran.
"""

import threading
import time
from types import SimpleNamespace as NS

import pytest

from nx_mcp.bridge import MainThreadDispatcher
from nx_mcp.core.errors import NXError
from nx_mcp.host import dialogs
from nx_mcp.sidecar.proxy import CatalogueProxy

BOX = dialogs.Dialog(7, "Edit Sketch", "The edit causes curves or dimensions to be deleted.")


def proxy(monkeypatch, *, alive, found=()):
    p = CatalogueProxy(NS(), NS(root="."), None)
    monkeypatch.setattr(p, "_descriptor", lambda: NS(pid=4242))
    monkeypatch.setattr(dialogs, "process_alive", lambda pid: alive)
    seen = []

    def scan(pid, **kwargs):
        seen.append((pid, kwargs.get("host_disabled")))
        return list(found)

    monkeypatch.setattr(dialogs, "scan", scan)
    return p, seen


def test_a_dialog_seen_from_outside_nx_replaces_the_timeout(monkeypatch):
    p, seen = proxy(monkeypatch, alive=True, found=[BOX])
    timeout = NXError("NX_OUTCOME_UNKNOWN", "no answer", details={"mutation_outcome": "unknown"})
    error = p.explain_stall(timeout)
    assert error.code == "NX_MODAL_DIALOG_OPEN" and error.category == "engineer_control"
    assert "“Edit Sketch”: The edit causes curves" in error.message
    assert error.details["mutation_outcome"] == "unknown" and "nx_get_operation" in error.hint
    assert error.details["dialogs"][0]["title"] == "Edit Sketch"
    assert seen == [(4242, dialogs.ANY_OWNER)]  # from outside: every owner needs proof


def test_nx_not_running_and_bridge_not_answering_are_told_apart(monkeypatch):
    down = NXError("NX_BRIDGE_UNAVAILABLE", "refused", details={"mutation_outcome": "not_started"})
    p, _ = proxy(monkeypatch, alive=False)
    assert p.explain_stall(down).message.startswith("NX (pid 4242) is not running")
    p, _ = proxy(monkeypatch, alive=True)
    assert "is running, so its NX MCP host is stopped" in p.explain_stall(down).message


@pytest.mark.parametrize("code", ["NX_INVALID_ARGUMENT", "NX_BUILD_FAILED"])
def test_other_errors_pass_unchanged(monkeypatch, code):
    p, seen = proxy(monkeypatch, alive=True, found=[BOX])
    error = NXError(code, "x")
    assert p.explain_stall(error) is error and seen == []


def test_a_broken_diagnostic_never_fails_the_call(monkeypatch):
    p, _ = proxy(monkeypatch, alive=True)

    def broken(pid, **kwargs):
        raise OSError("EnumWindows failed")

    monkeypatch.setattr(dialogs, "scan", broken)
    error = NXError("NX_BUSY", "busy")
    assert p.explain_stall(error) is error


def test_a_queue_timeout_names_the_running_call_not_a_dead_bridge():
    released = threading.Event()
    dispatcher = MainThreadDispatcher(
        lambda m, p: released.wait(5) and {}, timeout=5, queue_timeout=0.2
    )
    first = threading.Thread(target=lambda: dispatcher.call("nx_create_sketch_constraints", {}))
    first.start()
    runner = threading.Thread(target=lambda: dispatcher.drain(timeout=1))
    runner.start()
    while dispatcher._running is None:
        time.sleep(0.01)
    with pytest.raises(NXError) as caught:
        dispatcher.call("nx_extrude", {})
    released.set()
    first.join(2)
    runner.join(2)
    error = caught.value
    assert error.code == "NX_BUSY" and error.category == "bridge" and error.retry == "after_wait"
    assert "busy with nx_create_sketch_constraints" in error.message
    assert error.details["running"]["tool"] == "nx_create_sketch_constraints"
    assert error.details["mutation_outcome"] == "not_started"
    assert error.error_dict()["hint"].startswith("NX and the bridge are running")
