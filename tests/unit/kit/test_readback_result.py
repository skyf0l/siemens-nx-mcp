import math
from types import SimpleNamespace as NS

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import readback, result
from tests.kit import fake_nx


def _check(call, *a, **k):
    with pytest.raises(NXError) as error:
        call(*a, **k)
    assert error.value.code == "NX_VERIFICATION_FAILED"
    assert error.value.category == "verification_failed"
    return error.value.details


def test_expect():
    readback.expect(True, "volume")
    assert _check(readback.expect, False, "volume", volume=3.0) == {
        "check": "volume",
        "actual": {"volume": 3.0},
    }


def test_expect_close_scalars_and_vectors():
    assert readback.expect_close(25.0000001, 25, "offset") == 25.0000001
    assert _check(readback.expect_close, 25.1, 25, "offset")["check"] == "offset"
    readback.expect_close([0, 0, 1000.0005], [0, 0, 1000], "origin")  # scaled tolerance
    assert _check(readback.expect_close, [0, 0, 1], [0, 0], "origin")["check"] == "origin"
    assert _check(readback.expect_close, math.nan, 1.0, "radius")["check"] == "radius"
    readback.expect_close(1.05, 1.0, "loose", tol=0.1)


def test_direction_and_plane_checks():
    assert readback.expect_same_direction([0, 0, 2], [0, 0, 1], "normal") == [0, 0, 1]
    details = _check(readback.expect_same_direction, [0, 0, -1], [0, 0, 1], "normal")
    assert details["angle_deg"] == pytest.approx(180)
    readback.expect_parallel([0, 0, -1], [0, 0, 1], "axis")
    assert _check(readback.expect_parallel, [1, 0, 0], [0, 0, 1], "axis")["check"] == "axis"
    assert (
        _check(readback.expect_same_direction, [0, 0, 0], [0, 0, 1], "normal")["check"] == "normal"
    )
    assert readback.expect_on_plane([5, 5, 10], [0, 0, 10], [0, 0, 1], "on_plane[0]") == 0
    assert _check(readback.expect_on_plane, [5, 5, 11], [0, 0, 10], [0, 0, 1], "on_plane[1]")[
        "distance"
    ] == pytest.approx(1)


def test_rows_page_measured_rename():
    nx = fake_nx()
    block = nx.block(1, 1, 1)
    rows = result.rows(nx.tx, [block], "body")
    assert rows[0]["id"] == block.ref and rows[0]["kind"] == "body"
    selected, page = result.page(list(range(45)), 40, 20)
    assert selected == [40, 41, 42, 43, 44]
    assert page == {"offset": 40, "limit": 20, "returned": 5, "total": 45, "next_offset": None}
    assert result.page(list(range(45)), 0, 20)[1]["next_offset"] == 20
    for offset, limit in ((-1, 20), (0, 0), (0, 201), (True, 5)):
        with pytest.raises(NXError) as error:
            result.page([], offset, limit)
        assert error.value.code == "NX_INVALID_ARGUMENT"
    assert result.measured("2.5") == 2.5
    with pytest.raises(NXError) as error:
        result.measured(math.inf, "volume")
    assert error.value.code == "NX_INCOMPLETE_RESULT"

    def upper(name):
        block.Name = name.upper()

    block.SetName = upper
    assert result.rename(nx.tx, block, "base") == "BASE"


class Status:
    UnderConstrained = 1
    WellConstrained = 2


class IntText:
    """An NX 2406 enum value: compares unequal but stringifies as its integer."""

    def __init__(self, value):
        self.value = value

    def __str__(self):
        return str(self.value)


def test_enum_name():
    assert result.enum_name(2, Status) == "WellConstrained"
    assert result.enum_name(999, NS(A=1)) == "unknown_999"
    assert result.enum_name(IntText(1), Status) == "UnderConstrained"
    assert result.enum_name(13, NS(VButt=4, Fillet=13), ["VButt", "Fillet"]) == "Fillet"
    # Python NXOpen cannot expose a member called None: listed, it matches 0.
    assert result.enum_name(0, NS(Fillet=13), ["Fillet", "None"]) == "None"
    assert result.enum_name(7, NS(Fillet=13), ["Fillet"], default="7") == "7"


def test_enum_int_reads_ints_printed_members_and_value_attributes():
    class Printed:  # NX 2406 members that only print as their value
        def __str__(self):
            return "4"

    assert result.enum_int(3) == 3
    assert result.enum_int(Printed()) == 4
    assert result.enum_int(NS(value=2)) == 2
    with pytest.raises(ValueError):
        result.enum_int("Shaded")
