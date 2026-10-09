"""Generated MCP surface: profiles, gateways, catalogue proxy and legacy coexistence."""

import json
from unittest.mock import AsyncMock

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from nx_mcp.core import catalogue, evidence
from nx_mcp.core.catalogue import CatalogueError
from nx_mcp.runtime import NXToolError
from nx_mcp.server import create_server
from nx_mcp.sidecar import gateways
from nx_mcp.sidecar.proxy import CatalogueProxy, _walk_paths
from nx_mcp.workspace import Workspace

TESTED = {
    "nx_create_datum_plane": {"v2406": {"variants": ["offset"]}},
    "nx_list_objects": {"v2406": {"variants": []}},
    "nx_create_blend": {"v2406": {"variants": []}},
}


@pytest.fixture
def tested(monkeypatch):
    """Evidence: datum plane tested on v2406 for the offset variant only."""
    monkeypatch.setattr(evidence, "_CACHE", TESTED)
    monkeypatch.delenv("NX_MCP_DEV", raising=False)


def envelope(tool="nx_create_datum_plane", **extra):
    return {
        "ok": True,
        "tool": tool,
        "operation_id": "op_1",
        "mutation_outcome": "committed",
        "units": {"length": "mm", "angle": "deg"},
        "frame": "work_part_absolute",
        "data": {"feature": {"id": "f:2", "kind": "feature"}},
        "changes": {"created": [], "modified": [], "deleted": [], "references": "preserved"},
        "warnings": [],
        **extra,
    }


def server(tmp_path, bridge=None, surface="agent", build="v2406"):
    return create_server(bridge or AsyncMock(), Workspace(tmp_path), surface=surface, build=build)


def body(result):
    return result.structuredContent


OFFSET = {"method": "offset", "reference": "fc:7", "distance": "15 mm"}


# ---- profiles ---------------------------------------------------------------


