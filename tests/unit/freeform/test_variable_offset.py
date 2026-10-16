"""nx_create_variable_offset on the shared fake."""

import pytest

from tests.kit import run
from tests.unit.freeform.fakes import face, flat, freeform_nx, made, nx_refuses, sheet
from tests.unit.modeling.fakes import new_body

CORNERS = ([0, 0, 0], [0, 10, 0], [20, 10, 0], [20, 0, 0])  # NX's A..D on the plate


def surf_nx(result_box=(0, 0, 1, 20, 10, 4)):
    """The fake with a 20 x 10 plate face (normal +z) and NX's default corners; the commit
    builds a sheet with ``result_box``."""
    nx = freeform_nx()
    f = face(nx, flat(), area=200.0)
    f.at = lambda *uv: [float(uv[0]), float(uv[1]), 0.0]
    f.box = [0, 0, 0, 20, 10, 0]
    sheet(nx, (0, 0, 0, 20, 10, 0), [f])
    make = nx.builder

    def builder(kind, *args):
        b = make(kind, *args)
        if kind == "VariableOffsetBuilder":
            for k, p in zip("ABCD", CORNERS, strict=True):
                setattr(b, "Point" + k, nx.part.Points.CreatePoint(nx.nx.Point3d(*p)))
        return b

    nx.builder = builder

    def commit(_b):
        g = face(nx, flat(), area=201.4)
        return made(nx, "VAR_OFFSET_SHEET", [sheet(nx, result_box, [g])])

    nx.on_commit("VariableOffsetBuilder", commit)
    return nx, f


def test_variable_offset_at_the_face_corners():
    nx, f = surf_nx()
    ok = run(nx, "nx_create_variable_offset", face=f.ref, distances=[1, 2, 3, "4 mm"])
    assert ok.ok, ok
    b = nx.last("VariableOffsetBuilder")
    V = nx.nx.Features.VariableOffsetBuilder
    assert b.Face.Value is f and b.Method is V.MethodOptions.Linear
    assert [getattr(b, "OffsetAt" + k).RightHandSide for k in "ABCD"] == [
        "1.0", "2.0", "3.0", "4.0"]  # fmt: skip
    assert b.DistanceTolerance == pytest.approx(0.001)
    assert nx.license_queries == []  # NX decides: never asked first
    points = ok.data["points"]
    assert [list(p["point"]) for p in points] == [list(c) for c in CORNERS]
    assert [p["distance"] for p in points] == [1, 2, 3, 4]
    assert max(p["gap"] for p in points) == pytest.approx(0)
    assert ok.data["sheet"] is True and nx.live_builders == 0


def test_variable_offset_at_given_points_cubic():
    nx, f = surf_nx(result_box=(0, 0, 0.3, 20, 10, 3.7))
    given = [[5, 2, 0], [15, 2, 0], [15, 8, 0], [5, 8, 0]]
    ok = run(nx, "nx_create_variable_offset", face=f.ref, distances=[1, 1, 3, 3], points=given,
             method="cubic")  # fmt: skip
    assert ok.ok, ok
    b = nx.last("VariableOffsetBuilder")
    assert b.Method is nx.nx.Features.VariableOffsetBuilder.MethodOptions.Cubic
    coords = [getattr(b, "Point" + k).Coordinates for k in "ABCD"]
    assert [[c.X, c.Y, c.Z] for c in coords] == given
    assert [list(p["point"]) for p in ok.data["points"]] == given


def test_variable_offset_that_misses_a_point_rolls_back():
    nx, f = surf_nx(result_box=(0, 0, 1, 20, 10, 2))  # never reaches 3 and 4
    bad = run(nx, "nx_create_variable_offset", face=f.ref, distances=[1, 2, 3, 4])
    assert bad.error["details"]["check"] == "offset", bad
    assert bad.mutation_outcome == "rolled_back"


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"distances": [0, 0, 0, 0]}, "/distances"),
        ({"distances": [1, 2, 3]}, "/distances"),
        ({"distances": [1, 2, 3, 4], "points": [[0, 0, 5], [1, 0, 0], [1, 1, 0], [0, 1, 0]]},
         "/points/0"),
        ({"distances": [1, 2, 3, 4], "points": [[0, 0, 0], [1, 0, 0], [1, 0, 0], [0, 1, 0]]},
         "/points/2"),
    ],
)  # fmt: skip
def test_variable_offset_rejections_before_nx(args, field):
    nx, f = surf_nx()
    bad = run(nx, "nx_create_variable_offset", face=f.ref, **args)
    assert bad.error["field"] == field, bad
    assert bad.mutation_outcome == "not_started"
    assert not nx.builders


def test_variable_offset_without_license_reports_nx_refusal():
    nx, f = surf_nx()
    nx_refuses(nx, "VariableOffsetBuilder", "nx_freeform_1")
    bad = run(nx, "nx_create_variable_offset", face=f.ref, distances=[1, 2, 3, 4])
    assert bad.error["code"] == "NX_LICENSE_UNAVAILABLE" and bad.error["nx_code"] == 948801
    assert bad.error["details"]["license"] == "nx_freeform_1"
    assert bad.error["details"]["license_check"] == "missing" and nx.live_builders == 0


def test_variable_offset_of_a_solid_face_is_a_new_sheet():
    nx, f = surf_nx()
    solid = new_body(nx, [0, 0, -5, 20, 10, 0])
    f.GetBody = lambda: solid
    ok = run(nx, "nx_create_variable_offset", face=f.ref, distances=[1, 2, 3, 4])
    assert ok.ok and ok.data["sheet"] is True, ok
