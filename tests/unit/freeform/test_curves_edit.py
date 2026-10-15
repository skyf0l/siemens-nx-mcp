"""nx_create_bridge_curve, nx_create_joined_curve, nx_create_mirror_curve and
nx_create_trim_curve on the shared fake."""

import math

import pytest

from tests.kit import run
from tests.unit.freeform import fakes
from tests.unit.freeform.support import curves_nx, made

BRIDGE = "BridgeCurveBuilderEx"
JOIN = "JoinCurvesBuilder"
MIRROR = "MirrorCurveBuilder"
TRIM = "TrimCurve2FeatureBuilder"


def curve(nx, *points):
    """A work-part polyline curve with NX's ``GetLength`` (millimetres)."""
    c = fakes.curve(nx, *points)
    c.GetLength = lambda: sum(math.dist(a, b) for a, b in zip(points, points[1:], strict=False))
    return c


def _help_point(section):
    """The help point handed to a builder's own section (``AddToSection``)."""
    return section.AddToSection.calls[0][0][4]


def _xyz(p):
    return [p.X, p.Y, p.Z]


def test_bridge_leaves_the_chosen_ends_tangent():
    nx = curves_nx()
    a = curve(nx, [0, 0, 0], [50, 0, 0])
    b = curve(nx, [100, 30, 0], [150, 30, 0])
    path = [[50, 0, 0], [60, 0, 0], [90, 30, 0], [100, 30, 0]]
    nx.on_commit(BRIDGE, lambda bld: made(nx, "BRIDGE_CURVE", [path]))
    ok = run(nx, "nx_create_bridge_curve", start={"curve": a.ref, "end": "end"},
             end={"curve": b.ref, "end": "start"}, end_continuity="G2")  # fmt: skip
    assert ok.ok and ok.mutation_outcome == "committed", ok
    bld = nx.last(BRIDGE)
    assert _xyz(_help_point(bld.StartSection)) == [50, 0, 0]
    assert _xyz(_help_point(bld.EndSection)) == [100, 30, 0]
    kinds = nx.nx.GeometricUtilities.Continuity.ContinuityTypes
    assert bld.StartConnectivity.Continuity.ContinuityType is kinds.G1
    assert bld.EndConnectivity.Continuity.ContinuityType is kinds.G2
    assert ok.data["start_gap"] == 0 and ok.data["end_gap"] == 0
    assert ok.data["start_angle"] == pytest.approx(0) and ok.data["end_angle"] == pytest.approx(0)
    assert ok.data["start"] == [50, 0, 0] and ok.data["end"] == [100, 30, 0]


def test_bridge_tangency_is_checked_except_for_g0():
    nx = curves_nx()
    a = curve(nx, [0, 0, 0], [50, 0, 0])
    b = curve(nx, [100, 30, 0], [150, 30, 0])
    nx.on_commit(BRIDGE, lambda bld: made(nx, "BRIDGE_CURVE", [[[50, 0, 0], [100, 30, 0]]]))
    sides = {"start": {"curve": a.ref, "end": "end"}, "end": {"curve": b.ref, "end": "start"}}
    ok = run(nx, "nx_create_bridge_curve", **sides, start_continuity="G0", end_continuity="G0")
    assert ok.ok and ok.data["start_angle"] == pytest.approx(30.963757), ok.error
    same = run(nx, "nx_create_bridge_curve", start={"curve": a.ref, "end": "end"},
               end={"curve": a.ref, "end": "end"})  # fmt: skip
    assert same.error["field"] == "/end/end"
    touch = curve(nx, [50, 0, 0], [50, 40, 0])
    near = run(nx, "nx_create_bridge_curve", start={"curve": a.ref, "end": "end"},
               end={"curve": touch.ref, "end": "start"})  # fmt: skip
    assert near.error["code"] == "NX_INVALID_ARGUMENT" and near.mutation_outcome == "not_started"
    bad = run(nx, "nx_create_bridge_curve", **sides)
    assert bad.error["details"]["check"] == "start_angle" and bad.mutation_outcome == "rolled_back"