async def test_agent_profile_lists_gateways_and_existing_core_tools(tmp_path, tested):
    async with create_connected_server_and_client_session(server(tmp_path)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert list(tools) == [*gateways.NAMES, "nx_list_objects"]
    assert tools["nx_invoke_read"].annotations.readOnlyHint is True
    assert tools["nx_invoke"].annotations.destructiveHint is True
    listed = tools["nx_list_objects"]
    assert listed.annotations.readOnlyHint and listed.annotations.idempotentHint
    assert listed.title == "List objects" and listed.description.startswith("List objects")
    assert listed.inputSchema["properties"]["limit"]["maximum"] == 200


async def test_full_profile_lists_every_tool_with_annotations_from_effect(tmp_path, tested):
    full = server(tmp_path, surface="full")
    tools = {t.name: t for t in await full.list_tools()}
    assert set(gateways.NAMES) | set(catalogue.load()) <= set(tools)
    assert {"nx_thicken", "nx_status", "nx_result", "nx_inspect"} <= set(tools)
    datum = tools["nx_create_datum_plane"]
    assert datum.annotations.readOnlyHint is False and datum.annotations.destructiveHint is True
    assert {"operation_id", "intent"} <= set(datum.inputSchema["properties"])


def test_profile_defaults_to_agent_and_rejects_typos(tmp_path, monkeypatch):
    monkeypatch.delenv("NX_MCP_SURFACE", raising=False)
    assert create_server(AsyncMock(), Workspace(tmp_path)).nx_surface.profile == "agent"
    monkeypatch.setenv("NX_MCP_SURFACE", "full")
    assert create_server(AsyncMock(), Workspace(tmp_path)).nx_surface.profile == "full"
    monkeypatch.setenv("NX_MCP_SURFACE", "everything")
    with pytest.raises(ValueError, match="NX_MCP_SURFACE"):
        create_server(AsyncMock(), Workspace(tmp_path))


def test_a_catalogue_name_colliding_with_a_legacy_tool_stops_the_sidecar(tmp_path, monkeypatch):
    clash = dict(catalogue.load())
    clash["nx_status"] = clash["nx_list_objects"]
    monkeypatch.setattr(catalogue, "load", lambda: clash)
    with pytest.raises(CatalogueError, match="nx_status"):
        server(tmp_path)


# ---- discovery --------------------------------------------------------------


async def test_find_ranks_catalogue_above_legacy_and_marks_legacy_rows(tmp_path, tested):
    agent = server(tmp_path)
    rows = body(await agent.call_tool("nx_find_tools", {"query": "list faces"}))["data"]["tools"]
    assert rows[0] == {
        "name": "nx_list_objects",
        "summary": "List objects",
        "effect": "read",
        "domain": "inspection",
    }
    assert all(r.get("legacy") for r in rows[1:])
    rows = body(await agent.call_tool("nx_find_tools", {"query": "fillet"}))["data"]["tools"]
    names = [r["name"] for r in rows]
    assert names[0] == "nx_create_blend"  # synonym fillet -> blend, catalogue above legacy
    exact = body(await agent.call_tool("nx_find_tools", {"query": "nx_thicken"}))["data"]
    assert exact["tools"][0]["name"] == "nx_thicken"
    none = body(await agent.call_tool("nx_find_tools", {"query": "fillet zebra"}))
    assert none["data"]["tools"] == [] and none["page"]["total"] == 0
    reads = body(await agent.call_tool("nx_find_tools", {"query": "list", "effect": "read"}))
    assert all(r["effect"] == "read" for r in reads["data"]["tools"])


async def test_untested_tools_are_hidden_and_refused_unless_in_development(
    tmp_path, tested, monkeypatch
):
    monkeypatch.setattr(evidence, "_CACHE", {"nx_list_objects": {"v2406": {}}})
    bridge = AsyncMock()
    agent = server(tmp_path, bridge)
    rows = body(await agent.call_tool("nx_find_tools", {"query": "datum plane"}))["data"]["tools"]
    assert "nx_create_datum_plane" not in {r["name"] for r in rows}
    refused = body(
        await agent.call_tool("nx_invoke", {"tool": "nx_create_datum_plane", "arguments": OFFSET})
    )
    assert refused["error"]["code"] == "NX_UNKNOWN_TOOL" and not bridge.call.called
    monkeypatch.setenv("NX_MCP_DEV", "1")
    rows = body(await agent.call_tool("nx_find_tools", {"query": "datum plane"}))["data"]["tools"]
    assert rows[0]["name"] == "nx_create_datum_plane" and rows[0]["tested"] is False


async def test_untested_variant_is_refused_before_nx(tmp_path, tested):
    bridge = AsyncMock()
    result = await server(tmp_path, bridge).call_tool(
        "nx_invoke",
        {
            "tool": "nx_create_datum_plane",
            "arguments": {"method": "point_normal", "origin": [0, 0, 0], "normal": [0, 0, 1]},
        },
    )
    error = body(result)
    assert error["error"]["code"] == "NX_VERSION_UNSUPPORTED"
    assert (
        error["error"]["category"] == "unavailable" and error["mutation_outcome"] == "not_started"
    )
    assert not bridge.call.called


async def test_schemas_for_tools_variants_and_legacy(tmp_path, tested):
    agent = server(tmp_path)
    data = body(
        await agent.call_tool(
            "nx_get_tool_schemas",
            {
                "names": [
                    "nx_create_datum_plane#offset",
                    "nx_add_component",
                    "nx_nope",
                    "nx_list_objects",
                ],
                "include_output": True,
            },
        )
    )["data"]
    datum, legacy, listing = data["tools"]
    assert datum["input_schema"]["properties"]["method"] == {"const": "offset"}
    assert datum["example"] == OFFSET and datum["evidence"] == {"v2406": ["offset"]}
    assert datum["requires"] == ["work_part", "no_active_sketch"]
    assert "operation_id" not in datum["input_schema"]["properties"]
    assert "data" in datum["output_schema"]["properties"]
    assert legacy["legacy"] is True and "part_path" in legacy["input_schema"]["properties"]
    assert legacy["prerequisites"] and legacy["example"]
    assert listing["evidence"] == {"v2406": True}
    assert [m["name"] for m in data["not_found"]] == ["nx_nope"]
    wrong = body(
        await agent.call_tool("nx_get_tool_schemas", {"names": ["nx_create_datum_plane#zig"]})
    )
    assert wrong["error"]["details"]["not_found"][0]["close_matches"][0].endswith("#offset")
    bad = body(await agent.call_tool("nx_get_tool_schemas", {"names": []}))
    assert bad["error"]["code"] == "NX_INVALID_ARGUMENT" and bad["error"]["field"] == "/names"


# ---- catalogue calls ----------------------------------------------------------


async def test_invoke_forwards_validated_arguments_with_intent_and_operation_id(tmp_path, tested):
    bridge = AsyncMock()
    bridge.call.return_value = envelope()
    result = await server(tmp_path, bridge).call_tool(
        "nx_invoke",
        {
            "tool": "nx_create_datum_plane",
            "arguments": {**OFFSET, "operation_id": "op_retry"},
            "intent": "Add  the mounting\nplane",
        },
    )
    assert not result.isError and body(result)["data"]["feature"]["id"] == "f:2"
    method, params = bridge.call.call_args.args
    assert method == "nx_create_datum_plane"
    assert params == {**OFFSET, "operation_id": "op_retry", "intent": "Add the mounting plane"}
    assert json.loads(result.content[0].text) == body(result)


async def test_mutation_without_operation_id_gets_one_before_sending(tmp_path, tested):
    bridge = AsyncMock()
    bridge.call.return_value = envelope()
    await server(tmp_path, bridge).call_tool(
        "nx_invoke", {"tool": "nx_create_datum_plane", "arguments": OFFSET}
    )
    assert bridge.call.call_args.args[1]["operation_id"].startswith("op_")


@pytest.mark.parametrize(
    "arguments, field",
    [
        ({**OFFSET, "distance": "15 deg"}, "/distance"),
        ({"method": "three_points", "points": [[0, 0, 0], [1, 1]]}, "/points/1"),
        ({**OFFSET, "bogus": 1}, "/bogus"),
        ({**OFFSET, "reference": "Face 7"}, "/reference"),
    ],
)
async def test_invalid_arguments_never_reach_nx(tmp_path, tested, arguments, field):
    bridge = AsyncMock()
    result = await server(tmp_path, bridge).call_tool(
        "nx_invoke", {"tool": "nx_create_datum_plane", "arguments": arguments}
    )
    error = body(result)
    assert result.isError and not bridge.call.called
    assert error["mutation_outcome"] == "not_started"
    assert error["error"]["category"] == "invalid_argument"
    assert error["error"]["field"].startswith(field)
    assert error["error"]["retry"] == "after_fix"


async def test_kernel_error_envelope_is_returned_as_is(tmp_path, tested):
    kernel = {
        "ok": False,
        "tool": "nx_create_datum_plane",
        "operation_id": "op_9",
        "mutation_outcome": "rolled_back",
        "error": {
            "category": "verification_failed",
            "code": "NX_VERIFICATION_FAILED",
            "message": "offset mismatch",
            "hint": "report it",
            "field": None,
            "retry": "ask_engineer",
            "nx_code": 670015,
            "details": {"check": "offset"},
        },
        "changes": {"references": "preserved"},
    }
    bridge = AsyncMock()
    bridge.call.side_effect = NXToolError(
        "NX_VERIFICATION_FAILED",
        "offset mismatch",
        details={"mutation_outcome": "rolled_back", "envelope": kernel},
    )
    result = await server(tmp_path, bridge).call_tool(
        "nx_invoke", {"tool": "nx_create_datum_plane", "arguments": OFFSET}
    )
    assert result.isError and body(result) == kernel


@pytest.mark.parametrize(
    "error, outcome, code, retry",
    [
        # Forwarded, no outcome stated: unknown, never not_started.
        (NXToolError("NX_BRIDGE_OUTCOME_UNKNOWN", "lost"), "unknown", "NX_OUTCOME_UNKNOWN",
         "after_status_check"),
        (NXToolError("NX_MAIN_THREAD_UNAVAILABLE", "busy", details={"mutation_outcome": "unknown"}),
         "unknown", "NX_BRIDGE_UNAVAILABLE", "after_status_check"),
        # NX says it never started: stays not_started and retryable after a wait.
        (NXToolError("NX_BRIDGE_UNAVAILABLE", "down", details={"mutation_outcome": "not_started"}),
         "not_started", "NX_BRIDGE_UNAVAILABLE", "after_wait"),
        (NXToolError("NX_UI_DECLINED", "Not now", details={"mutation_outcome": "not_started"}),
         "not_started", "NX_CONTROL_DECLINED", "ask_engineer"),
        (NXToolError("NX_API_ERROR", "native", nx_code=1234), "unknown", "NX_ERROR",
         "after_status_check"),
    ],
)  # fmt: skip
async def test_bridge_errors_map_to_envelopes(tmp_path, tested, error, outcome, code, retry):
    bridge = AsyncMock()
    bridge.call.side_effect = error
    result = await server(tmp_path, bridge).call_tool(
        "nx_invoke", {"tool": "nx_create_datum_plane", "arguments": OFFSET}
    )
    failure = body(result)
    assert failure["mutation_outcome"] == outcome
    assert failure["error"]["code"] == code and failure["error"]["retry"] == retry
    assert failure["operation_id"].startswith("op_")
    if error.nx_code:
        assert failure["error"]["nx_code"] == 1234
    if code != error.code:
        assert failure["error"]["details"]["legacy_code"] == error.code


async def test_read_tool_failures_are_not_applicable(tmp_path, tested):
    bridge = AsyncMock()
    bridge.call.side_effect = NXToolError("NX_BRIDGE_OUTCOME_UNKNOWN", "lost")
    result = await server(tmp_path, bridge).call_tool("nx_list_objects", {"kind": "body"})
    assert body(result)["mutation_outcome"] == "not_applicable"
    bridge.call.side_effect = None
    bridge.call.return_value = {"status": "success"}  # not an envelope
    result = await server(tmp_path, bridge).call_tool("nx_list_objects", {"kind": "body"})
    assert body(result)["error"]["code"] == "NX_PROTOCOL_MISMATCH"


async def test_sidecar_fault_after_forwarding_is_unknown(tmp_path, tested):
    bridge = AsyncMock()
    bridge.call.side_effect = RuntimeError("socket exploded")
    failure = body(
        await server(tmp_path, bridge).call_tool(
            "nx_invoke", {"tool": "nx_create_datum_plane", "arguments": OFFSET}
        )
    )
    assert failure["mutation_outcome"] == "unknown"
    assert failure["error"]["retry"] == "after_status_check"


async def test_invoke_read_refuses_other_effects_before_validation(tmp_path, tested):
    bridge = AsyncMock()
    agent = server(tmp_path, bridge)
    for tool in ("nx_create_datum_plane", "nx_thicken"):
        failure = body(
            await agent.call_tool("nx_invoke_read", {"tool": tool, "arguments": {"bogus": 1}})
        )
        assert failure["error"]["code"] == "NX_UNSUPPORTED_OPTION"
        assert failure["error"]["field"] == "/tool" and "nx_invoke" in failure["error"]["hint"]
    bridge.call.return_value = envelope("nx_list_objects", mutation_outcome="not_applicable")
    ok = await agent.call_tool(
        "nx_invoke_read", {"tool": "nx_list_objects", "arguments": {"kind": "body"}}
    )
    assert not ok.isError
    bridge.call.return_value = {"status": "success", "connected": True}
    assert not (await agent.call_tool("nx_invoke_read", {"tool": "nx_status"})).isError
    nested = body(await agent.call_tool("nx_invoke", {"tool": "nx_invoke", "arguments": {}}))
    assert nested["error"]["code"] == "NX_UNSUPPORTED_OPTION"


async def test_compact_detail_moves_long_arrays_to_the_result_store(tmp_path, tested):
    bridge = AsyncMock()
    rows = [{"id": f"b:{i + 1}", "kind": "body"} for i in range(30)]
    bridge.call.return_value = envelope(
        "nx_list_objects",
        mutation_outcome="not_applicable",
        data={"kind": "body", "items": rows, "extra": list(range(25))},
        page={"offset": 0, "limit": 30, "returned": 30, "total": 30, "next_offset": None},
    )
    agent = server(tmp_path, bridge)
    compact = body(await agent.call_tool("nx_list_objects", {"kind": "body", "limit": 30}))
    assert len(compact["data"]["items"]) == 30  # a requested page is never cut
    assert len(compact["data"]["extra"]) == 20
    assert compact["omitted"] == {"/data/extra": {"total": 25, "returned": 20}}
    expanded = body(
        await agent.call_tool(
            "nx_invoke_read",
            {
                "tool": "nx_result",
                "arguments": {"result_id": compact["result_id"], "field": "/data/extra"},
            },
        )
    )
    assert expanded["total_count"] == 25
    full = body(
        await agent.call_tool(
            "nx_invoke_read",
            {"tool": "nx_list_objects", "arguments": {"kind": "body"}, "detail": "full"},
        )
    )
    assert "result_id" not in full and len(full["data"]["extra"]) == 25


def test_paths_are_resolved_inside_the_workspace(tmp_path):
    proxy = CatalogueProxy(AsyncMock(), Workspace(tmp_path), store=None)

    class Spec:
        paths = (("/path", "new", ()), ("/items/*/path", "read", ()))

    args = {"path": "out/a.step", "items": [{"path": "x.prt"}, {"path": "y.prt"}]}
    assert proxy._resolve_paths(Spec, args) is None
    assert args["path"] == str(tmp_path / "out" / "a.step")
    assert args["items"][1]["path"] == str(tmp_path / "y.prt")
    bad = proxy._resolve_paths(Spec, {"path": "../escape.step"})
    assert bad.code == "NX_PATH_OUTSIDE_WORKSPACE" and bad.field == "/path"
    reserved = proxy._resolve_paths(Spec, {"path": ".nx-mcp/receipts.json"})
    assert reserved.code in ("NX_PATH_RESERVED", None) or reserved is None
    assert _walk_paths({"items": []}, "/items/*/path") == []


# ---- legacy coexistence -----------------------------------------------------------


async def test_legacy_tools_stay_callable_in_both_profiles(tmp_path, tested):
    for surface in ("agent", "full"):
        bridge = AsyncMock()
        bridge.call.return_value = {"status": "success", "object": {"id": "obj_x", "kind": "body"}}
        legacy = server(tmp_path, bridge, surface=surface)
        direct = await legacy.call_tool("nx_thicken", {"faces": ["obj_s"], "first_offset": 5})
        via = await legacy.call_tool(
            "nx_invoke",
            {"tool": "nx_thicken", "arguments": {"faces": ["obj_s"], "first_offset": 5},
             "intent": "Pad the base"},
        )  # fmt: skip
        assert not direct.isError and not via.isError
        assert bridge.call.call_args.args[1]["intent"] == "Pad the base"


async def test_agent_presentation_never_relabels_a_finished_call(tmp_path, tested):
    bridge = AsyncMock()
    # A committed file write whose reported path cannot be linked as an artifact.
    bridge.call.return_value = {
        "status": "success",
        "path": "../outside.json",
        "sha256": "0" * 64,
        "size": 5,
        "mutation_outcome": "committed",
    }
    result = await server(tmp_path, bridge).call_tool(
        "nx_invoke", {"tool": "nx_save_presentation", "arguments": {"path": "a.json"}}
    )
    assert not result.isError and body(result)["mutation_outcome"] == "committed"


async def test_a_long_call_outlasting_the_bridge_wait_says_how_to_check(tmp_path, tested):
    bridge = AsyncMock()
    bridge.call.side_effect = NXToolError(
        "NX_BRIDGE_OUTCOME_UNKNOWN",
        "no answer within the wait",
        details={"operation_id": "op_long", "mutation_outcome": "unknown"},
    )
    failure = body(
        await server(tmp_path, bridge).call_tool(
            "nx_invoke",
            {"tool": "nx_create_datum_plane", "arguments": {**OFFSET, "operation_id": "op_long"}},
        )
    )
    assert failure["mutation_outcome"] == "unknown" and failure["operation_id"] == "op_long"
    assert failure["error"]["retry"] == "after_status_check"
    assert (
        "nx_operation_status" in failure["error"]["hint"] and "op_long" in failure["error"]["hint"]
    )


async def test_part_not_granted_keeps_its_category_and_hint(tmp_path, tested):
    bridge = AsyncMock()
    bridge.call.side_effect = NXToolError(
        "NX_PART_NOT_GRANTED",
        "The work part belongs to the engineer",
        suggestion="Ask the engineer to grant it on the NX MCP panel, or work on a copy",
        details={"mutation_outcome": "not_started"},
    )
    failure = body(
        await server(tmp_path, bridge).call_tool(
            "nx_invoke", {"tool": "nx_create_datum_plane", "arguments": OFFSET}
        )
    )
    assert failure["mutation_outcome"] == "not_started"
    assert failure["error"]["category"] == "engineer_control"
    assert failure["error"]["hint"].startswith("Ask the engineer to grant it")


def test_output_schema_allows_restore_ids_and_sheet_units():
    from nx_mcp.sidecar import schema

    out = schema.output_schema(catalogue.get("nx_list_objects"))["properties"]
    assert "restore_id" in out and "sheet_length" in out["units"]["properties"]
