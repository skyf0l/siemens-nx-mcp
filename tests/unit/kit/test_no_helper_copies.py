"""Shared helpers live once, in ``nx_mcp.kit`` (architecture.md 1.4, rule 2).

Fails when a module outside ``kit/`` defines its own copy of a kit helper (vector
math, reflections, validators, enum names, units, sheet and sketch helpers), at
module level or nested in a function, or re-codes a kit idiom inline (a raw
``GetLength()``, a one-rule ``ReplaceRules([...Dumb(...)])``). Import the kit
version instead.
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
    # sketch
    "snake",
}  # fmt: skip
# Also caught nested inside a function (``add`` or ``close`` closures are common
# and harmless; these names are not).
NESTED = {
    # geom.reflect / reflect_across_line, builders.fill
    "reflect", "reflect_point", "reflect_across_line", "mirror_point", "rule_bodies",
    "fill_collector",
}  # fmt: skip
HELPERS |= NESTED

# (module, function) copies not removed yet, with the reason. Remove an entry when
# its module migrates; a stale entry fails the test.
ALLOWLIST: dict[tuple[str, str], str] = {}


def _modules():
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in {"kit", "core"}:
            continue
        yield rel, path


def _copies():
    found = set()
    for rel, path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        top = set(map(id, tree.body))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = node.name.lstrip("_")
            if name in NESTED or (id(node) in top and name in HELPERS):
                found.add((rel.as_posix(), node.name))
    return found


def test_no_helper_copies_outside_kit():
    copies = _copies() - set(ALLOWLIST)
    assert not copies, f"Import these from nx_mcp.kit instead of copying them: {sorted(copies)}"


def test_allowlist_has_no_stale_entries():
    stale = set(ALLOWLIST) - _copies()
    assert not stale, f"Remove migrated entries from ALLOWLIST: {sorted(stale)}"


def _inline_idioms():
    """Calls that re-code a kit helper in place."""
    found = []
    for rel, path in _modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            where = f"{rel.as_posix()}:{node.lineno}"
            if node.func.attr == "GetLength":  # NX 2406 answers mm: kit.measure.length
                found.append(f"{where} GetLength() -> measure.length")
            first = node.args[0] if node.args else None
            if node.func.attr == "ReplaceRules" and isinstance(first, ast.List):
                rules = first.elts
                one = rules[0] if len(rules) == 1 else None
                if (
                    isinstance(one, ast.Call)
                    and isinstance(one.func, ast.Attribute)
                    and one.func.attr in {"CreateRuleBodyDumb", "CreateRuleFaceDumb",
                                          "CreateRuleEdgeDumb"}
                ):  # fmt: skip
                    found.append(f"{where} ReplaceRules([{one.func.attr}]) -> builders.fill")
    return found


def test_no_inline_copies_of_kit_idioms():
    found = _inline_idioms()
    assert not found, f"Use the kit helper instead: {found}"


KIT = ROOT / "kit"


def _body(node):
    """A function's statements without its docstring, names and positions stripped."""
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    args = [a.arg for a in node.args.args]
    text = ast.dump(ast.Module(body=body, type_ignores=[]), annotate_fields=False)
    for i, name in enumerate(args):  # parameter names do not make a different helper
        text = text.replace(f"'{name}'", f"'_arg{i}'")
    return text


def _kit_bodies():
    out = {}
    for path in sorted(KIT.glob("*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef) and len(node.body) > 0:
                out.setdefault(_body(node), f"kit/{path.name}:{node.name}")
    return out


def test_no_module_level_copy_of_a_kit_helper_body_under_another_name():
    kit = _kit_bodies()
    copies = []
    for rel, path in _modules():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef) and _body(node) in kit:
                copies.append(f"{rel.as_posix()}:{node.name} copies {kit[_body(node)]}")
    assert not copies, f"Import these from nx_mcp.kit instead of copying them: {copies}"


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
    from nx_mcp.core.errors import CODES, WARNING_CODES

    for path in sorted(KIT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                if text.startswith("NX_") and text.isupper() and " " not in text:
                    stable = text in CODES or text in WARNING_CODES
                    assert stable, f"{path.name}: {text} is not a stable code"
