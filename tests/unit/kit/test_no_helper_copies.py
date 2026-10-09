"""Shared helpers live once, in ``nx_mcp.kit`` (architecture.md 1.4, rule 2).

Fails when a module outside ``kit/`` defines its own module-level copy of a kit
helper (vector math, validators, enum names, units, sheet and sketch helpers).
Import the kit version instead (legacy code that must raise ``NXToolError``
imports it from ``nx_mcp.legacy_kit``).
"""

import ast
import sys
from pathlib import Path

import pytest

import nx_mcp

ROOT = Path(nx_mcp.__file__).parent

# Compared after stripping leading underscores (``_dot`` is ``dot``).
HELPERS = {
    # geom
    "add", "sub", "scale", "dot", "cross", "cross2", "norm", "dist", "unit", "unit_normal",
    "xyz", "rows", "matvec", "matmul", "transpose", "tol", "tolerance", "close", "parallel",
    "same_direction", "on_plane", "plane_through", "orthonormal_frame", "plane_frame",
    "frame_from_normal", "arc_plane_normal",
    # args
    "vec", "vector", "vec3", "number", "finite", "positive", "point2", "points3",
    "sheet_point", "ids", "id", "id_list", "object_id", "object_ids", "distinct_ids", "choice",
    # result / units / drawing / errors
    "enum_name", "page", "seen_true_shape", "drawing_work_view", "work_view_is_drawing",
    "native_input_error", "native_formula",
}  # fmt: skip

# (module, function) copies not removed yet, with the reason. Remove an entry when
# its module migrates; a stale entry fails the test.
ALLOWLIST = {
    (
        "authoring.py",
        "page",
    ): "legacy page shape {items,total,...}; kit.result.page replaces it per domain",
}


def _modules():
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in {"kit", "core"} or rel.name == "legacy_kit.py":
            continue
        yield rel, path


def _copies():
    found = set()
    for rel, path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            is_def = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if is_def and node.name.lstrip("_") in HELPERS:
                found.add((rel.as_posix(), node.name))
    return found


def test_no_module_level_helper_copies_outside_kit():
    copies = _copies() - set(ALLOWLIST)
    assert not copies, f"Import these from nx_mcp.kit instead of copying them: {sorted(copies)}"


def test_allowlist_has_no_stale_entries():
    stale = set(ALLOWLIST) - _copies()
    assert not stale, f"Remove migrated entries from ALLOWLIST: {sorted(stale)}"


KIT = ROOT / "kit"


@pytest.mark.parametrize("path", sorted(KIT.glob("*.py")), ids=lambda p: p.name)
def test_kit_imports_only_stdlib_and_core(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or ""]
        for name in names:
            top = name.split(".")[0]
            if top == "nx_mcp":
                assert name.startswith(("nx_mcp.core", "nx_mcp.kit")), f"{path.name}: {name}"
            else:
                assert top in sys.stdlib_module_names or top == "__future__", f"{path.name}: {name}"


def test_kit_codes_are_in_the_error_table():
    from nx_mcp.core.errors import CODES

    for path in sorted(KIT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                if text.startswith("NX_") and text.isupper() and " " not in text:
                    assert text in CODES, f"{path.name}: {text} is not a stable code"