def test_join_keeps_the_length_and_explains_gaps():
    nx = curves_nx()
    a = curve(nx, [0, 0, 0], [50, 0, 0])
    c = curve(nx, [50, 0, 0], [50, 40, 0])
    nx.on_commit(JOIN, lambda b: made(nx, "JOIN_CURVE", [[[0, 0, 0], [50, 0, 0], [50, 40, 0]]]))
    ok = run(nx, "nx_create_joined_curve", curves=[a.ref, c.ref])
    assert ok.ok and ok.data["length"] == pytest.approx(90), ok.error
    assert ok.data["source_length"] == pytest.approx(90) and ok.data["count"] == 1
    b = nx.last(JOIN)
    assert b.OutputCurveType is nx.nx.Features.JoinCurvesBuilder.OutputCurve.General
    assert b.DistanceTolerance == pytest.approx(0.001)
    nx.refuse(JOIN, 670001, "Gaps in string or multiple loops.")
    gap = run(nx, "nx_create_joined_curve", curves=[a.ref, c.ref], output="cubic")
    assert gap.error["code"] == "NX_BUILD_FAILED" and "end to end" in gap.error["hint"]
    nx.errors.pop(JOIN)
    nx.on_commit(JOIN, lambda b: made(nx, "JOIN_CURVE", [[[0, 0, 0], [50, 0, 0]]]))
    short = run(nx, "nx_create_joined_curve", curves=[curve(nx, [0, 0, 0], [50, 0, 0]).ref,
                curve(nx, [50, 0, 0], [50, 40, 0]).ref])  # fmt: skip
    assert short.error["details"]["check"] == "length"
    assert nx.live_builders == 0


def test_join_refuses_gaps_and_branches_before_nx():
    nx = curves_nx()
    a = curve(nx, [0, 0, 0], [50, 0, 0])
    far = curve(nx, [86, 0, 0], [120, 0, 0])
    gap = run(nx, "nx_create_joined_curve", curves=[a.ref, far.ref])
    assert gap.error["code"] == "NX_WRONG_GEOMETRY" and gap.error["field"] == "/curves/1"
    assert gap.error["details"]["gap"] == pytest.approx(36) and "bridge" in gap.error["hint"]
    assert gap.mutation_outcome == "not_started" and not nx.builders
    up = curve(nx, [50, 0, 0], [50, 40, 0])
    side = curve(nx, [50, 0, 0], [80, -10, 0])
    branch = run(nx, "nx_create_joined_curve", curves=[a.ref, up.ref, side.ref])
    assert branch.error["details"]["branch_at"] == [50, 0, 0]
    assert branch.error["details"]["curves"] == [0, 1, 2]
    loop = [curve(nx, [0, 0, 0], [10, 0, 0]), curve(nx, [10, 10, 0], [10, 0, 0]),
            curve(nx, [10, 10, 0], [0, 0, 0])]  # fmt: skip
    nx.on_commit(JOIN, lambda b: made(nx, "JOIN_CURVE", [[[0, 0, 0], [10, 0, 0], [10, 10, 0],
                                                         [0, 0, 0]]]))  # fmt: skip
    closed = run(nx, "nx_create_joined_curve", curves=[c.ref for c in loop])
    assert closed.ok, closed.error


def test_mirror_checks_symmetry_about_the_plane():
    nx = curves_nx()
    b = curve(nx, [100, 30, 0], [150, 30, 0])
    nx.on_commit(MIRROR, lambda bld: made(nx, "MIRROR_CURVE", [[[-100, 30, 0], [-150, 30, 0]]]))
    ok = run(nx, "nx_create_mirror_curve", curves=[b.ref], plane="yz")
    assert ok.ok and ok.data["symmetry_gap"] == pytest.approx(0), ok.error
    bld = nx.last(MIRROR)
    assert bld.PlaneOption is nx.nx.Features.MirrorCurveBuilder.PlaneOptions.NewPlane
    assert bld.NewPlane.normal == [1, 0, 0] and bld.NewPlane.origin == [0, 0, 0]
    nx.on_commit(MIRROR, lambda bld: made(nx, "MIRROR_CURVE", [[[-100, 31, 0], [-150, 31, 0]]]))
    bad = run(nx, "nx_create_mirror_curve", curves=[b.ref],
              plane={"origin": [0, 0, 0], "normal": [1, 0, 0]})  # fmt: skip
    assert bad.error["details"]["check"] == "symmetry"


