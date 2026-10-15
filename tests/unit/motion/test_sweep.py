"""nx_run_motion_sweep on the fake mechanism: hinge, slider, driven constraint, refusals."""

import math

import pytest

from nx_mcp.kit import geom
from tests.kit import run
from tests.unit.motion.fakes import STATUS, carried, motion_nx, rz

HINGE = {"origin": [0, 100, 15], "direction": [-1, 0, 0]}


def lid_angle(lid):
    """The lid's opening angle about -X (degrees) from its rotation matrix."""
    return math.degrees(math.atan2(-lid.r[2][1], lid.r[1][1]))


def hatch():
    """A lid hinged on the back edge of a frame and a stop block behind the hinge: the
    lid's top face meets the stop's top front edge (10 mm back, 45 mm up from the hinge
    line) at 102.53 degrees, the analytic gap of a 100 mm lid."""
    nx = motion_nx()
    frame, lid, stop = nx.add("FRAME"), nx.add("LID"), nx.add("STOP")

    def gap(a, b):
        bodies = {a.OwningComponent.Name, b.OwningComponent.Name}
        if bodies == {"LID", "STOP"}:
            back = -stop.t[1]  # the stop slides along -Y (the slider test)
            theta = math.radians(lid_angle(lid))
            if back > 0:
                d = max(0.0, 10.0 - back)
            elif theta <= math.pi / 2:
                d = 10.0
            else:
                d = max(0.0, 10.0 * math.sin(theta) + 45.0 * math.cos(theta))
            return d, [100.0, 100.0, 15.0], [100.0, 110.0, 60.0]
        return 50.0, [0.0, 0.0, 0.0], [0.0, 0.0, 50.0]

    nx.gap = gap
    return nx, frame, lid, stop


def test_hinge_sweep_reports_path_gap_first_contact_and_bisected_limit():
    nx, _frame, lid, stop = hatch()
    ok = run(
        nx, "nx_run_motion_sweep", joint="revolute", moving=[lid.ref], axis=HINGE,
        start=0, end=110, steps=11, track=[{"component": lid.ref, "point": [100, 0, 15]}],
        pairs=[{"a": lid.ref, "b": stop.ref}],
    )  # fmt: skip
    assert ok.ok, ok
    assert ok.mutation_outcome == "not_applicable"
    data = ok.data
    assert data["unit"] == "deg" and len(data["values"]) == 12
    assert data["reached"] == [0.0, 110.0] and data["stopped"] is None
    assert [m["id"] for m in data["moved"]] == [lid.ref]
    path = data["points"][0]["path"]
    assert path[9] == pytest.approx([100, 100, 115], abs=1e-6)
    assert path[11] == pytest.approx([100, 134.202014, 108.969262], abs=1e-6)
    gap = data["clearances"][0]
    assert gap["distances"][10] == pytest.approx(2.033910, abs=1e-6)
    assert gap["first_violation"] == {"index": 11, "value": 110.0}
    assert gap["limit"] == pytest.approx(102.516379, abs=1e-3)  # gap = 0.01 mm there
    assert gap["clear_range"] == pytest.approx([0, 102.516379], abs=1e-3)
    assert gap["min"] == 0.0 and gap["min_value"] == 110.0
    assert data["restored"] is True
    assert lid.t == pytest.approx([0, 0, 0]) and lid_angle(lid) == pytest.approx(0)


def test_min_clearance_moves_the_limit_to_the_required_gap():
    nx, _frame, lid, stop = hatch()
    ok = run(
        nx, "nx_run_motion_sweep", joint="hinge", moving=[lid.ref], axis=HINGE, start=0,
        end=110, steps=11, pairs=[{"a": lid.ref, "b": stop.ref}], min_clearance=5,
    )  # fmt: skip
    assert ok.ok, ok
    assert ok.data["joint"] == "revolute"
    gap = ok.data["clearances"][0]
    assert gap["first_violation"]["index"] == 10
    assert gap["limit"] == pytest.approx(96.301957, abs=1e-3)


def test_slider_sweep_moves_along_the_axis_in_part_units():
    nx, frame, lid, stop = hatch()
    ok = run(
        nx, "nx_run_motion_sweep", joint="slider", moving=[stop.ref],
        axis={"origin": [0, 0, 0], "direction": [0, -1, 0]}, start=0, end=20, steps=4,
        track=[{"component": stop.ref, "point": [0, 110, 10]}],
        pairs=[{"a": stop.ref, "b": lid.ref}, {"a": stop.body_ref, "b": frame.ref}],
    )  # fmt: skip
    assert ok.ok, ok
    data = ok.data
    assert data["unit"] == "mm"
    assert data["points"][0]["path"][4] == pytest.approx([0, 90, 10])
    gap = data["clearances"][0]
    assert gap["distances"] == pytest.approx([10, 5, 0, 0, 0])
    assert gap["limit"] == pytest.approx(9.99, abs=1e-3)
    assert data["clearances"][1]["a"]["kind"] == "body"
    assert stop.t == pytest.approx([0, 0, 0])


