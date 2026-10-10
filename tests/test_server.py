"""Tests for the MCP server entry point."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from nx_mcp.core import catalogue
from nx_mcp.server import create_server
from nx_mcp.sidecar import gateways
from tests.kit.bridges import RecordingBridge, StubBridge


def test_server_lists_every_tool_without_nx_imports(tmp_path):
    script = textwrap.dedent(
        """
        import asyncio
        import json
        import sys

        from nx_mcp.server import create_server

        async def main():
            server = create_server(surface="full")
            tools = [t.name for t in await server.list_tools()]
            forbidden = [name for name in ("NXOpen",) if name in sys.modules]
            print(json.dumps({"tools": tools, "forbidden": forbidden}))

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
    assert "nx_list_objects" in payload["tools"]
    assert set(payload["tools"]) <= {*gateways.NAMES, *catalogue.load()}
    assert payload["forbidden"] == []


@pytest.mark.asyncio
async def test_server_reads_workspace_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("NX_MCP_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("NX_MCP_DEV", "1")  # untested catalogue tools are listed and run
    bridge = RecordingBridge(
        {
            "ok": True,
            "tool": "nx_create_part",
            "mutation_outcome": "committed",
            "data": {"part": {"id": "p:1", "kind": "part", "name": "bracket", "part": "p:1"}},
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

    assert names <= {*gateways.NAMES, *catalogue.load()}
    assert not {"nx_run_journal", "nx_record_start", "nx_record_stop"} & names


def test_server_defaults_to_the_home_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from nx_mcp.sidecar import server as sidecar_server

    installed = []
    monkeypatch.setattr(
        sidecar_server,
        "install",
        lambda server, bridge, workspace, *a, **k: installed.append(workspace),
    )
    monkeypatch.delenv("NX_MCP_WORKSPACE")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    create_server(StubBridge(), surface="full")
    assert installed[0].root == (tmp_path / "NX_MCP_WORKSPACE").resolve()
