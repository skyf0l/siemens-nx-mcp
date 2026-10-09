import threading
import time

import pytest

from nx_mcp.bridge import MainThreadDispatcher
from nx_mcp.runtime import NXToolError


def test_expired_queued_mutation_never_executes():
    calls = []
    dispatcher = MainThreadDispatcher(lambda m, p: calls.append(m) or {}, timeout=0.01)
    with pytest.raises(NXToolError) as error:
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
        except NXToolError as e:
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


def test_agent_ui_lock_is_not_nested_and_handoff_releases_input():
    from types import SimpleNamespace

    from nx_mcp.interactive import InteractiveHost

    class UI:
        count = 1

        def AskLockStatus(self):
            return int(self.count > 0)

        def LockAccess(self):
            self.count += 1

        def UnlockAccess(self):
            self.count = max(0, self.count - 1)

        def CanOpenPart(self):
            return self.count == 0

    class Parts:
        Display = None

        def __iter__(self):
            return iter(())

    host = InteractiveHost.__new__(InteractiveHost)
    host.thread = threading.get_ident()
    host.mode = "agent"
    host.owns_lock = True
    host.window_disabled = False
    host.ui = UI()
    host.nx = SimpleNamespace(UI=SimpleNamespace(Status=SimpleNamespace(Lock=1)))
    host.main_hwnd = 1
    enabled = []
    host.user = SimpleNamespace(EnableWindow=lambda h, v: enabled.append(v))
    host.session = SimpleNamespace(Parts=Parts())
    host.completed = 0
    host.status = lambda: {}
    host.publish = lambda: None

    def execute(method, params):
        if method == "nx_create_part":
            assert host.ui.count == 0  # FileNew must run without an NX UI lock.
        return {"status": "success"}

    host.executor = SimpleNamespace(execute=execute, _history=[1], _checkpoints={"cp": 1})
    host.execute("nx_list_bodies", {})
    assert host.ui.count == 1  # Reads run inside the held lock: no unlock/relock pair.
    host.execute("nx_create_part", {})
    assert host.ui.count == 1  # Reacquire the lock released by FileNew exactly once.
    host.control("manual")  # manual = Ask me first
    assert host.ui.count == 0 and not enabled and host.mode == "ask"
    # References and checkpoints survive the handover; only an engineer edit seen
    # before the agent's next step invalidates them.
    assert host.executor._history and host.executor._checkpoints


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
        status = await asyncio.wait_for(client.call("nx_ui_control", {}), 1)
        assert status["snapshot_only"] and not running.done()
        release.set()
        assert (await running)["done"]
    finally:
        release.set()
        server.stop()


def test_resident_loop_ticks_without_timer_until_stopped(monkeypatch):
    from nx_mcp import interactive

    class Host:
        stopped = False
        last_error = None
        ticks = 0

        def tick(self):
            self.ticks += 1
            self.stopped = self.ticks == 3

    host = Host()
    calls = []

    def fake_start(workspace, descriptor, resident=False):
        calls.append(resident)
        interactive._host = host

    monkeypatch.setattr(interactive, "_host", None)
    monkeypatch.setattr(interactive, "start", fake_start)
    monkeypatch.setattr(interactive, "import_nxopen_namespaces", lambda: ["NXOpen.X (gone)"])
    pumps = []
    assert interactive.run_resident("w", "d", interval=0, pump=lambda: pumps.append(1)) is host
    assert calls == [True] and host.ticks == 3 and len(pumps) == 3
    assert "NXOpen.X" in host.last_error


def test_namespace_import_reports_missing():
    from nx_mcp.interactive import import_nxopen_namespaces

    assert import_nxopen_namespaces(("json", "nx_mcp_no_such_module")) == [
        "nx_mcp_no_such_module (No module named 'nx_mcp_no_such_module')"
    ]


def test_api_error_carries_traceback():
    from nx_mcp.runtime import api_error

    try:
        import nx_mcp_no_such_module  # noqa: F401
    except ImportError as exc:
        err = api_error(exc)
    assert err.code == "NX_API_ERROR"
    assert err.details["exception_type"] == "ModuleNotFoundError"
    assert "nx_mcp_no_such_module" in err.details["traceback"]


def test_queue_wait_and_run_time_have_separate_timeouts():
    import time as clock

    held = MainThreadDispatcher(lambda m, p: {}, timeout=5, queue_timeout=0.05)
    started = clock.monotonic()
    with pytest.raises(NXToolError) as error:
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
