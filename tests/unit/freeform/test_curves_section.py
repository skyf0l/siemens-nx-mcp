"""nx_create_section_curves and nx_create_intersection_curve on the shared fake."""

import math

import pytest

from nx_mcp.core import evidence
from tests.kit import run
from tests.unit.freeform.support import curves_nx, made, rectangle_x

T = "SectionCurveBuilder"


def _section(nx, stations):
    """Commit hook: one rectangle per x station (the block 100 x 50 x 20)."""
    nx.on_commit(T, lambda b: made(nx, "SECTION_CURVES", [s for x in stations for s in
                                                         rectangle_x(x)]))  # fmt: skip


def test_one_plane_cuts_the_block_and_reads_back_lengths_and_stations():
    nx = curves_nx()
    body = nx.block(100, 50, 20)
    _section(nx, [50])
    ok = run(nx, "nx_create_section_curves", objects=[body.ref], method="plane",
             plane={"origin": [50, 0, 0], "normal": [1, 0, 0]})  # fmt: skip
    assert ok.ok and ok.mutation_outcome == "committed", ok
    b = nx.last(T)
    types = nx.nx.Features.SectionCurveBuilder.PlaneType
    assert b.Type is types.Selected and b.Associative is True and b.Tolerance == 0.001
    assert b.ObjectsToSection.Add.calls[0][0] == ([body],)
    assert nx.planes[0].origin == [50, 0, 0] and nx.planes[0].normal == [1, 0, 0]
    assert b.SectionPlanes.Add.calls[0][0] == (nx.planes[0],)
    assert ok.data["count"] == 4 and ok.data["length"] == pytest.approx(140)
    assert ok.data["stations"] == [
        {"origin": [50, 0, 0], "normal": [1, 0, 0], "count": 4, "length": pytest.approx(140)}
    ]
    assert ok.data["curves"][0]["curve"]["kind"] == "curve" and ok.data["gap"] == 0
    assert nx.live_builders == 0


def test_parallel_planes_use_nx_parallel_type_and_flip_for_negative_spacing():
    nx = curves_nx()
    body = nx.block(100, 50, 20)
    _section(nx, [90, 70, 50])
    ok = run(nx, "nx_create_section_curves", objects=[body.ref], method="parallel",
             plane={"origin": [90, 0, 0], "normal": [1, 0, 0]}, count=3, spacing=-20)  # fmt: skip
    assert ok.ok, ok
    b = nx.last(T)
    assert b.Type is nx.nx.Features.SectionCurveBuilder.PlaneType.Parallel
    assert b.BasePlane.normal == [-1, 0, 0] and b.BasePlane.origin == [90, 0, 0]
    assert (b.StartDistance, b.EndDistance, b.StepDistance) == (0.0, 40.0, 20.0)
    assert [s["origin"][0] for s in ok.data["stations"]] == [90, 70, 50]
    assert [s["count"] for s in ok.data["stations"]] == [4, 4, 4]
    assert ok.data["length"] == pytest.approx(420)


def test_radial_planes_turn_about_the_axis_through_a_point_off_it():
    nx = curves_nx()
    body = nx.block(100, 50, 20)
    nx.on_commit(T, lambda b: made(nx, "SECTION_CURVES", [[[50, 0, 0], [50, 50, 0]]]))
    ok = run(nx, "nx_create_section_curves", objects=[body.ref], method="radial",
             plane={"origin": [50, 25, 0], "normal": [1, 0, 0]},
             axis={"origin": [50, 25, 0], "direction": [0, 0, 1]}, count=3, angle=-45)  # fmt: skip
    assert ok.ok, ok
    b = nx.last(T)
    assert b.Type is nx.nx.Features.SectionCurveBuilder.PlaneType.Radial
    turn = b.RadialPlaneVector
    assert [turn.Vector.X, turn.Vector.Y, turn.Vector.Z] == [0, 0, -1]
    p = b.RadialPlanePoint.Coordinates
    assert math.dist([p.X, p.Y, p.Z], [50, 25, 0]) > 1  # off the axis, on the first plane
    assert p.X == 50 and (b.StartAngle, b.EndAngle, b.StepAngle) == (0.0, 90.0, 45.0)
    second = ok.data["stations"][1]["normal"]
    assert second == pytest.approx([math.sqrt(0.5), -math.sqrt(0.5), 0])
    assert [s["count"] for s in ok.data["stations"]] == [1, 0, 0]


def test_bad_planes_fail_before_nx():
    nx = curves_nx()
    body = nx.block(100, 50, 20)
    off = run(nx, "nx_create_section_curves", objects=[body.ref], method="radial", plane="xy",
              axis="z", count=3, angle=30)  # fmt: skip
    assert off.error["field"] == "/plane" and off.mutation_outcome == "not_started"
    wide = run(nx, "nx_create_section_curves", objects=[body.ref], method="radial", plane="xz",
               axis="z", count=7, angle=30)  # fmt: skip
    assert wide.error["field"] == "/angle"
    missing = run(nx, "nx_create_section_curves", objects=[body.ref], method="parallel",
                  plane="yz", count=3)  # fmt: skip
    assert missing.error["category"] == "invalid_argument"
    zero = run(nx, "nx_create_section_curves", objects=[body.ref], method="parallel",
               plane="yz", count=3, spacing=0)  # fmt: skip
    assert zero.error["field"] == "/spacing" and not nx.builders


