"""kit.select takes bare principal names like gate 1 does ('-z', 'XY plane', 'top'); a
ref-shaped string stays a ref, and errors list the accepted forms."""

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import pose, select


@pytest.mark.parametrize(
    ("value", "normal"),
    [
        ("xy", [0, 0, 1]),
        ("XY plane", [0, 0, 1]),
        ("top", [0, 0, 1]),
        ({"principal": "Front"}, [0, -1, 0]),
        ("yz", [1, 0, 0]),
    ],
)
def test_planes_by_name(value, normal):
    obj, origin, got = select.plane(None, value)
    assert obj is None and origin == [0, 0, 0] and got == normal


@pytest.mark.parametrize(
    ("value", "vector"),
    [("x", [1, 0, 0]), ("-Z", [0, 0, -1]), ("+y", [0, 1, 0]), ("z axis", [0, 0, 1])],
)
def test_axes_and_directions_by_name(value, vector):
    assert select.axis(None, value)[2] == vector
    assert select.direction(None, value) == vector
    assert select.direction(None, {"principal": value}) == vector


def test_refs_stay_refs_and_errors_list_the_forms():
    assert select.principal("e:3", select.PRINCIPAL_AXES) is None
    assert select.principal("d:12", select.PRINCIPAL_PLANES, select.PLANE_ALIASES) is None
    with pytest.raises(NXError) as error:
        select.axis(None, {"principal": "w"})
    assert "'x'/'y'/'z'/'-x'/'-y'/'-z'" in error.value.message
    with pytest.raises(NXError) as error:
        select.direction(None, 5)
    assert "a direction is [x, y, z]" in error.value.message


def test_as_pose_takes_the_axis_angle_form():
    t, r = pose.as_pose({"axis": "z", "angle": 90.0, "origin": [10, 0, 0]})
    assert t == pytest.approx([10, -10, 0])
    assert [round(v, 9) for row in r for v in row] == [0, -1, 0, 1, 0, 0, 0, 0, 1]
