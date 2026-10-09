import os
import time
from unittest.mock import AsyncMock

import pytest

from nx_mcp.agent_guidance import guidance
from nx_mcp.agent_surface import ResultStore, compact_payload
from nx_mcp.kit.snapshots import maintain
from nx_mcp.server import create_server
from nx_mcp.workspace import Workspace


@pytest.mark.asyncio
async def test_guidance_examples_match_real_input_schemas(tmp_path):
    from nx_mcp.agent_guidance import GUIDANCE

    server = create_server(AsyncMock(), Workspace(tmp_path), surface="full")
    for name, (_, _, example) in GUIDANCE.items():
        tool = server._tool_manager.get_tool(name)
        assert tool is not None
        if example is not None:
            tool.fn_metadata.arg_model.model_validate(example)
        result = guidance(tool)
        assert result["native_evidence"]["status"] in {"tested", "experimental", "unavailable"}
    assert (
        guidance(server._tool_manager.get_tool("nx_list_topology"))["effects"][
            "geometry_or_assembly"
        ]
        is False
    )


def test_next_actions_use_returned_ids_without_fabrication():
    ref = {"id": "body_live", "kind": "body", "part_id": "owner"}
    result = compact_payload(
        {"bodies": [ref], "warnings": ["review"], "mutation_outcome": "committed"},
        "result_id",
        "nx_extrude",
    )
    assert result["next_actions"][0]["arguments"] == {"body": "body_live"}
    assert result["warnings"] == ["review"]
    assert "next_actions" not in compact_payload({"status": "error"}, "result_id")


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


def test_backend_page_cardinality_is_not_silently_truncated():
    refs = [{"id": str(i), "kind": "part"} for i in range(38)]
    payload = compact_payload(
        {"parts": refs, "count": 38, "total_count": 38, "next_offset": None}, "result_id"
    )
    assert len(payload["parts"]) == payload["count"] == 38
    assert payload["next_offset"] is None
    assert "/parts" not in payload.get("omitted", {})
    assert len(compact_payload({"objects": refs}, "result_id")["objects"]) == 20


def test_next_action_does_not_repeat_the_current_inspection():
    from nx_mcp.agent_guidance import next_actions

    assert not next_actions("nx_list_topology", {"body": {"id": "body", "kind": "body"}})


@pytest.mark.asyncio
async def test_spaced_discovery_and_optional_output_schema(tmp_path):
    server = create_server(AsyncMock(), Workspace(tmp_path), surface="agent")
    result = await server.call_tool("nx_find_tools", {"query": "list topology"})
    rows = result.structuredContent["data"]["tools"]
    assert rows[0]["name"] == "nx_list_topology" and rows[0]["legacy"] is True
    result = await server.call_tool("nx_get_tool_schemas", {"names": ["nx_list_topology"]})
    described = result.structuredContent["data"]["tools"][0]
    assert "input_schema" in described and "output_schema" not in described
    result = await server.call_tool(
        "nx_get_tool_schemas", {"names": ["nx_list_topology"], "include_output": True}
    )
    assert "output_schema" in result.structuredContent["data"]["tools"][0]
