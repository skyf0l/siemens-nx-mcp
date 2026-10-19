"""What the sidecar tells an agent when a call is refused before it leaves, and how it
cleans answers: one error envelope, suffix hints for this tool, ``..`` refused on both
boundaries, ``part`` on every schema of a tool acting on a part, variant schemas that
show only the variant, float noise removed."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nx_mcp.core import catalogue, evidence
from nx_mcp.core.receipts import operation_id_problem
from nx_mcp.kit.result import tidy
from nx_mcp.server import create_server
from nx_mcp.sidecar import schema
from nx_mcp.workspace import Workspace, WorkspaceViolation


@pytest.fixture
def every_tool(monkeypatch):
    every = {
        n: {"v2406": {"variants": list(s.variant_values())}} for n, s in catalogue.load().items()
    }
    monkeypatch.setattr(evidence, "_CACHE", every)
    monkeypatch.delenv("NX_MCP_DEV", raising=False)
    return every


def server(tmp_path, bridge=None, surface="full"):
    return create_server(bridge or AsyncMock(), Workspace(tmp_path), surface=surface,
                         build="v2406")  # fmt: skip


async def call(srv, name, arguments):
    return (await srv.call_tool(name, arguments)).structuredContent


# -- one envelope ------------------------------------------------------------------------


async def test_a_refusal_before_nx_has_the_kernels_envelope_fields(tmp_path, every_tool):
    bridge = AsyncMock()
    srv = server(tmp_path, bridge)
    refused = await call(srv, "nx_create_primitive", {"shape": "block", "length": [1]})
    assert refused["mutation_outcome"] == "not_started" and refused["error"]["field"]
    assert operation_id_problem(refused["operation_id"]) is None  # a fresh, valid id
    assert refused["changes"] == {"references": "preserved"}  # a model tool: nothing changed
    own = await call(srv, "nx_create_primitive", {"shape": "q", "operation_id": "my_block_01"})
    assert own["operation_id"] == "my_block_01"
    bad = await call(srv, "nx_create_primitive", {"shape": "block", "operation_id": "x"})
    assert bad["error"]["field"] == "/operation_id" and bad["operation_id"] != "x"
    read = await call(srv, "nx_list_objects", {"kind": "nope"})
    assert "operation_id" not in read and "changes" not in read
    bridge.call.assert_not_called()


# -- paths -------------------------------------------------------------------------------


def test_dot_dot_segments_are_refused_even_inside_the_workspace(tmp_path):
    workspace = Workspace(tmp_path)
    for path in ("smoke/a/../../evil.txt", "smoke\\a\\..\\b.txt", "../x.txt"):
        with pytest.raises(WorkspaceViolation, match=r"'\.\.' segments"):
            workspace.resolve(path)
    assert workspace.resolve("smoke/a..b/c.txt") == (tmp_path / "smoke/a..b/c.txt").resolve()


async def test_an_upload_with_dot_dot_writes_nothing(tmp_path, every_tool):
    refused = await call(server(tmp_path), "nx_upload_file",
                         {"path": "smoke/c/../../evil.txt", "content": "x"})  # fmt: skip
    assert refused["error"]["code"] == "NX_PATH_OUTSIDE_WORKSPACE"
    assert refused["error"]["field"] == "/path" and "'..'" in refused["error"]["message"]
    assert not (tmp_path / "evil.txt").exists()


async def test_a_wrong_suffix_hint_lists_this_tools_suffixes_not_unrelated_tools(
    tmp_path, every_tool
):
    srv = server(tmp_path)
    exp = (await call(srv, "nx_export_expressions", {"path": "smoke/bad.txt"}))["error"]
    assert exp["code"] == "NX_UNSUPPORTED_FILE_TYPE" and "smoke/bad.exp" in exp["hint"]
    assert "nx_upload_file" not in exp["hint"]
    (tmp_path / "smoke").mkdir(exist_ok=True)
    (tmp_path / "smoke" / "p.prt").write_text("x")
    points = (await call(srv, "nx_import_points", {"path": "smoke/p.prt"}))["error"]
    assert "nx_check_drawing_changes" not in points["hint"], points
    assert ".txt" in points["hint"] or ".csv" in points["hint"], points


# -- schemas -----------------------------------------------------------------------------


def test_part_is_listed_by_every_tool_acting_on_a_part(every_tool):
    specs = catalogue.load()
    for name in ("nx_measure_distance", "nx_get_true_shading", "nx_list_lights"):
        spec = specs.get(name)
        if spec is None:
            continue
        assert schema.takes_part_key(spec), name
        listed = schema.with_part_key(spec, schema.input_schema(spec, call_keys=False))
        assert listed["properties"]["part"] == schema.PART_KEY, name
    for name in ("nx_open_part", "nx_create_part", "nx_get_status", "nx_close_part"):
        assert not schema.takes_part_key(specs[name]), name


def test_a_variant_of_typed_items_shows_only_that_variants_item(every_tool):
    spec = catalogue.load()["nx_create_sketch_constraints"]
    whole = schema.input_schema(spec, call_keys=False)
    one = schema.variant_schema(spec, "tangent", call_keys=False)
    assert "x-variant" not in one and len(str(one)) < len(str(whole))
    item = one["properties"]["constraints"]["items"]
    assert set(item["properties"]) == {"type", "curves"} and item["required"] == ["type", "curves"]
    assert "tangent" in item["properties"]["type"]["enum"]
    assert "centerline" not in str(one)


# -- float noise -------------------------------------------------------------------------


def test_tidy_rounds_noise_and_zeroes_tiny_components_beside_large_ones():
    data = {
        "radius": 2.9999999999999996,
        "angle": 89.99999999999997,
        "pose": {"origin": [100.0, 6.1e-17, 0.0], "matrix": [[1.0, 3e-17], [-2e-17, 1.0]]},
        "alone": 1e-15,
        "names": ["a", 1.0000000000000002],
        "count": 3,
        "flag": True,
    }
    assert tidy(data) == {
        "radius": 3.0,
        "angle": 90.0,
        "pose": {"origin": [100.0, 0.0, 0.0], "matrix": [[1.0, 0.0], [0.0, 1.0]]},
        "alone": 1e-15,
        "names": ["a", 1.0],
        "count": 3,
        "flag": True,
    }


async def test_full_detail_answers_are_free_of_float_noise(tmp_path, every_tool):
    bridge = AsyncMock()
    bridge.call.return_value = {
        "ok": True, "tool": "nx_list_objects", "mutation_outcome": "not_applicable",
        "data": {"items": [{"radius": 2.9999999999999996, "at": [6.1e-17, 1.0, 0.0]}]},
    }  # fmt: skip
    answer = await call(server(tmp_path, bridge), "nx_list_objects",
                        {"kind": "body", "detail": "full"})  # fmt: skip
    assert answer["data"]["items"][0] == {"radius": 3.0, "at": [0.0, 1.0, 0.0]}


async def test_a_hint_names_only_variant_schemas_the_fetch_serves(tmp_path, every_tool):
    every_tool["nx_create_surface_analysis"] = {"v2406": {"variants": ["reflection"]}}
    bridge = AsyncMock()
    bridge.call.return_value = {
        "ok": False, "tool": "nx_create_surface_analysis", "mutation_outcome": "not_started",
        "error": {"category": "invalid_argument", "code": "NX_INVALID_ARGUMENT",
                  "message": "/faces: required", "field": "/faces", "retry": "after_fix",
                  "hint": "Add faces (nx_get_tool_schemas nx_create_surface_analysis#curvature_comb"
                          " or nx_create_surface_analysis#reflection)", "details": {}},
    }  # fmt: skip
    srv = server(tmp_path, bridge)
    answer = await call(
        srv, "nx_create_surface_analysis", {"type": "reflection", "faces": ["fc:1"]}
    )
    hint = answer["error"]["hint"]
    assert "#curvature_comb" not in hint and "nx_create_surface_analysis or" in hint, hint
    assert "nx_create_surface_analysis#reflection" in hint
    # gate 1 itself: a variant this build does not offer gets no schema pointer
    refused = await call(srv, "nx_create_surface_analysis", {"type": "curvature_comb"})
    assert "#curvature_comb" not in str(refused["error"]), refused


async def test_a_sidecar_tool_knows_its_caller(tmp_path, every_tool, monkeypatch):
    from nx_mcp.core import kernel

    seen = []
    real = kernel.run

    def spy(spec, args, ctx):
        seen.append(ctx.caller)
        return real(spec, args, ctx)

    monkeypatch.setattr(kernel, "run", spy)
    answer = await call(server(tmp_path), "nx_upload_file",
                        {"path": "smoke/c/note.txt", "content": "x"})  # fmt: skip
    assert answer["ok"], answer
    assert seen and seen[0]["id"].startswith("sc-")  # the sidecar's caller id


def _row(i, kind, name):
    return {"id": f"{kind[0]}:{i}", "kind": kind, "name": name, "part": "p:1"}


def test_envelopes_nested_in_a_result_get_terse_changes():
    from nx_mcp.sidecar.proxy import _project

    def step(n):
        feature = _row(n, "feature", f"EXTRUDE({n})")
        made = [_row(100 * n + k, "expression", f"p{k}") for k in range(30)] + [feature]
        return {"ok": True, "tool": "nx_create_extrude", "mutation_outcome": "committed",
                "data": {"feature": feature},
                "changes": {"created": made, "modified": [], "deleted": []}}  # fmt: skip

    batch = {"ok": True, "tool": "nx_run_batch", "mutation_outcome": "committed",
             "data": {"steps": [step(1), step(2)]}}  # fmt: skip
    operation = {"ok": True, "tool": "nx_get_operation", "mutation_outcome": "not_applicable",
                 "data": {"state": "committed", "result": batch}}  # fmt: skip
    saved = []

    class Store:
        def put(self, envelope):
            saved.append(envelope)
            return "result_" + "1" * 32

    compact = _project(operation, Store())
    first = compact["data"]["result"]["data"]["steps"][0]["changes"]
    assert first["created"] == [_row(1, "feature", "EXTRUDE(1)")]
    assert first["counts"] == {"created": {"expression": 30, "feature": 1}}
    assert "/data/result/data/steps/1/changes/created" in compact["omitted"]
    assert compact["result_id"] and saved == [operation]  # the full one stays readable


async def test_a_schema_entry_says_whether_the_tool_runs_in_a_batch(tmp_path, every_tool):
    data = (await call(server(tmp_path), "nx_get_tool_schemas",
                       {"names": ["nx_create_primitive", "nx_save_part"]}))["data"]  # fmt: skip
    entries = {e["name"]: e for e in data["tools"]}
    specs = catalogue.load()
    for name, entry in entries.items():
        assert entry.get("batchable", False) is specs[name].batchable, name
