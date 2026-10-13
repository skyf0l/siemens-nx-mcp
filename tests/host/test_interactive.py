import threading
import time

import pytest

from nx_mcp.bridge import MainThreadDispatcher
from nx_mcp.core.errors import NXError


def test_expired_queued_mutation_never_executes():
    calls = []
    dispatcher = MainThreadDispatcher(lambda m, p: calls.append(m) or {}, timeout=0.01)
    with pytest.raises(NXError) as error:
        dispatcher.call("nx_reposition_component", {})
    assert error.value.details["mutation_outcome"] == "not_started"
    dispatcher.drain()
    assert calls == []


def test_timeout_of_running_mutation_reports_unknown_without_duplicate():
    started = threading.Event()
    release = threading.Event()
    errors = []
    calls = []

    def execute(m, p):
        calls.append(m)
        started.set()
        release.wait(2)
        return {"done": True}

    dispatcher = MainThreadDispatcher(execute, timeout=0.05)

    def caller():
        try:
            dispatcher.call("mutation", {})
        except NXError as e:
            errors.append(e)

    client = threading.Thread(target=caller)
    client.start()
    worker = threading.Thread(target=lambda: dispatcher.drain(timeout=1))
    worker.start()
    assert started.wait(1)
    client.join(1)
    assert errors and errors[0].details["mutation_outcome"] == "unknown"
    release.set()
    worker.join(1)
    assert calls == ["mutation"]


def test_conservative_bounds_gap():
    from nx_mcp.domains.inspection.interference import _envelope_gap as envelope_gap

    assert envelope_gap([0, 0, 0, 10, 10, 10], [12, 0, 0, 22, 10, 10]) == 2
    assert envelope_gap([0, 0, 0, 10, 10, 10], [5, 0, 0, 15, 10, 10]) == 0
    assert envelope_gap([0, 0, 0, 10, 10, 10], [13, 14, 10, 23, 24, 20]) == 5


def test_auto_calls_never_lock_nx_and_give_the_window_back(monkeypatch):
    from types import SimpleNamespace

    from nx_mcp.host import dispatch
    from nx_mcp.interactive import InteractiveHost

    class UI:
        def AskLockStatus(self):
            return 0

        def LockAccess(self):
            raise AssertionError("Auto never calls UI.LockAccess")

        UnlockAccess = LockAccess

        def CanOpenPart(self):
            return True

    host = InteractiveHost.__new__(InteractiveHost)
    host.thread = threading.get_ident()
    host.mode = "auto"
    host.ui = UI()
    host.nx = SimpleNamespace(UI=SimpleNamespace(Status=SimpleNamespace(Lock=1)))
    host.main_hwnd = 1
    window = {"enabled": True}

    def enable(_hwnd, value):
        window["enabled"] = bool(value)

    host.user = SimpleNamespace(EnableWindow=enable, IsWindowEnabled=lambda h: window["enabled"])
    host.session = SimpleNamespace(Parts=SimpleNamespace(Display=None))
    host.completed = 0
    host.publish = lambda: None
    host.services = SimpleNamespace(history=[], checkpoints={})
    seen = []

    def execute(_services, method, params):
        seen.append((method, window["enabled"]))
        return {"status": "success"}

    monkeypatch.setattr(dispatch, "execute", execute)
    host.dispatcher = MainThreadDispatcher(host.execute, timeout=1)
    for method in ("nx_create_part", "nx_list_parts"):
        pending = SimpleNamespace(method=method)
        host.dispatcher.drain_where = lambda match, p=pending: host.execute(p.method, {}) or True
        assert host._run_light(pending)
    # NX's window was disabled for each call only, never UI-locked, and is enabled again.
    assert seen == [("nx_create_part", False), ("nx_list_parts", False)]
    assert window["enabled"] and not host.window_disabled and host.mode == "auto"


