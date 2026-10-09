"""Committed results remain retrievable when their payload exceeds bridge framing."""

import pytest

from nx_mcp.bridge import BridgeClient, BridgeServer
from nx_mcp.domains.session.results import nx_get_result
from nx_mcp.result_transport import bound_result
from tests.unit.parts_files.support import side


def test_large_result_pages_preserve_requested_cardinality(tmp_path):
    full = {
        "status": "success",
        "operation_id": "large-operation",
        "mutation_outcome": "committed",
        "restore_id": "display_test",
        "objects": [{"id": str(i), "data": "x" * 2000} for i in range(2032)],
    }
    bounded = bound_result(full, tmp_path / ".nx-mcp" / "bridge-results")
    assert bounded["mutation_outcome"] == "committed" and bounded["restore_id"] == "display_test"
    key = bounded["full_result"]["id"]
    read = side(tmp_path, nx_get_result, result_id=key, field="/objects", offset=100, limit=20)
    assert read.ok and read.mutation_outcome == "not_applicable"
    page = read.envelope["page"]
    assert len(read.data["items"]) == 20 and page["next_offset"] == 120 and page["total"] == 2032
    assert read.data["items"][0]["id"] == "100"


@pytest.mark.asyncio
async def test_bridge_bounds_large_native_commit(tmp_path):
    calls = []

    def execute(method, params):
        calls.append(method)
        return {
            "status": "success",
            "mutation_outcome": "committed",
            "operation_id": "large-operation",
            "objects": ["x" * 2048] * 2032,
        }

    server = BridgeServer(execute, token="test", result_directory=tmp_path)
    server.start()
    try:
        client = BridgeClient("127.0.0.1", server.port, token="test")
        result = await client.call("mutate", {})
        assert (
            result["mutation_outcome"] == "committed"
            and result["full_result"]["tool"] == "nx_get_result"
        )
        assert calls == ["mutate"]
    finally:
        server.stop()
