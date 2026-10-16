"""nx_create_isoparametric_curves, nx_create_isocline_curves and nx_create_curve_on_surface
on the shared fake."""

import math
from types import SimpleNamespace as NS

import pytest

from nx_mcp.core import evidence
from tests.kit import run
from tests.unit.freeform.fakes import face, flat, nx_refuses
from tests.unit.freeform.support import curves_nx, made

ISO = "IsoparametricCurvesBuilder"
INC = "IsoclineCurveBuilder"
COS = "CurveOnSurfaceBuilder"
SIN30, COS30 = 0.5, math.sqrt(3) / 2


def surface_nx(normal=(0, 0, 1)):
    """A fake with one face: the box [-20, -20, 0, 20, 20, 60] (UF distance) whose
    normal is ``normal`` everywhere (face props), and the isocline builder collection."""
    nx = curves_nx()
    f = face(nx, flat(), normal=normal)
    f.box = [-20, -20, 0, 20, 20, 60]
    f.at = lambda *p: list(p)
    nx.part.Features.FreeformCurveCollection = NS(
        CreateIsoclineCurveBuilder=lambda *a: nx.builder(INC, *a)
    )
    return nx, f


def _points(b):
    return [
        [c[0][0].Point.Coordinates.X, c[0][0].Point.Coordinates.Y, c[0][0].Point.Coordinates.Z]
        for c in b.ConstraintManager.Append.calls
    ]


def test_uniform_iso_curves_set_family_count_and_read_back_on_the_face():
    nx, f = surface_nx()
    lines = [[[20, 0, 0], [20, 0, 60]], [[-20, 0, 0], [-20, 0, 60]], [[20, 0, 0], [20, 0, 60]]]
    nx.on_commit(ISO, lambda b: made(nx, "ISOPARAMETRIC_CURVES", lines))
    ok = run(
        nx, "nx_create_isoparametric_curves", face=nx.ref_of(f, "face"), direction="u", count=3
    )
    assert ok.ok and ok.mutation_outcome == "committed", ok
    b = nx.last(ISO)
    B = nx.nx.Features.IsoparametricCurvesBuilder
    assert b.SelectObject.Value is f and b.Associative is True
    assert b.Direction is B.DirectionTypes.IsoU and b.Placement is B.PlacementTypes.Uniform
    assert b.Number == 3 and not b.ConstraintManager.Append.calls
    assert ok.data["count"] == 3 and ok.data["length"] == pytest.approx(180)
    assert ok.data["gap"] == 0 and nx.live_builders == 0


def test_iso_curves_through_and_between_points():
    nx, f = surface_nx()
    nx.on_commit(ISO, lambda b: made(nx, "ISOPARAMETRIC_CURVES", [[[-20, 0, 15], [20, 0, 15]]]))
    ok = run(nx, "nx_create_isoparametric_curves", face=nx.ref_of(f, "face"), direction="v",
             method="through_points", points=[[0, 0, 15]])  # fmt: skip
    assert ok.ok, ok
    b = nx.last(ISO)
    B = nx.nx.Features.IsoparametricCurvesBuilder
    assert b.Placement is B.PlacementTypes.ThroughPoints and b.Direction is B.DirectionTypes.IsoV
    assert _points(b) == [[0, 0, 15]] and "Number" not in b.__dict__
    nx.on_commit(ISO, lambda b: made(nx, "ISOPARAMETRIC_CURVES", [[[-20, 0, z], [20, 0, z]] for z
                                                                   in (15, 30, 45)]))  # fmt: skip
    ok = run(nx, "nx_create_isoparametric_curves", face=nx.ref_of(f, "face"), direction="uv",
             method="between_points", points=[[0, 0, 15], [0, 0, 45]], count=3)  # fmt: skip
    assert ok.ok and ok.data["count"] == 3, ok
    b = nx.last(ISO)
    assert b.Placement is B.PlacementTypes.BetweenPoints and b.Number == 3
    assert b.Direction is B.DirectionTypes.IsoUV and len(_points(b)) == 2


