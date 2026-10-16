from pathlib import Path

import pytest

from nx_mcp.workspace import Workspace, WorkspaceViolation


def test_workspace_accepts_relative_paths_inside_root(tmp_path: Path):
    workspace = Workspace(tmp_path)

    assert workspace.resolve("parts/bracket.prt") == tmp_path / "parts" / "bracket.prt"


@pytest.mark.parametrize("path", ["../outside.prt", "C:/outside.prt"])
def test_workspace_rejects_paths_outside_root(tmp_path: Path, path: str):
    workspace = Workspace(tmp_path)

    with pytest.raises(WorkspaceViolation, match="workspace"):
        workspace.resolve(path)


def test_workspace_accepts_absolute_path_already_resolved_inside_root(tmp_path: Path):
    workspace = Workspace(tmp_path)
    safe_path = tmp_path / "parts" / "bracket.prt"

    assert workspace.ensure_inside(safe_path) == safe_path


def test_workspace_rejects_absolute_path_outside_root(tmp_path: Path):
    workspace = Workspace(tmp_path)

    with pytest.raises(WorkspaceViolation, match="workspace"):
        workspace.ensure_inside(tmp_path.parent / "outside.prt")


def test_resolve_accepts_absolute_inside_root(tmp_path):
    path = tmp_path / "projects" / "controller" / "part.prt"
    assert Workspace(tmp_path).resolve(str(path)) == path


@pytest.mark.parametrize("path", ["C:relative.prt", r"\relative.prt", ".NX-MCP/state.json"])
def test_rejects_ambiguous_or_reserved_paths(tmp_path, path):
    with pytest.raises(WorkspaceViolation):
        Workspace(tmp_path).resolve(path)


def test_symlink_cannot_escape_workspace(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    try:
        (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks require host privileges")
    with pytest.raises(WorkspaceViolation):
        Workspace(root).resolve("escape/outside.prt")


ROOT = Path(__file__).resolve().parents[1]
# Bootstraps that run before nx_mcp is importable repeat default_workspace() verbatim.
STDLIB_DEFAULT = 'os.environ.get("NX_MCP_WORKSPACE") or str(Path.home() / "NX_MCP_WORKSPACE")'
STDLIB_USERS = ["nx_app/startup/nx_mcp_startup.py", "scripts/dev_lock.py", "scripts/dev_reload.py"]
IMPORT_USERS = [
    "src/nx_mcp/server.py",
    "src/nx_mcp/interactive.py",
    "src/nx_mcp/acceptance/runner.py",
    "nx_app/journals/start_batch.py",
]


def test_default_workspace_is_the_variable_else_the_home_folder(tmp_path, monkeypatch):
    from nx_mcp.workspace import default_workspace

    monkeypatch.setenv("NX_MCP_WORKSPACE", str(tmp_path / "set"))
    assert default_workspace() == tmp_path / "set"
    monkeypatch.delenv("NX_MCP_WORKSPACE")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert default_workspace() == tmp_path / "NX_MCP_WORKSPACE"


@pytest.mark.parametrize("variable", ["set", None])
def test_every_host_script_and_the_sidecar_share_the_default(tmp_path, monkeypatch, variable):
    import os

    from nx_mcp.workspace import default_workspace

    if variable:
        monkeypatch.setenv("NX_MCP_WORKSPACE", str(tmp_path / variable))
    else:
        monkeypatch.delenv("NX_MCP_WORKSPACE")
    for name in STDLIB_USERS:
        assert STDLIB_DEFAULT in (ROOT / name).read_text(encoding="utf-8"), name
    assert Path(eval(STDLIB_DEFAULT, {"os": os, "Path": Path})) == default_workspace()
    for name in IMPORT_USERS:
        assert "default_workspace()" in (ROOT / name).read_text(encoding="utf-8"), name


def test_no_drive_specific_workspace_defaults():
    sources = [*(ROOT / "src").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]
    sources += [*(ROOT / "nx_app").rglob("*.py")]
    for source in sources:
        text = source.read_text(encoding="utf-8")
        assert r"D:\CAD" not in text and r"D:\NX_MCP_WORKSPACE" not in text, source
