"""Swept Volume: the space a cylindrical or revolved tool sweeps along a path, cut from or
intersected with target bodies (rudder/outboard/propeller envelopes, clearance cuts).

NX 2406 lessons kept here (probed live with a d4 x 6 cylinder and a 70 x 20 x 6 block):

- ``SweepFeatureCollection.CreateSweptVolumeBuilder`` needs the basic freeform license
  (``nx_freeform_1``), asked before the builder.
- ``ToolBody`` takes a body collector; NX accepts only a body made by a cylinder,
  revolve or cylindrical extrude ("The tool body must be a revolved feature, cylindrical
  extrude, or a cylinder feature"). ``ToolPath`` is the builder's own section.
- The path's shape moves the tool from where it stands: a 30 long line far from the tool
  swept it 30 from its own position. Along a 50 line (cylinder axis z, base at z 0,
  intersected with a block z -3..3): (pi 2^2 + 4 x 50) x 3 = 637.699 exactly; subtract
  removes the same from the block. An L path 20 + 25: 3 x (4 x 45 + 4 pi - 4 (1 - pi/4)) =
  575.124 (the inner corner overlaps).
- ``BooleanData`` ``Create`` is refused ("Swept Volume feature cannot be created") at
  every tolerance probed, and once (tolerance 0.001 mm) hung NX for over 10 minutes; Unite
  left the target unchanged (8400 stayed 8400). Subtract and Intersect work, so the tool
  offers those two: intersect with a block that encloses the motion gives the envelope
  body. ``SweepOrientation`` ``NormalToPath`` was refused for every path probed
  (``ParallelToBody``, the default, keeps the tool's axis).
- ``CommitFeature`` returns None and ``GetCommittedObjects`` is empty: the feature
  ("Swept Volume") is the part's new feature.
- Tolerance 0.01 mm (part units) by default and at least: 0.001 mm hung NX for over 10
  minutes in a live test, so a smaller tolerance (or one below the part's modeling
  distance tolerance) is refused before NX.
"""

from __future__ import annotations

import contextlib
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import BodyRef, Doc, Items, Length, RefTo
from nx_mcp.domains.freeform import common
from nx_mcp.kit import bodies as kbodies
from nx_mcp.kit import builders, curve, geom, licenses, measure, readback, result, select, units

LICENSE = "nx_freeform_1"
TOLERANCE_MM = 0.01  # the default and the smallest tolerance accepted
PathRef = Annotated[str, RefTo("curve", "edge", doc="Curve or edge 'c:7'/'e:21'")]


class SweptData(TypedDict):
    feature: result.Row | None
    bodies: list[result.Row]  # the targets after the operation
    operation: Literal["subtract", "intersect"]
    volume_before: float  # targets' total volume before
    volume: float  # targets' total volume after
    swept: float  # subtract: volume removed; intersect: volume kept (envelope in targets)
    bounds: common.Bounds


def _min_tolerance(tx) -> float:
    """The smallest sweep tolerance in part units: 0.01 mm, or the part's modeling
    distance tolerance when that is larger."""
    low = units.part_tolerance(tx, TOLERANCE_MM)
    with contextlib.suppress(Exception):
        low = max(low, float(tx.part.Preferences.Modeling.DistanceToleranceData))
    return low


HINT = (
    "The tool must be a solid made by a cylinder, revolve or cylindrical extrude, the path "
    "one chain of curves starting level with the tool axis, and the swept space must cross "
    "the targets' faces"
)
HINTS = {
    4120009: "NX found no place on the path for the tool: start the path level with the tool "
    "axis (its point nearest the axis), e.g. above the tool's base centre",
    4120021: "The swept space lies wholly inside or outside the target: intersect needs a "
    "target the sweep crosses, e.g. a block thinner than the tool along its axis",
}


def _axis(tx, body):
    """``(point, unit direction)`` of the tool's axis: its first cylinder or cone face's
    axis (None when it has neither)."""
    for face in body.GetFaces():
        kind, point, direction = select.face_data(tx, face)
        if kind in (16, 17) and geom.norm(direction) > 1e-12:
            return point, geom.unit(direction)
    return None


def _check_start(tx, body, curves) -> None:
    """Refuse a path that does not start level with the tool axis.

    NX places the path at its point nearest the tool axis (seen along the axis) and
    sweeps the whole path from there: a straight path starting 10 off sideways cut a slot
    10 longer behind the tool, and one whose nearest point is an end that is not level
    with the axis failed ('The Tool Body is invalid', 4120009). The path start must be
    level: the offset from the axis to the start, across the axis, is normal to the path.
    """
    found = _axis(tx, body)
    if found is None:
        return
    origin, axis = found
    with curve.evaluator(tx, curves[0]) as e:
        start, tangent = e.at(0.0)

    def across(v):
        return geom.sub(v, geom.scale(axis, geom.dot(v, axis)))

    offset = across(geom.sub(start, origin))
    along = across(tangent)
    if geom.norm(along) <= 1e-9:
        return  # the path starts along the axis
    along = geom.unit(along)
    lead = geom.dot(offset, along)
    if abs(lead) <= units.part_tolerance(tx, TOLERANCE_MM) + geom.tol(start, origin):
        return
    shift = geom.scale(along, -lead)
    fail(
        "NX_INVALID_ARGUMENT",
        f"The path starts {abs(lead):.4g} ahead of or behind the tool axis: NX would sweep "
        "from the path's point nearest the axis instead of from the tool",
        field="/path/0",
        hint="Start the path level with the tool axis: move it by details.shift (or start it "
        "at the tool's base centre)",
        details={"lead": lead, "shift": [float(v) for v in shift]},
    )


