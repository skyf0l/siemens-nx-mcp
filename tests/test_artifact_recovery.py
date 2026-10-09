"""Sidecar artifacts: receipts, cancellation and forwarded paths.

Workspace file tools are catalogue tools (tests/unit/parts_files/test_workspace.py).
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nx_mcp.integration_server import artifact_call
from nx_mcp.recovery import OperationStore
from nx_mcp.runtime import NXToolError
from nx_mcp.server import create_server
from nx_mcp.workspace import Workspace


def test_workspace_metadata_and_cancellation_are_scoped(tmp_path):
    w = Workspace(tmp_path)
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "sub").mkdir()
    store = OperationStore(tmp_path)
    store.put({"operation_id": "batch-operation", "state": "running", "method": "nx_batch"})
    assert artifact_call("nx_cancel_operation", {"operation_id": "batch-operation"}, w)[
        "cancellation_requested"
    ]
    assert store.path("batch-operation").with_suffix(".cancel").exists()
    with pytest.raises(NXToolError):
        artifact_call("nx_cancel_operation", {"operation_id": "not-seen"}, w)
    assert (
        artifact_call("nx_operation_status", {"operation_id": "batch-operation"}, w)["state"]
        == "running"
    )


@pytest.mark.asyncio
async def test_mcp_paths_are_validated_before_bridge_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")
    bridge = AsyncMock()
    bridge.call.return_value = {"ok": True, "data": {}}
    server = create_server(bridge, Workspace(tmp_path), surface="full")
    await server.call_tool("nx_export_inspection_report", {"path": "p.zip"})
    assert Path(bridge.call.call_args.args[1]["path"]) == tmp_path / "p.zip"


def test_workspace_info_names_the_root(tmp_path):
    w = Workspace(tmp_path)
    assert artifact_call("nx_workspace_info", {}, w)["root"] == str(tmp_path)
