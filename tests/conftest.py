"""Shared test fixtures."""

import pytest

from tests.fakes import rig  # noqa: F401


@pytest.fixture(autouse=True)
def _isolated_workspace(tmp_path_factory, monkeypatch):
    """Never let a developer's NX_MCP_WORKSPACE leak into a test."""
    monkeypatch.setenv("NX_MCP_WORKSPACE", str(tmp_path_factory.mktemp("workspace")))
