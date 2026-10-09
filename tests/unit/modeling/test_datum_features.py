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
    nx.on_commit("DatumAxisBuilder", lambda b: axis_feature(nx, (0, 0, 0), (1, 1, 0)))
    two = run(
        nx, "nx_create_datum_axis", method="two_points", start=[1, 1, 0], end=[3, 3, 0], name="ax"
    )
    assert two.ok and two.data["direction"] == pytest.approx([S, S, 0])
    assert nx.last("DatumAxisBuilder").Point2.Coordinates.X == 3


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
    bad = run(nx, "nx_create_datum_csys", origin=[0, 0, 0], x_axis=[1, 0, 0], y_axis=[1, 1, 0])
    assert bad.error["field"] == "/y_axis" and bad.mutation_outcome == "not_started"
    nx.on_commit("DatumCsysBuilder", lambda b: csys_feature(nx, (0, 0, 0), (0, 1, 0), (-1, 0, 0)))
    wrong = run(nx, "nx_create_datum_csys", origin=[0, 0, 0], x_axis=[1, 0, 0], y_axis=[0, 1, 0])
    assert wrong.error["details"]["check"] == "x_axis" and wrong.mutation_outcome == "rolled_back"


def test_point_feature_reads_back_the_position():
    nx = modeling_nx()

    def made(b):
        f = feature(nx, "POINT")
        f.GetEntities = lambda: [NS(Coordinates=b.Point.Coordinates)]
        return f

    nx.on_commit("PointFeatureBuilder", made)
    ok = run(nx, "nx_create_datum_point", position=[10, 0, 5], name="tip")
    assert ok.ok and ok.data["position"] == [10, 0, 5]
    assert nx.part.Features[-1].Name == "tip" and nx.live_builders == 0
