"""The unauthenticated HTTP surface binds loopback unless explicitly opted out."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nx_mcp import http_surface
from nx_mcp.http_surface import create_app, resolve_bind
from nx_mcp.workspace import Workspace


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("NX_MCP_HTTP_HOST", "NX_MCP_HTTP_PORT", "NX_MCP_HTTP_ALLOW_REMOTE"):
        monkeypatch.delenv(name, raising=False)


def test_defaults_to_loopback():
    assert resolve_bind() == ("127.0.0.1", 8765)
    assert "192.168" not in Path(http_surface.__file__).read_text(encoding="utf-8")


def test_environment_selects_host_and_port(monkeypatch):
    monkeypatch.setenv("NX_MCP_HTTP_HOST", "localhost")
    monkeypatch.setenv("NX_MCP_HTTP_PORT", "9001")
    assert resolve_bind() == ("localhost", 9001)
    assert resolve_bind("127.0.0.1", 9100) == ("127.0.0.1", 9100)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.52.10"])
def test_non_loopback_requires_explicit_opt_in(monkeypatch, host, tmp_path):
    with pytest.raises(ValueError, match="allow-remote"):
        resolve_bind(host)
    with pytest.raises(ValueError, match="allow-remote"):
        create_app(AsyncMock(), Workspace(tmp_path), host=host)
    monkeypatch.setenv("NX_MCP_HTTP_HOST", host)
    with pytest.raises(ValueError):
        http_surface.main([])
    assert resolve_bind(host, allow_remote=True) == (host, 8765)
    monkeypatch.setenv("NX_MCP_HTTP_ALLOW_REMOTE", "1")
    assert resolve_bind() == (host, 8765)
