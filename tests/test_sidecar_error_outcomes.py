"""Sidecar error codes must tell an agent whether a call may have run in NX."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from nx_mcp import bridge as bridge_module
from nx_mcp.bridge import CLIENT_TIMEOUT, DISPATCHER_TIMEOUT, BridgeClient, MainThreadDispatcher
from nx_mcp.runtime import NXToolError
from nx_mcp.server import create_server
from nx_mcp.workspace import Workspace


def payload(result):
    assert result.isError
    return result.structuredContent


# ---- 1. unknown tools never reach NX ----
async def test_unknown_tool_on_full_surface_is_tool_not_found(tmp_path):
    server = create_server(AsyncMock(), Workspace(tmp_path), surface="full")
    error = payload(await server.call_tool("nx_list_objectz", {}))
    assert error["mutation_outcome"] == "not_started"
    assert error["error"]["code"] == "NX_UNKNOWN_TOOL"
    assert error["error"]["category"] == "not_found"
    assert "nx_list_objects" in error["error"]["details"]["close_matches"]


# ---- 2. bridge transport: before vs after the request was written ----
async def test_connect_failure_before_send_is_retryable_not_started():
    probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()
    client = BridgeClient("127.0.0.1", port, token="t", timeout=2)
    with pytest.raises(NXToolError) as caught:
        await client.call("nx_extrude", {"operation_id": "op_1"})
    assert caught.value.code == "NX_BRIDGE_UNAVAILABLE"
    assert caught.value.retryable is True
    assert caught.value.details["mutation_outcome"] == "not_started"


async def test_timeout_after_send_is_unknown_and_not_retryable():
    received = asyncio.Event()

    async def silent(reader, writer):
        await reader.readline()
        received.set()
        await asyncio.sleep(5)
        writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        client = BridgeClient("127.0.0.1", port, token="t", timeout=0.3)
        with pytest.raises(NXToolError) as caught:
            await client.call("nx_extrude", {"operation_id": "op_sent"})
    finally:
        server.close()
    assert received.is_set()
    error = caught.value
    assert error.code == "NX_BRIDGE_OUTCOME_UNKNOWN"
    assert error.retryable is False
    assert error.details == {"operation_id": "op_sent", "mutation_outcome": "unknown"}
    assert "nx_get_operation" in error.message


async def test_dropped_connection_after_send_is_unknown():
    async def drop(reader, writer):
        await reader.readline()
        writer.close()

    server = await asyncio.start_server(drop, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(NXToolError) as caught:
            await BridgeClient("127.0.0.1", port, token="t", timeout=2).call(
                "nx_extrude", {"operation_id": "op_drop"}
            )
    finally:
        server.close()
    assert caught.value.retryable is False
    assert caught.value.details["mutation_outcome"] == "unknown"


def test_client_waits_longer_than_the_nx_dispatcher():
    assert CLIENT_TIMEOUT > DISPATCHER_TIMEOUT
    assert BridgeClient("127.0.0.1", 1, token="t").timeout == CLIENT_TIMEOUT
    assert bridge_module.DescriptorBridgeClient("x.json").timeout == CLIENT_TIMEOUT
    dispatcher = MainThreadDispatcher(lambda m, p: {})
    assert dispatcher._timeout == DISPATCHER_TIMEOUT
    dispatcher.stop()
    with pytest.raises(NXToolError) as caught:
        dispatcher.call("nx_extrude", {})
    assert caught.value.details["mutation_outcome"] == "not_started"


# ---- 3. agent surface: unknown tool names ----
async def test_agent_invoke_unknown_tool_is_tool_not_found(tmp_path):
    bridge = AsyncMock()
    server = create_server(bridge, Workspace(tmp_path), surface="agent")
    error = payload(
        await server.call_tool(
            "nx_invoke", {"tool": "nx_list_objectz", "arguments": {"kind": "body"}}
        )
    )
    assert error["error"]["code"] == "NX_UNKNOWN_TOOL" and error["error"]["field"] == "/tool"
    assert error["mutation_outcome"] == "not_started"
    assert "nx_list_objects" in error["error"]["details"]["close_matches"]
    assert "nx_list_objects" in error["error"]["hint"]
    error = payload(await server.call_tool("nx_no_such_tool_at_all", {}))
    assert error["error"]["code"] == "NX_UNKNOWN_TOOL"
    bridge.call.assert_not_called()
