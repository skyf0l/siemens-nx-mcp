"""nx_create_datum_axis / csys / point on the shared fake, through the real kernel."""

from types import SimpleNamespace as NS

import pytest

from tests.kit import run
from tests.kit.objects import Object, matrix, point
from tests.unit.modeling.fakes import feature, modeling_nx

S = 2**-0.5


def axis_feature(nx, origin, direction):
    f = feature(nx, "DATUM_AXIS")
    axis = Object("DATUM_AXIS")
    axis.Origin, axis.Direction = point(*origin), point(*direction)
    f.DatumAxis = axis
    return f


def test_axis_point_direction_and_two_points():
    nx = modeling_nx()
    nx.on_commit("DatumAxisBuilder", lambda b: axis_feature(nx, (5, 5, 5), (0, 0, 2)))
    ok = run(
        nx, "nx_create_datum_axis", method="point_direction", origin=[5, 5, 0], direction=[0, 0, 3]
    )
    assert ok.ok, ok
    assert ok.data["direction"] == [0, 0, 1] and ok.data["datum_axis"]["kind"] == "datum_axis"
    b = nx.last("DatumAxisBuilder")
    assert b.Type is nx.nx.Features.DatumAxisBuilder.Types.PointAndDir
    nx.on_commit("DatumAxisBuilder", lambda b: axis_feature(nx, (1, 1, 0), (1, 1, 0)))
    two = run(
        nx, "nx_create_datum_axis", method="two_points", start=[1, 1, 0], end=[3, 3, 0], name="ax"
    )
    assert two.ok and two.data["direction"] == pytest.approx([S, S, 0])
    # Built as point and direction from start: NX's TwoPoints axis puts its origin
    # beyond the start, where a helix on the axis would then begin.
    b = nx.last("DatumAxisBuilder")
    assert b.Type is nx.nx.Features.DatumAxisBuilder.Types.PointAndDir
    assert [b.Point.Coordinates.X, b.Point.Coordinates.Y, b.Point.Coordinates.Z] == [1, 1, 0]
    assert two.data["origin"] == [1, 1, 0]
    assert two.data["datum_axis"]["name"] in ("ax", "AX") and two.data["feature"]["name"] == "ax"


def test_axis_off_the_requested_line_rolls_back():
    nx = modeling_nx()
    nx.on_commit("DatumAxisBuilder", lambda b: axis_feature(nx, (1, 0, 0), (0, 0, 1)))
    bad = run(
        nx, "nx_create_datum_axis", method="point_direction", origin=[0, 0, 0], direction=[0, 0, 1]
    )
    assert bad.error["details"]["check"] == "on_line" and bad.mutation_outcome == "rolled_back"


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"method": "two_points", "start": [1, 1, 1], "end": [1, 1, 1]}, "/end"),
        ({"method": "two_points", "start": [1, 1, 1]}, "/end"),
        ({"method": "point_direction", "origin": [0, 0, 0], "direction": [0, 0, 0]}, "/direction"),
    ],
)
def test_axis_argument_errors(args, field):
    nx = modeling_nx()
    bad = run(nx, "nx_create_datum_axis", **args)
    assert bad.error["field"] == field and bad.mutation_outcome == "not_started"
    assert nx.builders == []


def csys_feature(nx, origin, x, y):
    z = (x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2], x[0] * y[1] - x[1] * y[0])
    f = feature(nx, "DATUM_CSYS")
    frame = Object("CSYS")
    frame.Origin, frame.Orientation = point(*origin), NS(Element=matrix(x, y, z))
    f.GetEntities = lambda: [frame]
    return f


def test_csys_normalises_axes_and_reads_back_the_frame():
    nx = modeling_nx()
    nx.on_commit("DatumCsysBuilder", lambda b: csys_feature(nx, (10, 5, 10), (S, S, 0), (-S, S, 0)))
    ok = run(nx, "nx_create_datum_csys", origin=[10, 5, 10], x_axis=[1, 1, 0], y_axis=[-1, 1, 0])
    assert ok.ok, ok
    assert ok.data["z_axis"] == pytest.approx([0, 0, 1])
    bad = run(nx, "nx_create_datum_csys", origin=[0, 0, 0], x_axis=[1, 0, 0], y_axis=[-2, 0, 0])
    assert bad.error["field"] == "/y_axis" and bad.mutation_outcome == "not_started"
    assert "parallel" in bad.error["message"] and nx.live_builders == 0
    nx.on_commit("DatumCsysBuilder", lambda b: csys_feature(nx, (0, 0, 0), (0, 1, 0), (-1, 0, 0)))
    wrong = run(nx, "nx_create_datum_csys", origin=[0, 0, 0], x_axis=[1, 0, 0], y_axis=[0, 1, 0])
    assert wrong.error["details"]["check"] == "x_axis" and wrong.mutation_outcome == "rolled_back"