def test_a_curve_off_its_plane_or_the_body_rolls_back():
    nx = curves_nx()
    body = nx.block(100, 50, 20)
    nx.on_commit(T, lambda b: made(nx, "SECTION_CURVES", [[[51, 0, 0], [51, 50, 0]]]))
    bad = run(nx, "nx_create_section_curves", objects=[body.ref],
              plane={"origin": [50, 0, 0], "normal": [1, 0, 0]})  # fmt: skip
    assert bad.error["details"]["check"] == "plane" and bad.mutation_outcome == "rolled_back"
    nx.on_commit(T, lambda b: made(nx, "SECTION_CURVES", [[[50, 0, 30], [50, 50, 30]]]))
    bad = run(nx, "nx_create_section_curves", objects=[body.ref],
              plane={"origin": [50, 0, 0], "normal": [1, 0, 0]})  # fmt: skip
    assert bad.error["details"]["check"] == "objects"
    nx.on_commit(T, lambda b: made(nx, "SECTION_CURVES", []))
    bad = run(nx, "nx_create_section_curves", objects=[body.ref], plane="yz")
    assert bad.error["details"]["check"] == "curves" and nx.live_builders == 0


def test_license_and_native_refusals():
    nx = curves_nx()
    body = nx.block(100, 50, 20)
    nx.licenses["solid_modeling"] = False
    bad = run(nx, "nx_create_section_curves", objects=[body.ref], plane="yz")
    assert bad.error["code"] == "NX_LICENSE_UNAVAILABLE" and not nx.builders
    evidence.forget_licenses()  # a tested tool's refusal is remembered until NX restarts
    nx.licenses["solid_modeling"] = True
    nx.refuse(T, 670200, "No intersection")
    bad = run(nx, "nx_create_section_curves", objects=[body.ref], plane="yz")
    assert bad.error["code"] == "NX_BUILD_FAILED" and bad.error["nx_code"] == 670200
    assert nx.live_builders == 0


# ---------------------------------------------------------------------------
# Intersection curve

INT = "IntersectionCurveBuilder"


def test_intersection_with_a_plane_uses_the_faces_of_a_body():
    nx = curves_nx()
    body = nx.block(100, 50, 20)
    nx.on_commit(INT, lambda b: made(nx, "INTERSECTION_CURVES", rectangle_x(30)))
    ok = run(nx, "nx_create_intersection_curve", first=[body.ref],
             plane={"origin": [30, 0, 0], "normal": [1, 0, 0]})  # fmt: skip
    assert ok.ok and ok.data["count"] == 4 and ok.data["length"] == pytest.approx(140), ok
    b = nx.last(INT)
    assert b.FirstFace.ReplaceRules.calls[0][0][0] == [("FaceDumb", body.faces)]
    assert b.SecondPlane.origin == [30, 0, 0] and not b.SecondFace.ReplaceRules.calls
    assert ok.data["gap"] == 0


def test_intersection_of_two_face_sets_and_its_checks():
    nx = curves_nx()
    body = nx.block(100, 50, 20)
    top, side = body.face((0, 0, 1)), body.face((1, 0, 0))
    nx.on_commit(INT, lambda b: made(nx, "INTERSECTION_CURVES", [[[100, 0, 20], [100, 50, 20]]]))
    ok = run(nx, "nx_create_intersection_curve", first=[top.ref], second=[side.ref])
    assert ok.ok and ok.data["length"] == pytest.approx(50), ok
    assert nx.last(INT).SecondFace.ReplaceRules.calls[0][0][0] == [("FaceDumb", [side])]
    shared = run(nx, "nx_create_intersection_curve", first=[top.ref], second=[body.ref])
    assert shared.error["field"] == "/second" and shared.mutation_outcome == "not_started"
    both = run(nx, "nx_create_intersection_curve", first=[top.ref], second=[side.ref],
               plane="xy")  # fmt: skip
    assert both.error["category"] == "invalid_argument"
    nx.on_commit(INT, lambda b: made(nx, "INTERSECTION_CURVES", [[[100, 0, 25], [100, 50, 25]]]))
    off = run(nx, "nx_create_intersection_curve", first=[top.ref], second=[side.ref])
    assert off.error["details"]["check"] == "gap" and off.mutation_outcome == "rolled_back"


def test_inch_part_lengths_are_in_inches():
    nx = curves_nx("in")
    body = nx.block(4, 2, 1)
    nx.on_commit(T, lambda b: made(nx, "SECTION_CURVES", rectangle_x(2, 0, 2, 0, 1)))
    ok = run(nx, "nx_create_section_curves", objects=[body.ref], plane={"origin": [2, 0, 0],
             "normal": [1, 0, 0]})  # fmt: skip
    assert ok.ok and ok.data["length"] == pytest.approx(6), ok
    assert nx.last(T).Tolerance == pytest.approx(0.001 / 25.4)
