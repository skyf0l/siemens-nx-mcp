"""scripts/build_release.py: required NX files and the tested NX builds in release.json."""

import json
import runpy
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD = runpy.run_path(str(ROOT / "scripts" / "build_release.py"))


def test_tested_builds_come_from_the_evidence_files(tmp_path):
    (tmp_path / "a.json").write_text(
        json.dumps({"tools": {"nx_x": {"v2406": {}, "v2412": {}}, "nx_y": {"v2406": {}}}})
    )
    (tmp_path / "b.json").write_text(json.dumps({"nx_z": {"v2312": {}}, "note": "text"}))
    assert BUILD["tested_builds"](tmp_path) == ["v2312", "v2406", "v2412"]
    assert BUILD["tested_builds"](tmp_path / "missing") == []


def test_the_repository_evidence_names_the_reference_build():
    assert "v2406" in BUILD["tested_builds"](ROOT / "src" / "nx_mcp" / "evidence")


def bundle(tmp_path, files):
    wheels = tmp_path / "wheels"
    wheels.mkdir(parents=True)
    with zipfile.ZipFile(wheels / "nx_mcp-1.0-py3-none-any.whl", "w") as z:
        z.writestr("nx_mcp/evidence/session.json", "{}")
    for name in files:
        path = tmp_path / "source" / "nx_app" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# nx: threaded\n")
    return tmp_path


def test_the_release_needs_the_startup_app_and_the_generic_journal(tmp_path):
    complete = ["startup/nx_mcp_startup.py", "journals/start.py"]
    BUILD["check_contents"](bundle(tmp_path / "ok", complete), "1.0")
    with pytest.raises(SystemExit, match="nx_app/journals/start.py"):
        BUILD["check_contents"](bundle(tmp_path / "old", complete[:1]), "1.0")
