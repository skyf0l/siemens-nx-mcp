import math

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import geom
from tests.kit.objects import matrix, point


def test_vector_basics():
    assert geom.add([1, 2, 3], [1, 1, 1]) == [2, 3, 4]
    assert geom.sub([1, 2, 3], [1, 1, 1]) == [0, 1, 2]
    assert geom.scale([1, 2, 3], 2) == [2, 4, 6]
    assert geom.dot([1, 2, 3], [4, 5, 6]) == 32
    assert geom.cross([1, 0, 0], [0, 1, 0]) == [0, 0, 1]
    assert geom.cross2([1, 0], [0, 1]) == 1
    assert geom.norm([3, 4, 0]) == 5
    assert geom.dist([0, 0, 0], [0, 3, 4]) == 5
    with pytest.raises(ValueError):
        geom.dot([1, 2], [1, 2, 3])  # mismatched lengths are a bug, not a truncation


def test_unit_validates_with_field():
    assert geom.unit([0, 0, 5]) == [0, 0, 1]
    assert geom.unit((3, 4, 0), "/normal") == [0.6, 0.8, 0]
    for bad in ([0, 0, 0], [1, 2], "xyz", [math.inf, 0, 0], [1, "a", 0]):
        with pytest.raises(NXError) as error:
            geom.unit(bad, "/normal")
        assert error.value.code == "NX_INVALID_ARGUMENT" and error.value.field == "/normal"
        assert error.value.message.startswith("normal ")


def test_tolerances_scale_with_coordinates():
    assert geom.tol() == 1e-6
    assert geom.tol([1000, 0, 0], [0, -2000, 0]) == pytest.approx(2e-3)
    assert geom.close(1000.0, 1000.0005) and not geom.close(1.0, 1.01)
    assert geom.close(1.0, 1.0 + 1e-5, rel=1e-4)


def test_direction_predicates():
    z = [0, 0, 1]
    assert geom.parallel(z, [0, 0, -1]) and not geom.same_direction(z, [0, 0, -1])
    assert geom.same_direction(z, [0, 0, 1]) and not geom.parallel(z, [1, 0, 0])
    assert geom.on_plane([5, 5, 10], [0, 0, 10], z)
    assert not geom.on_plane([5, 5, 10.1], [0, 0, 10], z)
    assert geom.on_plane([5, 5, 10.1], [0, 0, 10], z, tol=0.2)


def test_plane_through_points():
    assert geom.plane_through([[0, 0, 5], [0, 10, 5], [10, 0, 5]]) == pytest.approx([0, 0, -1])
    with pytest.raises(NXError, match="collinear") as error:
        geom.plane_through([[0, 0, 0], [1, 1, 1], [2, 2, 2]], "/points")
    assert error.value.field == "/points"
    with pytest.raises(NXError, match="exactly three"):
        geom.plane_through([[0, 0, 0], [1, 1, 1]])
    with pytest.raises(NXError, match=r"points\[1\] must contain three numbers"):
        geom.plane_through([[0, 0, 0], [1, 1], [2, 2, 2]])


def test_frames():
    x, y, z = geom.orthonormal_frame([1, 0, 0], [0, 1, 0])
    assert z == [0, 0, 1]
    with pytest.raises(NXError, match="orthonormal"):
        geom.orthonormal_frame([1, 0, 0], [1, 1, 0])
    for normal in ([0, 0, 1], [1, 0, 0], [1, 2, 3]):
        x, y, z = geom.frame_from_normal(normal)
        assert geom.cross(x, y) == pytest.approx(z)
        assert z == pytest.approx(geom.unit(normal))
        assert geom.dot(x, y) == pytest.approx(0, abs=1e-12)


def test_matrices_and_nx_values():
    m = matrix((0, 1, 0), (-1, 0, 0), (0, 0, 1))  # rotated 90 degrees about z
    r = geom.rows(m)
    assert geom.matvec(r, [1, 0, 0]) == [0, 1, 0]  # local x maps to the frame's X axis
    assert geom.matmul(r, geom.transpose(r)) == geom.IDENTITY
    assert geom.xyz(point(1, 2, 3)) == [1.0, 2.0, 3.0]
    assert all(isinstance(v, float) for v in geom.xyz(point(1, 2, 3)))


def test_arc_plane_normal():
    center = [30.0, 25.0, 20.0]
    full = [[38.0, 25.0, 20.0], [26.0, 31.928, 20.0], [26.0, 18.072, 20.0]]
    assert abs(geom.arc_plane_normal(center, full)[2]) == pytest.approx(1.0)
    assert geom.arc_plane_normal(center, [[38.0, 25.0, 20.0], [22.0, 25.0, 20.0]]) is None
