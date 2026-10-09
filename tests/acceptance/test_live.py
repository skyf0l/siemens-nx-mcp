"""Release gate: every acceptance scenario passes on the live NX bridge (real_nx)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nx_mcp.acceptance import runner

pytestmark = pytest.mark.real_nx


async def test_every_scenario_passes_on_live_nx(tmp_path) -> None:
    if os.environ.get("NX_MCP_REAL_NX") != "1":
        pytest.skip("requires NX_MCP_REAL_NX=1 on a dedicated Siemens NX runner")
    workspace = Path(os.environ["NX_MCP_WORKSPACE"]).resolve()
    owner = os.environ.get("NX_MCP_REAL_NX_OWNER", "pytest-real-nx")
    # Evidence of this run goes to tmp_path; commit evidence with `acceptance run` + `fold`.
    code = await runner.run(None, workspace=workspace, owner=owner, evidence_root=tmp_path)
    assert code == 0
    assert list(tmp_path.glob("*/*/*.json"))