def test_iso_curve_inputs_are_checked_before_nx():
    nx, f = surface_nx()
    off = run(nx, "nx_create_isoparametric_curves", face=nx.ref_of(f, "face"), method="through_points",
              points=[[0, 0, 15], [0, 0, 90]])  # fmt: skip
    assert off.error["field"] == "/points/1" and off.mutation_outcome == "not_started"
    two = run(nx, "nx_create_isoparametric_curves", face=nx.ref_of(f, "face"), method="between_points",
              points=[[0, 0, 15]], count=2)  # fmt: skip
    assert two.error["field"] == "/points"
    missing = run(nx, "nx_create_isoparametric_curves", face=nx.ref_of(f, "face"), method="uniform")
    assert missing.error["category"] == "invalid_argument" and not nx.builders


def test_a_curve_off_the_face_or_missing_a_point_rolls_back():
    nx, f = surface_nx()
    nx.on_commit(ISO, lambda b: made(nx, "ISOPARAMETRIC_CURVES", [[[30, 0, 0], [30, 0, 60]]]))
    bad = run(nx, "nx_create_isoparametric_curves", face=nx.ref_of(f, "face"), count=1)
    assert bad.error["details"]["check"] == "faces" and bad.mutation_outcome == "rolled_back"
    nx.on_commit(ISO, lambda b: made(nx, "ISOPARAMETRIC_CURVES", [[[-20, 0, 20], [20, 0, 20]]]))
    bad = run(nx, "nx_create_isoparametric_curves", face=nx.ref_of(f, "face"), method="through_points",
              points=[[0, 0, 15]])  # fmt: skip
    assert bad.error["details"]["check"] == "points" and nx.live_builders == 0


# ---------------------------------------------------------------------------
# Isocline


def test_single_isocline_sets_the_builder_and_checks_the_draft_angle():
    nx, f = surface_nx(normal=(0, COS30, SIN30))  # 30 degrees from z to the tangent plane
    nx.on_commit(INC, lambda b: made(nx, "ISOCLINE_CURVE", [[[-10, 0, 15], [10, 0, 15]]]))
    ok = run(nx, "nx_create_isocline_curves", faces=[nx.ref_of(f, "face")], direction="z", angle=30)
    assert ok.ok and ok.data["angles"] == [30], ok
    assert ok.data["angle_error"] == pytest.approx(0, abs=1e-9)
    b = nx.last(INC)
    B = nx.nx.Features.IsoclineCurveBuilder
    assert b.Face.ReplaceRules.calls[0][0][0] == [("FaceDumb", [f])]
    v = b.ReferenceDirection.Vector
    assert [v.X, v.Y, v.Z] == [0, 0, 1] and b.IsAssociative is True
    assert b.IsoclineTypes is B.CurveTypes.Single and b.IsoclineAngle.RightHandSide == "30.0"
    bad = run(
        nx, "nx_create_isocline_curves", faces=[nx.ref_of(f, "face")], direction="z", angle=45
    )
    assert bad.error["details"]["check"] == "angle" and bad.mutation_outcome == "rolled_back"


def test_isocline_range_lists_its_angles_and_checks_them():
    nx, f = surface_nx(normal=(0, COS30, SIN30))
    nx.on_commit(INC, lambda b: made(nx, "ISOCLINE_CURVE", [[[-10, 0, 15], [10, 0, 15]]]))
    ok = run(nx, "nx_create_isocline_curves", faces=[nx.ref_of(f, "face")], direction=[0, 0, 1],
             method="range", start_angle=0, end_angle=70, step=30)  # fmt: skip
    assert ok.ok and ok.data["angles"] == [0, 30, 60], ok
    b = nx.last(INC)
    assert b.IsoclineTypes is nx.nx.Features.IsoclineCurveBuilder.CurveTypes.Multiple
    assert (b.StartAngle.RightHandSide, b.EndAngle.RightHandSide,
            b.IntervalAngle.RightHandSide) == ("0.0", "70.0", "30.0")  # fmt: skip
    back = run(nx, "nx_create_isocline_curves", faces=[nx.ref_of(f, "face")], direction="z", method="range",
               start_angle=30, end_angle=10, step=5)  # fmt: skip
    assert back.error["field"] == "/end_angle" and back.mutation_outcome == "not_started"
    wide = run(
        nx, "nx_create_isocline_curves", faces=[nx.ref_of(f, "face")], direction="z", angle=90
    )
    assert wide.error["category"] == "invalid_argument"


