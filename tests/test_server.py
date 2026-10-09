"""Tests for the MCP server entry point."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from nx_mcp.agent_guidance import MANIFEST
from nx_mcp.certified import CERTIFIED_TOOL_NAMES
from nx_mcp.server import create_server

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


def test_server_lists_every_tool_without_nx_imports(tmp_path):
    script = textwrap.dedent(
        """
        import asyncio
        import json
        import sys

        from nx_mcp.server import create_server

        async def main():
            server = create_server(surface="full")
            added = server.nx_surface.added_names()
            tools = [t for t in await server.list_tools() if t.name not in added]
            forbidden = [name for name in ("NXOpen",) if name in sys.modules]
            print(json.dumps({"tools": [tool.name for tool in tools], "forbidden": forbidden}))

        asyncio.run(main())
        """
    )
    environment = os.environ | {"NX_MCP_WORKSPACE": str(tmp_path)}

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert (
        len(payload["tools"]) == len(MANIFEST["tools"])
        and set(payload["tools"]) > CERTIFIED_TOOL_NAMES
    )
    assert payload["forbidden"] == []


@pytest.mark.asyncio
async def test_server_reads_workspace_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("NX_MCP_WORKSPACE", str(tmp_path))
    bridge = RecordingBridge(
        {
            "part": {"id": "part-1", "kind": "part", "name": "bracket", "part_id": "part-1"},
            "message": "created",
        }
    )
    server = create_server(bridge, surface="full")

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "nx_create_part", {"path": "parts/bracket.prt", "units": "mm"}
        )

    assert result.isError is False
    [(method, params)] = bridge.calls
    assert method == "nx_create_part"
    assert params["path"] == str(tmp_path / "parts" / "bracket.prt")


@pytest.mark.asyncio
async def test_removed_profile_flags_have_no_effect(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NX_MCP_ENABLE_EXPERIMENTAL", "0")
    monkeypatch.setenv("NX_MCP_ENABLE_JOURNAL", "1")
    server = create_server(StubBridge(), surface="full")

    async with create_connected_server_and_client_session(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}

    names -= server.nx_surface.added_names()
    assert names == set(MANIFEST["tools"]) and names > CERTIFIED_TOOL_NAMES
    assert not {"nx_run_journal", "nx_record_start", "nx_record_stop"} & names


def test_server_requires_a_workspace(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("NX_MCP_WORKSPACE")
    with pytest.raises(ValueError, match="NX_MCP_WORKSPACE"):
        create_server(StubBridge(), surface="full")
