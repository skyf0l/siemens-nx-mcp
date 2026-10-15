"""kit.readback face read-backs (draft angle, moved faces, same surface) and kit.bodies
open boundary loops, on the shared fake."""

import math
from types import SimpleNamespace as NS

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import bodies, readback
from tests.kit.objects import point
from tests.unit.modeling.fakes import modeling_nx


def test_draft_angle_is_zero_for_a_wall_and_ninety_for_a_floor():
    assert readback.draft_angle([1, 0, 0], [0, 0, 1]) == pytest.approx(0.0)
    assert readback.draft_angle([0, 0, -1], [0, 0, 1]) == pytest.approx(90.0)
    tilted = [math.cos(math.radians(5)), 0, math.sin(math.radians(5))]
    assert readback.draft_angle(tilted, [0, 0, 2]) == pytest.approx(5.0)


def _face(nx, kind, origin, direction, radius=0.0):
    block = nx.block(10, 10, 10)
    face = nx.remember(nx.face(normal=direction, origin=origin, kind=kind, body=block))
    face.radius = radius
    return face


def test_expect_moved_reads_planes_along_the_normal_and_cylinders_across_the_axis():
    nx = modeling_nx()
    hole = _face(nx, "cylinder", (10, 10, 0), (0, 0, 1), 4.0)
    top = _face(nx, "plane", (0, 0, 10), (0, 0, 1))
    before = [readback.face_frame(nx.tx, f) for f in (hole, top)]
    nx.face_data[hole.Tag] = ("cylinder", (18, 10, 5), (0, 0, 1))  # slid 8 in x (z: along axis)
    nx.face_data[top.Tag] = ("plane", (3, 0, 10), (0, 0, 1))  # an in-plane slide shows nothing
    assert readback.expect_moved(nx.tx, before, [hole, top], [8, 0, 0]) == 1
    with pytest.raises(NXError) as error:
        readback.expect_moved(nx.tx, before, [hole, top], [0, 8, 0])
    assert error.value.details["check"] == "moved[0]"


def test_expect_moved_skips_faces_nx_consumed():
    nx = modeling_nx()
    top = _face(nx, "plane", (0, 0, 10), (0, 0, 1))
    before = [readback.face_frame(nx.tx, top)]
    del nx.face_data[top.Tag]  # AskFaceData now fails: a dead tag
    assert readback.face_frame(nx.tx, top) is None
    assert readback.expect_moved(nx.tx, before, [top], [0, 0, 5]) == 0


def test_same_surface_of_planes_and_cylinders():
    plane = {"type": readback.PLANE, "point": [0, 0, 5], "dir": [0, 0, 1], "radius": 0.0}
    assert readback.same_surface(plane, {**plane, "point": [7, 3, 5], "dir": [0, 0, -1]})
    assert not readback.same_surface(plane, {**plane, "point": [0, 0, 6]})
    cyl = {"type": readback.CYLINDER, "point": [1, 1, 0], "dir": [0, 0, 1], "radius": 3.0}
    assert readback.same_surface(cyl, {**cyl, "point": [1, 1, 40]})
    assert not readback.same_surface(cyl, {**cyl, "radius": 4.0})
    assert not readback.same_surface(cyl, plane)


def _edge(faces, *points):
    return NS(GetFaces=lambda: list(faces), GetVertices=lambda: [point(*p) for p in points])


def test_open_loops_counts_boundary_loops_of_a_sheet():
    square = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
    cup = NS(GetEdges=lambda: [_edge(["f"], square[i], square[(i + 1) % 4]) for i in range(4)]
             + [_edge(["f", "g"], (0, 0, 0), (0, 0, -1))])  # fmt: skip
    assert bodies.open_loops(cup) == 1
    tube = NS(GetEdges=lambda: [_edge(["f"], (5, 0, 0), (5, 0, 0)),
                                _edge(["f"], (5, 0, 9), (5, 0, 9))])  # fmt: skip
    assert bodies.open_loops(tube) == 2
    assert bodies.open_loops(NS(GetEdges=lambda: [NS()])) is None  # no edge faces to read
