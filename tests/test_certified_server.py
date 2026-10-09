from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from nx_mcp.core import catalogue
from nx_mcp.runtime import NXToolError
from nx_mcp.server import create_server
from nx_mcp.sidecar import gateways
from nx_mcp.workspace import Workspace

pytestmark = pytest.mark.integration


class StubBridge:
    async def call(self, method: str, params: dict):
        return {
            "connected": True,
            "nx_version": "NX test",
            "bridge_protocol": 1,
            "active_part": None,
        }


class RecordingBridge:
    def __init__(self, response: dict):
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method: str, params: dict):
        self.calls.append((method, params))
        return self.response


@pytest.mark.asyncio
async def test_server_always_lists_every_tool_and_no_code_execution_tools():
    server = create_server(StubBridge(), surface="full")

    async with create_connected_server_and_client_session(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}

    assert names <= {*gateways.NAMES, *catalogue.load()}  # only gateways and catalogue tools
    assert not {"nx_run_journal", "nx_record_start", "nx_record_stop"} & names


@pytest.mark.asyncio
async def test_create_part_resolves_path_inside_workspace(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")  # untested catalogue tools are listed and run
    bridge = RecordingBridge(
        {
            "ok": True,
            "tool": "nx_create_part",
            "mutation_outcome": "committed",
            "data": {"part": {"id": "p:1", "kind": "part", "name": "bracket", "part": "p:1"}},
        }
    )
    server = create_server(bridge, Workspace(tmp_path), surface="full")

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "nx_create_part", {"path": "parts/bracket.prt", "units": "mm"}
        )

    assert result.isError is False
    [(method, params)] = bridge.calls
    assert method == "nx_create_part"
    assert params["path"] == str(tmp_path / "parts" / "bracket.prt")
    assert params["units"] == "mm"
    assert params["operation_id"].startswith("op_")


@pytest.mark.asyncio
async def test_file_tool_rejects_workspace_escape_before_bridge_call(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")
    bridge = RecordingBridge({})
    server = create_server(bridge, Workspace(tmp_path), surface="full")

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("nx_open_part", {"path": "../secret.prt"})

    assert result.isError is True
    assert "NX_PATH_OUTSIDE_WORKSPACE" in result.content[0].text
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_bridge_error_preserves_machine_readable_code(monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")

    class ErrorBridge:
        async def call(self, method: str, params: dict):
            raise NXToolError("NX_NO_WORK_PART", "No work part is open")

    server = create_server(ErrorBridge(), surface="full")

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("nx_save_part", {})

    assert result.isError is True
    assert "NX_NO_WORK_PART" in result.content[0].text


@pytest.mark.asyncio
async def test_tools_publish_strict_input_and_output_schemas(monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")
    server = create_server(StubBridge(), surface="full")

    async with create_connected_server_and_client_session(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert tools["nx_create_part"].inputSchema["properties"]["units"]["enum"] == ["mm", "in"]
    placement = tools["nx_create_sketch"].inputSchema["properties"]["placement"]
    assert {"principal", "on", "path"} <= {k for b in placement["anyOf"] for k in b["properties"]}
    assert tools["nx_create_extrude"].inputSchema["required"] == ["sketch", "end"]
    assert all(tool.inputSchema.get("additionalProperties") is False for tool in tools.values())


@pytest.mark.asyncio
async def test_experimental_file_tools_still_enforce_workspace_boundary(tmp_path: Path):
    bridge = RecordingBridge({})
    server = create_server(
        bridge,
        Workspace(tmp_path),
    )

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("nx_capture_view", {"path": "../screen.png"})

    assert result.isError is True
    assert "NX_PATH_OUTSIDE_WORKSPACE" in result.content[0].text
    assert bridge.calls == []
