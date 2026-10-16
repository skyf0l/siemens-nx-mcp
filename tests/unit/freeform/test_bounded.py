"""nx_create_bounded_plane on the shared fake."""

import pytest

from tests.kit import run
from tests.kit.fake import FakeSection
from tests.unit.freeform.fakes import (
    curve,
    edge,
    face,
    flat,
    freeform_nx,
    made,
    nx_refuses,
    sheet,
)

RECT = [[0, 0, 0], [40, 0, 0], [40, 20, 0], [0, 20, 0]]


def rect_curves(nx, z=0.0, lift=None):
    pts = [[x, y, z] for x, y, _ in RECT]
    if lift is not None:
        pts[2][2] += lift
    return [curve(nx, pts[i], pts[(i + 1) % 4]) for i in range(4)]


def bounded_nx(area=800.0):
    nx = freeform_nx()
    make = nx.builder

    def builder(kind, *args):
        b = make(kind, *args)
        if kind == "BoundedPlaneBuilder":
            object.__setattr__(b, "BoundingCurves", FakeSection())
        return b

    nx.builder = builder

    def commit(_b):
        f = face(nx, flat(), area=area)
        f.box = [0, 0, 0, 40, 20, 0]
        ends = [edge(nx, RECT[i], RECT[(i + 1) % 4], [f]) for i in range(4)]
        return made(nx, "BOUNDED_PLANE", [sheet(nx, (0, 0, 0, 40, 20, 0), [f], ends)])

    nx.on_commit("BoundedPlaneBuilder", commit)
    return nx


def test_bounded_plane_fills_a_closed_loop():
    nx = bounded_nx()
    loop = rect_curves(nx)
    ok = run(nx, "nx_create_bounded_plane", boundary=[c.ref for c in loop[::-1]])
    assert ok.ok, ok
    b = nx.last("BoundedPlaneBuilder")
    assert b.BoundingCurves.rules == [("CurveDumb", loop[::-1])]
    assert nx.license_queries == []  # NX decides: never asked first
    assert ok.data["loops"] == 1 and ok.data["area"] == pytest.approx(800)
    assert [abs(v) for v in ok.data["normal"]] == pytest.approx([0, 0, 1])
    assert ok.data["gap"] == pytest.approx(0) and ok.data["sheet"] is True
    assert nx.live_builders == 0


def test_bounded_plane_counts_a_hole_loop():
    nx = bounded_nx(area=700.0)
    outer = rect_curves(nx)
    inner = [curve(nx, [10, 5, 0], [20, 5, 0]), curve(nx, [20, 5, 0], [10, 5, 0])]
    ok = run(nx, "nx_create_bounded_plane", boundary=[c.ref for c in outer + inner])
    assert ok.ok, ok
    assert ok.data["loops"] == 2


@pytest.mark.parametrize(
    ("make", "field", "code"),
    [
        (lambda nx: rect_curves(nx)[:3], "/boundary", "NX_WRONG_GEOMETRY"),
        (lambda nx: rect_curves(nx, lift=3), "/boundary", "NX_WRONG_GEOMETRY"),
    ],
)  # fmt: skip
def test_bounded_plane_refuses_open_or_3d_loops(make, field, code):
    nx = bounded_nx()
    bad = run(nx, "nx_create_bounded_plane", boundary=[c.ref for c in make(nx)])
    assert bad.error["code"] == code and bad.error["field"] == field, bad
    assert bad.mutation_outcome == "not_started" and not nx.builders


def test_bounded_plane_refuses_a_repeated_curve():
    nx = bounded_nx()
    loop = rect_curves(nx)
    bad = run(nx, "nx_create_bounded_plane", boundary=[c.ref for c in loop] + [loop[0].ref])
    assert bad.error["field"] == "/boundary" and not nx.builders


def test_bounded_plane_without_license_reports_nx_refusal_naming_the_license():
    nx = bounded_nx()
    nx_refuses(nx, "BoundedPlaneBuilder", "nx_freeform_1")
    bad = run(nx, "nx_create_bounded_plane", boundary=[c.ref for c in rect_curves(nx)])
    assert bad.error["code"] == "NX_LICENSE_UNAVAILABLE" and bad.error["nx_code"] == 948801
    assert bad.error["details"]["license"] == "nx_freeform_1"
    assert bad.error["details"]["license_check"] == "missing" and nx.live_builders == 0
    assert nx.builders  # NX was asked: the license server's answer is only a note


def test_bounded_plane_that_misses_the_boundary_rolls_back():
    nx = bounded_nx()
    loop = rect_curves(nx, z=5.0)  # the fake sheet stays at z 0
    bad = run(nx, "nx_create_bounded_plane", boundary=[c.ref for c in loop])
    assert bad.error["details"]["check"] == "boundary", bad
    assert bad.mutation_outcome == "rolled_back"


def test_bounded_plane_refuses_two_separate_outer_loops():
    nx = bounded_nx()
    left = rect_curves(nx)
    right = [curve(nx, [60 + a[0], a[1], 0], [60 + b[0], b[1], 0])
             for a, b in zip(RECT, RECT[1:] + RECT[:1], strict=True)]  # fmt: skip
    bad = run(nx, "nx_create_bounded_plane", boundary=[c.ref for c in left + right])
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and "one outer loop" in bad.error["message"]
    assert [len(loop) for loop in bad.error["details"]["loops"]] == [4, 4]
    assert bad.mutation_outcome == "not_started" and not nx.builders


def test_bounded_plane_says_when_planar_loops_lie_in_different_planes():
    nx = bounded_nx()
    low, high = rect_curves(nx, z=0.0), rect_curves(nx, z=50.0)
    bad = run(nx, "nx_create_bounded_plane", boundary=[c.ref for c in low + high])
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and "different planes" in bad.error["message"]
    assert "n_sided" not in bad.error["hint"] and "one call per loop" in bad.error["hint"]
