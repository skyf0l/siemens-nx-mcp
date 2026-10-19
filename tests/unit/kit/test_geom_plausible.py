"""kit.geom plausibility checks and kit.readback coincident bodies."""

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import geom, readback


def test_a_length_far_beyond_the_geometry_is_refused_with_its_field():
    size = geom.extent([0, 0, 0], [30, 40, 0])
    assert size == 50
    geom.check_plausible(5000, size, "/distance", "distance")  # 100 times: still taken
    with pytest.raises(NXError) as caught:
        geom.check_plausible(-5001, size, "/distance", "distance")
    error = caught.value
    assert error.code == "NX_INVALID_ARGUMENT" and error.field == "/distance"
    assert error.details["size"] == 50 and "100 times" in error.message
    geom.check_plausible(1e9, 0.0, "/distance", "distance")  # no size: unchecked


def test_a_point_outside_the_modeling_box_is_refused():
    geom.check_in_box([1e5, 0, 0], geom.SIZE_BOX_MM, "/position", "position")
    with pytest.raises(NXError) as caught:
        geom.check_in_box([1e30, 0, 0], geom.SIZE_BOX_MM, "/position", "position")
    assert caught.value.field == "/position" and caught.value.details["limit"] == 5e5


def test_coincident_bodies_share_box_and_volume():
    box = ([0, 0, 0], [10, 10, 10])
    others = [
        ("same", ([0, 0, 0], [10, 10, 10.000001]), 1000.0),
        ("moved", ([1, 0, 0], [11, 10, 10]), 1000.0),
        ("hollow", ([0, 0, 0], [10, 10, 10]), 600.0),
    ]
    assert readback.coincident(box, 1000.0, others) == ["same"]
    assert readback.coincident(box, None, others) == ["same", "hollow"]
    assert readback.same_box(box, box) and not readback.same_box(box, others[1][1])