def linkage():
    """A rocker (pivot D, 80 mm) driven by an angle constraint and a crank that follows it
    through its loop until the toggle at 128.68 degrees, past which the loop's concentric
    constraint no longer solves (NX 2406: the driven constraint itself still does)."""
    nx = motion_nx()
    crank = nx.add("CRANK", rz(90))
    rocker = nx.add("ROCKER", rz(80.25691282921025, [100, 0, 0]))
    drive = nx.constraint("Angle", 80.25691282921025)
    loop = nx.constraint("Concentric")
    nx.solves = []

    def solve(constraint, value):
        nx.solves.append(value)
        rocker.t, rocker.r = rz(value, [100, 0, 0])
        if value > 128.68218745348943:
            return {loop: STATUS.CannotSolve}
        crank.t, crank.r = rz(90 + (value - 80.25691282921025) / 2)
        return {}

    nx.solve = solve
    return nx, crank, rocker, drive, loop


def test_constraint_sweep_stops_where_the_assembly_no_longer_solves():
    nx, crank, rocker, drive, loop = linkage()
    ok = run(
        nx, "nx_run_motion_sweep", joint="constraint", constraint=drive.ref, start=90,
        end=140, steps=10, track=[{"component": rocker.ref, "axis": [0, 0, 1]},
                                  {"component": crank.ref, "axis": [0, 0, 1]}],
    )  # fmt: skip
    assert ok.ok, ok
    data = ok.data
    assert data["values"] == [90, 95, 100, 105, 110, 115, 120, 125]
    assert data["reached"] == [90, 125]
    stopped = data["stopped"]
    assert stopped["index"] == 8 and stopped["value"] == 130
    assert [c["id"] for c in stopped["constraints"]] == [loop.ref]
    assert stopped["limit"] == pytest.approx(128.682187, abs=0.01)
    rocker_angles = data["angles"][0]
    assert rocker_angles["angles"][0] == pytest.approx(9.743087, abs=1e-6)
    assert rocker_angles["travel"] == pytest.approx(35)
    assert data["angles"][1]["angles"][0] == pytest.approx(9.743087 / 2, abs=1e-6)
    assert {m["name"] for m in data["moved"]} == {"CRANK", "ROCKER"}
    assert nx.undos >= 14  # every bisection try restarts from the design pose
    assert drive.Expression.Value == pytest.approx(80.25691282921025)
    assert sum(rocker.r, []) == pytest.approx(sum(rz(80.25691282921025)[1], []))
    assert loop.status == STATUS.Solved


def test_constraint_sweep_that_cannot_start_fails_and_restores():
    nx, _crank, rocker, drive, _loop = linkage()
    ok = run(nx, "nx_run_motion_sweep", joint="constraint", constraint=drive.ref,
             start=135, end=170, steps=5)  # fmt: skip
    assert not ok.ok
    assert ok.error["code"] == "NX_CONSTRAINT_UNSATISFIED" and ok.error["field"] == "/start"
    assert sum(rocker.r, []) == pytest.approx(sum(rz(80.25691282921025)[1], []))


def test_tracked_angle_is_continuous_past_half_a_turn():
    nx = motion_nx()
    arm = nx.add("ARM")
    ok = run(
        nx, "nx_run_motion_sweep", joint="revolute", moving=[arm.ref],
        axis={"origin": [0, 0, 0], "direction": [0, 0, 1]}, start=0, end=270, steps=3,
        track=[{"component": arm.ref, "axis": [0, 0, 1], "point": [10, 0, 0]}],
    )  # fmt: skip
    assert ok.ok, ok
    turned = ok.data["angles"][0]
    assert turned["angles"] == pytest.approx([0, 90, 180, 270])
    assert turned["travel"] == pytest.approx(270)
    assert ok.data["points"][0]["path"][3] == pytest.approx([0, -10, 0], abs=1e-9)
    assert carried(arm, [10, 0, 0]) == pytest.approx([10, 0, 0])


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"joint": "revolute", "start": 0, "end": 90}, "/moving"),
        ({"joint": "revolute", "moving": ["LID"], "start": 0, "end": 90}, "/axis"),
        ({"joint": "revolute", "moving": ["LID"], "axis": HINGE, "start": 30, "end": 30},
         "/end"),
        ({"joint": "revolute", "moving": ["LID"], "axis": HINGE, "start": 0, "end": 90,
          "pairs": [{"a": "LID", "b": "LID"}]}, "/pairs/0"),
        ({"joint": "revolute", "moving": ["LID", "LID"], "axis": HINGE, "start": 0,
          "end": 90}, "/moving/1"),
        ({"joint": "constraint", "start": 0, "end": 90}, "/constraint"),
        ({"joint": "constraint", "constraint": "ANGLE", "start": 30, "end": 190}, "/end"),
        ({"joint": "constraint", "constraint": "FIX", "start": 0, "end": 10}, "/constraint"),
        ({"joint": "revolute", "moving": ["LID"], "axis": HINGE, "start": 0, "end": 90,
          "min_clearance": -1}, "/min_clearance"),
        ({"joint": "revolute", "moving": ["LID"], "axis": HINGE, "start": 0, "end": 90,
          "track": [{"component": "LID"}]}, "/track/0"),
    ],
)  # fmt: skip
def test_refusals_name_the_field_and_change_nothing(args, field):
    nx, _frame, lid, _stop = hatch()
    names = {"LID": lid.ref, "ANGLE": nx.constraint("Angle", 90).ref,
             "FIX": nx.constraint("Fix").ref}  # fmt: skip

    def swap(value):
        if isinstance(value, list):
            return [swap(v) for v in value]
        if isinstance(value, dict):
            return {k: swap(v) for k, v in value.items()}
        return names.get(value, value) if isinstance(value, str) else value

    ok = run(nx, "nx_run_motion_sweep", **swap(args))
    assert not ok.ok
    assert ok.error["category"] == "invalid_argument", ok.error
    assert ok.error["field"] == field
    assert ok.mutation_outcome == "not_applicable"  # a read tool
    assert nx.undos == 0


