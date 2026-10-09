"""Tests for the real-NX acceptance runner outside a live NX session."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from nx_mcp.real_smoke import run, run_iteration


class NonTextErrorClient:
    async def call_tool(self, name: str, arguments: dict):
        return SimpleNamespace(isError=True, content=[SimpleNamespace()])


@pytest.mark.asyncio
async def test_real_smoke_reports_non_text_error_content(tmp_path: Path):
    with pytest.raises(RuntimeError, match="nx_get_status failed: unknown error"):
        await run_iteration(NonTextErrorClient(), tmp_path, 1)


@pytest.mark.asyncio
async def test_real_smoke_rejects_non_positive_iteration_count(tmp_path: Path):
    with pytest.raises(ValueError, match="iterations must be at least 1"):
        await run(tmp_path, 0)