@pytest.mark.parametrize("raised", [SystemExit(3), KeyboardInterrupt()])
def test_base_exceptions_from_a_tool_are_errors_and_keep_the_panel_mode(monkeypatch, raised):
    # Regression: a SystemExit in an nx_dev_python snippet escaped to tick().
    from types import SimpleNamespace

    from nx_mcp.host import dispatch
    from nx_mcp.interactive import InteractiveHost

    class UI:
        def AskLockStatus(self):
            return 0

    host = InteractiveHost.__new__(InteractiveHost)
    host.thread = threading.get_ident()
    host.mode, host._light = "auto", True  # a call the scheduler runs in Auto
    host.ui = UI()
    host.nx = SimpleNamespace(UI=SimpleNamespace(Status=SimpleNamespace(Lock=1)))
    host.session = SimpleNamespace(Parts=SimpleNamespace(Display=None))
    host.completed = 0
    host.publish = lambda: None
    host.services = SimpleNamespace(history=[], checkpoints={})

    def boom(_services, method, params):
        raise raised

    monkeypatch.setattr(dispatch, "execute", boom)
    dispatcher = MainThreadDispatcher(host.execute, timeout=1)
    results = []
    caller = threading.Thread(target=lambda: _call(dispatcher, results))
    caller.start()
    while not dispatcher.drain(timeout=1):
        pass
    caller.join(1)
    assert isinstance(results[0], NXError) and type(raised).__name__ in results[0].message
    assert host.mode == "auto"


def _call(dispatcher, results):
    try:
        results.append(dispatcher.call("nx_dev_python", {"code": "raise SystemExit"}))
    except NXError as error:
        results.append(error)


@pytest.mark.asyncio
async def test_interactive_transport_services_health_during_blocked_request():
    import asyncio

    from nx_mcp.bridge import BridgeClient, BridgeServer

    entered, release = threading.Event(), threading.Event()

    def dispatch(method, params):
        if method == "work":
            entered.set()
            release.wait(5)
            return {"done": True}
        return {"activity": "running", "snapshot_only": True}

    server = BridgeServer(dispatch, token="test", concurrent_requests=True)
    server.start()
    try:
        client = BridgeClient("127.0.0.1", server.port, token="test", timeout=2)
        running = asyncio.create_task(client.call("work", {}))
        assert await asyncio.to_thread(entered.wait, 1)
        status = await asyncio.wait_for(client.call("nx_get_status", {}), 1)
        assert status["snapshot_only"] and not running.done()
        release.set()
        assert (await running)["done"]
    finally:
        release.set()
        server.stop()


def test_namespace_import_reports_missing():
    from nx_mcp.interactive import import_nxopen_namespaces

    assert import_nxopen_namespaces(("json", "nx_mcp_no_such_module")) == [
        "nx_mcp_no_such_module (No module named 'nx_mcp_no_such_module')"
    ]


def test_unexpected_errors_keep_the_traceback_out_of_details(monkeypatch):
    from nx_mcp.core.errors import classify

    monkeypatch.setenv("NX_MCP_DEV", "1")
    try:
        import nx_mcp_no_such_module  # noqa: F401
    except ImportError as exc:
        err = classify(exc)
    assert err.code == "NX_ERROR"
    assert err.details["exception_type"] == "ModuleNotFoundError"
    assert "traceback" not in err.details and "nx_mcp_no_such_module" in (err.trace or "")


def test_queue_wait_and_run_time_have_separate_timeouts():
    import time as clock

    held = MainThreadDispatcher(lambda m, p: {}, timeout=5, queue_timeout=0.05)
    started = clock.monotonic()
    with pytest.raises(NXError) as error:
        held.call("nx_extrude", {})
    assert error.value.details["mutation_outcome"] == "not_started"
    assert clock.monotonic() - started < 1 and held.parked == 0

    def slow(m, p):
        clock.sleep(0.25)
        return {"done": True}

    # Queued 0.15 s, then runs 0.25 s: within a 0.3 s run timeout counted from the start.
    dispatcher = MainThreadDispatcher(slow, timeout=0.3, queue_timeout=1)
    worker = threading.Timer(0.15, lambda: dispatcher.drain(timeout=1))
    worker.start()
    assert dispatcher.call("nx_extrude", {}) == {"done": True}
    worker.join()


