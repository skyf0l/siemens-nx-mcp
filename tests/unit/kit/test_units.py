import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import units
from tests.kit import fake_nx


def test_normalize_never_returns_inch():
    assert units.normalize("inch") == "in" and units.normalize("IN") == "in"
    assert units.normalize("mm") == "mm"
    with pytest.raises(NXError) as error:
        units.normalize("cm")
    assert error.value.code == "NX_UNSUPPORTED_OPTION"


@pytest.mark.parametrize("unit", ["mm", "in"])
def test_part_and_sheet_units(unit):
    nx = fake_nx(units=unit)
    assert units.part_units(nx.tx) == unit
    sheet = nx.sheet(units="in")
    assert units.sheet_units(nx.tx, sheet) == "in"
    assert units.part_to_sheet(nx.tx, sheet) == pytest.approx(1.0 if unit == "in" else 1 / 25.4)


def test_unknown_sheet_units():
    nx = fake_nx()
    sheet = nx.sheet()
    sheet.Units = "furlong"
    with pytest.raises(NXError) as error:
        units.sheet_units(nx.tx, sheet)
    assert error.value.code == "NX_UNSUPPORTED_OPTION"


def test_conversion():
    assert units.factor("in", "mm") == 25.4
    assert units.convert(10, "length", "in") == 10.0  # a plain number is already in target units
    assert units.convert("25.4 mm", "length", "in") == pytest.approx(1.0)
    assert units.convert(" 1in ", "length", "mm") == pytest.approx(25.4)
    assert units.convert("1e1 mm", "sheet_length", "mm") == 10.0
    assert units.convert("180 deg", "angle", "rad") == pytest.approx(3.141592653589793)
    assert units.convert("0.5 rad", "angle", "deg") == pytest.approx(28.64788975654116)
    for bad, dim in (
        ("30 deg", "length"),
        ("10 mm", "angle"),
        ("ten mm", "length"),
        (True, "length"),
    ):
        with pytest.raises(NXError) as error:
            units.convert(bad, dim, "mm" if dim != "angle" else "deg", field="/distance")
        assert error.value.code == "NX_INVALID_ARGUMENT" and error.value.field == "/distance"


def test_tolerances_respect_inch_parts():
    assert units.tolerance("mm") == 0.001
    assert units.tolerance("in") == pytest.approx(0.001 / 25.4)
    assert units.part_tolerance(fake_nx(units="in").tx) == pytest.approx(0.001 / 25.4)
    assert units.part_tolerance(fake_nx().tx, mm=0.01) == 0.01


def test_say():
    assert units.say(10.0, "mm") == "10 mm"
    assert units.say(1 / 3, "in") == "0.333333 in"
