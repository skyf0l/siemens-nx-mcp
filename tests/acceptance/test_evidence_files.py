"""Committed evidence: deterministic proof only, never a local path or a username."""

import getpass
import json
import re
from pathlib import Path

import pytest

from nx_mcp.acceptance import runner

FILES = sorted(runner.EVIDENCE.rglob("*.json")) + sorted(
    Path(runner.core_evidence.EVIDENCE_DIR).glob("*.json")
)
TOP = {
    "format", "scenario", "scenario_sha256", "domain", "covers", "nx_version", "nx_build",
    "sources", "recorded", "passed", "checks", "failed_checks", "steps",
}  # fmt: skip
STEP = {"index", "call", "setup", "passed", "checks"}
FAILED_STEP = STEP | {"error", "failures"}
# A drive path (C:\ or C:/), a UNC path (four backslashes in JSON text), or a home
# directory on any OS.
ABSOLUTE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]|\\\\\\\\[A-Za-z0-9]|/(?:Users|home)/", re.IGNORECASE
)


def test_evidence_files_exist():
    assert any(p.parent.parent.parent == runner.EVIDENCE for p in FILES)
    assert any(p.parent == Path(runner.core_evidence.EVIDENCE_DIR) for p in FILES)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.relative_to(runner.ROOT).as_posix())
def test_no_evidence_file_records_an_absolute_path_or_username(path):
    text = path.read_text(encoding="utf-8")
    assert not ABSOLUTE.search(text), ABSOLUTE.search(text)
    for home in {str(Path.home()), Path.home().as_posix()}:
        assert home.lower() not in text.lower()
    user = getpass.getuser()
    for prefix in ("\\\\Users\\\\", "/Users/", "/home/"):  # JSON escapes a backslash
        assert (prefix + user).lower() not in text.lower()


@pytest.mark.parametrize(
    "path",
    [p for p in FILES if p.is_relative_to(runner.EVIDENCE)],
    ids=lambda p: p.relative_to(runner.EVIDENCE).as_posix(),
)
def test_evidence_files_hold_only_the_deterministic_proof(path):
    record = json.loads(path.read_text(encoding="utf-8"))
    assert set(record) == TOP and record["format"] == runner.EVIDENCE_FORMAT
    assert record["nx_build"] != record["nx_version"]  # NX's own build, e.g. 2406.9160
    assert record["passed"] == (not path.name.endswith(".last-failure.json"))
    for step in record["steps"]:
        assert set(step) <= (STEP if step["passed"] else FAILED_STEP) and set(step) >= STEP
