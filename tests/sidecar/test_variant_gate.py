"""Listings follow the connected NX build: untested variants and tools NX refused for a
missing license this session are left out; before NX connects the default build is
assumed and said so; a change of the listing sends tools/list_changed."""

import json
from unittest.mock import AsyncMock

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from nx_mcp.bridge import BridgeDescriptor
from nx_mcp.core import catalogue, evidence
from nx_mcp.server import create_server
from nx_mcp.sidecar import gateways
from nx_mcp.workspace import Workspace

TESTED = {
    "nx_create_datum_plane": {"v2406": {"variants": ["offset"]}},
    "nx_list_objects": {"v2406": {"variants": []}},
    "nx_create_blend": {"v2406": {"variants": []}},
}
OFFSET = {"method": "offset", "reference": "fc:7", "distance": "15 mm"}


@pytest.fixture
def tested(monkeypatch):
    monkeypatch.setattr(evidence, "_CACHE", TESTED)
    monkeypatch.delenv("NX_MCP_DEV", raising=False)


def body(result):
    return result.structuredContent


class StatusBridge:
    """A bridge with a descriptor whose nx_get_status reports remembered licenses."""

    def __init__(self, path, licenses=()):
        self.descriptor_path = path
        self.licenses = list(licenses)
        self.calls = []
        BridgeDescriptor.create(1234, "NX 2406.1700", token="t" * 64, pid=42).write(path)

    async def call(self, method, params):
        self.calls.append((method, params))
        if method == "nx_get_status":
            bridge = {"nx_version": "v2406", "license_unavailable": self.licenses}
            return {"ok": True, "tool": method, "data": {"bridge": bridge}}
        return {
            "ok": False,
            "tool": method,
            "mutation_outcome": "rolled_back",
            "error": {"category": "unavailable", "code": "NX_LICENSE_UNAVAILABLE"},
        }


def remembered(tool, variant=None):
    return {"tool": tool, "variant": variant, "nx_code": 948801, "since": "2026-10-09T10:00:00"}