def test_trim_keeps_the_piece_at_keep_between_planes():
    nx = curves_nx()
    line = curve(nx, [0, 0, 0], [100, 0, 0])
    nx.on_commit(TRIM, lambda b: made(nx, "TRIM_CURVE2", [[[30, 0, 0], [60, 0, 0]]]))
    planes = [
        {"origin": [30, 0, 0], "normal": [1, 0, 0]},
        {"origin": [60, 0, 0], "normal": [1, 0, 0]},
    ]
    ok = run(nx, "nx_create_trim_curve", curve=line.ref, planes=planes, keep=[45, 5, 0])
    assert ok.ok and ok.data["length"] == pytest.approx(30), ok.error
    b = nx.last(TRIM)
    assert len(b.BoundingObjectList.Append.calls) == 2
    assert b.KeepOrDiscard is nx.nx.Features.TrimCurve2Builder.KeepDiscard.Keep
    assert _xyz(b.SelectTrimRegion.calls[0][0][0]) == [45, 0, 0]
    assert ok.data["source_length"] == pytest.approx(100) and ok.data["locations"] == [
        [30, 0, 0],
        [60, 0, 0],
    ]
    outside = run(nx, "nx_create_trim_curve", curve=line.ref, keep=[10, 0, 0],
                  planes=[{"origin": [120, 0, 0], "normal": [1, 0, 0]}])  # fmt: skip
    assert outside.error["field"] == "/planes/0" and "extended" in outside.error["message"]
    nx.on_commit(TRIM, lambda b: made(nx, "TRIM_CURVE2", [[[0, 0, 0], [100, 0, 0]]]))
    none = run(nx, "nx_create_trim_curve", curve=line.ref, planes=planes, keep=[45, 0, 0])
    assert none.error["details"]["check"] in ("keep", "length")


def test_equal_segments_of_a_closed_curve_also_cut_at_the_seam():
    nx = curves_nx()
    square = [[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0], [0, 0, 0]]
    ring = curve(nx, *square)
    sides = [[square[i], square[i + 1]] for i in range(4)]
    nx.on_commit(TRIM, lambda b: made(nx, "TRIM_CURVE2", sides))
    ok = run(nx, "nx_create_trim_curve", curve=ring.ref, method="divide", segments=4)
    assert ok.ok and ok.data["count"] == 4, ok.error
    cuts = [_xyz(c[0][0]) for c in nx.last(TRIM).SelectDivideLocation.calls]
    assert [c for p in cuts for c in p] == pytest.approx([0, 0, 0, 10, 0, 0, 10, 10, 0, 0, 10, 0])


def test_divide_into_equal_segments_and_at_points():
    nx = curves_nx()
    line = curve(nx, [0, 0, 0], [100, 0, 0])
    four = [[[25 * i, 0, 0], [25 * (i + 1), 0, 0]] for i in range(4)]
    nx.on_commit(TRIM, lambda b: made(nx, "TRIM_CURVE2", four))
    ok = run(nx, "nx_create_trim_curve", curve=line.ref, method="divide", segments=4)
    assert ok.ok and ok.data["count"] == 4, ok.error
    b = nx.last(TRIM)
    assert b.OperationOption is nx.nx.Features.TrimCurve2Builder.Operation.Divide
    assert [_xyz(c[0][0]) for c in b.SelectDivideLocation.calls] == [
        [25, 0, 0],
        [50, 0, 0],
        [75, 0, 0],
    ]
    assert [p[0] for p in ok.data["locations"]] == pytest.approx([25, 50, 75])
    # NX lists the pieces in any order: they come back along the line from its start
    three = [[[70, 0, 0], [100, 0, 0]], [[0, 0, 0], [20, 0, 0]], [[20, 0, 0], [70, 0, 0]]]
    nx.on_commit(TRIM, lambda b: made(nx, "TRIM_CURVE2", three))
    at = run(
        nx, "nx_create_trim_curve", curve=line.ref, method="divide", at=[[20, 0, 0], [70, 0, 0]]
    )
    assert at.ok and at.data["count"] == 3, at.error
    assert [c["length"] for c in at.data["curves"]] == pytest.approx([20, 50, 30])
    assert [c["start"][0] for c in at.data["curves"]] == pytest.approx([0, 20, 70])
    end = run(nx, "nx_create_trim_curve", curve=line.ref, method="divide", at=[[0, 0, 0]])
    assert end.error["field"] == "/at"
    off = run(nx, "nx_create_trim_curve", curve=line.ref, method="divide",
              at=[[20, 0, 0], [60, 7.3, 0]])  # fmt: skip
    assert off.error["field"] == "/at/1" and off.mutation_outcome == "not_started"
    assert off.error["details"]["distance"] == pytest.approx(7.3)
    assert off.error["details"]["nearest"] == pytest.approx([60, 0, 0])
    uneven = run(nx, "nx_create_trim_curve", curve=line.ref, method="divide", segments=3)
    assert uneven.error["details"]["check"] == "segments"


