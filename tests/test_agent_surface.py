"""Agent-profile contracts across a real in-memory MCP boundary (no NX kernel)."""

from unittest.mock import AsyncMock

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from nx_mcp.core import catalogue
from nx_mcp.server import create_server
from nx_mcp.sidecar import gateways
from nx_mcp.sidecar.artifacts import ResultStore
from nx_mcp.sidecar.server import CORE_TOOLS
from nx_mcp.workspace import Workspace


def test_snapshot_persistence_and_path_rejection(tmp_path):
    store = ResultStore(tmp_path)
    identity = store.put({"answer": 42})
    assert ResultStore(tmp_path).get(identity) == {"answer": 42}
    with pytest.raises(ValueError):
        store.get("../../secret")


@pytest.mark.asyncio
async def test_discovery_lists_gateways_and_core_tools(tmp_path):
    bridge = AsyncMock()
    bridge.call.return_value = {
        "status": "success",
        "parts": [],
        "count": 0,
        "warnings": [],
        "units": "mm",
    }
    server = create_server(bridge, Workspace(tmp_path), surface="agent")
    async with create_connected_server_and_client_session(server) as client:
        tools = (await client.list_tools()).tools
        # Four gateways plus the core tools that exist in the catalogue.
        assert [t.name for t in tools][:4] == list(gateways.NAMES)
        listed = {t.name for t in tools}
        core = {
            n
            for n in CORE_TOOLS
            if catalogue.get(n) and catalogue.get(n).effect in ("read", "view")
        }  # an unknown build shows only read and view tools
        assert "nx_list_objects" in listed and listed == set(gateways.NAMES) | core
        result = await client.call_tool("nx_find_tools", {"query": "nx_list_objects"})
        assert result.structuredContent["data"]["tools"][0]["name"] == "nx_list_objects"
        result = await client.call_tool("nx_get_tool_schemas", {"names": ["nx_list_objects"]})
        schema = result.structuredContent["data"]["tools"][0]["input_schema"]
        assert "include" in schema["properties"]


@pytest.mark.asyncio
async def test_artifact_metadata_resource_and_reserved_path(tmp_path):
    (tmp_path / "test.txt").write_bytes(b"binary bytes")
    server = create_server(AsyncMock(), Workspace(tmp_path), surface="agent")
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("nx_download_file", {"path": "test.txt"})
        assert not result.isError and "data_base64" not in result.structuredContent
        link = next(c for c in result.content if c.type == "resource_link")
        artifact = await client.read_resource(link.uri)
        assert artifact.contents[0].blob
        assert len((await client.list_resource_templates()).resourceTemplates) == 1
        bad = await client.call_tool("nx_download_file", {"path": ".nx-mcp/secret"})
        assert bad.isError


@pytest.mark.asyncio
async def test_discovery_paging_and_argument_errors(tmp_path):
    server = create_server(AsyncMock(), Workspace(tmp_path), surface="agent")
    for args in [{"limit": 21}, {"offset": -1}, {"domain": "wrong"}, {"bogus": 1}]:
        bad = await server.call_tool("nx_find_tools", args)
        assert bad.isError and bad.structuredContent["error"]["code"] == "NX_INVALID_ARGUMENT"
    result = await server.call_tool(
        "nx_find_tools", {"limit": 2, "offset": 2, "domain": "inspection"}
    )
    assert len(result.structuredContent["data"]["tools"]) == 2
    assert result.structuredContent["page"]["next_offset"] == 4
    assert (await server.call_tool("nx_invoke", {"tool": "missing", "arguments": {}})).isError
    assert (await server.call_tool("missing", {})).isError


def test_profile_validation(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="NX_MCP_SURFACE"):
        create_server(AsyncMock(), Workspace(tmp_path), surface="typo")
    monkeypatch.delenv("NX_MCP_WORKSPACE")
    with pytest.raises(ValueError, match="NX_MCP_WORKSPACE"):
        create_server(AsyncMock(), surface="agent")


def test_dual_http_profiles(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from nx_mcp.http_surface import create_app

    app = create_app(AsyncMock(), Workspace(tmp_path))
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        # Without a bridge descriptor the build is unknown: only read/view catalogue tools show.
        readable = [s for s in catalogue.load().values() if s.effect in ("read", "view")]
        extra = len(gateways.NAMES) + len(readable)
        core = [n for n in CORE_TOOLS if n in {s.name for s in readable}]
        agent = len(gateways.NAMES) + len(core)
        for path, count in [("/mcp", extra), ("/agent/mcp", agent)]:
            response = client.post(
                path,
                headers={"Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            assert response.status_code == 200
            assert len(response.json()["result"]["tools"]) == count


@pytest.mark.asyncio
async def test_resource_traversal_and_size_limit(tmp_path):
    from mcp.server.fastmcp.exceptions import ResourceError

    server = create_server(AsyncMock(), Workspace(tmp_path), surface="agent")
    (tmp_path / ".nx-mcp").mkdir()
    (tmp_path / ".nx-mcp" / "secret").write_text("secret")
    large = tmp_path / "large.bin"
    with large.open("wb") as stream:
        stream.truncate(8 * 1024 * 1024 + 1)
    for uri in [
        "nx-artifact://workspace/.nx-mcp%2Fsecret",
        "nx-artifact://workspace/large.bin",
        "nx-artifact://workspace/..%2Fsecret",
    ]:
        with pytest.raises((ResourceError, ValueError)):
            await server.read_resource(uri)


@pytest.mark.asyncio
async def test_gateway_rejects_ignored_top_level_arguments(tmp_path):
    bridge = AsyncMock()
    server = create_server(bridge, Workspace(tmp_path), surface="agent")
    result = await server.call_tool(
        "nx_invoke",
        {
            "tool": "nx_list_bodies",
            "arguments": {},
            "operation_id": "wrong_place",
        },
    )
    assert result.isError and not bridge.call.called
