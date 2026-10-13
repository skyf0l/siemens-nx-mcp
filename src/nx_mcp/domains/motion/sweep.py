"""``nx_run_motion_sweep``: range of motion and clearances of a mechanism (hinges, tillers,
tilt and trim, davits), swept on the assembly itself and always put back.

A sweep drives one degree of freedom through ``steps`` equal steps from ``start`` to
``end`` and samples the mechanism at every step:

- ``revolute``: the ``moving`` components turn together about ``axis`` by the value in
  degrees (0 = their current pose; right hand about the axis direction).
- ``slider``: they shift along the axis direction by the value in part units.
- ``constraint``: an angle or distance assembly constraint is set to the value and the
  assembly solves, so every component its constraints tie to it follows (a 4-bar
  steering linkage, a tilt ram). A value the solver cannot reach ends the sweep there
  (``stopped``, with the constraints that broke): the mechanism's range of motion. NX
  2406 solves the driven constraint past a linkage's reach by opening the loop, so every
  constraint of the assembly is checked, and the reach limit is bisected
  (``stopped.limit``; each try restarts from the design pose, as a broken loop does not
  close again).

Per step it reports tracked points carried by a component (a lid edge, a tiller end),
signed rotation angles of components about an axis (relative to the pose before the
sweep) and the minimum distance of each clearance pair (0: touching or overlapping).
For each pair the first step below ``min_clearance`` (contact when 0) is reported and
the limit value is bisected between that step and the clear one before.

Everything happens inside ``tx.scratch()``: the moves and constraint values are undone
and every component is read back at its pose from before the sweep. NX Motion is not
used: it needs a Motion solver license, and without one NX blocks on a license dialog
(``kit.kinematics`` notes).
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    Aliases,
    Axis,
    Doc,
    Items,
    Point3,
    Range,
    RefTo,
    Vec3,
    forbid_extra,
)
from nx_mcp.kit import bodies, geom, kinematics, select, units
from nx_mcp.kit import pose as kpose
from nx_mcp.kit.result import Row

MAX_STEPS = 120
REFINE_ITERATIONS = 14
CONTACT_MM = 0.01  # distance at or below which two bodies touch
VALUE_MAX = 100000.0

JointType = Annotated[
    Literal["revolute", "slider", "constraint"],
    Aliases(
        hinge="revolute",
        pivot="revolute",
        rotation="revolute",
        rotate="revolute",
        prismatic="slider",
        linear="slider",
        translation="slider",
        slide="slider",
        assembly_constraint="constraint",
        driven_constraint="constraint",
    ),
    Doc("constraint: drive an assembly constraint"),
]
ComponentId = Annotated[str, RefTo("component", doc="Component id 'o:N' (nx_list_components)")]
Moving = Annotated[list[ComponentId], Items(1, 20), Doc("Components moving together")]
ConstraintId = Annotated[str, RefTo("assembly_constraint", doc="Angle/distance constraint 'ac:N'")]
Value = Annotated[
    float,
    Range(ge=-VALUE_MAX, le=VALUE_MAX),
    Doc("Degrees, or part units (slider, distance)"),
]
PairSide = Annotated[str, RefTo("component", "body", doc="Component 'o:N' or body 'b:N'")]


class _TrackRequired(TypedDict):
    component: ComponentId


class Track(_TrackRequired, total=False):
    point: Annotated[Point3, Doc("Absolute point to follow")]
    axis: Annotated[Vec3, Doc("Report the angle about it")]


class Pair(TypedDict):
    a: PairSide
    b: PairSide


forbid_extra(Track, Pair)


class Step(TypedDict):
    index: int
    value: float


class PointPath(TypedDict):
    component: Row
    start: list[float]
    path: list[list[float]]


class AnglePath(TypedDict):
    component: Row
    axis: list[float]
    angles: list[float]
    min: float
    max: float
    travel: float


class Clearance(TypedDict, total=False):
    a: Row
    b: Row
    distances: list[float]
    min: float
    min_value: float
    min_points: list[list[float]]
    first_violation: Step | None
    limit: float | None
    clear_range: list[float] | None


class SweepData(TypedDict, total=False):
    joint: str
    unit: str
    values: list[float]
    reached: list[float]
    stopped: dict | None
    moved: list[Row]
    points: list[PointPath]
    angles: list[AnglePath]
    clearances: list[Clearance]
    contact_tolerance: float
    min_clearance: float
    restored: bool


def _summary(args, say) -> str:
    joint = args.get("joint") or "revolute"
    return f"Sweep {joint} from {args.get('start')} to {args.get('end')}"


def _direct(tx, ref, field):
    c = select.resolve(tx, ref, "component", field=field)
    root = tx.part.ComponentAssembly.RootComponent
    parent = getattr(c, "Parent", None)
    if root is None or parent is None or int(parent.Tag) != int(root.Tag):
        fail("NX_WRONG_OWNER", f"{ref} is not a direct component of the assembly", field=field,
             hint="Move a subassembly by its own component, or drive a constraint")  # fmt: skip
    if bodies.suppressed(c):
        fail("NX_INVALID_ARGUMENT", f"{ref} is suppressed", field=field)
    return c


def _side(tx, ref, field):
    obj = select.resolve(tx, ref, "component", "body", field=field)
    found = [b for b in bodies.of(tx, obj) if getattr(b, "IsSolidBody", True)]
    if not found:
        fail("NX_WRONG_GEOMETRY", f"{ref} has no solid body to measure", field=field)
    return obj, found


def _check_values(joint, start, end, steps):
    if not (math.isfinite(start) and math.isfinite(end)) or abs(end - start) <= 1e-9:
        fail("NX_INVALID_ARGUMENT", "start and end must differ", field="/end")
    if joint == "revolute" and max(abs(start), abs(end)) > 3600:
        fail("NX_INVALID_ARGUMENT", "A revolute sweep turns at most 3600 degrees", field="/end")
    if not 1 <= steps <= MAX_STEPS:
        fail("NX_INVALID_ARGUMENT", f"steps is 1 to {MAX_STEPS}", field="/steps")


@tool(
    target="assembly",
    effect="read",
    label="Sweep mechanism motion",
    summary=_summary,
    keywords=(
        "range of motion",
        "motion clearance",
        "hinge sweep",
        "kinematic sweep",
        "linkage motion",
        "swept interference",
        "rudder steering linkage",
        "tilt trim davit",
    ),
    requires=("work_part", "assembly"),
    variants="joint",
    cost="heavy",
    example={"joint": "constraint", "constraint": "ac:7", "start": 0, "end": 90},
)
def nx_run_motion_sweep(
    tx: Tx,
    *,
    joint: JointType,
    start: Value,
    end: Value,
    steps: Annotated[int, Range(ge=1, le=MAX_STEPS), Doc("Equal steps; samples = steps+1")] = 12,
    moving: Moving | None = None,
    axis: Annotated[Axis, Doc("Hinge axis, or slider direction")] | None = None,
    constraint: ConstraintId | None = None,
    track: Annotated[list[Track], Items(0, 10), Doc("Points/angles to follow")] | None = None,
    pairs: Annotated[list[Pair], Items(0, 10), Doc("Clearance pairs")] | None = None,
    min_clearance: Annotated[float, Range(ge=0), Doc("Required gap; 0: contact")] = 0,
) -> SweepData:
    """Sweep a hinge, slider or driven assembly constraint through its range of motion.
    Per step: tracked points, angles, pair gaps; first contact and limit. Put back after."""
    _check_values(joint, start, end, steps)
    needed = ("constraint",) if joint == "constraint" else ("moving", "axis")
    for name, given in zip(
        ("moving", "axis", "constraint"), (moving, axis, constraint), strict=True
    ):
        if name in needed and given is None:
            fail("NX_INVALID_ARGUMENT", f"joint {joint} needs {name}", field="/" + name)
    for i, item in enumerate(track or []):
        if item.get("point") is None and item.get("axis") is None:
            fail("NX_INVALID_ARGUMENT", "A track item takes point, axis or both",
                 field=f"/track/{i}")  # fmt: skip
    min_gap = float(min_clearance or 0.0)
    if min_gap < 0:
        fail("NX_INVALID_ARGUMENT", "min_clearance cannot be negative", field="/min_clearance")
    for i, pair in enumerate(pairs or []):
        if pair["a"] == pair["b"]:
            fail("NX_INVALID_ARGUMENT", "A clearance pair needs two objects", field=f"/pairs/{i}")
    with tx.checks():
        part = tx.part
        root = part.ComponentAssembly.RootComponent
        children = list(root.GetChildren()) if root is not None else []
        group, origin, direction, driven, unit = [], None, None, None, "deg"
        if joint in ("revolute", "slider"):
            seen = set()
            for i, ref in enumerate(moving or []):
                c = _direct(tx, ref, f"/moving/{i}")
                if int(c.Tag) in seen:
                    fail("NX_INVALID_ARGUMENT", f"{ref} is listed twice", field=f"/moving/{i}")
                seen.add(int(c.Tag))
                group.append(c)
            _obj, origin, direction = select.axis(tx, axis, "/axis")
            if joint == "slider":
                unit = units.part_units(tx)
        else:
            driven = select.resolve(tx, constraint, "assembly_constraint", field="/constraint")
            cls = tx.nx.Positioning.Constraint
            if driven.ConstraintType == cls.Type.Angle:
                if not (0 <= start <= 180 and 0 <= end <= 180):
                    fail("NX_INVALID_ARGUMENT", "An angle constraint takes 0 to 180 degrees",
                         field="/end", hint="Split a wider swing over a revolute sweep")  # fmt: skip
            elif driven.ConstraintType == cls.Type.Distance:
                unit = units.part_units(tx)
            else:
                fail("NX_INVALID_ARGUMENT", "constraint must be an angle or distance constraint",
                     field="/constraint")  # fmt: skip
            if driven.Suppressed:
                fail("NX_INVALID_ARGUMENT", f"{constraint} is suppressed", field="/constraint")
        tracked_points, tracked_angles = [], []
        for i, item in enumerate(track or []):
            c = _direct(tx, item["component"], f"/track/{i}/component")
            if item.get("point") is not None:
                tracked_points.append((c, [float(v) for v in item["point"]]))
            if item.get("axis") is not None:
                tracked_angles.append((c, geom.unit(item["axis"], f"/track/{i}/axis")))
        sides = []
        for i, pair in enumerate(pairs or []):
            a = _side(tx, pair["a"], f"/pairs/{i}/a")
            b = _side(tx, pair["b"], f"/pairs/{i}/b")
            sides.append((a, b))
    contact = units.part_tolerance(tx, CONTACT_MM)
    threshold = max(min_gap, contact)
    design = {int(c.Tag): (c, kinematics.pose(c)) for c in children}
    values = [start + (end - start) * i / steps for i in range(steps + 1)]
    group_start = [kinematics.pose(c) for c in group]

    def apply(value: float, mark) -> list:
        """Put the mechanism at ``value``; the constraints that do not solve there."""
        if joint == "constraint":
            return kinematics.drive(tx, driven, value, mark)
        for c, p0 in zip(group, group_start, strict=True):
            if joint == "revolute":
                target = kpose.rotate_about(p0, origin, direction, value)
            else:
                target = (geom.add(p0[0], geom.scale(direction, value)), p0[1])
            kinematics.place(tx, c, target)
            got = kinematics.pose(c)
            if not kpose.close(got, target, tol=max(1e-6, geom.tol(target[0]))):
                fail("NX_CONSTRAINT_UNSATISFIED",
                     f"{c.Name} did not move freely: assembly constraints hold it",
                     hint="Drive the constraint that positions it (joint constraint), or "
                     "suppress its constraints first", field="/moving")  # fmt: skip
        return []

    def reset(mark) -> None:
        if joint == "constraint":  # a solve past the reach opens the loop: restart
            tx.services.undo_to(mark)

    def gaps():
        return [kinematics.gap(tx, a[1], b[1]) for a, b in sides]

    samples: list[dict] = []
    stopped: dict = {}
    moved: dict[int, object] = {}
    refined: dict[int, float] = {}
    with tx.scratch() as mark:
        for i, value in enumerate(values):
            tx.checkpoint(i, len(values))
            unsolved = apply(value, mark)
            if unsolved:
                stopped = {
                    "index": i,
                    "value": value,
                    "reason": "the assembly does not solve there",
                    "constraints": [tx.ref(c, "assembly_constraint") for c in unsolved[:10]],
                }
                break
            sample = {
                "value": value,
                "poses": {tag: kinematics.pose(c) for tag, (c, _p) in design.items()},
                "gaps": gaps(),
            }
            for tag, (c, p0) in design.items():
                if not kpose.close(sample["poses"][tag], p0, tol=max(1e-6, geom.tol(p0[0]))):
                    moved[tag] = c
            samples.append(sample)
        if samples:
            if stopped and stopped["index"] > 0:
                good, bad = samples[-1]["value"], stopped["value"]
                for _ in range(REFINE_ITERATIONS):
                    tx.checkpoint()
                    mid = (good + bad) / 2.0
                    reset(mark)
                    if apply(mid, mark):
                        bad = mid
                    else:
                        good = mid
                stopped["limit"] = good
            for k in range(len(sides)):
                first = next((j for j, s in enumerate(samples) if s["gaps"][k][0] <= threshold),
                             None)  # fmt: skip
                if not first:
                    continue  # never, or already at the first step: nothing to bisect
                good, bad = samples[first - 1]["value"], samples[first]["value"]
                for _ in range(REFINE_ITERATIONS):
                    tx.checkpoint()
                    mid = (good + bad) / 2.0
                    reset(mark)
                    ok = not apply(mid, mark)
                    if ok and kinematics.gap(tx, sides[k][0][1], sides[k][1][1])[0] > threshold:
                        good = mid
                    else:
                        bad = mid
                refined[k] = (good + bad) / 2.0
    _restored(tx, design)
    if not samples:
        fail("NX_CONSTRAINT_UNSATISFIED", f"The assembly does not solve at start={start}",
             field="/start", details={"stopped": stopped})  # fmt: skip
    return _result(tx, joint, unit, values, samples, stopped, moved, tracked_points,
                   tracked_angles, sides, refined, contact, min_gap, threshold, design)  # fmt: skip


def _restored(tx, design) -> None:
    """Every component is back at its pose from before the sweep."""
    for c, p0 in design.values():
        got = kinematics.pose(c)
        if not kpose.close(got, p0, tol=max(1e-6, geom.tol(p0[0]))):
            fail("NX_ROLLBACK_FAILED", f"{c.Name} did not return to its pose after the sweep",
                 details={"component": c.Name, "translation": got[0]})  # fmt: skip


def _num(value: float) -> float:
    return round(float(value), 6)


def _point(p) -> list[float]:
    return [_num(v) for v in p]


def _result(tx, joint, unit, values, samples, stopped, moved, tracked_points, tracked_angles,
            sides, refined, contact, min_gap, threshold, design) -> SweepData:  # fmt: skip
    reached = [values[0], samples[-1]["value"]]
    out: SweepData = {
        "joint": joint,
        "unit": unit,
        "values": [_num(s["value"]) for s in samples],
        "reached": [_num(v) for v in reached],
        "moved": [tx.ref(c, "component") for c in moved.values()],
        "contact_tolerance": contact,
        "min_clearance": min_gap,
        "restored": True,
        "stopped": None,
    }
    if stopped:
        out["stopped"] = {
            **stopped,
            **{k: _num(stopped[k]) for k in ("value", "limit") if k in stopped},
        }
    paths: list[PointPath] = []
    for c, point in tracked_points:
        p0 = design[int(c.Tag)][1]
        path = [_point(kinematics.carry(point, p0, s["poses"][int(c.Tag)])) for s in samples]
        paths.append({"component": tx.ref(c, "component"), "start": _point(point), "path": path})
    out["points"] = paths
    turned: list[AnglePath] = []
    for c, axis in tracked_angles:
        p0 = design[int(c.Tag)][1]
        history: list[float] = []
        for s in samples:
            angle = kinematics.angle_about(p0, s["poses"][int(c.Tag)], axis)
            history.append(kinematics.unwrap(history[-1] if history else None, angle))
        turned.append({
            "component": tx.ref(c, "component"), "axis": _point(axis),
            "angles": [_num(a) for a in history], "min": _num(min(history)),
            "max": _num(max(history)), "travel": _num(max(history) - min(history)),
        })  # fmt: skip
    out["angles"] = turned
    rows: list[Clearance] = []
    for k, ((a, _ab), (b, _bb)) in enumerate(sides):
        distances = [s["gaps"][k][0] for s in samples]
        low = min(range(len(distances)), key=distances.__getitem__)
        first = next((j for j, d in enumerate(distances) if d <= threshold), None)
        row: Clearance = {
            "a": tx.ref(a),
            "b": tx.ref(b),
            "distances": [_num(d) for d in distances],
            "min": _num(distances[low]),
            "min_value": _num(samples[low]["value"]),
            "min_points": [_point(samples[low]["gaps"][k][1]), _point(samples[low]["gaps"][k][2])],
            "first_violation": None,
            "limit": None,
            "clear_range": [reached[0], reached[1]],
        }
        if first is not None:
            row["first_violation"] = {"index": first, "value": _num(samples[first]["value"])}
            limit = refined.get(k, samples[first]["value"])
            row["limit"] = _num(limit)
            row["clear_range"] = None if first == 0 else [_num(values[0]), _num(limit)]
        rows.append(row)
    out["clearances"] = rows
    return out
