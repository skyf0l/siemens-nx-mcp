"""kit.measure, builders.boolean/matrix/fixed_plane and geom.rotation (modeling additions)."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import builders, geom, measure
from tests.kit import fake_nx
from tests.kit.objects import point


def mass_nx(units="mm"):
    nx = fake_nx(units=units)
    nx.nx.MeasureBodies = NS(AnalysisUnit=NS(KilogramMillimeter="kgmm"))
    disposed = []
    seen = {}

    def props(found, accuracy, bodies):
        seen.update(units=list(found), accuracy=accuracy)
        return NS(
            Volume=sum(b.volume for b in bodies),
            Area=600.0,
            Centroid=point(25.4, 0, 50.8),
            Dispose=lambda: disposed.append(True),
        )

    nx.part.MeasureManager = NS(NewMassProperties=props)
    nx.part.UnitCollection = NS(FindObject=lambda name: name)
    return nx, disposed, seen


def test_mass_is_reported_in_part_units_and_always_disposed():
    nx, disposed, seen = mass_nx()
    body = nx.block(10, 10, 10)
    body.volume = 1000.0
    got = measure.mass(nx.tx, [body])
    assert got == {"volume": 1000.0, "area": 600.0, "centroid": [25.4, 0.0, 50.8]}
    assert seen["units"][1] == "CubicMilliMeter" and seen["accuracy"] == 0.999
    assert disposed == [True]
    inch, _, _ = mass_nx("in")
    block = inch.block(1, 1, 1)
    block.volume = 25.4**3
    got = measure.mass(inch.tx, [block])
    assert got["volume"] == pytest.approx(1.0) and got["area"] == pytest.approx(600 / 25.4**2)
    assert got["centroid"] == pytest.approx([1.0, 0.0, 2.0])
    assert measure.volume(inch.tx, block) == pytest.approx(1.0)


def test_mass_needs_bodies_and_disposes_on_failure():
    nx, disposed, _ = mass_nx()
    with pytest.raises(NXError) as error:
        measure.mass(nx.tx, [])
    assert error.value.code == "NX_INVALID_ARGUMENT"

    nx.part.MeasureManager = NS(
        NewMassProperties=lambda *a: NS(Volume="n/a", Dispose=lambda: disposed.append(True))
    )
    with pytest.raises(NXError) as error:
        measure.mass(nx.tx, [nx.block(1, 1, 1)])
    assert error.value.code == "NX_ERROR" and disposed == [True]


@pytest.mark.parametrize(("units", "want"), [("mm", 60.0), ("in", 60.0 / 25.4)])
def test_length_converts_nx_millimetres_to_part_units(units, want):
    nx = fake_nx(units=units)
    edge = NS(GetLength=lambda: 50.8)
    assert measure.length(nx.tx, [edge, NS(GetLength=lambda: 9.2)]) == pytest.approx(want)
    assert measure.length(nx.tx, edge) == pytest.approx(want * 50.8 / 60.0)


def _line(nx, tag, end, reading):
    """A straight curve from the origin to ``end`` (part units) whose ``GetLength`` reads
    ``reading``; UF Eval answers its points in part units."""
    shapes = getattr(nx, "line_ends", {})
    nx.line_ends = shapes
    shapes[tag] = end
    nx.uf.Eval = NS(
        Initialize=lambda t: t,
        AskLimits=lambda h: (0.0, 1.0),
        EvaluateUnitVectors=lambda h, t: ([v * t for v in shapes[h]], [1.0, 0.0, 0.0]),
    )
    return NS(Tag=tag, GetLength=lambda: reading)


@pytest.mark.parametrize(
    ("units", "end", "reading", "want"),
    [
        ("in", [4.0, 0, 0], 101.6, 4.0),  # millimetres, as documented
        ("in", [4.0, 0, 0], 4.0, 4.0),  # part units read as mm (live 2026-10-13)
        ("mm", [101.6, 0, 0], 101.6, 101.6),
        ("mm", [101.6, 0, 0], 101.6 * 25.4, 101.6),  # a mm part read as inches
        ("in", [0.5, 0, 0], 0.5 * 25.4, 0.5),
    ],
)
def test_length_takes_the_units_reading_that_matches_the_sampled_curve(units, end, reading, want):
    """NX 2406 GetLength units follow hidden session state (50.8 or 2 for the same 2 in
    line of a quietly worked inch part): the reading is checked against UF Eval points."""
    nx = fake_nx(units=units)
    line = _line(nx, 41, end, reading)
    assert measure.length(nx.tx, line) == pytest.approx(want)
    other = _line(nx, 42, end, reading)
    assert measure.length(nx.tx, [line, other]) == pytest.approx(2 * want)


def test_length_of_a_curve_uf_eval_cannot_sample_keeps_nx_millimetres():
    nx = fake_nx(units="in")
    assert measure.length(nx.tx, NS(GetLength=lambda: 4.0)) == pytest.approx(4.0 / 25.4)


def test_bounds_union_of_objects():
    nx = fake_nx()
    a, b = nx.block(1, 1, 1), nx.block(2, 2, 2, origin=(5, 0, -1))
    boxes = {int(a.Tag): a.box, int(b.Tag): b.box}
    nx.uf.ModlGeneral = NS(AskBoundingBox=lambda tag: boxes[tag])
    assert measure.bounds(nx.tx, [a, b]) == ([0.0, 0.0, -1.0], [7.0, 2.0, 1.0])
    assert measure.bounds(nx.tx, a) == ([0.0, 0.0, 0.0], [1.0, 1.0, 1.0])


def test_boolean_configures_create_or_targets():
    nx = fake_nx()
    types = NS(Create="create", Unite="unite", Subtract="subtract", Intersect="intersect")
    nx.nx.GeometricUtilities = NS(BooleanOperation=NS(BooleanType=types))
    op = NS(Type=None, targets=None)
    op.SetTargetBodies = lambda bodies: setattr(op, "targets", list(bodies))
    assert builders.boolean(nx.tx, op, None) == [] and op.Type == "create"
    body = nx.block(1, 1, 1)
    combine = {"operation": "subtract", "targets": [body.ref]}
    assert builders.boolean(nx.tx, op, combine) == [body]
    assert op.Type == "subtract" and op.targets == [body]


def test_matrix_rows_are_the_columns_of_the_rotation():
    nx = fake_nx()
    nx.nx.Matrix3x3 = lambda: NS()
    r = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]  # 90 degrees about z
    m = builders.matrix(nx.tx, r)
    assert (m.Xx, m.Xy, m.Xz) == (0.0, 1.0, 0.0)  # the image of x
    assert geom.rows(m) == [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]


def test_fixed_plane_has_the_requested_normal():
    nx = fake_nx()
    nx.nx.Matrix3x3 = lambda: NS()
    made = {}

    def create(origin, matrix):
        made.update(origin=(origin.X, origin.Y, origin.Z), z=(matrix.Zx, matrix.Zy, matrix.Zz))
        return "plane"

    nx.part.Datums.CreateFixedDatumPlane = create
    assert builders.fixed_plane(nx.tx, [1, 2, 3], [0, 1, 0]) == "plane"
    assert made["origin"] == (1.0, 2.0, 3.0) and made["z"] == pytest.approx((0, 1, 0))


def test_rotation_validation():
    assert geom.rotation([[1, 0, 0], [0, 1, 0], [0, 0, 1]]) == geom.IDENTITY
    for bad in (
        [[1, 0, 0], [0, 1, 0]],
        [[1, 0, 0], [0, 1, 0], [0, 0, -1]],
        [[2, 0, 0], [0, 1, 0], [0, 0, 1]],
    ):
        with pytest.raises(NXError) as error:
            geom.rotation(bad, "/transform/rotation")
        assert error.value.field.startswith("/transform/rotation")
