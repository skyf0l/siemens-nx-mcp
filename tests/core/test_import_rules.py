"""core and domains import without pydantic and NXOpen; import rules by AST."""

import ast
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "nx_mcp"

BLOCKED = r"""
import importlib.abc, sys

class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {"pydantic", "pydantic_core", "NXOpen", "mcp", "typing_extensions"}:
            raise ImportError("blocked in the NX-side import test: " + name)
        return None

sys.meta_path.insert(0, Block())
from nx_mcp.core import catalogue, kernel, evidence, receipts, references, summary, tx, types, validate
specs = catalogue.load()
assert "nx_create_datum_plane" in specs, sorted(specs)
leaked = sorted(m for m in sys.modules if m.split(".")[0] in {"pydantic", "NXOpen", "mcp"})
assert not leaked, leaked
print("ok", len(specs))
"""


def test_core_and_domains_import_without_pydantic_or_nxopen():
    result = subprocess.run(
        [sys.executable, "-c", BLOCKED], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("ok")


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module


def test_core_imports_only_core_and_domains_only_core_kit_and_self():
    for path in (SRC / "core").rglob("*.py"):
        for name in _imports(path):
            assert not name.startswith("nx_mcp.") or name.startswith("nx_mcp.core"), (path, name)
            assert name.split(".")[0] != "NXOpen", (path, name)
    for path in (SRC / "domains").rglob("*.py"):
        parts = path.relative_to(SRC / "domains").parts
        own = "nx_mcp.domains." + parts[0] if len(parts) > 1 else "nx_mcp.domains"
        for name in _imports(path):
            assert name.split(".")[0] != "NXOpen", (path, name)
            if name.startswith("nx_mcp"):
                assert name.startswith(("nx_mcp.core", "nx_mcp.kit", own)), (path, name)