async def test_listed_schema_offers_only_tested_variants(tmp_path, tested):
    server = create_server(AsyncMock(), Workspace(tmp_path), surface="full", build="v2406")
    async with create_connected_server_and_client_session(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    method = tools["nx_create_datum_plane"].inputSchema["properties"]["method"]
    assert json.dumps(method).count('"offset"') == 1 and "point_normal" not in json.dumps(method)
    agent = create_server(AsyncMock(), Workspace(tmp_path), surface="agent", build="v2406")
    data = body(await agent.call_tool("nx_get_tool_schemas", {"names": ["nx_create_datum_plane"]}))[
        "data"
    ]
    assert "point_normal" not in json.dumps(data["tools"][0]["input_schema"])


async def test_unavailable_variant_schema_says_why(tmp_path, tested):
    agent = create_server(AsyncMock(), Workspace(tmp_path), surface="agent", build="v2406")
    data = body(
        await agent.call_tool(
            "nx_get_tool_schemas",
            {"names": ["nx_create_datum_plane#point_normal", "nx_create_datum_plane#offset"]},
        )
    )["data"]
    [missing] = data["not_available"]
    assert missing["name"] == "nx_create_datum_plane#point_normal"
    assert missing["reason"].startswith("not available on this NX build")
    assert "'point_normal' is not tested on NX build v2406" in missing["reason"]
    only = body(
        await agent.call_tool(
            "nx_get_tool_schemas", {"names": ["nx_create_datum_plane#point_normal"]}
        )
    )
    assert only["error"]["code"] == "NX_VERSION_UNSUPPORTED"
    assert "not available on this NX build" in only["error"]["message"]


def test_search_matches_only_offered_variants():
    entry = gateways.Entry(
        "nx_create_thing", effect="model", domain="modeling", label="Thing", description="x"
    )
    offered = {"nx_create_thing": ("louver", "dimple")}
    found = gateways.search([entry], "dimple", variants=lambda e: offered[e.name])
    assert found == [entry]
    offered["nx_create_thing"] = ("louver",)
    assert gateways.search([entry], "dimple", variants=lambda e: offered[e.name]) == []


async def test_tool_without_an_offered_variant_is_hidden(tmp_path, tested, monkeypatch):
    agent = create_server(AsyncMock(), Workspace(tmp_path), surface="agent", build="v2406")

    async def names(query):
        rows = body(await agent.call_tool("nx_find_tools", {"query": query}))["data"]["tools"]
        return [r["name"] for r in rows]

    assert "nx_create_datum_plane" in await names("datum plane")
    monkeypatch.setitem(TESTED, "nx_create_datum_plane", {"v2406": {"variants": []}})
    assert "nx_create_datum_plane" not in await names("datum plane")


async def test_development_mode_offers_untested_variants_marked(tmp_path, tested, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")
    agent = create_server(AsyncMock(), Workspace(tmp_path), surface="agent", build="v2406")
    data = body(
        await agent.call_tool(
            "nx_get_tool_schemas",
            {"names": ["nx_create_datum_plane", "nx_create_datum_plane#point_normal"]},
        )
    )["data"]
    whole, variant = data["tools"]
    assert "point_normal" in json.dumps(whole["input_schema"])
    assert (
        "point_normal" in whole["untested_variants"] and "offset" not in whole["untested_variants"]
    )
    assert variant["input_schema"]["properties"]["method"]["const"] == "point_normal"


async def test_without_nx_the_default_build_is_assumed_and_said(tmp_path, tested):
    bridge = AsyncMock()
    bridge.descriptor_path = tmp_path / "missing.json"
    agent = create_server(bridge, Workspace(tmp_path), surface="agent")
    data = body(await agent.call_tool("nx_find_tools", {"query": "datum plane"}))["data"]
    assert data["listing"]["build"] == evidence.DEFAULT_BUILD and data["listing"]["assumed"]
    assert "not connected" in data["listing"]["note"]
    assert "nx_create_datum_plane" in {r["name"] for r in data["tools"]}


async def test_remembered_license_hides_and_refuses_before_nx(tmp_path, tested, monkeypatch):
    monkeypatch.setitem(TESTED, "nx_create_datum_plane", {"v2406": {"variants": ["offset"]}})
    bridge = StatusBridge(tmp_path / "bridge.json", [remembered("nx_create_blend")])
    agent = create_server(bridge, Workspace(tmp_path), surface="agent")
    rows = body(await agent.call_tool("nx_find_tools", {"query": "blend"}))["data"]["tools"]
    assert "nx_create_blend" not in {r["name"] for r in rows}
    refused = body(await agent.call_tool("nx_invoke", {"tool": "nx_create_blend", "arguments": {}}))
    assert refused["error"]["code"] == "NX_LICENSE_UNAVAILABLE"
    assert refused["mutation_outcome"] == "not_started"
    assert "remembered from 2026-10-09T10:00:00" in refused["error"]["message"]
    assert [c[0] for c in bridge.calls] == ["nx_get_status"]
    # A remembered variant leaves the enum; with no variant left the tool goes too.
    bridge.licenses.append(remembered("nx_create_datum_plane", "offset"))
    await agent.nx_surface.proxy.refresh(force=True)
    schemas = body(
        await agent.call_tool("nx_get_tool_schemas", {"names": ["nx_create_datum_plane#offset"]})
    )
    assert "no NX license on this seat" in schemas["error"]["message"]
    refused = body(
        await agent.call_tool("nx_invoke", {"tool": "nx_create_datum_plane", "arguments": OFFSET})
    )
    assert refused["error"]["code"] == "NX_LICENSE_UNAVAILABLE"


async def test_license_failure_refreshes_and_sends_list_changed(tmp_path, tested):
    bridge = StatusBridge(tmp_path / "bridge.json")
    server = create_server(bridge, Workspace(tmp_path), surface="full")
    notes = []

    async def handler(message):
        notes.append(getattr(getattr(message, "root", None), "method", None))

    async with create_connected_server_and_client_session(
        server, message_handler=handler
    ) as client:
        listed = {t.name for t in (await client.list_tools()).tools}
        assert "nx_create_blend" in listed
        bridge.licenses.append(remembered("nx_create_blend"))  # what NX now remembers
        failed = await client.call_tool("nx_create_blend", {"edges": ["e:1"], "radius": "1 mm"})
        assert failed.structuredContent["error"]["code"] == "NX_LICENSE_UNAVAILABLE"
        listed = {t.name for t in (await client.list_tools()).tools}
    assert "nx_create_blend" not in listed
    assert "notifications/tools/list_changed" in notes


def test_capabilities_announce_list_changed(tmp_path):
    server = create_server(AsyncMock(), Workspace(tmp_path), surface="agent", build="v2406")
    options = server._mcp_server.create_initialization_options()
    assert options.capabilities.tools.listChanged is True
    assert catalogue.get("nx_close_part").params["part"].required