def _feature_tags(part):
    return {int(f.Tag) for f in part.Features}


@tool(
    effect="model",
    label="Create swept volume",
    summary="Swept volume of {tool} along {path}, {operation} {targets}",
    keywords=(
        "swept volume",
        "tool envelope",
        "motion envelope",
        "clearance cut",
        "sweep body along path",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"tool": "b:3", "path": ["c:5"], "operation": "intersect", "targets": ["b:9"]},
)
def nx_create_swept_volume(
    tx: Tx,
    *,
    tool: Annotated[BodyRef, Doc("Solid of a cylinder, revolve or cylindrical extrude feature")],
    path: Annotated[list[PathRef], Items(1, 100), Doc("Chained curves/edges: the motion's shape")],
    targets: Annotated[list[BodyRef], Items(1, 20), Doc("Solid bodies to cut or intersect")],
    operation: Annotated[
        Literal["subtract", "intersect"],
        Doc("subtract: cut the swept space; intersect: keep only the swept space"),
    ] = "subtract",
    tolerance: Annotated[Length, Doc("Default and minimum 0.01 mm (finer can hang NX)")]
    | None = None,
) -> SweptData:
    """Cut or intersect targets with the space a cylindrical/revolved tool sweeps along a path,
    axis kept. Start the path level with the tool axis (the path's point nearest the axis,
    e.g. above the base centre): the tool then moves by the path's shape from where it
    stands. Envelope body: intersect with a block the sweep pierces (thinner than the tool
    along its axis); a block enclosing the whole sweep is refused. Reports the volume change."""
    if tool in targets:
        fail("NX_INVALID_ARGUMENT", "The tool cannot be a target", field="/targets")
    with tx.checks():
        body = select.one(tx, tool, "body", field="/tool")
        if common.is_sheet(body):
            fail("NX_WRONG_GEOMETRY", "The tool must be a solid", field="/tool")
        found = select.bodies(tx, targets, "/targets")
        for i, target in enumerate(found):
            if common.is_sheet(target):
                fail("NX_WRONG_GEOMETRY", "Targets must be solids", field=f"/targets/{i}")
            if int(target.Tag) == int(body.Tag):
                fail("NX_INVALID_ARGUMENT", "The tool cannot be a target", field=f"/targets/{i}")
        curves = [
            select.one(tx, ref, "curve", "edge", field=f"/path/{i}") for i, ref in enumerate(path)
        ]
        if len({int(c.Tag) for c in curves}) != len(curves):
            fail("NX_INVALID_ARGUMENT", "A path curve is listed twice", field="/path")
        _check_start(tx, body, curves)
        floor = _min_tolerance(tx)
        tol = floor if tolerance is None else float(tolerance)
        if tol < floor * (1 - 1e-9):
            least = units.say(floor, units.part_units(tx))
            fail(
                "NX_INVALID_ARGUMENT",
                f"tolerance must be at least {least}: a finer sweep can hang NX for minutes",
                field="/tolerance",
                hint=f"Pass {least} or more, or omit tolerance for the default",
                details={"minimum": floor, "units": units.part_units(tx)},
            )
    licenses.require(tx, (LICENSE,), f"This NX seat has no {LICENSE} license for this command",
                     strict=True)  # fmt: skip
    part = tx.part
    before = measure.volume(tx, found)
    features = _feature_tags(part)
    G = tx.nx.GeometricUtilities.BooleanOperation.BooleanType
    try:
        with builders.build(tx, part.Features.SweepFeatureCollection.CreateSweptVolumeBuilder,
                            None) as b:  # fmt: skip
            b.ToolBody = builders.collector(tx, [body], "Body")
            builders.curve_section(tx, curves, start=curve.ends(tx, curves[0])[0], into=b.ToolPath)
            b.DistanceTolerance = tol
            b.BooleanData.Type = G.Subtract if operation == "subtract" else G.Intersect
            b.BooleanData.SetTargetBodies(found)
            b.commit()
    except NXError as error:
        if error.code != "NX_BUILD_FAILED":
            raise
        raise common.explain(error, HINTS.get(error.nx_code or 0, HINT)) from None
    made = [f for f in part.Features if int(f.Tag) not in features]
    live = {int(x.Tag) for x in part.Bodies}
    after_bodies = [x for x in found if int(x.Tag) in live]
    readback.expect(bool(after_bodies), "targets", "NX left no target body")
    kbodies.expect_sound(tx, after_bodies)
    after = measure.volume(tx, after_bodies)
    limit = 1e-6 * max(1.0, before)
    swept = before - after if operation == "subtract" else after
    readback.expect(swept > limit, "volume", "The swept space does not meet the targets",
                    volume_before=before, volume=after)  # fmt: skip
    low, high = measure.bounds(tx, after_bodies, exact=True)
    return {
        "feature": tx.ref(made[-1], kind="feature") if made else None,
        "bodies": [tx.ref(x, kind="body") for x in after_bodies],
        "operation": operation,
        "volume_before": before,
        "volume": after,
        "swept": swept,
        "bounds": {"min": low, "max": high},
    }
