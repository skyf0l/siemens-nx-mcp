"""Native release receipts must distinguish failures and changed user sessions."""

import ast
import hashlib
import json
import runpy
from pathlib import Path

import pytest

RUNNER = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/validate_native_release.py")
)


def test_session_verification_checks_placements_and_unsaved_state():
    before = {
        "parts": [{"path": "a.prt", "work": True, "display": True, "modified": False}],
        "components": [{"path": "p.prt", "translation": [1, 2, 3]}],
    }
    RUNNER["verify_session"](before, before)
    with pytest.raises(RuntimeError, match="component source"):
        RUNNER["verify_session"](before, {**before, "components": []})
    with pytest.raises(RuntimeError, match="saved state"):
        RUNNER["verify_session"](
            before, {**before, "parts": [{**before["parts"][0], "modified": True}]}
        )


def test_receipt_manifest_hashes_nested_artifacts(tmp_path):
    (tmp_path / "sheets").mkdir()
    (tmp_path / "sheets" / "view.png").write_bytes(b"fixture")
    (tmp_path / "release-validation.json").write_text("not self-hashed")
    assert RUNNER["manifest_files"](tmp_path) == [
        {"path": "sheets/view.png", "size": 7, "sha256": hashlib.sha256(b"fixture").hexdigest()}
    ]


@pytest.mark.asyncio
async def test_agent_ux_client_forwards_tool_name_arguments(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    runner = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/validate_agent_ux.py")
    )
    session = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=SimpleNamespace(isError=False, structuredContent={"status": "success"})
        )
    )
    client = runner["Client"](session, tmp_path / "ux")
    client.schemas = {"nx_create_reference_set": {"properties": {}}}
    await client.call("nx_create_reference_set", name="SOLIDS", objects=["body"])
    session.call_tool.assert_awaited_once_with(
        "nx_create_reference_set", {"name": "SOLIDS", "objects": ["body"]}
    )


def expected_tool_count(script: Path) -> int:
    """Evaluate a script's EXPECTED_TOOL_COUNT assignment without running the suite."""
    tree = ast.parse(script.read_text(encoding="utf-8"))
    (node,) = [
        n
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", None) == "EXPECTED_TOOL_COUNT" for t in n.targets)
    ]
    namespace = {"__file__": str(script), "Path": Path, "runpy": runpy, "json": json}
    exec(compile(ast.Module([node], []), str(script), "exec"), namespace)
    return namespace["EXPECTED_TOOL_COUNT"]


def registered_tool_count(tmp_path) -> int:
    from unittest.mock import AsyncMock

    from nx_mcp.server import create_server
    from nx_mcp.workspace import Workspace

    server = create_server(AsyncMock(), Workspace(tmp_path), surface="full")
    return len(server._tool_manager._tools)


def test_native_suites_accept_runner_profile_count(tmp_path):
    """Every release script expects exactly the tools the server registers.

    The count includes capability_fragments (load_manifest), not only
    capability_manifest.json, and an added tool must not strand a suite on a literal.
    """
    root = Path(__file__).resolve().parents[1]
    registered = registered_tool_count(tmp_path)
    scripts = [root / "scripts/accept_release.py", root / "scripts/validate_native_release.py"]
    scripts += [
        root / "acceptance" / f"validate_{name}.py"
        for name in [
            "advanced_tools",
            "authoring_tools",
            "engineering_tools",
            "freeform_manufacturing",
            "project_folders",
            "sheet_metal",
            "visual_tools",
        ]
    ]
    for script in scripts:
        assert expected_tool_count(script) == registered, script.name
        source = script.read_text(encoding="utf-8")
        if script.parent.name == "acceptance":
            assert 'os.environ.get("NX_EXPECTED_TOOL_COUNT", str(EXPECTED_TOOL_COUNT))' in source
    assert RUNNER["EXPECTED_TOOL_COUNT"] == registered
