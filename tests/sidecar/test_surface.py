"""Generated MCP surface: profiles, gateways and catalogue proxy."""

import json
import time
from typing import Annotated, TypedDict
from unittest.mock import AsyncMock

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from nx_mcp.bridge import QUEUE_TIMEOUT
from nx_mcp.core import catalogue, evidence
from nx_mcp.core.errors import NXError
from nx_mcp.core.types import PartRef, PathTo
from nx_mcp.server import create_server
from nx_mcp.sidecar import gateways
from nx_mcp.sidecar.proxy import CatalogueProxy, _walk_paths
from nx_mcp.workspace import Workspace

TESTED = {
    "nx_create_datum_plane": {"v2406": {"variants": ["offset"]}},
    "nx_list_objects": {"v2406": {"variants": []}},
    "nx_create_blend": {"v2406": {"variants": []}},
    "nx_get_result": {"v2406": {"variants": []}},
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
    assert list(tools) == [*gateways.NAMES, "nx_get_result", "nx_list_objects"]
    assert tools["nx_invoke_read"].annotations.readOnlyHint is True
    # destructiveHint true is MCP's default for a tool that is not read-only: left out.
    assert tools["nx_invoke"].annotations.readOnlyHint is None
    assert tools["nx_invoke"].annotations.destructiveHint is None
    listed = tools["nx_list_objects"]
    assert listed.annotations.readOnlyHint and listed.annotations.idempotentHint
    assert listed.title == "List objects" and listed.description.startswith(
        "List work-part objects"
    )
    assert listed.inputSchema["properties"]["limit"]["maximum"] == 200


async def test_nx_set_control_is_gone_and_the_agent_profile_has_18_tools(tmp_path, monkeypatch):
    """Only the engineer changes the panel mode: no tool does, nx_get_status reports it."""
    from nx_mcp.sidecar import server as surface

    monkeypatch.delenv("NX_MCP_DEV", raising=False)
    evidence.reload()
    assert "nx_set_control" not in catalogue.load()
    assert "nx_set_control" not in surface.CORE_TOOLS and len(surface.CORE_TOOLS) == 14
    names = [t.name for t in await server(tmp_path).list_tools()]
    assert names == [*gateways.NAMES, *surface.CORE_TOOLS] and len(names) == 18
    full = {t.name for t in await server(tmp_path, surface="full").list_tools()}
    assert "nx_set_control" not in full and "nx_get_status" in full


async def test_full_profile_lists_every_tool_with_annotations_from_effect(tmp_path, monkeypatch):
    """Derived from the real evidence files and catalogue: the full profile lists the
    gateways and every catalogue tool tested on the build."""
    monkeypatch.delenv("NX_MCP_DEV", raising=False)
    evidence.reload()
    full = server(tmp_path, surface="full")
    tools = {t.name: t for t in await full.list_tools()}
    tested = {n for n, s in catalogue.load().items() if evidence.visible(s, "v2406")}
    untested = set(catalogue.load()) - tested
    assert "nx_create_datum_plane" in tested  # the real evidence is loaded
    assert set(gateways.NAMES) | tested == set(tools)
    assert not untested & set(tools)  # decision 11: untested tools are hidden
    assert not {"nx_result", "nx_inspect", "nx_result_cleanup", "nx_read_result"} & set(tools)
    datum = tools["nx_create_datum_plane"]
    # Both are MCP's defaults (not read-only, destructive), so the listing leaves them out.
    assert datum.annotations.readOnlyHint is None and datum.annotations.destructiveHint is None
    assert {"operation_id", "intent"} <= set(datum.inputSchema["properties"])


def test_profile_defaults_to_agent_and_rejects_typos(tmp_path, monkeypatch):
    monkeypatch.delenv("NX_MCP_SURFACE", raising=False)
    assert create_server(AsyncMock(), Workspace(tmp_path)).nx_surface.profile == "agent"
    monkeypatch.setenv("NX_MCP_SURFACE", "full")
    assert create_server(AsyncMock(), Workspace(tmp_path)).nx_surface.profile == "full"
    monkeypatch.setenv("NX_MCP_SURFACE", "everything")
    with pytest.raises(ValueError, match="NX_MCP_SURFACE"):
        create_server(AsyncMock(), Workspace(tmp_path))


# ---- discovery --------------------------------------------------------------


async def test_find_ranks_by_name_keywords_and_synonyms(tmp_path, tested):
    agent = server(tmp_path)
    rows = body(await agent.call_tool("nx_find_tools", {"query": "list faces"}))["data"]["tools"]
    assert rows[0] == {
        "name": "nx_list_objects",
        "summary": "List objects",
        "effect": "read",
        "domain": "inspection",
        "core": True,  # also a direct MCP tool in the agent profile
    }
    rows = body(await agent.call_tool("nx_find_tools", {"query": "fillet"}))["data"]["tools"]
    names = [r["name"] for r in rows]
    assert names[0] == "nx_create_blend"  # synonym fillet -> blend
    exact = body(await agent.call_tool("nx_find_tools", {"query": "nx_list_objects"}))["data"]
    assert exact["tools"][0]["name"] == "nx_list_objects"
    partial = body(await agent.call_tool("nx_find_tools", {"query": "fillet zebra"}))
    assert partial["data"]["tools"][0]["name"] == "nx_create_blend"
    assert partial["data"]["partial"] is True  # no tool matched every word
    none = body(await agent.call_tool("nx_find_tools", {"query": "zebra quokka"}))
    assert none["data"]["tools"] == [] and none["page"]["total"] == 0
    assert none["data"]["domains"] and "No tool matches" in none["data"]["hint"]  # never bare
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


async def test_schemas_for_tools_and_variants(tmp_path, tested):
    agent = server(tmp_path)
    data = body(
        await agent.call_tool(
            "nx_get_tool_schemas",
            {
                "names": [
                    "nx_create_datum_plane#offset",
                    "nx_nope",
                    "nx_list_objects",
                ],
                "include_output": True,
            },
        )
    )["data"]
    datum, listing = data["tools"]
    assert datum["input_schema"]["properties"]["method"] == {"const": "offset"}
    assert datum["example"] == OFFSET  # tested on the connected build: no marker
    assert "evidence" not in datum and "tested" not in datum
    assert datum["requires"] == ["work_part", "no_active_sketch"]
    assert "operation_id" not in datum["input_schema"]["properties"]
    assert "data" in datum["output_schema"]["properties"]
    assert "evidence" not in listing and "tested" not in listing
    assert [m["name"] for m in data["not_found"]] == ["nx_nope"]
    wrong = body(
        await agent.call_tool("nx_get_tool_schemas", {"names": ["nx_create_datum_plane#zig"]})
    )
    assert wrong["error"]["details"]["not_found"][0]["close_matches"][0].endswith("#offset")
    bad = body(await agent.call_tool("nx_get_tool_schemas", {"names": []}))
    assert bad["error"]["code"] == "NX_INVALID_ARGUMENT" and bad["error"]["field"] == "/names"


def _variant_schema(entry: dict, variant: str) -> dict:
    """tool#variant's schema from a grouped entry: the shared part plus the variant's own."""
    own = entry["variants"][variant]["input_schema"]
    merged = {**entry["input_schema"], **{k: v for k, v in own.items() if k != "required"}}
    merged["properties"] = {**entry["input_schema"]["properties"], **own.get("properties", {})}
    required = [*entry["input_schema"].get("required", []), *own.get("required", [])]
    if required:
        merged["required"] = required
    return merged


@pytest.mark.parametrize(
    ("tool", "variants"),
    [
        ("nx_create_drawing_view", ("section", "detail", "base")),
        ("nx_create_sketch_curves", ("line", "arc")),  # x-variant forms
        ("nx_create_pattern", ("linear", "circular")),  # ByVariant parameters
    ],
)
async def test_variants_of_one_tool_share_their_schema_once(tmp_path, every_tool, tool, variants):
    agent = server(tmp_path)

    async def fetch(names):
        result = await agent.call_tool("nx_get_tool_schemas", {"names": names})
        return result, body(result)["data"]["tools"]

    singles = {}
    separate = 0
    for v in variants:  # one variant per call: self-contained, as before
        result, (single,) = await fetch([f"{tool}#{v}"])
        assert single["name"] == f"{tool}#{v}" and "variants" not in single
        singles[v] = single
        separate += len(result.content[0].text)
    names = [f"{tool}#{v}" for v in variants]
    _, entries = await fetch([names[0], "nx_list_objects", *names[1:], names[0]])
    assert [e["name"] for e in entries] == [tool, "nx_list_objects"]
    grouped = entries[0]
    assert list(grouped["variants"]) == list(variants)
    for v, single in singles.items():
        merged = _variant_schema(grouped, v)
        expected = single["input_schema"]
        assert sorted(merged.pop("required")) == sorted(expected.pop("required"))
        assert merged == expected, v
        own = grouped["variants"][v]
        for key, value in single.items():  # every other key is shared or the variant's own
            if key not in ("name", "input_schema"):
                assert grouped.get(key, own.get(key)) == value, (v, key)
    assert grouped["description"] == singles[variants[0]]["description"]
    assert "example" not in grouped and "evidence" not in grouped
    alone, _ = await fetch(names)
    assert len(alone.content[0].text) < separate


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
    expires = params.pop("expires_at")  # the ghost guard's start deadline (bridge.py)
    assert abs(expires - (time.time() + QUEUE_TIMEOUT)) < 5
    assert params.pop("caller")["id"].startswith("sc-")  # who sends it (core/busy.py)
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
    bridge.call.side_effect = NXError(
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
        (NXError("NX_OUTCOME_UNKNOWN", "lost"), "unknown", "NX_OUTCOME_UNKNOWN",
         "after_status_check"),
        (NXError("NX_BRIDGE_UNAVAILABLE", "busy", details={"mutation_outcome": "unknown"}),
         "unknown", "NX_BRIDGE_UNAVAILABLE", "after_status_check"),
        # NX says it never started: stays not_started and retryable after a wait.
        (NXError("NX_BRIDGE_UNAVAILABLE", "down", details={"mutation_outcome": "not_started"}),
         "not_started", "NX_BRIDGE_UNAVAILABLE", "after_wait"),
        (NXError("NX_CONTROL_DECLINED", "Not now", details={"mutation_outcome": "not_started"}),
         "not_started", "NX_CONTROL_DECLINED", "ask_engineer"),
        (NXError("NX_ERROR", "native", nx_code=1234), "unknown", "NX_ERROR",
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
    assert failure["error"]["code"] == error.code
    assert "mutation_outcome" not in failure["error"]["details"]  # stated once, at the top


async def test_read_tool_failures_are_not_applicable(tmp_path, tested):
    bridge = AsyncMock()
    bridge.call.side_effect = NXError("NX_OUTCOME_UNKNOWN", "lost")
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
    for tool in ("nx_create_datum_plane",):
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
    assert compact["expand_with"] == "nx_get_result"
    expanded = body(
        await agent.call_tool(
            "nx_invoke_read",
            {
                "tool": "nx_get_result",
                "arguments": {"result_id": compact["result_id"], "field": "/data/extra"},
            },
        )
    )
    assert expanded["page"]["total"] == 25 and expanded["data"]["items"][:2] == [0, 1]
    assert bridge.call.await_count == 1  # expanding never reaches NX
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


async def test_agent_presentation_never_relabels_a_finished_call(tmp_path, tested, monkeypatch):
    bridge = AsyncMock()
    # A committed file write whose reported path cannot be linked as an artifact.
    data = {"path": "../outside.json", "sha256": "0" * 64, "size": 5, "objects": 1, "sections": 0}
    bridge.call.return_value = envelope("nx_save_presentation", data=data)
    monkeypatch.setitem(TESTED, "nx_save_presentation", {"v2406": {"variants": []}})
    result = await server(tmp_path, bridge).call_tool(
        "nx_invoke", {"tool": "nx_save_presentation", "arguments": {"path": "a.json"}}
    )
    assert not result.isError and body(result)["mutation_outcome"] == "committed"


async def test_a_long_call_outlasting_the_bridge_wait_says_how_to_check(tmp_path, tested):
    bridge = AsyncMock()
    bridge.call.side_effect = NXError(
        "NX_OUTCOME_UNKNOWN",
        "no answer within the wait",
        details={"operation_id": "op_long_call", "mutation_outcome": "unknown"},
    )
    failure = body(
        await server(tmp_path, bridge).call_tool(
            "nx_invoke",
            {
                "tool": "nx_create_datum_plane",
                "arguments": {**OFFSET, "operation_id": "op_long_call"},
            },
        )
    )
    assert failure["mutation_outcome"] == "unknown" and failure["operation_id"] == "op_long_call"
    assert failure["error"]["retry"] == "after_status_check"
    assert (
        "nx_get_operation" in failure["error"]["hint"]
        and "op_long_call" in failure["error"]["hint"]
    )


async def test_part_not_granted_keeps_its_category_and_hint(tmp_path, tested):
    bridge = AsyncMock()
    bridge.call.side_effect = NXError(
        "NX_PART_NOT_GRANTED",
        "The work part belongs to the engineer",
        hint="Ask the engineer to grant it on the NX MCP panel, or work on a copy",
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


# ---- discovery regressions -----------------------------------------------------


def _entries():
    return [
        gateways.Entry(
            s.name,
            effect=s.effect,
            domain=s.domain,
            label=s.label,
            description=s.description,
            keywords=s.keywords,
        )
        for s in catalogue.load().values()
    ]


@pytest.mark.parametrize(
    ("query", "first"),
    [
        ("create part", "nx_create_part"),
        ("new part", "nx_create_part"),
        ("create a new part", "nx_create_part"),
        ("create part units", "nx_create_part"),  # all words: only a description match
        ("create new part mm", "nx_create_part"),
        ("close part", "nx_close_part"),
        ("close the part without saving", "nx_close_part"),
        ("save part", "nx_save_part"),
        ("open part", "nx_open_part"),
        ("sketch rectangle", "nx_create_sketch_curves"),
        ("fillet edges", "nx_create_blend"),
        ("set expression", "nx_set_expression"),
    ],
)
def test_find_ranks_the_obvious_tool_first(query, first):
    found, _ = gateways.ranked(_entries(), query)
    assert found[0].entry.name == first, [h.entry.name for h in found[:5]]


def test_find_keeps_unrelated_tools_below_name_matches():
    names = [h.entry.name for h in gateways.ranked(_entries(), "create part")[0][:3]]
    assert not any("explosion" in n or n.startswith("nx_export") for n in names), names


# ---- discovery regressions (builder agents, 2026-10-10): a scored OR ------------


def _top3(query):
    specs = catalogue.load()
    hits, _ = gateways.ranked(_entries(), query, variants=lambda e: specs[e.name].variant_values())
    return [h.entry.name for h in hits[:3]]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("save close part", ["nx_save_part", "nx_close_part"]),
        ("new part", ["nx_create_part"]),
        ("close part", ["nx_close_part"]),
        ("revolve profile", ["nx_create_revolve"]),
        ("fillet edges", ["nx_create_blend"]),
        ("section view", ["nx_create_section_view"]),
        ("set expression", ["nx_set_expression"]),
        ("pattern blades around axis", ["nx_create_pattern"]),
        ("unite bodies", ["nx_create_boolean"]),
        ("measure distance", ["nx_measure_distance"]),
        ("screenshot", ["nx_capture_view"]),
        ("mate parts", ["nx_create_assembly_constraint"]),
        # queries a pump-cover task uses
        ("isometric view", ["nx_create_drawing_view"]),
        ("rectangle sketch", ["nx_create_sketch"]),
        ("list edges", ["nx_list_objects", "nx_find_geometry"]),
        ("new assembly part", ["nx_create_part"]),
        ("assembly constraints status", ["nx_list_objects"]),
        ("edges in drawing view", ["nx_list_view_edges"]),  # nx_find_geometry refuses views
        ("redo", ["nx_redo_operation", "nx_undo_operation"]),
        # earlier misses
        ("pocket", ["nx_create_extrude"]),
        ("rectangular pocket", ["nx_create_extrude"]),
        ("create assembly", ["nx_create_part"]),
        ("edge selection by direction", ["nx_find_geometry"]),
        ("vertical edges", ["nx_find_geometry"]),
        ("create assembly component", ["nx_create_component"]),
        ("drawing sheet views", ["nx_create_drawing_sheet"]),
        ("block body dimensions", ["nx_create_primitive"]),
        # freeform, weight, motion and optimization tools (2026-10-12)
        ("offset surface", ["nx_create_offset_surface"]),
        ("fill hole surface", ["nx_create_n_sided_surface"]),
        ("ruled surface", ["nx_create_ruled"]),
        ("weight and centre of gravity report", ["nx_measure_assembly_mass"]),
        ("center of gravity", ["nx_measure_assembly_mass", "nx_measure_mass"]),
        ("export mass report", ["nx_export_mass_report"]),
        ("assert component mass", ["nx_set_component_mass"]),
        ("component group", ["nx_create_component_group", "nx_list_component_groups"]),
        ("rudder swing clearance", ["nx_run_motion_sweep"]),
        ("hinge range of motion", ["nx_run_motion_sweep"]),
        ("steering linkage", ["nx_run_motion_sweep"]),
        ("topology optimization bracket", ["nx_create_topology_study"]),
        ("lightweight bracket", ["nx_create_topology_study", "nx_run_topology_study"]),
    ],
)
def test_find_puts_the_expected_tools_in_the_top_three(query, expected):
    top = _top3(query)
    assert set(expected) <= set(top), top


@pytest.mark.parametrize(
    ("query", "first", "below"),
    [
        ("create expression", "nx_set_expression", "nx_export_expressions"),
        ("set material", "nx_set_material", "nx_get_material"),
        ("add part attribute", "nx_set_attributes", "nx_get_attributes"),
        ("make block", "nx_create_primitive", "nx_create_drawing_sheet"),
        ("create drawing sheet", "nx_create_drawing_sheet", "nx_activate_drawing_sheet"),
        ("pocket", "nx_create_extrude", "nx_create_emboss_body"),
    ],
)
def test_find_ranks_creating_tools_first_for_action_words(query, first, below):
    names = [h.entry.name for h in gateways.ranked(_entries(), query)[0]]
    assert names[0] == first and names.index(below) > 0, names[:5]


@pytest.mark.parametrize(
    ("name", "suggested"),
    [
        ("nx_fillet_edges", "nx_create_blend"),
        ("nx_round_edges", "nx_create_blend"),
        ("nx_cut_extrude", "nx_create_extrude"),
        ("nx_create_pocket", "nx_create_extrude"),
        ("nx_redo", "nx_redo_operation"),
        ("nx_list_objcts", "nx_list_objects"),  # a typo keeps its spelling first
    ],
)
async def test_unknown_tool_names_suggest_tools_by_their_words(
    tmp_path, every_tool, name, suggested
):
    agent = server(tmp_path)
    schemas = body(await agent.call_tool("nx_get_tool_schemas", {"names": [name]}))
    assert schemas["error"]["details"]["not_found"][0]["close_matches"][0] == suggested
    invoked = body(await agent.call_tool("nx_invoke", {"tool": name, "arguments": {}}))
    assert invoked["error"]["code"] == "NX_UNKNOWN_TOOL"
    assert invoked["error"]["details"]["close_matches"][0] == suggested


def test_find_puts_create_part_first_for_create_new_part():
    assert _top3("create new part")[0] == "nx_create_part"


def test_find_ranks_a_tool_matching_every_word_above_partial_ones():
    hits, partial = gateways.ranked(_entries(), "save part")
    assert not partial and hits[0].entry.name == "nx_save_part"
    assert hits[0].matched == ("save", "part")
    # A rarer word counts more: "save" alone outranks "part" alone.
    rest = {h.entry.name: h.score for h in hits}
    assert rest["nx_save_presentation"] > rest["nx_activate_part"]


@pytest.fixture
def every_tool(monkeypatch):
    every = {
        n: {"v2406": {"variants": list(s.variant_values())}} for n, s in catalogue.load().items()
    }
    monkeypatch.setattr(evidence, "_CACHE", every)
    monkeypatch.delenv("NX_MCP_DEV", raising=False)


async def test_find_answers_several_tasks_in_one_query_with_a_hint(tmp_path, every_tool):
    query = "revolve sketch extrude pattern expression unite"
    data = body(await server(tmp_path).call_tool("nx_find_tools", {"query": query}))["data"]
    names = [r["name"] for r in data["tools"]]
    assert "nx_create_revolve" in names and "nx_create_extrude" in names, names
    assert data["partial"] is True and "queries" in data["hint"]
    assert all(r["matched"] for r in data["tools"])  # each row says what it matched


async def test_find_queries_returns_the_top_tools_per_task(tmp_path, every_tool):
    agent = server(tmp_path)
    data = body(
        await agent.call_tool("nx_find_tools", {"queries": ["revolve", "pattern", "unite"]})
    )["data"]
    firsts = [item["tools"][0]["name"] for item in data["results"]]
    assert firsts == ["nx_create_revolve", "nx_create_pattern", "nx_create_boolean"]
    assert [item["query"] for item in data["results"]] == ["revolve", "pattern", "unite"]
    assert all(len(item["tools"]) <= 5 for item in data["results"])  # 5 per query by default
    assert "matched" not in data["results"][0]["tools"][0]  # one word: nothing to say
    both = body(await agent.call_tool("nx_find_tools", {"query": "x", "queries": ["revolve"]}))
    assert both["error"]["code"] == "NX_INVALID_ARGUMENT" and "not both" in both["error"]["message"]
    for bad in ([], ["a"] * 21, [""]):
        refused = body(await agent.call_tool("nx_find_tools", {"queries": bad}))
        assert refused["error"]["code"] == "NX_INVALID_ARGUMENT", bad


async def test_find_searches_close_spellings_of_unknown_words(tmp_path, every_tool):
    agent = server(tmp_path)
    data = body(await agent.call_tool("nx_find_tools", {"query": "revovle"}))["data"]
    assert "revolve" in data["did_you_mean"] and "partial" not in data
    assert data["tools"][0]["name"] == "nx_create_revolve"
    data = body(await agent.call_tool("nx_find_tools", {"query": "chamfre edges"}))["data"]
    assert data["did_you_mean"] == ["chamfer"] and data["tools"][0]["name"] == "nx_create_chamfer"
    data = body(await agent.call_tool("nx_find_tools", {"queries": ["scrrenshot"]}))["data"]
    assert data["results"][0]["tools"][0]["name"] == "nx_capture_view"


async def test_empty_find_returns_a_domain_overview(tmp_path):
    every = {
        n: {"v2406": {"variants": list(s.variant_values())}} for n, s in catalogue.load().items()
    }
    evidence._CACHE = every  # restored by the module fixture of other tests
    try:
        data = body(await server(tmp_path).call_tool("nx_find_tools", {}))["data"]
    finally:
        evidence._CACHE = None
    domains = {row["domain"]: row for row in data["domains"]}
    assert set(domains) == {s.domain for s in catalogue.load().values()}
    assert sum(row["tools"] for row in data["domains"]) == len(catalogue.load())
    assert domains["parts_files"]["key"][0] == "nx_create_part"
    assert "nx_invoke" in data["call_with"] and "hint" in data


def test_key_tools_of_the_overview_exist_in_their_domain():
    specs = catalogue.load()
    for domain, names in gateways.KEY_TOOLS.items():
        assert all(specs[n].domain == domain for n in names), domain


async def test_find_rows_say_to_call_through_the_gateways(tmp_path, tested):
    data = body(await server(tmp_path).call_tool("nx_find_tools", {"query": "fillet"}))["data"]
    assert data["call_with"].startswith("nx_invoke")


@pytest.mark.parametrize(
    ("operation_id", "words"),
    [
        ("e1", "too short: 2 of at least 8"),
        ("r4_part", "too short: 7 of at least 8"),
        ("blade row 1", "outside A-Z a-z 0-9 _ -: ' '"),
        ("x" * 129, "too long: 129 of at most 128"),
    ],
)
async def test_a_bad_operation_id_says_what_is_wrong(tmp_path, tested, operation_id, words):
    bridge = AsyncMock()
    failure = body(
        await server(tmp_path, bridge).call_tool(
            "nx_invoke",
            {
                "tool": "nx_create_datum_plane",
                "arguments": {**OFFSET, "operation_id": operation_id},
            },
        )
    )
    assert failure["mutation_outcome"] == "not_started" and not bridge.call.called
    assert words in failure["error"]["message"], failure["error"]["message"]
    assert "optional" in failure["error"]["hint"] and failure["error"]["field"] == "/operation_id"


async def test_operation_id_schema_states_the_rule(tmp_path, tested):
    tools = {t.name: t for t in await server(tmp_path, surface="full").list_tools()}
    rule = tools["nx_create_datum_plane"].inputSchema["properties"]["operation_id"]
    assert rule["minLength"] == 8 and rule["maxLength"] == 128
    assert rule["pattern"] == "^[A-Za-z0-9_-]+$"
    invoke = tools["nx_invoke"].inputSchema["properties"]["arguments"]["description"]
    assert "operation_id" in invoke and "8-128" in invoke


# ---- compact changes ---------------------------------------------------------


class _Store:
    def __init__(self):
        self.saved = []

    def put(self, envelope):
        self.saved.append(envelope)
        return "result_" + "0" * 32


def _ref(i, kind, name):
    return {"id": f"{kind[0]}:{i}", "kind": kind, "name": name, "part": "p:1"}


def test_compact_changes_keep_the_main_object_and_count_the_rest():
    from nx_mcp.sidecar.proxy import _project

    pattern = _ref(10, "feature", "Pattern Feature(11)")
    created = [_ref(20 + k, "expression", f"p{17 + k}") for k in range(8)]
    created += [pattern] + [_ref(40 + k, "feature", f"Pattern Feature(11:{k}A)") for k in range(35)]
    full = {
        "ok": True,
        "tool": "nx_create_pattern",
        "mutation_outcome": "committed",
        "data": {"feature": pattern, "bodies": [_ref(3, "body", "")], "volume": 1.0000000000000002},
        "changes": {"created": created, "modified": [], "deleted": [], "references": "listed"},
    }
    store = _Store()
    compact = _project(full, store)
    # The rows data names at its top level stay: changes alone tell what was made.
    assert compact["changes"]["created"] == [pattern]
    assert compact["changes"]["counts"] == {"created": {"expression": 8, "feature": 36}}
    assert compact["omitted"]["/changes/created"] == {"total": 44, "returned": 1}
    assert compact["data"]["volume"] == 1.0 and compact["result_id"]
    assert store.saved == [full] and len(full["changes"]["created"]) == 44  # full kept


def test_compact_changes_keep_rows_data_shows_and_drop_nx_helpers():
    from nx_mcp.sidecar.proxy import _project

    curves = [_ref(k, "curve", f"Line{k}") for k in range(12)]
    full = {
        "ok": True,
        "tool": "nx_create_sketch_curves",
        "mutation_outcome": "committed",
        "data": {"sketch": _ref(1, "sketch", "S"), "curves": [{"curve": c} for c in curves]},
        "changes": {"created": curves, "modified": [], "deleted": [], "references": "preserved"},
    }
    compact = _project(full, _Store())
    assert compact["changes"]["created"] == [curves[0], curves[-1]]  # also in data
    assert compact["changes"]["counts"] == {"created": {"curve": 12}}
    assert compact["omitted"]["/changes/created"]["shown"] == "first_last"
    helpers = [_ref(70 + k, "point", f"ENTITY 2 {k} 1") for k in range(4)]
    helpers += [_ref(80 + k, "curve", f"ENTITY 3 {k} 1") for k in range(4)]
    datum = _ref(9, "datum_plane", "DATUM_PLANE(3)")
    full["data"] = {"active": True}
    full["changes"]["created"] = [*helpers, datum]
    assert _project(full, _Store())["changes"]["created"] == [datum]


def test_small_compact_result_needs_no_result_id():
    """A helper row costs less than the result_id, omitted and counts that would replace
    it; the row data shows stays."""
    from nx_mcp.sidecar.proxy import _project

    feature = _ref(5, "feature", "EXTRUDE(2)")
    helper = _ref(1, "expression", "p0")
    full = {
        "ok": True,
        "tool": "nx_create_extrude",
        "mutation_outcome": "committed",
        "data": {"feature": feature},
        "changes": {
            "created": [helper, feature],
            "modified": [],
            "deleted": [],
            "references": "preserved",
        },
    }
    store = _Store()
    compact = _project(full, store)
    assert compact["changes"]["created"] == [helper, feature] and "result_id" not in compact
    assert store.saved == [] and full["changes"]["created"] == [helper, feature]


def _pattern_envelope(instances):
    """A separate-body pattern of ``instances`` (seed included), as NX 2406 reports it."""
    seed_body = _ref(3, "body", "")
    new = [_ref(100 + k, "body", "") for k in range(instances - 1)]
    pattern = _ref(10, "feature", "Pattern Feature [Linear](11)")
    created = [_ref(20 + k, "expression", f"p{17 + k}") for k in range(9)]
    created += [pattern, *new]
    created += [
        _ref(200 + k, "feature", f"Pattern Feature [Linear](11:{k}A)") for k in range(instances)
    ]
    return {
        "ok": True,
        "tool": "nx_create_pattern",
        "mutation_outcome": "committed",
        "data": {
            "feature": pattern,
            "bodies": [seed_body, *new],
            "volume": 18000.0,
            "layout": "linear",
            "count": instances,
            "new_bodies": instances - 1,
        },
        "changes": {"created": created, "modified": [], "deleted": [], "references": "listed"},
    }


def test_compact_pattern_shows_first_and_last_bodies():
    from nx_mcp.sidecar.proxy import _project, _size

    full = _pattern_envelope(18)
    store = _Store()
    compact = _project(full, store)
    data = compact["data"]
    assert [b["id"] for b in data["bodies"]] == ["b:3", "b:116"]  # seed body, last instance
    assert data["new_bodies"] == 17 and data["count"] == 18
    assert compact["omitted"]["/data/bodies"] == {"total": 18, "returned": 2, "shown": "first_last"}
    # Created keeps the main objects data names (pattern feature, new bodies): first, last.
    assert [r["id"] for r in compact["changes"]["created"]] == ["f:10", "b:116"]
    assert compact["changes"]["counts"] == {"created": {"body": 17, "expression": 9, "feature": 19}}
    assert store.saved == [full] and len(full["data"]["bodies"]) == 18
    assert _size(compact) < 1000 and _size(full) > 4000


def test_compact_boolean_shows_first_and_last_deleted_tool():
    from nx_mcp.sidecar.proxy import _project

    target = _ref(3, "body", "")
    tools = [_ref(100 + k, "body", "") for k in range(18)]
    feature = _ref(10, "feature", "UNITE(30)")
    full = {
        "ok": True,
        "tool": "nx_create_boolean",
        "mutation_outcome": "committed",
        "data": {"feature": feature, "bodies": [target], "volume": 1.0, "deleted_bodies": 18},
        "changes": {
            "created": [feature],
            "modified": [target],
            "deleted": tools,
            "references": "listed",
        },
    }
    compact = _project(full, _Store())
    changes = compact["changes"]
    assert changes["created"] == [feature] and changes["modified"] == [target]  # also in data
    assert changes["deleted"] == [tools[0], tools[-1]]
    assert changes["counts"] == {"deleted": {"body": 18}}
    assert compact["omitted"] == {
        "/changes/deleted": {"total": 18, "returned": 2, "shown": "first_last"}
    }
    three = {**full, "changes": {**full["changes"], "deleted": tools[:3]}}
    assert _project(three, _Store())["changes"]["deleted"] == tools[:3]


def test_read_results_keep_their_body_lists():
    from nx_mcp.sidecar.proxy import _project

    bodies = [_ref(k, "body", "") for k in range(6)]
    full = {"ok": True, "tool": "nx_list_bodies", "mutation_outcome": "not_applicable",
            "data": {"bodies": bodies}}  # fmt: skip
    assert _project(full, _Store())["data"]["bodies"] == bodies


async def test_find_hints_only_at_several_tasks_and_notes_what_is_missing(tmp_path, every_tool):
    agent = server(tmp_path)

    async def find(arguments):
        return body(await agent.call_tool("nx_find_tools", arguments))["data"]

    singles = ("assembly constraints status", "edges in drawing view", "new assembly part",
               "thread standard size", "expand handle result")  # fmt: skip
    for single in singles:
        assert "hint" not in await find({"query": single}), single
    assert "queries" in (await find({"query": "extrude sketch, then pattern holes"}))["hint"]
    assert "queries" in (await find({"query": "revolve shaft then chamfer edge"}))["hint"]
    redo = await find({"query": "redo"})
    assert redo["tools"][0]["name"] == "nx_redo_operation" and "note" not in redo
    pocket = await find({"query": "pocket"})
    assert pocket["tools"][0]["name"] == "nx_create_extrude" and "subtract" in pocket["note"]
    many = await find({"queries": [f"task {i}" for i in range(20)]})
    assert len(many["results"]) == 20


class Closed(TypedDict):
    closed: bool


def nx_close_widget(tx, *, part: PartRef | Annotated[str, PathTo("read", ".prt")]) -> Closed:
    """Close a widget part given by ref or path."""
    return {"closed": True}


def test_a_ref_in_a_ref_or_path_union_is_not_resolved_as_a_path(tmp_path):
    from nx_mcp.core.catalogue import make_spec

    spec = make_spec(
        nx_close_widget,
        effect="file",
        label="Close widget",
        keywords=("a", "b", "c"),
        domain="parts_files",
        example={"part": "p:1"},
    )
    proxy = CatalogueProxy(AsyncMock(), Workspace(tmp_path), store=None)
    ref = {"part": "p:3"}
    assert proxy._resolve_paths(spec, ref) is None and ref == {"part": "p:3"}
    path = {"part": "parts/a.prt"}
    assert proxy._resolve_paths(spec, path) is None
    assert path["part"] == str(tmp_path / "parts" / "a.prt")


async def test_listed_status_names_only_the_two_panel_modes(tmp_path, every_tool):
    """The panel has Auto and Ask me first; the removed 'agent (Agent drives)' mode must
    not come back in any listed text (a stale sidecar process once served it)."""
    for surface in ("agent", "full"):
        tools = {t.name: t for t in await server(tmp_path, surface=surface).list_tools()}
        status = tools["nx_get_status"].description or ""
        assert "auto or ask" in status, status
        text = " ".join(t.description or "" for t in tools.values())
        assert "Agent drives" not in text and "auto, agent" not in text
