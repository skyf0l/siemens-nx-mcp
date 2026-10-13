"""The sidecar side of the natural argument forms: the Pydantic adapter accepts what
``core.validate`` accepts, the proxy reports every invalid field and only the offered
variants, and fetched schemas name the fields each variant needs."""

import json

import jsonschema
import pytest
from pydantic import TypeAdapter

from nx_mcp.core import evidence
from nx_mcp.core import types as T
from nx_mcp.sidecar import schema
from nx_mcp.sidecar.proxy import CatalogueProxy
from tests.core.test_natural_forms import widget

ADAPT = schema._Converter()


@pytest.mark.parametrize(
    ("hint", "name", "value", "expected"),
    [
        (T.Direction, None, "-Z", {"principal": "-z"}),
        (T.Axis, None, "+x axis", {"principal": "x"}),
        (T.Axis, None, "e:3", "e:3"),
        (T.Plane, None, "XY plane", {"principal": "xy"}),
        (T.Plane, None, "front", {"principal": "xz"}),
        (T.Point3, None, "[1, 2, 3]", (1.0, 2.0, 3.0)),
        (T.Percent, "share", "50%", 50.0),
        (T.Fraction, "at", "50%", 0.5),
        (T.Factor, "scale", "2x", 2.0),
        (float, "zoom", "200%", 2.0),
        (T.Pose, None, {"axis": "z", "angle": "30deg"}, {"axis": "z", "angle": "30deg"}),
    ],
)
def test_the_adapter_takes_the_natural_forms(hint, name, value, expected):
    assert TypeAdapter(ADAPT(hint, name)).validate_python(value) == expected


@pytest.mark.parametrize(
    ("hint", "value"), [(T.Axis, "xy"), (T.Plane, "q"), (float, "50%"), (T.Percent, "2x")]
)
def test_the_adapter_refuses_what_gate_1_refuses(hint, value):
    with pytest.raises(ValueError):
        TypeAdapter(ADAPT(hint, "depth")).validate_python(value)


def proxy(offered=None):
    out = CatalogueProxy.__new__(CatalogueProxy)
    out.variants = lambda spec: offered if offered is not None else spec.variant_values()
    return out


def test_the_proxy_reports_every_invalid_field_and_the_offered_variants():
    spec = widget(required_by={"hole": ("view", "origin")})
    error = proxy()._invalid(spec, {"kind": "hole", "note": 7, "bogus": 1})
    fields = [r["field"] for r in error.details["errors"]]
    assert fields == ["/bogus", "/note", "/view", "/origin"] and error.field == "/bogus"
    narrowed = proxy(("plain",))._invalid(spec, {"kind": "bend!"})
    assert "expected one of 'plain'" in narrowed.message
    args = {"kind": "Hole", "view": "v:1", "origin": "e:2"}
    assert proxy()._invalid(spec, args) is None and args["kind"] == "hole"


def test_fetched_schemas_name_the_fields_of_each_variant():
    spec = widget(required_by={"hole": ("view", "origin"), "bend": ("view",)})
    listed = schema.input_schema(spec)
    assert "anyOf" not in listed  # MCP listings take no top-level anyOf
    assert schema.required_by(spec) == {"hole": ["view", "origin"], "bend": ["view"]}
    assert schema.required_by(spec, ("hole", "plain")) == {"hole": ["view", "origin"]}
    hole = schema.variant_schema(spec, "hole")
    assert hole["required"] == ["kind", "view", "origin"]
    jsonschema.validate({"kind": "hole", "view": "v:1", "origin": "e:2"}, hole)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"kind": "hole", "view": "v:1"}, hole)


def test_grouped_variants_share_one_key():
    spec = widget(required_by={"hole": ("view",), "bend": ("view",)})
    needs = schema.required_by(spec)
    assert needs == {"hole|bend": ["view"]}
    assert len(json.dumps(needs, separators=(",", ":"))) <= 30


def test_the_sidecar_lets_an_inferable_variant_through(monkeypatch):
    from tests.core.test_natural_forms import edit_widget

    spec = edit_widget(variant_from="table")
    assert "kind" not in schema.input_schema(spec).get("required", [])
    assert proxy()._invalid(spec, {"table": "t:3"}) is None
    monkeypatch.setattr(evidence, "_CACHE", {spec.name: {"v2406": {"variants": ["revision"]}}})
    assert evidence.check(spec, "v2406", {"table": "t:3"}) is None  # NX infers, then gates


async def test_nx_get_tool_schemas_shows_the_fields_each_variant_needs(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from nx_mcp.server import create_server
    from nx_mcp.workspace import Workspace

    monkeypatch.setenv("NX_MCP_DEV", "1")
    agent = create_server(AsyncMock(), Workspace(tmp_path), surface="agent", build="v2406")
    names = ["nx_create_datum_axis", "nx_create_datum_axis#two_points"]
    result = await agent.call_tool("nx_get_tool_schemas", {"names": names})
    whole, variant = result.structuredContent["data"]["tools"]
    assert whole["required_by"]["two_points"] == ["start", "end"]
    assert "anyOf" not in whole["input_schema"] and "required_by" not in variant
    assert variant["input_schema"]["required"] == ["method", "start", "end"]
    listed = {
        t.name: t
        for t in await create_server(
            AsyncMock(), Workspace(tmp_path), surface="full", build="v2406"
        ).list_tools()
    }
    assert "anyOf" not in listed["nx_create_datum_axis"].inputSchema


def test_the_instructions_scope_unit_strings_to_length_and_angle_fields():
    """The instructions promised '10 mm' everywhere; [x, y, z] coordinates take numbers."""
    from nx_mcp.sidecar.server import AGENT_INSTRUCTIONS, FULL_INSTRUCTIONS

    for text in (AGENT_INSTRUCTIONS, FULL_INSTRUCTIONS):
        assert "ength and angle fields typed number or string" in text
        assert "[x, y, z] points" in text and "numbers only" in text
    assert TypeAdapter(ADAPT(T.Length)).validate_python("10 mm") == "10 mm"
    assert TypeAdapter(ADAPT(T.Angle)).validate_python("90 deg") == "90 deg"
    with pytest.raises(ValueError):
        TypeAdapter(ADAPT(T.Point3)).validate_python(["10 mm", 0, 0])
    with pytest.raises(ValueError):
        TypeAdapter(ADAPT(T.Vec3)).validate_python(["1 mm", 0, 0])
