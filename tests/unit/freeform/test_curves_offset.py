"""nx_create_offset_curve on the shared fake."""

import pytest

from tests.kit import NXException, point, run
from tests.unit.freeform.curves_fakes import curves_nx, made
from tests.unit.freeform.fakes import curve

OFF = "OffsetCurveBuilder"
LOOP = [[0, 50, 20], [100, 50, 20], [100, 0, 20], [0, 0, 20], [0, 50, 20]]


def _rect(x0, y0, x1, y1, z):
    c = [[x0, y1, z], [x1, y1, z], [x1, y0, z], [x0, y0, z]]
    return [[c[i], c[(i + 1) % 4]] for i in range(4)]


def offset_nx(direction=(0, -1, 0), seed=(0, 50, 20), refuse=None):
    """A fake whose offset builders compute ``direction`` at ``seed`` (NX's in-plane side)."""
    nx = curves_nx()
    make = nx.builder

    def builder(kind, *args):
        b = make(kind, *args)
        if kind == OFF:

            def compute():
                if refuse:
                    raise NXException(670001, refuse)
                return point(*direction), point(*seed)

            b.ComputeOffsetDirection = compute
            b.ReverseDirection = False
        return b

    nx.builder = builder
    return nx, curve(nx, *LOOP)


def test_distance_toward_a_point_reverses_nx_side_and_reads_back():
    nx, loop = offset_nx()  # NX offsets inward (-y at the seed): the point asks outward
    nx.on_commit(OFF, lambda b: made(nx, "OFFSET_CURVE", _rect(-5, -5, 105, 55, 20)))
    ok = run(nx, "nx_create_offset_curve", curves=[loop.ref], method="distance", distance=5,
             toward=[50, 200, 20])  # fmt: skip
    assert ok.ok and ok.mutation_outcome == "committed", ok
    b = nx.last(OFF)
    assert b.Type is nx.nx.Features.OffsetCurveBuilder.Types.Distance
    assert b.OffsetDistance.RightHandSide == "5.0" and b.ReverseDirection is True
    assert b.CurvesToOffset.AddToSection.calls[0][0][0] == [("CurveDumb", [loop])]
    assert ok.data["expected"] == 5 and ok.data["measured_min"] == pytest.approx(5)
    assert ok.data["length"] == pytest.approx(340) and ok.data["count"] == 4


def test_distance_on_nx_side_and_a_wrong_distance_rolls_back():
    nx, loop = offset_nx()
    nx.on_commit(OFF, lambda b: made(nx, "OFFSET_CURVE", _rect(5, 5, 95, 45, 20)))
    ok = run(nx, "nx_create_offset_curve", curves=[loop.ref], distance=5, toward=[50, 25, 20])
    assert ok.ok and nx.last(OFF).ReverseDirection is False, ok
    nx.on_commit(OFF, lambda b: made(nx, "OFFSET_CURVE", _rect(6, 6, 94, 44, 20)))
    bad = run(nx, "nx_create_offset_curve", curves=[loop.ref], distance=5)
    assert bad.error["details"]["check"] == "distance" and bad.mutation_outcome == "rolled_back"


def test_draft_height_side_comes_from_an_undone_trial_build():
    nx, loop = offset_nx()

    def commit(b):  # NX drafts down unless the height is negated
        z = 30 if str(b.DraftHeight.RightHandSide).startswith("-(") else 10
        return made(nx, "OFFSET_CURVE", _rect(-10, -10, 110, 60, z))

    nx.on_commit(OFF, commit)
    ok = run(nx, "nx_create_offset_curve", curves=[loop.ref], method="draft", height=10, angle=45,
             direction="z", toward=[50, 200, 20])  # fmt: skip
    assert ok.ok, ok.error
    trial, real = [b for b in nx.builders if b.kind == OFF]
    assert trial.DraftHeight.RightHandSide == "10.0" and trial.ReverseDirection is True
    assert real.DraftHeight.RightHandSide == "-(10.0)" and real.ReverseDirection is False
    assert real.DraftAngle.RightHandSide == "45.0"
    assert ok.data["expected"] == pytest.approx(14.142136)
    assert ok.data["curves"][0]["start"][2] == 30 and nx.live_builders == 0
    down = run(nx, "nx_create_offset_curve", curves=[loop.ref], method="draft", height=10,
               angle=45, direction="-z")  # fmt: skip
    assert down.ok and len([b for b in nx.builders if b.kind == OFF]) == 4, down.error


def test_axial_3d_moves_along_the_axis_by_the_signed_distance():
    nx = curves_nx()
    line = curve(nx, [0, 100, 0], [100, 100, 0])
    nx.on_commit(OFF, lambda b: made(nx, "OFFSET_CURVE", [[[0, 100, -5], [100, 100, -5]]]))
    ok = run(nx, "nx_create_offset_curve", curves=[line.ref], method="axial_3d", distance=-5,
             direction="z")  # fmt: skip
    assert ok.ok and ok.data["measured_min"] == pytest.approx(5), ok
    b = nx.last(OFF)
    assert b.Type is nx.nx.Features.OffsetCurveBuilder.Types.Axial3d
    assert b.Offset3dDistance.RightHandSide == "-5.0"
    v = b.AxialOffsetDirection.Vector
    assert [v.X, v.Y, v.Z] == [0, 0, 1]
    nx.on_commit(OFF, lambda b: made(nx, "OFFSET_CURVE", [[[0, 100, 5], [100, 100, 5]]]))
    bad = run(nx, "nx_create_offset_curve", curves=[line.ref], method="axial_3d", distance=-5,
              direction="z")  # fmt: skip
    assert bad.error["details"]["check"] == "side"


def test_offset_argument_checks_and_a_straight_line_refusal():
    nx, loop = offset_nx(refuse="Input curves form straight line")
    cases = [
        ({"distance": -5}, "/distance"),
        ({"method": "draft", "height": 5}, None),
        ({"method": "draft", "height": 5, "angle": 90}, "/angle"),
        ({"method": "draft", "height": -5, "angle": 10}, "/height"),
        ({"method": "axial_3d", "distance": 5}, None),
        ({"method": "axial_3d", "distance": 0, "direction": "z"}, "/distance"),
    ]
    for extra, field in cases:
        bad = run(nx, "nx_create_offset_curve", curves=[loop.ref], **extra)
        assert bad.error["category"] == "invalid_argument", (extra, bad)
        assert field is None or bad.error["field"] == field, (extra, bad.error)
    assert not nx.builders
    line = run(nx, "nx_create_offset_curve", curves=[loop.ref], distance=5)
    assert line.error["code"] == "NX_BUILD_FAILED" and "axial_3d" in line.error["hint"]
    assert nx.live_builders == 0
