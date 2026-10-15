"""Bridge timing and the ghost guard (red team A27/A28, phase P5).

A heavy or long call runs for its cost budget before its caller stops waiting; a queued
call whose client went away (closed its socket) never runs; an expired queued call
leaves the queue.
"""

import asyncio
import threading
import time

import pytest

from nx_mcp import bridge
from nx_mcp.bridge import (
    CLIENT_TIMEOUT,
    DISPATCHER_TIMEOUT,
    QUEUE_TIMEOUT,
    BridgeClient,
    BridgeServer,
    DescriptorBridgeClient,
    MainThreadDispatcher,
)
from nx_mcp.core import catalogue
from nx_mcp.core.errors import NXError


def test_run_timeouts_follow_the_cost_class():
    catalogue.load()
    assert bridge.run_timeout("nx_set_camera") == DISPATCHER_TIMEOUT  # light
    assert bridge.run_timeout("nx_run_batch") == 330.0  # heavy: 300 s budget + 30
    assert bridge.run_timeout("nx_import_model") == 1830.0  # long: 1800 s budget + 30
    assert bridge.run_timeout("not_a_tool") == DISPATCHER_TIMEOUT
    assert bridge.client_timeout("nx_set_camera") == CLIENT_TIMEOUT  # solo light: unchanged
    assert bridge.client_timeout("nx_import_model") == QUEUE_TIMEOUT + 1830.0 + 15.0
    client = BridgeClient("127.0.0.1", 1, token="t", per_cost=True)
    assert client.timeout_for("nx_import_model") > client.timeout_for("nx_set_camera")
    assert BridgeClient("127.0.0.1", 1, token="t").timeout_for("nx_import_model") == CLIENT_TIMEOUT
    assert DescriptorBridgeClient("x.json").per_cost is True


def _run(dispatcher, method):
    """``dispatcher.call(method)`` on a caller thread while this thread drains it."""
    out = {}

    def caller():
        try:
            out["result"] = dispatcher.call(method, {})
        except NXError as error:
            out["error"] = error

    thread = threading.Thread(target=caller)
    thread.start()
    dispatcher.drain(timeout=1)
    thread.join(2)
    return out


def test_a_long_call_gets_its_result_instead_of_unknown():
    def slow(method, params):
        time.sleep(0.2)
        return {"done": method}

    fixed = MainThreadDispatcher(slow, timeout=0.05)
    assert _run(fixed, "nx_import_model")["error"].details["mutation_outcome"] == "unknown"
    per_cost = MainThreadDispatcher(slow, timeout=0.05, run_timeout=lambda m: 1.0)
    assert _run(per_cost, "nx_import_model")["result"] == {"done": "nx_import_model"}


def test_unknown_and_not_started_say_different_things():
    started = MainThreadDispatcher(lambda m, p: time.sleep(0.2) or {}, timeout=0.05)
    unknown = _run(started, "x")["error"]
    assert not unknown.retryable and "may still be running" in unknown.message
    assert unknown.code == "NX_OUTCOME_UNKNOWN"  # NX runs it: never "bridge not running"
    held = MainThreadDispatcher(lambda m, p: {}, timeout=5, queue_timeout=0.05)
    with pytest.raises(NXError) as caught:
        held.call("x", {})
    assert caught.value.retryable and caught.value.code == "NX_BUSY"
    assert "did not start" in caught.value.message and "Nothing ran" in caught.value.message
    assert held.waiting() == [] and len(held._calls.queue) == 0  # the expired call left


async def test_a_cancelled_client_never_gets_a_ghost_run():
    """MCP cancellation: the sidecar closes its socket while the call is queued."""
    ran = []
    dispatcher = MainThreadDispatcher(lambda m, p: ran.append(m) or {"ran": m})
    server = BridgeServer(dispatcher.call, token="t", concurrent_requests=True)
    server.start()
    try:
        client = BridgeClient("127.0.0.1", server.port, token="t", timeout=5)
        task = asyncio.create_task(client.call("nx_create_primitive", {"operation_id": "op_x"}))
        for _ in range(100):
            if dispatcher.waiting():
                break
            await asyncio.sleep(0.01)
        pending = dispatcher.waiting()[0]
        assert pending.peer is not None  # the worker kept the client's socket
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)  # the FIN reaches the NX side
        await asyncio.to_thread(dispatcher.drain, 1)
        assert ran == []  # never ran
        assert pending.error.code == "NX_CALL_EXPIRED"
        assert pending.error.details["mutation_outcome"] == "not_started"
    finally:
        server.stop()


async def test_a_waiting_client_still_gets_its_answer():
    dispatcher = MainThreadDispatcher(lambda m, p: {"ran": m})
    server = BridgeServer(dispatcher.call, token="t", concurrent_requests=True)
    server.start()
    try:
        client = BridgeClient("127.0.0.1", server.port, token="t", timeout=5)
        task = asyncio.create_task(client.call("nx_create_primitive", {}))
        for _ in range(100):
            if dispatcher.waiting():
                break
            await asyncio.sleep(0.01)
        await asyncio.to_thread(dispatcher.drain, 1)
        assert (await task) == {"ran": "nx_create_primitive"}
    finally:
        server.stop()


def test_a_read_fails_fast_when_nx_does_not_take_it():
    """A read waits READ_QUEUE_TIMEOUT at most (nothing ran, retry); others the full queue."""
    assert bridge.queue_timeout("nx_list_parts") == bridge.READ_QUEUE_TIMEOUT < QUEUE_TIMEOUT
    assert bridge.queue_timeout("nx_create_extrude") == QUEUE_TIMEOUT
    assert bridge.queue_timeout("not_a_tool") == QUEUE_TIMEOUT
    waits = {"nx_list_parts": 0.05, "nx_create_extrude": 5.0}
    held = MainThreadDispatcher(lambda m, p: {}, timeout=5, queue_timeout_for=waits.get)
    started = time.monotonic()
    with pytest.raises(NXError) as caught:
        held.call("nx_list_parts", {})
    assert time.monotonic() - started < 2
    assert caught.value.code == "NX_BUSY" and "waited 0 s" in caught.value.message
    assert caught.value.details["mutation_outcome"] == "not_started"
    broken = MainThreadDispatcher(lambda m, p: {}, timeout=5, queue_timeout=0.05,
                                  queue_timeout_for=lambda m: 1 / 0)  # fmt: skip
    with pytest.raises(NXError):  # a failing rule falls back to queue_timeout
        broken.call("x", {})
