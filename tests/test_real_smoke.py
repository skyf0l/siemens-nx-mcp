"""The real-NX smoke outside a live NX session: its steps and failure reporting."""

from pathlib import Path

import pytest

from nx_mcp import real_smoke
from nx_mcp.acceptance import runner
from nx_mcp.core import catalogue


def test_smoke_steps_are_catalogue_calls_in_runner_format():
    tools = catalogue.load()
    for step in real_smoke.STEPS:
        assert step["call"] in tools, step["call"]
        assert set(step) <= runner.STEP_KEYS


@pytest.mark.asyncio
async def test_real_smoke_rejects_non_positive_iteration_count(tmp_path: Path):
    with pytest.raises(ValueError, match="iterations must be at least 1"):
        await real_smoke.run(tmp_path, 0)


def test_failures_name_the_step_or_where_the_run_stopped():
    failed = runner.StepResult(3, "nx_create_extrude", False, False, 1, 0.1, failures=["boom"])
    assert real_smoke._failures([failed], 5) == ["[3] nx_create_extrude: boom"]
    passed = runner.StepResult(1, "nx_get_status", False, True, 1, 0.1)
    assert real_smoke._failures([passed], 5) == ["stopped after step 1 of 5"]
