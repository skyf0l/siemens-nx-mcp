"""Sidecar gate 2 rules and what the surface tells an agent: suffixes, folders, paths shown
workspace-relative, the ``part`` call key in every schema that takes it, per-variant
schemas and discovery (exact words first, tools this build does not offer)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nx_mcp.core import catalogue, evidence
from nx_mcp.server import create_server
from nx_mcp.sidecar import gateways, schema
from nx_mcp.workspace import Workspace, kinds, relative, scrub, suffix_of


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


# -- workspace helpers -------------------------------------------------------------------


def test_suffix_kinds_and_format_suffixes():
    assert kinds((".prt",)) == [(".prt",)]
    assert kinds((".step", ".stp", ".igs", ".iges", ".x_t")) == [
        (".step", ".stp"),
        (".igs", ".iges"),
        (".x_t",),
    ]
    found = [suffix_of(f, (".step", ".stp", ".igs", ".iges", ".x_t", ".jt")) for f in
             ("step", "step_ap242", "iges", "parasolid", "jt", None)]  # fmt: skip
    assert found == [".step", ".step", ".iges", None, ".jt", None]


def test_scrub_writes_workspace_paths_relative(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    inside = root / "smoke" / "a.prt"
    windows = str(inside)
    assert scrub(f"[WinError 183] cannot create '{windows}'", root) == (
        "[WinError 183] cannot create 'smoke/a.prt'"
    )
    assert scrub(windows.replace("\\", "/") + " done", root) == "smoke/a.prt done"
    assert scrub(f"root {root} end", root) == "root . end"
    assert scrub(f"{root}2/x", root) == f"{root}2/x"  # another folder stays
    assert scrub("no path here", root) == "no path here"
    assert relative(root, inside) == "smoke/a.prt"
    assert relative(root, tmp_path / "elsewhere.prt") == "elsewhere.prt"  # never the host path


# -- gate 2 in the sidecar ---------------------------------------------------------------


async def test_a_new_file_without_suffix_takes_its_formats_suffix(tmp_path, every_tool):
    bridge = AsyncMock()
    bridge.call.return_value = {"ok": True, "tool": "nx_export_model",
                                "mutation_outcome": "committed", "data": {}}  # fmt: skip
    srv = server(tmp_path, bridge)
    await call(srv, "nx_export_model", {"path": "out/body", "format": "iges"})
    assert bridge.call.call_args.args[1]["path"].endswith("body.iges")
    refused = await call(srv, "nx_export_model", {"path": "out/body"})
    error = refused["error"]
    assert error["code"] == "NX_INVALID_ARGUMENT" and error["field"] == "/path"
    assert error["retry"] == "after_fix" and "needs a file suffix" in error["message"]
    assert refused["mutation_outcome"] == "not_started"
    assert bridge.call.call_count == 1


async def test_an_upload_without_suffix_is_refused_not_made_a_part(tmp_path, every_tool):
    refused = await call(server(tmp_path), "nx_upload_file", {"path": "notes/readme",
                                                              "content": "x"})  # fmt: skip
    assert refused["error"]["code"] == "NX_INVALID_ARGUMENT"
    assert "needs a file suffix" in refused["error"]["message"]
    assert not (tmp_path / "notes" / "readme.prt").exists()


async def test_a_wrong_suffix_is_fixable_and_names_the_tool_that_takes_it(tmp_path, every_tool):
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "base.step").write_text("x")
    bridge = AsyncMock()
    refused = await call(server(tmp_path, bridge), "nx_open_part", {"path": "in/base.step"})
    error = refused["error"]
    assert error["code"] == "NX_UNSUPPORTED_FILE_TYPE" and error["retry"] == "after_fix"
    assert "nx_import_model" in error["hint"] and "leave the suffix out" not in error["hint"]
    assert error["details"]["path"] == "in/base.step"
    assert str(tmp_path) not in str(refused)
    bridge.call.assert_not_called()


async def test_a_folder_where_a_file_is_wanted_is_named(tmp_path, every_tool):
    (tmp_path / "parts").mkdir()
    srv = server(tmp_path)
    for path in ("parts", "parts/"):
        refused = await call(srv, "nx_open_part", {"path": path})
        error = refused["error"]
        assert error["code"] == "NX_WRONG_KIND" and "is a folder" in error["message"], path
        assert error["retry"] == "after_fix"


async def test_results_and_errors_show_workspace_relative_paths(tmp_path, every_tool):
    absolute = str(tmp_path / "smoke" / "a.prt")
    bridge = AsyncMock()
    bridge.call.return_value = {
        "ok": True, "tool": "nx_list_parts", "mutation_outcome": "not_applicable",
        "data": {"parts": [{"path": absolute}], "note": f"saved to {absolute}"},
    }  # fmt: skip
    answer = await call(server(tmp_path, bridge, "full"), "nx_list_parts", {})
    assert answer["data"]["parts"][0]["path"] == "smoke/a.prt"
    assert answer["data"]["note"] == "saved to smoke/a.prt"


# -- the part key and unknown fields -----------------------------------------------------


async def test_every_schema_of_a_tool_taking_part_lists_it(tmp_path, every_tool):
    specs = catalogue.load()
    takes = sorted(n for n, s in specs.items() if schema.takes_part_key(s))
    assert "nx_update_model" in takes and "nx_update_assembly" in takes
    srv = server(tmp_path, surface="agent")
    for start in range(0, len(takes), 10):
        data = (await call(srv, "nx_get_tool_schemas", {"names": takes[start : start + 10]}))[
            "data"
        ]
        for entry in data["tools"]:
            assert entry["input_schema"]["properties"]["part"] == schema.PART_KEY, entry["name"]
    full = {t.name: t for t in await server(tmp_path).list_tools()}
    assert "part" in full["nx_update_model"].inputSchema["properties"]
    assert "part" not in full["nx_list_parts"].inputSchema["properties"]


async def test_an_unknown_field_offers_the_part_key(tmp_path, every_tool):
    bridge = AsyncMock()
    refused = await call(server(tmp_path, bridge), "nx_update_model", {"prt": "housing"})
    error = refused["error"]
    assert "takes no arguments" not in error["message"]
    assert "did you mean 'part'" in error["message"] and "part" in error["details"]["accepted"]
    bridge.call.assert_not_called()


# -- per-variant schemas -----------------------------------------------------------------


def test_variant_claims_come_from_descriptions_naming_variants_only():
    values = ("plane", "parallel", "radial", "faces", "keep_in", "keep_out", "half_section")
    assert schema.variant_claim("parallel/radial: number of sections", values) == {
        "parallel",
        "radial",
    }
    assert schema.variant_claim("by faces: the faces", values) == {"faces"}
    assert schema.variant_claim("Half section only: x", values) == {"half_section"}
    assert schema.variant_claim("keep_in/out: bodies", values) is None  # 'out' is no variant
    assert schema.variant_claim("Ref: edge", values) is None
    assert schema.variant_claim("Sheet units", values) is None


def test_a_variant_schema_leaves_out_the_fields_of_other_variants():
    spec = catalogue.load()["nx_create_section_curves"]
    parallel = schema.variant_schema(spec, "parallel", call_keys=False)["properties"]
    radial = schema.variant_schema(spec, "radial", call_keys=False)["properties"]
    assert "spacing" in parallel and "axis" not in parallel and "angle" not in parallel
    assert "axis" in radial and "spacing" not in radial
    assert "count" in parallel and "count" in radial


async def test_grouped_variants_show_each_variants_own_fields(tmp_path, every_tool):
    names = ["nx_create_section_curves#parallel", "nx_create_section_curves#radial"]
    data = (await call(server(tmp_path, surface="agent"), "nx_get_tool_schemas",
                       {"names": names}))["data"]  # fmt: skip
    (entry,) = data["tools"]
    own = entry["variants"]["radial"]["input_schema"]["properties"]
    assert "axis" in own and len(own) > 1  # more than the variant's const


# -- discovery ---------------------------------------------------------------------------


def test_a_typo_is_respelled_but_a_real_word_beside_known_words_is_not():
    entries = [
        gateways.Entry(s.name, effect=s.effect, domain=s.domain, label=s.label,
                       description=s.description, keywords=s.keywords)
        for s in catalogue.load().values()
    ]  # fmt: skip
    index = gateways.Index(entries)
    assert gateways.typo("chamfre", "chamfer") and gateways.typo("revovle", "revolve")
    assert not gateways.typo("routing", "rounding")
    searched, spellings = index.respell("chamfre edges")
    assert spellings == ["chamfer"]
    unknown = "routing" if not index.tools("rout") else "zzrouting"
    _, spellings = index.respell(f"pipe {unknown} solid")
    assert "rounding" not in spellings


def test_close_spellings_count_less_than_words_typed():
    entry = gateways.Entry("nx_move_thing", effect="model", domain="modeling",
                           label="Move thing", description="Move a thing")  # fmt: skip
    exact, _ = entry.match(["move"])
    fuzzy, _ = entry.match(["move"], fuzzy=frozenset({"move"}))
    assert fuzzy == pytest.approx(exact * gateways.FUZZY, rel=0.5) and fuzzy < exact


async def test_find_names_tools_this_build_does_not_offer(tmp_path, every_tool, monkeypatch):
    hidden = dict(every_tool)
    del hidden["nx_create_blend"]
    monkeypatch.setattr(evidence, "_CACHE", hidden)
    data = (await call(server(tmp_path, surface="agent"), "nx_find_tools",
                       {"query": "blend edges"}))["data"]  # fmt: skip
    rows = data["not_available"]
    assert rows[0]["name"] == "nx_create_blend" and "not tested" in rows[0]["reason"]
    assert all(r["name"] != "nx_create_blend" for r in data["tools"])


async def test_core_rows_say_they_are_direct_mcp_tools(tmp_path, every_tool):
    data = (await call(server(tmp_path, surface="agent"), "nx_find_tools",
                       {"query": "list parts"}))["data"]  # fmt: skip
    rows = {r["name"]: r for r in data["tools"]}
    assert rows["nx_list_parts"]["core"] is True
    assert "core" not in rows.get("nx_close_part", {"name": "x"})
    assert "core:true" in data["call_with"]
    full = (await call(server(tmp_path), "nx_find_tools", {"query": "list parts"}))["data"]
    assert all("core" not in r for r in full["tools"]) and "core" not in full["call_with"]


def test_every_file_a_tool_reads_can_be_uploaded():
    """An agent can bring any input file into the workspace: nx_upload_file takes every
    suffix some tool's ``read`` path takes."""
    specs = catalogue.load()
    uploads = {s for _p, _m, suffixes in specs["nx_upload_file"].paths for s in suffixes}
    reads = {
        s for spec in specs.values() for _p, mode, suffixes in spec.paths if mode == "read"
        for s in suffixes
    }  # fmt: skip
    assert reads <= uploads, sorted(reads - uploads)
