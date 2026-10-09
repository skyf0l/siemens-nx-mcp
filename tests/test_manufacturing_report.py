"""Manufacturing report regressions: contracts, transport recovery and analytic DXF."""

import pytest

from nx_mcp.domains.session.results import nx_get_result
from nx_mcp.result_transport import bound_result, encoded
from nx_mcp.runtime import NXToolError
from tests.unit.parts_files.support import side


def test_result_storage_failure_preserves_commit_and_restore(tmp_path, monkeypatch):
    from pathlib import Path

    def fail(*_):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", fail)
    with pytest.raises(NXToolError) as error:
        bound_result(
            {
                "operation_id": "op",
                "mutation_outcome": "committed",
                "restore_id": "restore",
                "objects": ["x" * 4096] * 300,
            },
            tmp_path,
        )
    assert error.value.code == "NX_RESULT_STORAGE_FAILED"
    assert error.value.details["mutation_outcome"] == "committed"
    assert error.value.details["restore_id"] == "restore"


def test_nested_oversize_pages_remain_bounded_and_addressable(tmp_path):
    value = {str(i): ["x" * 2048] * 150 for i in range(120)}
    full = {
        "status": "success",
        "operation_id": "op",
        "mutation_outcome": "committed",
        "objects": value,
    }
    receipt = bound_result(full, tmp_path / ".nx-mcp" / "bridge-results")
    key = receipt["full_result"]["id"]
    page = side(tmp_path, nx_get_result, result_id=key, field="/objects", offset=100, limit=20)
    assert list(page.data["value"]) == [str(i) for i in range(100, 120)]
    assert page.envelope["page"]["total"] == 120 and page.envelope["page"]["next_offset"] is None
    assert len(encoded(page.envelope)) < 512 * 1024
    child = side(tmp_path, nx_get_result, result_id=key, field="/objects/101", offset=140, limit=10)
    assert len(child.data["items"]) == 10 and child.data["items"][0] == "x" * 2048


@pytest.fixture
def dimension_fixture(monkeypatch):
    import sys
    from types import SimpleNamespace as NS
    from unittest.mock import Mock

    from nx_mcp.drawing_preferences import DrawingPreferencesMixin

    annotation = NS(
        ToleranceType=NS(
            NotSet=0, BilateralTwoLines=1, BilateralOneLine=2, LimitTwoLines=3, Basic=4, Reference=5
        ),
        DimensionUnit=NS(Millimeters=0, Inches=1, Meters=2, Micrometers=3),
        DecimalPointCharacter=NS(Period=0, Comma=1),
    )
    monkeypatch.setitem(sys.modules, "NXOpen", NS(Annotations=annotation))
    monkeypatch.setitem(sys.modules, "NXOpen.Annotations", annotation)
    formatting = NS(
        DisplayTrailingZeros=False, PrimaryDimensionUnit=0, DecimalPointCharacter=0, Dispose=Mock()
    )
    prefs = NS(
        GetUnitsFormatPreferences=lambda: formatting,
        SetUnitsFormatPreferences=Mock(),
        Dispose=Mock(),
    )
    dim = NS(
        GetDimensionPreferences=lambda: prefs,
        SetDimensionPreferences=Mock(),
        ComputedSize=318.61,
        IsRetained=False,
        NumberOfAssociativities=2,
        NominalDecimalPlaces=1,
        ToleranceDecimalPlaces=1,
        UpperToleranceValue=0,
        LowerToleranceValue=0,
        UpperMetricToleranceValue=0,
        LowerMetricToleranceValue=0,
        MetricNominalDecimalPlaces=1,
        MetricToleranceDecimalPlaces=1,
        ToleranceType=0,
        RedisplayObject=Mock(),
    )
    e = DrawingPreferencesMixin()
    e._engineering_owned = lambda *args: dim
    e._reference = lambda *args: {"id": "dimension"}
    e._work_part = lambda: None
    e._units = lambda: "mm"
    return e, dim, formatting
