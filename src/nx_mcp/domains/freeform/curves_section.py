"""Section curves (bodies or faces cut by one plane, parallel planes or radial planes) and
intersection curves (faces with faces or with a plane).

NX 2406 lessons kept here:

- ``CreateSectionCurveBuilder``: ``ObjectsToSection`` and ``SectionPlanes`` are selection
  lists (``Add``); ``Type`` ``Selected`` cuts by the listed planes, ``Parallel`` by
  ``BasePlane`` moved by ``StartDistance``..``EndDistance`` in steps of ``StepDistance``
  (plain floats, not expressions; NXOpen refuses Python ints), ``Radial`` by full planes
  through the axis ``RadialPlaneVector`` turned ``StartAngle``..``EndAngle`` by
  ``StepAngle`` from the plane through the axis and ``RadialPlanePoint`` (a point on the
  axis is refused: "Point for plane lies on axis"; ``BasePlane`` is not needed). A block
  100 x 50 x 20 cut at x = 50 gives 4 lines 50/20/20/50; a cylinder d 40 h 30 cut through
  its axis gives lines 40/40/30/30.
- ``CreateIntersectionCurveBuilder``: ``FirstFace``/``SecondFace`` are face collectors,
  ``SecondPlane`` a smart plane; a cylinder side r 20 with z = 10 gives one arc of
  length 125.66371.
- A plane given as a datum or face ref is used as a fixed plane through its origin and
  normal (the curves stay associative to the cut bodies and faces).
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    Angle,
    Axis,
    Doc,
    Items,
    Length,
    Plane,
    Point3,
    Range,
    RefTo,
    Vec3,
    axis_rotation,
)
from nx_mcp.domains.freeform import curves_common as cc
from nx_mcp.kit import args, builders, geom, measure, readback, select, units

MAX_PLANES = 100
SolidRef = Annotated[str, RefTo("body", "face", doc="Body or face 'b:3'/'fc:7'")]
Solids = Annotated[list[SolidRef], Items(1, 200)]


class Station(TypedDict):
    origin: Point3
    normal: Vec3
    count: int  # curves on this plane
    length: float


class SectionData(cc.CurvesData):
    stations: list[Station]
    gap: float  # largest sampled distance of a curve from its plane or from the objects


def _stations(tx, method, plane, count, spacing, axis, angle):
    """``(stations, nx)``: every cutting plane ``(origin, unit normal)`` and how NX builds
    them (``("selected",)``, ``("parallel", origin, normal, step)`` or
    ``("radial", at, direction, normal, step)``)."""
    _obj, origin, normal = select.plane(tx, plane, "/plane")
    if method == "plane":
        return [(origin, normal)], ("selected",)
    if method == "parallel":
        args.need(count=count, spacing=spacing)
        step = float(spacing)
        if geom.close(step, 0.0, 1e-12):
            fail("NX_INVALID_ARGUMENT", "spacing must not be zero", field="/spacing")
        stations = [(geom.add(origin, geom.scale(normal, i * step)), normal) for i in range(count)]
        return stations, ("parallel", origin, normal, step)
    args.need(axis=axis, count=count, angle=angle)
    _a, at, direction = select.axis(tx, axis, "/axis")
    off = abs(geom.dot(geom.sub(at, origin), normal))
    if abs(geom.dot(direction, normal)) > 1e-6 or off > geom.tol(at, origin):
        fail(
            "NX_INVALID_ARGUMENT",
            "radial: the plane must contain the axis",
            field="/plane",
            hint="Give the first radial plane through the axis, e.g. axis 'z' with plane 'xz'",
        )
    step = float(angle)
    if geom.close(step, 0.0, 1e-12) or abs(step) * (count - 1) >= 180.0 - 1e-9:
        fail(
            "NX_INVALID_ARGUMENT",
            "radial: angle must be nonzero and angle x (count - 1) below 180 degrees "
            "(a full plane repeats after 180)",
            field="/angle",
        )
    stations = [
        (list(at), geom.matvec(axis_rotation(direction, i * step), normal)) for i in range(count)
    ]
    return stations, ("radial", at, direction, normal, step)


def _configure(tx, b, objs, stations, how):
    T = tx.nx.Features.SectionCurveBuilder.PlaneType
    tol = units.part_tolerance(tx)
    b.Associative = True
    b.Tolerance = tol
    b.CurveFitData.Tolerance = tol
    b.ObjectsToSection.Add(objs)
    if how[0] == "selected" or len(stations) == 1:
        b.Type = T.Selected
        for origin, normal in stations:
            b.SectionPlanes.Add(cc.plane_object(tx, origin, normal))
        return
    if how[0] == "parallel":
        _, origin, normal, step = how
        b.Type = T.Parallel
        b.BasePlane = cc.plane_object(tx, origin, normal if step > 0 else geom.scale(normal, -1))
        b.StartDistance = 0.0
        b.EndDistance = abs(step) * (len(stations) - 1)
        b.StepDistance = abs(step)
        return
    _, at, direction, normal, step = how
    b.Type = T.Radial
    turn = direction if step > 0 else geom.scale(direction, -1)
    b.RadialPlaneVector = builders.direction(tx, turn, at)
    # A point of the first plane off the axis (NX refuses one on the axis).
    reach = max(1.0, geom.norm(at))
    b.RadialPlanePoint = builders.point(
        tx, geom.add(at, geom.scale(geom.cross(normal, direction), reach))
    )
    b.StartAngle = 0.0
    b.EndAngle = abs(step) * (len(stations) - 1)
    b.StepAngle = abs(step)


def _read_back(tx, curves, stations, faces):
    """``(per-station rows, worst gap)``: each curve lies on one plane and on the faces."""
    rows = [
        {"origin": cc.clean(o), "normal": cc.clean(n), "count": 0, "length": 0.0}
        for o, n in stations
    ]
    worst = 0.0
    sampled = curves[: cc.MAX_SAMPLED]
    for c in sampled:
        tx.checkpoint()
        points = cc.samples(tx, c)
        gaps = [cc.plane_gap_points(points, o, n) for o, n in stations]
        k = min(range(len(stations)), key=gaps.__getitem__)
        limit = cc.gap_tolerance(tx, *points)
        readback.expect(gaps[k] <= limit, "plane", "A section curve is off its plane", gap=gaps[k])
        worst = max(worst, gaps[k])
        rows[k]["count"] += 1
        rows[k]["length"] += measure.length(tx, c)
    on_faces = cc.worst_gap(tx, sampled, faces)
    readback.expect(
        on_faces <= cc.gap_tolerance(tx), "objects", "A section curve is off the objects",
        gap=on_faces,
    )  # fmt: skip
    return rows, max(worst, on_faces)


@tool(
    effect="model",
    label="Create section curves",
    summary={
        "plane": "Section {objects} by a plane",
        "parallel": "Section {objects} by {count} parallel planes",
        "radial": "Section {objects} by {count} radial planes",
    },
    keywords=(
        "section curves",
        "stations waterlines buttocks",
        "cut by plane",
        "radial sections",
        "contour curves",
        "cross sections",
    ),
    requires=("work_part", "no_active_sketch"),
    variants="method",
    required_by={"parallel": ("count", "spacing"), "radial": ("axis", "count", "angle")},
    cost="heavy",
    previewable=True,
    example={"objects": ["b:1"], "method": "parallel", "plane": "yz", "count": 5, "spacing": 20},
)
def nx_create_section_curves(
    tx: Tx,
    *,
    objects: Annotated[Solids, Doc("Bodies or faces to cut")],
    method: Literal["plane", "parallel", "radial"] = "plane",
    plane: Annotated[
        Plane,
        Doc("plane: the cut; parallel: the first plane; radial: the first plane (holds axis)"),
    ],
    count: Annotated[int, Range(ge=1, le=MAX_PLANES), Doc("parallel/radial: planes")] | None = None,
    spacing: Annotated[Length, Doc("parallel: step along the plane normal; - goes against")]
    | None = None,
    axis: Annotated[Axis, Doc("radial: the axis all planes contain")] | None = None,
    angle: Annotated[Angle, Doc("radial: step between planes, right-hand about axis")]
    | None = None,
) -> SectionData:
    """Cut bodies or faces by planes into associative section curves. method: plane (one),
    parallel (count planes, spacing apart: stations, waterlines, buttocks with plane 'yz',
    'xy', 'xz') or radial (count planes about axis, angle apart). Returns curves with length
    and ends, and per-plane stations. Loops without curves: nx_measure_section."""
    with tx.checks():
        objs, faces = cc.solids(tx, objects, "/objects")
        stations, how = _stations(tx, method, plane, count, spacing, axis, angle)
    cc.licensed(tx)
    with builders.build(tx, tx.part.Features.CreateSectionCurveBuilder, None) as b:
        _configure(tx, b, objs, stations, how)
        feature = b.commit()
    curves = cc.output_curves(tx, feature)
    rows, gap = _read_back(tx, curves, stations, faces)
    return {**cc.describe(tx, feature, curves), "stations": rows, "gap": gap}


# ---------------------------------------------------------------------------
# Intersection curve


class IntersectionData(cc.CurvesData):
    gap: float  # largest sampled distance of a curve from either side


@tool(
    effect="model",
    label="Create intersection curve",
    summary=lambda a, say: (
        f"Intersect {say('first')} with {say('second')}"
        if a.get("second")
        else f"Intersect {say('first')} with a plane"
    ),
    keywords=("intersection curve", "face intersection", "surface intersection", "cut line"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"first": ["fc:7"], "second": ["fc:12"]},
)
def nx_create_intersection_curve(
    tx: Tx,
    *,
    first: Annotated[Solids, Doc("Faces, or bodies (all their faces)")],
    second: Annotated[Solids, Doc("Faces or bodies; or give plane")] | None = None,
    plane: Annotated[Plane, Doc("Instead of second: cut first with this plane")] | None = None,
) -> IntersectionData:
    """Create associative curves where the first faces meet the second faces or a plane.
    Bodies stand for all their faces. Returns each curve (length, ends) and gap, the largest
    distance from either side. Many parallel planes through bodies: nx_create_section_curves."""
    args.exactly_one(second=second, plane=plane)
    with tx.checks():
        _objs, faces1 = cc.solids(tx, first, "/first")
        if second is not None:
            _objs, faces2 = cc.solids(tx, second, "/second")
            if {int(f.Tag) for f in faces1} & {int(f.Tag) for f in faces2}:
                fail("NX_INVALID_ARGUMENT", "first and second share a face", field="/second")
            cut = None
        else:
            faces2 = []
            _o, origin, normal = select.plane(tx, plane, "/plane")
            cut = (origin, normal)
    cc.licensed(tx)
    tol = units.part_tolerance(tx)
    with builders.build(tx, tx.part.Features.CreateIntersectionCurveBuilder, None) as b:
        b.Associative = True
        b.Tolerance = tol
        b.CurveFitData.Tolerance = tol
        builders.fill(tx, b.FirstFace, faces1, "Face")
        if cut is None:
            builders.fill(tx, b.SecondFace, faces2, "Face")
        else:
            b.SecondPlane = cc.plane_object(tx, *cut)
        feature = b.commit()
    curves = cc.output_curves(tx, feature)
    gap = cc.worst_gap(tx, curves, faces1)
    other = cc.worst_gap(tx, curves, faces2) if cut is None else cc.plane_gap(tx, curves, *cut)
    gap = max(gap, other)
    readback.expect(gap <= cc.gap_tolerance(tx), "gap", "A curve is off the faces", gap=gap)
    return {**cc.describe(tx, feature, curves), "gap": gap}