def test_isocline_takes_a_studio_license_too_and_refuses_without_either():
    nx, f = surface_nx(normal=(0, COS30, SIN30))
    nx.on_commit(INC, lambda b: made(nx, "ISOCLINE_CURVE", [[[-10, 0, 15], [10, 0, 15]]]))
    nx.licenses["solid_modeling"] = False
    nx.licenses["studio_free_form"] = True
    ok = run(nx, "nx_create_isocline_curves", faces=[nx.ref_of(f, "face")], direction="z", angle=30)
    assert ok.ok, ok
    nx_refuses(nx, INC, "studio_free_form")
    bad = run(
        nx, "nx_create_isocline_curves", faces=[nx.ref_of(f, "face")], direction="z", angle=30
    )
    assert bad.error["code"] == "NX_LICENSE_UNAVAILABLE" and bad.error["nx_code"] == 948801
    assert bad.error["details"]["license"] == "solid_modeling"
    assert bad.error["details"]["license_check"] == "missing" and nx.live_builders == 0
    assert bad.error["details"]["licenses"] == ["solid_modeling", "studio_free_form"]
    evidence.forget_licenses()


# ---------------------------------------------------------------------------
# Curve on surface


def test_curve_on_surface_passes_through_its_points_on_the_faces():
    nx, f = surface_nx()
    pts = [[20, 0, 10], [0, 20, 30], [-20, 0, 50]]
    nx.on_commit(COS, lambda b: made(nx, "CURVE_ON_SURFACE", [pts]))
    ok = run(nx, "nx_create_curve_on_surface", faces=[nx.ref_of(f, "face")], points=pts)
    assert ok.ok and ok.data["miss"] == 0 and ok.data["gap"] == 0, ok
    b = nx.last(COS)
    assert b.CurveType is nx.nx.Features.CurveOnSurfaceBuilder.CurveTypeEnum.Spline
    assert b.Faces.ReplaceRules.calls[0][0][0] == [("FaceDumb", [f])]
    assert b.DistanceTolerance == 0.001 and b.AngleTolerance == 0.5 and b.IsPeriodic is False
    assert _points(b) == pts and ok.data["count"] == 1


def test_curve_on_surface_checks_points_and_reads_back_misses():
    nx, f = surface_nx()
    off = run(
        nx,
        "nx_create_curve_on_surface",
        faces=[nx.ref_of(f, "face")],
        points=[[0, 0, 10], [0, 0, 70]],
    )
    assert off.error["field"] == "/points/1" and off.mutation_outcome == "not_started"
    same = run(
        nx,
        "nx_create_curve_on_surface",
        faces=[nx.ref_of(f, "face")],
        points=[[0, 0, 10], [0, 0, 10]],
    )
    assert same.error["field"] == "/points/1" and not nx.builders
    nx.on_commit(COS, lambda b: made(nx, "CURVE_ON_SURFACE", [[[0, 0, 10], [0, 0, 40]]]))
    miss = run(nx, "nx_create_curve_on_surface", faces=[nx.ref_of(f, "face")], points=[[0, 0, 10], [0, 0, 50]],
               periodic=True)  # fmt: skip
    assert miss.error["details"]["check"] == "points" and miss.mutation_outcome == "rolled_back"
    assert nx.last(COS).IsPeriodic is True and nx.live_builders == 0
