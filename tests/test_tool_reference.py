"""The generated tool reference covers the catalogue and renders schema types."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/generate_tool_reference.py"
SPEC = importlib.util.spec_from_file_location("tool_reference", SCRIPT)
assert SPEC and SPEC.loader
reference = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reference)


def test_render_lists_arguments_types_defaults_and_evidence():
    rows = [
        {
            "name": "nx_create_widget",
            "domain": "modeling",
            "label": "Create widget",
            "effect": "model",
            "description": "Create a widget.",
            "variants": ["a", "b"],
            "schema": {
                "properties": {
                    "method": {"enum": ["a", "b"]},
                    "origin": {"$ref": "#/$defs/Point3"},
                    "limit": {"type": "integer", "default": 20, "description": "Rows | page"},
                },
                "required": ["method"],
                "$defs": {"Point3": {"description": "[x, y, z]", "type": "array"}},
            },
            "evidence": {"variants": ["a"], "scenario": "acceptance/modeling/w.json", "checks": 4},
        },
        {
            "name": "nx_get_thing",
            "domain": "session",
            "label": "Get thing",
            "effect": "read",
            "description": "Read a thing.",
            "variants": [],
            "schema": {"properties": {}},
            "evidence": None,
        },
    ]
    text = reference.render(rows)
    assert "2 catalogue tools in 2 domains; 1 have NX 2406" in text
    assert "| `method` | `a` \\| `b` | required |" in text
    assert "| `origin` | Point3 | optional | [x, y, z] |" in text
    assert "| `limit` | integer | `20` | Rows \\| page |" in text
    assert "tested: a (untested: b); 4 checks in `acceptance/modeling/w.json`" in text
    assert "NX 2406: untested (refused in production)" in text and "No arguments." in text


def test_default_reference_covers_every_catalogue_tool():
    from nx_mcp.core import catalogue

    rows = reference.load_rows()
    assert {r["name"] for r in rows} == set(catalogue.load())
    rendered = reference.render(rows)
    assert all(f"### `{name}`" in rendered for name in catalogue.load())
    assert "| Argument | Type | Default | Notes |" in rendered
