"""Shared test fixtures."""

import pytest

from tests.kit.rig import rig  # noqa: F401


@pytest.fixture(autouse=True)
def _fresh_license_memory():
    """Each test starts a new NX session: no remembered license failures."""
    from nx_mcp.core import evidence

    evidence.forget_licenses()
    yield
    evidence.forget_licenses()


@pytest.fixture(autouse=True)
def _isolated_workspace(tmp_path_factory, monkeypatch):
    """Never let a developer's NX_MCP_WORKSPACE leak into a test."""
    monkeypatch.setenv("NX_MCP_WORKSPACE", str(tmp_path_factory.mktemp("workspace")))
