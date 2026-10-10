import os
import time
from unittest.mock import AsyncMock

import pytest

from nx_mcp.kit.snapshots import maintain
from nx_mcp.server import create_server
from nx_mcp.sidecar.artifacts import ResultStore
from nx_mcp.workspace import Workspace


def test_retention_dry_run_protection_and_untouched_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_MCP_RESULT_MAX_BYTES", "1000")
    store = ResultStore(tmp_path)
    old = store.put({"old": "x" * 100})
    path = store.root / (old + ".json")
    os.utime(path, (time.time() - 100, time.time() - 100))
    unrelated = store.root / "not-a-snapshot.json"
    unrelated.write_text("keep")
    receipt = tmp_path / ".nx-mcp/operations/operation.json"
    receipt.parent.mkdir()
    receipt.write_text("keep")
    preview = maintain(store.root, max_age_seconds=10)
    assert preview["selected_count"] == 1 and path.exists()
    result = maintain(store.root, apply=True, max_age_seconds=10)
    assert result["applied"] and not path.exists()
    assert unrelated.read_text() == receipt.read_text() == "keep"
    current = store.put({"current": "x" * 100})
    result = maintain(store.root, apply=True, max_bytes=1, protect=current)
    assert result["selected_count"] == 0
    with pytest.raises(ValueError):
        maintain(store.root, max_bytes=0)
    with pytest.raises(OSError):
        store.put({"large": "x" * 2000})


@pytest.mark.asyncio
async def test_spaced_discovery_and_optional_output_schema(tmp_path):
    server = create_server(AsyncMock(), Workspace(tmp_path), surface="agent")
    result = await server.call_tool("nx_find_tools", {"query": "list   topology"})
    rows = result.structuredContent["data"]["tools"]
    assert rows[0]["name"] == "nx_list_objects"
    result = await server.call_tool("nx_get_tool_schemas", {"names": ["nx_list_objects"]})
    described = result.structuredContent["data"]["tools"][0]
    assert "input_schema" in described and "output_schema" not in described
    result = await server.call_tool(
        "nx_get_tool_schemas", {"names": ["nx_list_objects"], "include_output": True}
    )
    assert "output_schema" in result.structuredContent["data"]["tools"][0]
