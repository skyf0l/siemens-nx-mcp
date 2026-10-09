"""Sidecar error codes must tell an agent whether a call may have run in NX."""

import asyncio
import types
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp import FastMCP

from nx_mcp import bridge as bridge_module
from nx_mcp.bridge import CLIENT_TIMEOUT, DISPATCHER_TIMEOUT, BridgeClient, MainThreadDispatcher
from nx_mcp.integration_server import configure, tool_definitions
from nx_mcp.runtime import NXToolError
from nx_mcp.server import create_server
from nx_mcp.workspace import Workspace


def payload(result):
    assert result.isError
    return result.structuredContent


# ---- 1. failures after forwarding are internal errors with unknown outcome ----
async def test_failure_after_bridge_success_is_internal_and_unknown(tmp_path):
    bridge = AsyncMock()
    bridge.call.return_value = None  # e.g. a malformed bridge result
    server = create_server(bridge, Workspace(tmp_path), surface="full")
    error = payload(
        await server.call_tool(
            "nx_thicken", {"faces": ["obj_f"], "first_offset": 5, "operation_id": "op_ext_1"}
        )
    )
    bridge.call.assert_awaited_once()
    assert error["code"] == "NX_INTERNAL_ERROR"
    assert error["retryable"] is False
    assert error["details"]["mutation_outcome"] == "unknown"
    assert error["details"]["operation_id"] == "op_ext_1"


async def test_capture_read_failure_after_success_is_not_an_argument_error(tmp_path):
    bridge = AsyncMock()
    # NX reports a committed capture whose file is missing from the workspace.
    bridge.call.return_value = {"status": "success", "path": "shot.png", "sha256": "0"}
    server = create_server(bridge, Workspace(tmp_path), surface="full")
    error = payload(await server.call_tool("nx_screenshot", {"path": "shot.png"}))
    assert error["code"] == "NX_INTERNAL_ERROR"
    assert error["details"]["mutation_outcome"] == "unknown"
    assert error["retryable"] is False


async def test_schema_validation_failure_is_not_started(tmp_path):
    bridge = AsyncMock()
    server = create_server(bridge, Workspace(tmp_path), surface="full")
    error = payload(await server.call_tool("nx_thicken", {"faces": "f", "first_offset": 1}))
    assert error["code"] == "NX_INVALID_ARGUMENT"
    assert error["details"]["mutation_outcome"] == "not_started"
    bridge.call.assert_not_called()


async def test_unknown_tool_on_full_surface_is_tool_not_found(tmp_path):
    server = create_server(AsyncMock(), Workspace(tmp_path), surface="full")
    error = payload(await server.call_tool("nx_thicke", {}))
    assert error["mutation_outcome"] == "not_started"
    assert error["error"]["code"] == "NX_UNKNOWN_TOOL"
    assert error["error"]["category"] == "not_found"
    assert "nx_thicken" in error["error"]["details"]["close_matches"]


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
    assert "nx_operation_status" in error.message


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


async def test_sidecar_merge_keeps_retryable_consistent_with_outcome(tmp_path):
    bridge = AsyncMock()
    server = create_server(bridge, Workspace(tmp_path), surface="full")
    args = {"faces": ["obj_f"], "first_offset": 5, "operation_id": "op_merge"}
    # No outcome from the transport: the sidecar marks it unknown, so never retryable.
    bridge.call.side_effect = NXToolError("NX_BRIDGE_UNAVAILABLE", "lost", retryable=True)
    error = payload(await server.call_tool("nx_thicken", args))
    assert error["details"]["mutation_outcome"] == "unknown"
    assert error["retryable"] is False
    # A provable not_started stays retryable.
    bridge.call.side_effect = NXToolError(
        "NX_BRIDGE_UNAVAILABLE",
        "down",
        retryable=True,
        details={"mutation_outcome": "not_started"},
    )
    error = payload(await server.call_tool("nx_thicken", args))
    assert error["details"]["mutation_outcome"] == "not_started"
    assert error["retryable"] is True


# ---- 3. agent surface: unknown tool names ----
async def test_agent_invoke_unknown_tool_is_tool_not_found(tmp_path):
    bridge = AsyncMock()
    server = create_server(bridge, Workspace(tmp_path), surface="agent")
    error = payload(
        await server.call_tool("nx_invoke", {"tool": "nx_thicke", "arguments": {"faces": []}})
    )
    assert error["error"]["code"] == "NX_UNKNOWN_TOOL" and error["error"]["field"] == "/tool"
    assert error["mutation_outcome"] == "not_started"
    assert "nx_thicken" in error["error"]["details"]["close_matches"]
    assert "nx_thicken" in error["error"]["hint"]
    error = payload(await server.call_tool("nx_no_such_tool_at_all", {}))
    assert error["error"]["code"] == "NX_UNKNOWN_TOOL"
    bridge.call.assert_not_called()


# ---- 6. one schema per tool ----
async def test_each_tool_has_one_definition_with_strict_arguments(tmp_path):
    server = create_server(AsyncMock(), Workspace(tmp_path), surface="full")
    tools = {t.name: t for t in await server.list_tools()}
    thicken = tools["nx_thicken"].inputSchema
    assert {"faces", "first_offset", "second_offset"} <= set(thicken["properties"])
    assert thicken["required"] == ["faces", "first_offset"]


def test_duplicate_tool_definitions_are_rejected(tmp_path):
    def nx_twice():
        pass

    def nx_twice_again():
        pass

    first = types.ModuleType("first")
    second = types.ModuleType("second")
    first.nx_twice, second.nx_twice = nx_twice, nx_twice_again
    with pytest.raises(RuntimeError, match="nx_twice is defined twice"):
        tool_definitions([first, second])
    # A re-export of the same function is one definition.
    second.nx_twice = nx_twice
    assert tool_definitions([first, second]) == {"nx_twice": nx_twice}

    mcp = FastMCP("dup")

    @mcp.tool()
    async def nx_thicken(faces: list[str]) -> dict:
        return {}

    with pytest.raises(RuntimeError, match="nx_thicken"):
        configure(mcp, AsyncMock(), Workspace(tmp_path))