def csys_from_builder(nx):
    """Commit the frame the tool asked for (its Xform's origin and axes)."""

    def made(b):
        xform = nx.part.Xforms.made[-1]
        return csys_feature(nx, xform["origin"], xform["x"], xform["y"])

    return made


def frame_nx():
    nx = modeling_nx()
    made = []

    def create_xform(origin, x, y, *_rest):
        made.append(
            {
                "origin": (origin.X, origin.Y, origin.Z),
                "x": (x.X, x.Y, x.Z),
                "y": (y.X, y.Y, y.Z),
            }
        )
        return NS()

    nx.part.Xforms = NS(CreateXform=create_xform, made=made)
    nx.part.CoordinateSystems = NS(CreateCoordinateSystem=lambda *_a: NS())
    nx.nx.Point3d = lambda *v: NS(X=v[0], Y=v[1], Z=v[2])
    nx.nx.Vector3d = lambda *v: NS(X=v[0], Y=v[1], Z=v[2])
    nx.on_commit("DatumCsysBuilder", csys_from_builder(nx))
    return nx


@pytest.mark.parametrize(
    ("given", "x", "y"),
    [
        ({}, (1, 0, 0), (0, 1, 0)),
        ({"x_axis": [0, 2, 0]}, (0, 1, 0), (-1, 0, 0)),  # z stays world Z
        ({"y_axis": [1, 1, 0]}, (S, -S, 0), (S, S, 0)),
        ({"x_axis": [0, 0, 1]}, (0, 0, 1), (0, 1, 0)),  # x along Z: y is world Y
        ({"y_axis": [0, 0, -1]}, (1, 0, 0), (0, 0, -1)),
    ],
)
def test_csys_defaults_keep_z_near_world_z(given, x, y):
    nx = frame_nx()
    ok = run(nx, "nx_create_datum_csys", origin=[1, 2, 3], **given)
    assert ok.ok, ok
    assert ok.data["x_axis"] == pytest.approx(list(x))
    assert ok.data["y_axis"] == pytest.approx(list(y))
    assert ok.data["origin"] == [1, 2, 3] and not ok.warnings


def test_csys_makes_y_perpendicular_with_a_warning_and_names_the_frame():
    nx = frame_nx()
    ok = run(
        nx,
        "nx_create_datum_csys",
        origin=[10, 0, 0],
        x_axis=[1, 0, 0],
        y_axis=[1, 1, 0],
        name="frame1",
    )
    assert ok.ok, ok
    assert ok.data["y_axis"] == pytest.approx([0, 1, 0])
    assert ok.data["z_axis"] == pytest.approx([0, 0, 1])
    (warning,) = ok.warnings
    assert warning["code"] == "NX_APPROXIMATED" and "45 deg" in warning["message"]
    assert ok.data["datum_csys"]["name"] == "frame1" and ok.data["feature"]["name"] == "frame1"


def test_point_feature_reads_back_the_position():
    nx = modeling_nx()

    def made(b):
        f = feature(nx, "POINT")
        p = Object("POINT")
        p.Coordinates = b.Point.Coordinates
        f.GetEntities = lambda: [p]
        return f

    nx.on_commit("PointFeatureBuilder", made)
    ok = run(nx, "nx_create_datum_point", position=[10, 0, 5], name="tip")
    assert ok.ok and ok.data["position"] == [10, 0, 5]
    assert nx.part.Features[-1].Name == "tip" and nx.live_builders == 0
    # The point row is what a later call selects; it carries the name too.
    assert ok.data["point"]["kind"] == "point"
    assert ok.data["point"]["name"] == "tip"