def test_bridge_counts_connections_in_flight_and_parked_callers():
    import asyncio

    from nx_mcp.bridge import BridgeClient, BridgeServer

    progress = threading.Event()
    dispatcher = MainThreadDispatcher(lambda m, p: {"ran": m}, progress=progress)
    server = BridgeServer(
        lambda m, p: dispatcher.call(m, p), token="t", concurrent_requests=True, progress=progress
    )
    server.start(accept_thread=False)  # the caller accepts (the panel's FD_ACCEPT)
    server.socket.setblocking(False)
    try:
        assert server.accept_pending() == 0  # nothing waiting: never blocks
        results = []
        client = threading.Thread(
            target=lambda: results.append(
                asyncio.run(BridgeClient("127.0.0.1", server.port, token="t").call("x", {}))
            )
        )
        client.start()
        deadline = time.monotonic() + 5
        while not server.pending_connection() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert server.accept_pending(lambda sock: sock.setblocking(True)) == 1
        while dispatcher.parked == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert server.in_flight == 1 and dispatcher.parked == 1 and progress.is_set()
        progress.clear()
        dispatcher.drain()
        assert dispatcher.parked == 0  # settled on the UI thread, before the reply
        client.join(5)
        assert results == [{"ran": "x"}] and server.in_flight == 0 and progress.is_set()
    finally:
        server.stop()
    assert server.accept_pending() == 0


def test_tick_gives_bridge_workers_the_gil_until_queued_and_answered():
    import asyncio

    from nx_mcp.bridge import BridgeClient, BridgeServer
    from nx_mcp.interactive import InteractiveHost

    progress = threading.Event()
    host = InteractiveHost.__new__(InteractiveHost)
    host.dispatcher = MainThreadDispatcher(lambda m, p: {"ran": m}, progress=progress)
    host.server = BridgeServer(
        lambda m, p: host.dispatcher.call(m, p),
        token="t",
        concurrent_requests=True,
        progress=progress,
    )
    host.server.start()  # fallback: an accept thread
    try:
        results = []
        client = threading.Thread(
            target=lambda: results.append(
                asyncio.run(BridgeClient("127.0.0.1", host.server.port, token="t").call("x", {}))
            )
        )
        client.start()
        deadline = time.monotonic() + 5
        while not (host.server.in_flight or host.server.pending_connection()):
            assert time.monotonic() < deadline
            time.sleep(0.002)
        host._let_bridge_run(5)
        assert [p.method for p in host.dispatcher.waiting()] == ["x"]
        host.dispatcher.drain()
        host._let_bridge_run(5)
        assert host.server.in_flight == 0  # the answer is written
        client.join(5)
        assert results == [{"ran": "x"}]
    finally:
        host.server.stop()


def test_request_arrival_ticks_at_once_or_right_after_a_busy_tick():
    from unittest.mock import Mock

    from nx_mcp.interactive import InteractiveHost

    host = InteractiveHost.__new__(InteractiveHost)
    host._fast_accept = True
    host.server = Mock()
    host.panel = Mock()
    host.tick = Mock()
    host._prepare_socket = Mock()
    host.busy = False
    host.bridge_ready()
    host.server.accept_pending.assert_called_once_with(host._prepare_socket)
    host.tick.assert_called_once()
    host.busy = True
    host.bridge_ready()
    assert host.tick.call_count == 1 and host._rewake  # the busy tick wakes again


def test_start_defaults_to_auto_also_in_dev_and_sets_the_dev_title(monkeypatch):
    from types import SimpleNamespace

    from nx_mcp import interactive

    def init(self, *args):
        self.mode, self.stopped = "ask", False
        self.status = lambda: {}
        self.services = SimpleNamespace(dev_tools={})

    monkeypatch.setattr(interactive, "_host", None)
    monkeypatch.setattr(interactive.InteractiveHost, "__init__", init)
    interactive.start("w", "d", True, dev=True)
    host = interactive._host
    assert host.mode == "auto" and host.dev
    host.requested_mode = host.running_method = None
    host.waiting_count = 0
    host.panel_text = lambda: ""
    assert host.panel_state()[1] == "NX MCP DEV — Auto"
    assert "nx_dev_python" in host.services.dev_tools  # Dev launcher only.
    host.stopped = True
    interactive.start("w", "d", True)
    assert "nx_dev_python" not in interactive._host.services.dev_tools


def test_a_development_snippet_that_changed_nothing_is_a_read_in_the_activity_list():
    from nx_mcp import interactive

    assert interactive._model_unchanged("nx_dev_python", {"mutation_outcome": "not_applicable"})
    assert not interactive._model_unchanged("nx_dev_python", {"mutation_outcome": "committed"})
    assert not interactive._model_unchanged("nx_dev_python", None)
    assert interactive._model_unchanged("nx_get_status", {"mutation_outcome": "committed"})
