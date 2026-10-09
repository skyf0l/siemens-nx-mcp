"""Sidecar artifacts: receipts, cancellation, inline captures and forwarded paths.

Workspace file tools are catalogue tools (tests/unit/parts_files/test_workspace.py).
"""

import base64
import hashlib
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
async def test_mcp_paths_are_validated_before_bridge_dispatch(tmp_path):
    bridge = AsyncMock()
    bridge.call.return_value = {"status": "success"}
    server = create_server(bridge, Workspace(tmp_path), surface="full")
    await server.call_tool(
        "nx_batch",
        {"operations": [{"method": "nx_add_component", "params": {"part_path": "p.prt"}}]},
    )
    assert (
        Path(bridge.call.call_args.args[1]["operations"][0]["params"]["part_path"])
        == tmp_path / "p.prt"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["valid", "changed", "large"])
@pytest.mark.parametrize("method", ["nx_screenshot", "nx_render_view"])
async def test_inline_capture_delivery_checks_committed_artifact(tmp_path, kind, method):
    p = tmp_path / "capture.png"
    data = b"png" if kind != "large" else b"x" * (8 * 1024 * 1024 + 1)
    p.write_bytes(data)
    result = {
        "status": "success",
        "path": str(p),
        "sha256": hashlib.sha256(data).hexdigest() if kind != "changed" else "0" * 64,
    }
    bridge = AsyncMock()
    bridge.call.return_value = result
    server = create_server(bridge, Workspace(tmp_path), surface="full")
    response = await server.call_tool(method, {"path": "capture.png"})
    images = [v for v in response.content if v.type == "image"]
    if kind == "valid":
        assert base64.b64decode(images[0].data) == data
    elif kind == "large":
        assert not images and response.structuredContent["warnings"]
    else:
        assert (
            response.isError
            and response.structuredContent["details"]["mutation_outcome"] == "committed"
        )


def test_workspace_info_names_the_root(tmp_path):
    w = Workspace(tmp_path)
    assert artifact_call("nx_workspace_info", {}, w)["root"] == str(tmp_path)
