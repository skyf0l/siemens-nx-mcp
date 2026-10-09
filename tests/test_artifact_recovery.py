"""Sidecar artifacts: forwarded paths.

Workspace file tools are catalogue tools (tests/unit/parts_files/test_workspace.py);
receipts and cancellation are tests/unit/session/test_status.py.
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nx_mcp.server import create_server
from nx_mcp.workspace import Workspace


@pytest.mark.asyncio
async def test_mcp_paths_are_validated_before_bridge_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")
    bridge = AsyncMock()
    bridge.call.return_value = {"ok": True, "data": {}}
    server = create_server(bridge, Workspace(tmp_path), surface="full")
    await server.call_tool("nx_export_inspection_report", {"path": "p.zip"})
    assert Path(bridge.call.call_args.args[1]["path"]) == tmp_path / "p.zip"
