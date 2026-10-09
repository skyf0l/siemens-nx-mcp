"""nx_create_datum_plane on the shared fake kit, through the real kernel."""

from types import SimpleNamespace as NS

import pytest

from tests.kit import fake_nx, run
from tests.kit.objects import Feature


def datum_nx(units="mm"):
    """A fake NX whose DatumPlaneBuilder.GetPlane() returns one recording plane."""
    nx = fake_nx(units=units)
    nx.nx.PlaneTypes = NS(
        MethodType=NS(Distance="Distance", PointDir="PointDir", ThreePoints="ThreePoints")
    )
    make = nx.builder

    def builder(kind, *args):
        b = make(kind, *args)
        if kind == "DatumPlaneBuilder":
            plane = NS(
                Expression=NS(RightHandSide=None),
                SetUpdateOption=lambda _o: None,
                SetMethod=lambda m: setattr(plane, "method", m),
                SetGeometry=lambda g: setattr(plane, "geometry", list(g)),
                Evaluate=lambda: None,
            )
            object.__setattr__(b, "plane", plane)
            object.__setattr__(b, "GetPlane", lambda: plane)
        return b

    nx.builder = builder
    return nx


def plane_feature(nx, origin, normal):
    """What NX commits: a datum plane feature that owns its DatumPlane."""
    feature = Feature("DATUM_PLANE")
    feature.OwningPart, feature.IsOccurrence = nx.part, False
    feature.DatumPlane = nx.datum_plane(origin=origin, normal=normal)
    nx.part.Features.append(feature)
    return feature


def test_offset_converts_units_and_rolls_back_on_readback_mismatch():
    nx = datum_nx(units="in")
    top = nx.block(2, 1, 1).face(normal=(0, 0, 1))  # top face at z = 1 in
    nx.on_commit("DatumPlaneBuilder", lambda b: plane_feature(nx, (0, 0, 2), (0, 0, 1)))
    ok = run(nx, "nx_create_datum_plane", method="offset", reference=top.ref, distance="25.4 mm")
    assert ok.mutation_outcome == "committed" and ok.units["length"] == "in", ok
    assert nx.last("DatumPlaneBuilder").plane.Expression.RightHandSide == "1.0"
    assert nx.last("DatumPlaneBuilder").plane.method == "Distance"
    assert nx.live_builders == 0 and ok.changes["references"] == "preserved"
    assert ok.data["origin"] == [0, 0, 2] and ok.data["normal"] == [0, 0, 1]
    assert [r["kind"] for r in ok.changes["created"]] == ["feature"]

    nx.on_commit("DatumPlaneBuilder", lambda b: plane_feature(nx, (0, 0, 3), (0, 0, 1)))
    features_before, marks_before = list(nx.part.Features), nx.undo_marks
    bad = run(nx, "nx_create_datum_plane", method="offset", reference=top.ref, distance=1)
    assert bad.error["category"] == "verification_failed"
    assert bad.error["details"]["check"] == "offset"
    # The committed call keeps its mark (nx_undo); the failed one leaves none.
    assert bad.mutation_outcome == "rolled_back" and nx.undo_marks == marks_before
    assert nx.live_builders == 0
    assert list(nx.part.Features) == features_before


def test_negative_offset_from_a_datum_plane_goes_against_its_normal():
    nx = datum_nx()
    base = nx.datum_plane(origin=(0, 0, 25), normal=(0, 0, 1))
    nx.on_commit("DatumPlaneBuilder", lambda b: plane_feature(nx, (0, 0, -15), (0, 0, 1)))
    ok = run(nx, "nx_create_datum_plane", method="offset", reference=base.ref, distance=-40)
    assert ok.ok and nx.last("DatumPlaneBuilder").plane.Expression.RightHandSide == "-40.0"
    assert ok.data["datum_plane"]["kind"] == "datum_plane"


def test_point_normal_normalises_and_names():
    nx = datum_nx()
    s = 2**-0.5
    nx.on_commit("DatumPlaneBuilder", lambda b: plane_feature(nx, (5, 5, 5), (1, 1, 0)))
    ok = run(
        nx,
        "nx_create_datum_plane",
        method="point_normal",
        origin=[5, 5, 5],
        normal=[1, 1, 0],
        name="mid_plane",
    )
    assert ok.ok, ok
    assert ok.data["normal"] == pytest.approx([s, s, 0])
    plane = nx.last("DatumPlaneBuilder").plane
    assert plane.method == "PointDir"
    assert pytest.approx(s) == plane.geometry[1].Vector.X  # unit direction handed to NX
    assert nx.part.Features[-1].Name in ("mid_plane", "MID_PLANE")


def test_three_points_and_collinear_points():
    nx = datum_nx()
    nx.on_commit("DatumPlaneBuilder", lambda b: plane_feature(nx, (0, 0, 5), (0, 0, -1)))
    ok = run(
        nx,
        "nx_create_datum_plane",
        method="three_points",
        points=[[0, 0, 5], [0, 10, 5], [10, 0, 5]],
    )
    assert ok.ok and ok.data["normal"] == [0, 0, -1]
    assert nx.last("DatumPlaneBuilder").plane.method == "ThreePoints"
    built = len(nx.builders)
    bad = run(
        nx, "nx_create_datum_plane", method="three_points", points=[[0, 0, 0], [1, 1, 1], [2, 2, 2]]
    )
    assert bad.error["category"] == "invalid_argument" and bad.error["field"] == "/points"
    assert bad.mutation_outcome == "not_started" and len(nx.builders) == built


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"method": "offset", "distance": 5}, "/reference"),
        ({"method": "offset", "reference": "fa:1"}, "/distance"),
        ({"method": "point_normal", "origin": [0, 0, 0], "normal": [0, 0, 0]}, "/normal"),
        ({"method": "offset", "reference": "fa:1", "distance": "5 deg"}, "/distance"),
        ({"method": "three_points", "points": [[0, 0, 0], [1, 0, 0]]}, "/points"),
        ({"method": "offset", "reference": "fa:1", "distance": 5, "flip": True}, "/flip"),
    ],
)
def test_invalid_arguments_are_not_started(args, field):
    nx = datum_nx()
    bad = run(nx, "nx_create_datum_plane", **args)
    assert bad.error["category"] == "invalid_argument" and bad.error["field"] == field
    assert bad.mutation_outcome == "not_started" and nx.builders == []


def test_wrong_kind_and_native_refusal():
    nx = datum_nx()
    body = nx.block(10, 10, 10)
    edge = body.edges[0]
    bad = run(nx, "nx_create_datum_plane", method="offset", reference=edge.ref, distance=1)
    assert bad.error["code"] == "NX_WRONG_KIND" and nx.builders == []
    nx.refuse("DatumPlaneBuilder", code=670015, message="Plane cannot be created")
    top = body.face(normal=(0, 0, 1))
    failed = run(nx, "nx_create_datum_plane", method="offset", reference=top.ref, distance=1)
    assert failed.error["code"] == "NX_BUILD_FAILED" and failed.error["nx_code"] == 670015
    assert failed.mutation_outcome == "rolled_back" and nx.live_builders == 0


def test_active_sketch_is_refused_before_nx():
    nx = datum_nx()
    nx.session.ActiveSketch = nx.sketch()
    bad = run(
        nx, "nx_create_datum_plane", method="point_normal", origin=[0, 0, 0], normal=[0, 0, 1]
    )
    assert bad.error["code"] == "NX_SKETCH_ACTIVE" and "nx_finish_sketch" in bad.error["hint"]
    assert bad.mutation_outcome == "not_started" and nx.builders == []
