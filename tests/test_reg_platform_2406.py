"""Regressions found by the NX 2406 platform/file-tool regression (reg_platform)."""

import pytest

from tests.fakes import Feature

pytestmark = pytest.mark.fake_nx


def test_rename_by_unique_name(rig):
    """Renaming by name raised KeyError (NX_API_ERROR)."""
    feature = Feature("Block")
    feature.Timestamp = 3
    rig.part.Features.append(feature)
    result = rig.e.execute("nx_rename_object", {"object_id": "Block", "name": "Renamed"})
    assert result["object"]["kind"] == "feature" and feature.Name == "Renamed"
