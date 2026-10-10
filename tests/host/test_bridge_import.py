"""Checks that the bridge source imports without third-party site packages."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_nx_bridge_import_succeeds_without_site_packages() -> None:
    """The bridge module must import using only the standard library and source tree."""
    source_root = Path(__file__).parents[2] / "src"
    environment = os.environ | {"PYTHONPATH": str(source_root)}

    result = subprocess.run(
        [sys.executable, "-S", "-c", "import nx_mcp.nx_bridge"],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_nx_host_import_has_no_sidecar_dependencies():
    source_root = Path(__file__).parents[2] / "src"
    modules = "nx_mcp.host.dispatch, nx_mcp.host.services, nx_mcp.interactive"
    result = subprocess.run(
        [sys.executable, "-S", "-c", "import " + modules],
        capture_output=True,
        text=True,
        env=os.environ | {"PYTHONPATH": str(source_root)},
    )
    assert result.returncode == 0, result.stderr


class FakeSession:
    def __init__(self, variable="v2406", **release):
        self.variable = variable
        for name, value in release.items():
            setattr(self, name, value)

    def GetEnvironmentVariableValue(self, name):
        assert name == "UGII_VERSION"
        return self.variable


def test_the_bridge_reports_nx_s_own_release_number():
    from nx_mcp.core.evidence import build_key
    from nx_mcp.nx_bridge import detect_nx_version

    live = FakeSession(FullReleaseNumber="2406.9160", ReleaseNumber="2406.9160")
    assert detect_nx_version(live) == "2406.9160" and build_key("2406.9160") == "v2406"
    assert detect_nx_version(FakeSession(ReleaseNumber="2406.9160")) == "2406.9160"
    # The build key never changes: a release of another major falls back to UGII_VERSION.
    assert detect_nx_version(FakeSession(ReleaseNumber="12.0.2")) == "v2406"
    assert detect_nx_version(FakeSession(ReleaseNumber="2506.1")) == "v2406"
    assert detect_nx_version(FakeSession()) == "v2406"
    assert detect_nx_version(FakeSession(None, ReleaseNumber="2606.100")) == "2606.100"
    assert detect_nx_version(FakeSession(None)) == "NX build unknown"