def test_trim_argument_checks():
    nx = curves_nx()
    line = curve(nx, [0, 0, 0], [100, 0, 0])
    plane = [{"origin": [30, 0, 0], "normal": [1, 0, 0]}]
    cases = [
        ({"planes": plane}, "/keep"),
        ({"planes": plane, "keep": [1, 0, 0], "segments": 3}, "/segments"),
        ({"method": "divide"}, None),
        ({"method": "divide", "planes": plane, "segments": 3}, None),
        ({"method": "divide", "segments": 3, "keep": [1, 0, 0]}, "/keep"),
    ]
    for extra, field in cases:
        bad = run(nx, "nx_create_trim_curve", curve=line.ref, **extra)
        assert bad.error["category"] == "invalid_argument", (extra, bad)
        assert field is None or bad.error["field"] == field, (extra, bad.error)
    assert not nx.builders
    nx.refuse(TRIM, 670001, "No divide location found.")
    native = run(nx, "nx_create_trim_curve", curve=line.ref, method="divide", segments=2)
    assert native.error["code"] == "NX_BUILD_FAILED" and nx.live_builders == 0


def test_bridge_tangent_magnitudes():
    nx = curves_nx()
    a = curve(nx, [0, 0, 0], [50, 0, 0])
    b = curve(nx, [100, 30, 0], [150, 30, 0])
    path = [[50, 0, 0], [60, 0, 0], [90, 30, 0], [100, 30, 0]]
    nx.on_commit(BRIDGE, lambda bld: made(nx, "BRIDGE_CURVE", [path]))
    sides = {"start": {"curve": a.ref, "end": "end"}, "end": {"curve": b.ref, "end": "start"}}
    ok = run(nx, "nx_create_bridge_curve", **sides, start_magnitude=2.5)
    assert ok.ok, ok.error
    bld = nx.last(BRIDGE)
    kinds = nx.nx.Features.BridgeCurveBuilderEx.ShapeControlMethods
    assert bld.ShapeControlMethod is kinds.TangentMagnitude
    assert bld.TangentMagnitude.StartTangentMagnitude.RightHandSide == "2.5"
    assert ok.data["start_magnitude"] == 2.5 and ok.data["end_magnitude"] == 1.0
    g0 = run(nx, "nx_create_bridge_curve", **sides, end_continuity="G0", end_magnitude=2)
    assert g0.error["field"] == "/end_magnitude" and g0.mutation_outcome == "not_started"


def test_trim_takes_an_edge():
    nx = curves_nx()
    rim = fakes.edge(nx, [0, 0, 0], [100, 0, 0])
    rim.GetLength = lambda: 100.0
    nx.on_commit(TRIM, lambda b: made(nx, "TRIM_CURVE2", [[[0, 0, 0], [30, 0, 0]]]))
    ok = run(nx, "nx_create_trim_curve", curve=rim.ref,
             planes=[{"origin": [30, 0, 0], "normal": [1, 0, 0]}], keep=[10, 0, 0])  # fmt: skip
    assert ok.ok and ok.data["length"] == pytest.approx(30), ok.error
    assert nx.last(TRIM).CurveToTrim.AddToSection.calls[0][0][0] == [("EdgeDumb", [rim])]