def test_a_component_held_by_constraints_is_refused_and_put_back():
    nx, _frame, lid, _stop = hatch()
    nx.asm.ComponentAssembly.MoveComponent = lambda c, shift, m: None  # constraints hold it
    lid.t = [1.0, 2.0, 3.0]
    ok = run(nx, "nx_run_motion_sweep", joint="revolute", moving=[lid.ref], axis=HINGE,
             start=10, end=20, steps=1)  # fmt: skip
    assert not ok.ok
    assert ok.error["code"] == "NX_CONSTRAINT_UNSATISFIED" and ok.error["field"] == "/moving"
    assert lid.t == [1.0, 2.0, 3.0]


def test_a_nested_component_is_refused():
    nx, _frame, lid, _stop = hatch()
    from tests.unit.motion.fakes import Component

    inner = Component(nx, "INNER", ([0.0, 0.0, 0.0], geom.IDENTITY), lid)
    ok = run(nx, "nx_run_motion_sweep", joint="revolute", moving=[nx.ref_of(inner, "component")],
             axis=HINGE, start=0, end=10)  # fmt: skip
    assert not ok.ok
    assert ok.error["code"] == "NX_WRONG_OWNER" and ok.error["field"] == "/moving/0"


def test_start_end_and_min_clearance_take_unit_strings():
    nx, _frame, lid, stop = hatch()
    ok = run(
        nx, "nx_run_motion_sweep", joint="revolute", moving=[lid.ref], axis=HINGE,
        start="0 deg", end="1.9198621771937625 rad", steps=11,
        pairs=[{"a": lid.ref, "b": stop.ref}], min_clearance="5 mm",
    )  # fmt: skip
    assert ok.ok, ok
    assert ok.data["reached"] == pytest.approx([0.0, 110.0])
    assert ok.data["min_clearance"] == 5.0
    assert ok.data["clearances"][0]["limit"] == pytest.approx(96.301957, abs=1e-3)
    slid = run(
        nx, "nx_run_motion_sweep", joint="slider", moving=[stop.ref],
        axis={"origin": [0, 0, 0], "direction": [0, -1, 0]}, start="0 mm", end="2 cm",
        steps=4, pairs=[{"a": stop.ref, "b": lid.ref}],
    )  # fmt: skip
    assert slid.ok and slid.data["values"] == pytest.approx([0, 5, 10, 15, 20])  # cm: 20 mm
    slid = run(
        nx, "nx_run_motion_sweep", joint="slider", moving=[stop.ref],
        axis={"origin": [0, 0, 0], "direction": [0, -1, 0]}, start="0 mm", end="20 mm",
        steps=4, pairs=[{"a": stop.ref, "b": lid.ref}],
    )  # fmt: skip
    assert slid.ok and slid.data["values"] == pytest.approx([0, 5, 10, 15, 20])


@pytest.mark.parametrize(
    ("start", "end", "field"),
    [("10 mm", 90, "/start"), (0, "90 mm", "/end"), ("abc", 90, "/start"),
     (0, 200000, "/end")],
)  # fmt: skip
def test_a_value_of_the_wrong_kind_is_refused(start, end, field):
    nx, _frame, lid, _stop = hatch()
    ok = run(nx, "nx_run_motion_sweep", joint="revolute", moving=[lid.ref], axis=HINGE,
             start=start, end=end)  # fmt: skip
    assert not ok.ok and ok.error["field"] == field, ok.error
    assert ok.error["category"] == "invalid_argument"
    assert nx.undos == 0
