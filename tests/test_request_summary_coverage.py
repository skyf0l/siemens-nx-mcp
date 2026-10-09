"""Every registered tool has its own panel summary, built from schema-shaped arguments."""

import asyncio
import itertools
from unittest.mock import AsyncMock

import pytest

from nx_mcp.request_summary import MAX_LENGTH, has_template, humanize, summarize
from nx_mcp.server import create_server
from nx_mcp.workspace import Workspace

# Plain-text arguments (names, labels, catalog rows); every other string is an object ID.
TEXT = {
    "name": "Bracket",
    "label": "before fillet",
    "prefix": "rev_b",
    "expression": "p12",
    "formula": "20",
    "letter": "A",
    "standard": "Metric Coarse",
    "size": "M10 x 1.5",
    "title": "Mass",
    "field": "<W$=@$PART_NAME>",
    "orientation": "isometric",
    "sketch_name": "profile",
    "name_contains": "bolt",
    "path_prefix": "D:/CAD/",
    "layer": "OUTLINE",
    "material": "Steel",
    "method": "Thread Mill",
    "radial_engage": "Standard",
    "data_base64": "AAAA",
    "sha256": "0" * 64,
    "bend_table": "smoke/bends.xlsx",
    "bend_allowance_formula": "(Radius+(Thickness*0.44))*rad(Angle)",
    "bend_deduction_formula": "2*Thickness",
    "template": "smoke/a3.prt",
    "thread_size": "M10 x 1.5",
    "text": "Deburr all edges",
    "characteristic": "Position",
}
# Arguments that are text for one tool and an object ID for another.
TOOL_TEXT = {
    ("nx_capabilities", "tool"): "nx_extrude",
    ("nx_set_sheet_metal_defaults", "tool"): "T1",
}
PATHS = {"path", "part_path", "output_path"}
NOT_IDS = {"operation_id", "checkpoint_id", "preview_id", "restore_id", "result_id"}


def name_of(object_id):
    return object_id.removeprefix("obj_").upper() if object_id.startswith("obj_") else None


def tools():
    server = create_server(AsyncMock(), Workspace(__import__("pathlib").Path(".")))
    return {tool.name: tool.inputSchema for tool in asyncio.run(server.list_tools())}


NUMBERS = itertools.count(10, 5)


def _value(name, schema, defs, full, tool=""):
    if (tool, name) in TOOL_TEXT:
        return TOOL_TEXT[tool, name]
    if "$ref" in schema:
        return _value(name, defs[schema["$ref"].split("/")[-1]], defs, full)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [s for s in schema[key] if s.get("type") != "null"]
            return _value(name, options[0], defs, full)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][-1] if full else schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        properties = schema.get("properties")
        if not properties:
            return {"p12": 20, "p13": 5} if name in ("values", "params") else {}
        return _arguments(schema, defs, full)
    if kind == "array":
        items = schema.get("items", {})
        if name == "operations" and not items.get("properties") and "oneOf" not in items:
            items = {"const": {"method": "nx_set_expression", "params": TEXT}}
        n = schema.get("minItems") or 3
        n = min(max(n, 3 if items.get("type") == "string" else n), schema.get("maxItems") or n)
        return [_value(name, items, defs, full) for _ in range(n)]
    if kind == "string":
        if name in PATHS:
            return "smoke/part.prt"
        if name in TEXT:
            return TEXT[name]
        return "op-7" if name in NOT_IDS else f"obj_{name}"
    if kind == "boolean":
        return full
    if kind == "integer":
        return 3
    if kind == "number":
        return float(next(NUMBERS) % 90 + 5)
    return None


def _arguments(schema, defs, full, tool=""):
    required = set(schema.get("required", []))
    return {
        name: _value(name, sub, defs, full, tool)
        for name, sub in schema.get("properties", {}).items()
        if full or name in required
    }


REGISTERED = tools()


@pytest.mark.parametrize("full", [False, True], ids=["required", "all"])
@pytest.mark.parametrize("tool", sorted(REGISTERED))
def test_every_tool_has_a_short_summary_without_ids(tool, full):
    schema = REGISTERED[tool]
    params = _arguments(schema, schema.get("$defs", {}), full, tool)
    text = summarize(tool, params, name_of=name_of, units="mm")
    assert has_template(tool)
    assert text != humanize(tool), text
    assert len(text) <= MAX_LENGTH and not text.endswith("…"), text
    # Opaque IDs are shown by name; no Python reprs leak into the panel.
    assert "obj_" not in text and "None" not in text, text
    assert "{" not in text and "[" not in text, text
