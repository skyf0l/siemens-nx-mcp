"""Curve editing features: bridge two curve ends, join curves into one, mirror curves about
a plane, and trim or divide a curve at planes, points or equal lengths.

NX 2406 lessons kept here:

- Bridge: ``CreateBridgeCurveBuilderEx(None)`` with Section start and end objects; the
  help point of each section (the chosen end) picks the end the bridge attaches to, and
  the bridge leaves it outward (continuing the curve) at the default 0 % location. A 100 %
  location turned the tangent back along the curve, so it is never set. Continuity is
  ``StartConnectivity.Continuity.ContinuityType`` (G0..G3); tangent magnitudes stay 1.
  Two lines, A end to B start, G1: the bridge left A along +x and reached B along +x.
- Join: ``CreateJoinCurvesBuilder(None)``, input in ``Section``; General output keeps the
  shape exactly (50 + 40 mm lines joined 90 mm). A 1 mm gap: "Gaps in string or multiple
  loops".
- Mirror: ``CreateMirrorCurveBuilder(None)``, input in ``Curve``, ``PlaneOption`` NewPlane
  with a smart plane in ``NewPlane``.
- Trim/divide: ``CreateTrimCurve2FeatureBuilder(None)``. Bounding objects work as planes
  only (``TrimCurveBoundingObjectBuilder`` method SelectPlane, ``BoundingPlane``): with
  SelectObject, a curve, datum plane or line in ``BoundingObjectList`` always failed
  "Bounding objects are either suppressed or modeled away", so cut points and equal
  lengths become planes normal to the curve there. Every bounding builder is appended to
  ``BoundingObjectList`` as soon as it is created: one left unappended blocked
  ``Part.Close`` ("still referenced") and the part could not be closed again. Trim: Keep
  plus ``SelectTrimRegion(point)``; a plane beyond the curve end did not extend the curve
  (natural or linear extension), so planes must cross the curve. Divide: ``OperationOption``
  Divide plus ``SelectDivideLocation(point)`` per cut ("No divide location found" without
  one). A half circle divided at three normal planes gave four 39.27 arcs. A closed curve
  is not broken at its seam: two cuts gave two pieces, three cuts (one at the seam) three.
  ``GetEntities`` lists the pieces in no stable order (a 100 mm line cut at 20 and 70 once
  came back 30/20/50), so they are sorted along the source by their midpoints.
- ``CreateDivideCurveBuilder`` (Divide Curve) is not used: it cuts the input curve itself
  (non-associative; the input became the first piece).
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Plane, Point3, Range, RefTo
from nx_mcp.domains.freeform import curves_common as cc
from nx_mcp.kit import (
    args,
    builders,
    expr,
    geom,
    measure,
    readback,
    result,
    select,
    units,
)
from nx_mcp.kit import curve as kcurve

StringRef = Annotated[str, RefTo("curve", "edge", doc="Curve or edge 'c:7'/'e:21'")]
Continuity = Literal["G0", "G1", "G2", "G3"]
CONTINUITY_DOC = "G0 touch, G1 tangent, G2 curvature, G3 curvature rate"
MAGNITUDE_DOC = "G1 and above: tangent magnitude (default 1)"
ANGLE_TOL = 0.01  # degrees, G1 and higher
JOIN_TYPES = {"general": "General", "cubic": "Cubic", "quintic": "Quintic"}
SAMPLES = 1000  # curve samples for plane crossings and arc lengths
GAP_HINT = "Join needs one chain of curves meeting end to end (gaps under 0.001 mm)"


class CurveEnd(TypedDict):
    curve: StringRef
    end: Annotated[Literal["start", "end"], Doc("The end of that curve the bridge attaches to")]


class BridgeData(TypedDict):
    feature: result.Row
    curve: result.Row
    length: float
    start: Point3
    end: Point3
    start_gap: float  # distance from the chosen end of start.curve
    end_gap: float
    start_angle: float  # degrees between the bridge and start.curve's outward tangent
    end_angle: float
    start_magnitude: float  # tangent magnitudes the bridge was built with
    end_magnitude: float


class JoinData(cc.CurvesData):
    source_length: float


class MirrorData(cc.CurvesData):
    symmetry_gap: float  # largest distance of a reflected source sample from the mirror
    skipped: list[str]  # input refs lying in the plane (not mirrored)


class TrimData(cc.CurvesData):  # curves: the pieces along the source from its start
    source_length: float
    locations: list[Point3]  # divide: the cut points


def _angle(u, v) -> float:
    return math.degrees(math.acos(max(-1.0, min(1.0, geom.dot(geom.unit(u), geom.unit(v))))))


def _end_frame(tx, obj, which):
    """``(point, outward unit tangent)`` at the start or end of a curve or edge."""
    with kcurve.evaluator(tx, obj) as e:
        point, tangent = e.at(1.0 if which == "end" else 0.0)
    return point, tangent if which == "end" else geom.scale(tangent, -1.0)


def _only(tx, feature):
    made = cc.output_curves(tx, feature)
    readback.expect(len(made) == 1, "curves", "NX built more than one curve", count=len(made))
    return made[0]


# ---- bridge ---------------------------------------------------------------------------------
@tool(
    effect="model",
    label="Create bridge curve",
    summary=lambda a, say: "Bridge two curve ends",
    keywords=("bridge curve", "connect curves", "blend curve", "tangent connection", "g2 curve"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"start": {"curve": "c:7", "end": "end"}, "end": {"curve": "c:9", "end": "start"}},
)
def nx_create_bridge_curve(
    tx: Tx,
    *,
    start: CurveEnd,
    end: CurveEnd,
    start_continuity: Annotated[Continuity, Doc(CONTINUITY_DOC)] = "G1",
    end_continuity: Annotated[Continuity, Doc(CONTINUITY_DOC)] = "G1",
    start_magnitude: Annotated[float, Range(gt=0, le=100), Doc(MAGNITUDE_DOC)] | None = None,
    end_magnitude: Annotated[float, Range(gt=0, le=100), Doc(MAGNITUDE_DOC)] | None = None,
) -> BridgeData:
    """Create an associative bridge curve between an end of one curve or edge and an end of
    another. Each side is {curve, end: start|end}; the bridge leaves that end outward,
    continuing the curve, with the side's continuity and tangent magnitude (default 1;
    larger keeps it closer to the curve's direction longer). Returns the gaps and the
    tangent angles at both ends (0 for G1 and above)."""
    for name, value, kind in (("start_magnitude", start_magnitude, start_continuity),
                              ("end_magnitude", end_magnitude, end_continuity)):  # fmt: skip
        if value is not None and kind == "G0":
            fail("NX_INVALID_ARGUMENT", f"{name} applies to G1 and above (a G0 end has no "
                 "tangent)", field=f"/{name}")  # fmt: skip
    with tx.checks():
        first = select.one(tx, start["curve"], "curve", "edge", field="/start/curve")
        second = select.one(tx, end["curve"], "curve", "edge", field="/end/curve")
        if first is second and start["end"] == end["end"]:
            fail("NX_INVALID_ARGUMENT", "start and end name the same curve end", field="/end/end")
        p0, out0 = _end_frame(tx, first, start["end"])
        p1, out1 = _end_frame(tx, second, end["end"])
        if geom.dist(p0, p1) <= cc.gap_tolerance(tx, p0, p1):
            fail("NX_INVALID_ARGUMENT", "The two ends touch: there is nothing to bridge",
                 field="/end")  # fmt: skip
    kinds = tx.nx.Features.BridgeCurveBuilderEx
    continuity = tx.nx.GeometricUtilities.Continuity.ContinuityTypes
    with (
        cc.licensed(tx, "nx_freeform_1"),
        builders.build(tx, tx.part.Features.CreateBridgeCurveBuilderEx, None) as b,
    ):
        b.StartObjectType = kinds.StartObjectTypes.Section
        b.EndObjectType = kinds.EndObjectTypes.Section
        builders.curve_section(tx, [first], start=p0, into=b.StartSection)
        builders.curve_section(tx, [second], start=p1, into=b.EndSection)
        b.StartConnectivity.Continuity.ContinuityType = getattr(continuity, start_continuity)
        b.EndConnectivity.Continuity.ContinuityType = getattr(continuity, end_continuity)
        if start_magnitude is not None or end_magnitude is not None:
            b.ShapeControlMethod = kinds.ShapeControlMethods.TangentMagnitude
            magnitude = b.TangentMagnitude
            if start_magnitude is not None:
                expr.drive(magnitude.StartTangentMagnitude, float(start_magnitude))
            if end_magnitude is not None:
                expr.drive(magnitude.EndTangentMagnitude, float(end_magnitude))
        feature = b.commit()
    bridge = _only(tx, feature)
    with kcurve.evaluator(tx, bridge) as e:
        (s, ts), (q, tq) = e.at(0.0), e.at(1.0)
    if geom.dist(s, p1) + geom.dist(q, p0) < geom.dist(s, p0) + geom.dist(q, p1):
        (s, ts), (q, tq) = (q, geom.scale(tq, -1.0)), (s, geom.scale(ts, -1.0))
    limit = cc.gap_tolerance(tx, p0, p1)
    gaps = geom.dist(s, p0), geom.dist(q, p1)
    angles = _angle(ts, out0), _angle(tq, geom.scale(out1, -1.0))
    for i, side in enumerate(("start", "end")):
        readback.expect(gaps[i] <= limit, f"{side}_gap", f"The bridge misses the {side} curve",
                        gap=gaps[i])  # fmt: skip
        if (start_continuity, end_continuity)[i] != "G0":
            readback.expect(angles[i] <= ANGLE_TOL, f"{side}_angle",
                            f"The bridge is not tangent at the {side}", angle=angles[i])  # fmt: skip
    return {
        "feature": tx.ref(feature, kind="feature"),
        "curve": tx.ref(bridge, kind="curve"),
        "length": measure.length(tx, bridge),
        "start": cc.clean(s),
        "end": cc.clean(q),
        "start_gap": gaps[0],
        "end_gap": gaps[1],
        "start_angle": angles[0],
        "end_angle": angles[1],
        "start_magnitude": 1.0 if start_magnitude is None else float(start_magnitude),
        "end_magnitude": 1.0 if end_magnitude is None else float(end_magnitude),
    }


# ---- join -----------------------------------------------------------------------------------
@tool(
    effect="model",
    label="Create joined curve",
    summary="Join {curves}",
    keywords=("join curves", "merge curves", "single spline", "combine curves", "chain to spline"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"curves": ["c:7", "c:8", "c:9"]},
)
def nx_create_joined_curve(
    tx: Tx,
    *,
    curves: Annotated[list[StringRef], Items(2, 200), Doc("Curves or edges chained end to end")],
    output: Annotated[
        Literal["general", "cubic", "quintic"], Doc("general: exact shape; cubic/quintic: approx")
    ] = "general",
) -> JoinData:
    """Join curves or edges that chain end to end into one associative curve (Join Curves);
    the inputs stay. Gaps over 0.001 mm or branches are refused. general keeps the exact
    shape and length; cubic and quintic approximate it with one degree 3 or 5 spline."""
    with tx.checks():
        objs = cc.strings(tx, curves, "/curves")
        limit = units.part_tolerance(tx) + geom.tol(*(kcurve.ends(tx, objs[0])))
        problem = cc.chain_problem(tx, objs, limit)
        if problem is not None:
            index, kind, details = problem
            fail(
                "NX_WRONG_GEOMETRY",
                f"The curves branch at {details['branch_at']}" if kind == "branch" else
                "The curves do not chain end to end" + (f": a {details['gap']:.4g} gap"
                                                         if details.get("gap") else ""),
                field=f"/curves/{index}",
                hint="Join one chain without branches; close a gap with nx_create_bridge_curve"
                if kind == "gap" else "Join one chain without branches: leave out a branch",
                details=details,
            )  # fmt: skip
    source = measure.length(tx, objs)
    try:
        with (
            cc.licensed(tx),
            builders.build(tx, tx.part.Features.CreateJoinCurvesBuilder, None) as b,
        ):
            tol = units.part_tolerance(tx)
            b.DistanceTolerance = tol
            b.AngleTolerance = 0.5
            b.OutputCurveType = getattr(
                tx.nx.Features.JoinCurvesBuilder.OutputCurve, JOIN_TYPES[output]
            )
            builders.curve_section(tx, objs, into=b.Section)
            feature = b.commit()
    except NXError as error:
        if error.code == "NX_BUILD_FAILED":
            error.hint = GAP_HINT
        raise
    joined = _only(tx, feature)
    data = cc.describe(tx, feature, [joined])
    limit = cc.gap_tolerance(tx) if output == "general" else 0.01 * source
    readback.expect(abs(data["length"] - source) <= limit, "length",
                    "The joined length differs from the inputs'", length=data["length"],
                    source_length=source)  # fmt: skip
    return {**data, "source_length": source}


# ---- mirror ---------------------------------------------------------------------------------
@tool(
    effect="model",
    label="Create mirror curve",
    summary="Mirror {curves}",
    keywords=(
        "mirror curve",
        "symmetric curve",
        "reflect curves",
        "reflect wire across plane",
        "mirror edges",
        "symmetric copy",
    ),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"curves": ["c:7"], "plane": "yz"},
)
def nx_create_mirror_curve(
    tx: Tx,
    *,
    curves: Annotated[list[StringRef], Items(1, 200), Doc("Curves or edges to mirror")],
    plane: Plane,
) -> MirrorData:
    """Create associative mirror copies of curves or edges about a plane. The plane is 'yz',
    a datum plane, a planar face or {origin, normal}, copied when the call runs (a later
    datum move does not follow). The inputs stay. Curves lying in the plane are skipped
    (their mirror is themselves; listed in skipped). Bodies and features: nx_create_mirror."""
    with tx.checks():
        objs = cc.strings(tx, curves, "/curves")
        _obj, origin, normal = select.plane(tx, plane, "/plane")
        limit = cc.gap_tolerance(tx, origin)
        flat = [i for i, c in enumerate(objs) if cc.plane_gap(tx, [c], origin, normal) <= limit]
        if len(flat) == len(objs):
            fail("NX_INVALID_ARGUMENT", "Every curve lies in the mirror plane: its mirror would "
                 "coincide with it", field="/curves",
                 hint="Pick a plane the curves are not in")  # fmt: skip
    skipped = [curves[i] for i in flat]
    objs = [c for i, c in enumerate(objs) if i not in flat]
    if skipped:
        tx.warn("NX_NOTE", f"Skipped {', '.join(skipped)}: in the mirror plane, the mirror "
                "would duplicate the curve")  # fmt: skip
    with (
        cc.licensed(tx),
        builders.build(tx, tx.part.Features.CreateMirrorCurveBuilder, None) as b,
    ):
        builders.curve_section(tx, objs, into=b.Curve)
        b.PlaneOption = tx.nx.Features.MirrorCurveBuilder.PlaneOptions.NewPlane
        b.NewPlane = cc.plane_object(tx, origin, normal)
        feature = b.commit()
    made = cc.output_curves(tx, feature)
    gap = 0.0
    for c in objs[: cc.MAX_SAMPLED]:
        tx.checkpoint()
        for p in cc.samples(tx, c):
            image = geom.reflect(p, origin, normal)
            gap = max(gap, min(kcurve.point_distance(tx, m, image)[0] for m in made))
    readback.expect(gap <= cc.gap_tolerance(tx), "symmetry", "A mirror is not symmetric",
                    gap=gap)  # fmt: skip
    return {**cc.describe(tx, feature, made), "symmetry_gap": gap, "skipped": skipped}


# ---- trim / divide -------------------------------------------------------------------------
def _table(tx, obj, n=SAMPLES):
    """``(params, points, cumulative lengths)`` of ``n`` + 1 samples of a curve."""
    with kcurve.evaluator(tx, obj) as e:
        ts = [i / n for i in range(n + 1)]
        pts = [e.at(t)[0] for t in ts]
    lengths = [0.0]
    for a, b in zip(pts, pts[1:], strict=False):
        lengths.append(lengths[-1] + geom.dist(a, b))
    return ts, pts, lengths


def _crossings(tx, obj, table, origin, normal) -> list[list[float]]:
    """Points where the curve crosses a plane (sign changes refined by bisection)."""
    ts, pts, _l = table
    side = [geom.dot(geom.sub(p, origin), normal) for p in pts]
    found = []
    with kcurve.evaluator(tx, obj) as e:
        for i in range(len(ts) - 1):
            if side[i] == 0.0:
                found.append(pts[i])
            elif side[i] * side[i + 1] < 0:
                low, high, s_low = ts[i], ts[i + 1], side[i]
                for _ in range(50):
                    mid = (low + high) / 2
                    s_mid = geom.dot(geom.sub(e.at(mid)[0], origin), normal)
                    if s_mid * s_low > 0:
                        low, s_low = mid, s_mid
                    else:
                        high = mid
                found.append(e.at((low + high) / 2)[0])
    if side[-1] == 0.0:
        found.append(pts[-1])
    return found


def _interior(tx, obj, points, field, closed) -> list[list[float]]:
    """``points`` that lie inside the curve (an open curve's ends cannot be cut)."""
    ends = kcurve.ends(tx, obj)
    limit = cc.gap_tolerance(tx, *ends)
    if not closed and any(geom.dist(p, q) <= limit for p in points for q in ends):
        fail("NX_INVALID_ARGUMENT", "A cut lies on an end of the curve", field=field)
    return points


def _cuts(tx, obj, table, closed, planes, at, segments):
    """``(cut planes [(origin, normal)], locations)`` of a trim or divide; equal segments of
    a closed curve also cut at its seam (NX does not break a closed curve there)."""
    if planes is not None:
        cut = []
        for i, value in enumerate(planes):
            _o, origin, normal = select.plane(tx, value, f"/planes/{i}")
            cut.append((origin, normal))
        locations = []
        for i, (origin, normal) in enumerate(cut):
            hits = _crossings(tx, obj, table, origin, normal)
            if not hits:
                fail("NX_INVALID_ARGUMENT", f"Plane {i} does not cross the curve (a curve is "
                     "not extended to a plane)", field=f"/planes/{i}")  # fmt: skip
            locations.extend(h for h in hits if not any(geom.dist(h, q) < 1e-9 for q in locations))
        return cut, locations
    with kcurve.evaluator(tx, obj) as e:
        if at is not None:
            feet = [e.closest(list(p)) for p in at]
            for i, (p, (foot, _d)) in enumerate(zip(at, feet, strict=True)):
                off = geom.dist(list(p), foot)
                if off > cc.gap_tolerance(tx, foot):
                    fail(
                        "NX_INVALID_ARGUMENT",
                        f"at[{i}] lies {off:.4g} off the curve",
                        field=f"/at/{i}",
                        hint="Give points on the curve; the nearest curve point is in "
                        "details.nearest",
                        details={"distance": off, "nearest": cc.clean(foot)},
                    )
        else:
            ts, _pts, lengths = table
            total = lengths[-1]
            feet = []
            for k in range(0 if closed else 1, int(segments)):
                goal = total * k / int(segments)
                i = next(j for j in range(1, len(lengths)) if lengths[j] >= goal)
                span = lengths[i] - lengths[i - 1] or 1.0
                t = ts[i - 1] + (goal - lengths[i - 1]) / span * (ts[i] - ts[i - 1])
                feet.append(e.at(t))
    return [(p, geom.unit(d)) for p, d in feet], [p for p, _d in feet]


@tool(
    effect="model",
    label="Create trim curve",
    summary={"trim": "Trim {curve} at {planes}", "divide": "Divide {curve}"},
    keywords=("trim curve", "divide curve", "split curve", "cut curve at plane", "equal segments"),
    requires=("work_part", "no_active_sketch"),
    variants="method",
    required_by={"trim": ("planes", "keep")},
    cost="heavy",
    previewable=True,
    example={"curve": "c:7", "method": "trim", "planes": ["yz"], "keep": [-20, 0, 0]},
)
def nx_create_trim_curve(
    tx: Tx,
    *,
    curve: StringRef,
    method: Literal["trim", "divide"] = "trim",
    planes: Annotated[
        list[Plane], Items(1, 20), Doc("Planes crossing the curve: trim bounds or cuts")
    ]
    | None = None,
    keep: Annotated[Point3, Doc("trim: a point on or near the piece to keep")] | None = None,
    at: Annotated[list[Point3], Items(1, 100), Doc("divide: points on the curve to cut at")]
    | None = None,
    segments: Annotated[int, Range(ge=2, le=100), Doc("divide: equal-length pieces")] | None = None,
) -> TrimData:
    """Trim a curve or edge to bounding planes, or divide it into pieces, as an associative
    feature; the input stays. trim: planes plus keep, a point picking the piece kept. divide:
    one of planes (cut where each crosses), at (points on the curve) or segments (equal
    lengths); curves lists the pieces from its start. Planes must cross the curve (no
    extension): give planes ('yz', datum plane, planar face, {origin, normal})."""
    if method == "trim":
        args.need(planes=planes, keep=keep)
        for name, value in (("at", at), ("segments", segments)):
            if value is not None:
                fail("NX_INVALID_ARGUMENT", f"{name} applies to divide only", field=f"/{name}")
    else:
        args.exactly_one(planes=planes, at=at, segments=segments)
        if keep is not None:
            fail("NX_INVALID_ARGUMENT", "keep applies to trim only", field="/keep")
    with tx.checks():
        obj = select.one(tx, curve, "curve", "edge", field="/curve")
        table = _table(tx, obj)
        ends = (table[1][0], table[1][-1])
        closed = geom.dist(*ends) <= cc.gap_tolerance(tx, *ends)
        cut, locations = _cuts(tx, obj, table, closed, planes, at, segments)
        field = "/planes" if planes is not None else ("/at" if at is not None else "/segments")
        _interior(tx, obj, locations, field, closed)
    source = measure.length(tx, obj)
    kinds = tx.nx.Features.TrimCurve2Builder
    method_kinds = tx.nx.GeometricUtilities.TrimCurveBoundingObjectBuilder.Method
    with (
        cc.licensed(tx),
        builders.build(tx, tx.part.Features.CreateTrimCurve2FeatureBuilder, None) as b,
    ):
        builders.curve_section(tx, [obj], into=b.CurveToTrim)
        for origin, normal in cut:
            bounding = b.CreateTrimCurveBoundingObjectBuilder()
            b.BoundingObjectList.Append(bounding)  # at once: see the module notes
            bounding.BoundingObjectMethodType = method_kinds.SelectPlane
            bounding.BoundingPlane = cc.plane_object(tx, origin, normal)
        if method == "divide":
            b.OperationOption = kinds.Operation.Divide
        b.UpdateTrimRegionsAndDivideLocations()
        if method == "trim":
            b.KeepOrDiscard = kinds.KeepDiscard.Keep
            b.SelectTrimRegion(tx.nx.Point3d(*_foot(tx, obj, keep)))
        else:
            for p in locations:
                b.SelectDivideLocation(tx.nx.Point3d(*(float(v) for v in p)))
        feature = b.commit()
    # NX lists the pieces in no stable order: sort them along the source from its start.
    made = kcurve.along(tx, obj, cc.output_curves(tx, feature))
    data = cc.describe(tx, feature, made)
    limit = cc.gap_tolerance(tx)
    if method == "trim":
        foot = _foot(tx, obj, keep)
        near = min(kcurve.point_distance(tx, m, foot)[0] for m in made)
        readback.expect(near <= limit, "keep", "The kept piece is not the one at keep", gap=near)
        readback.expect(data["length"] < source - limit, "length", "Nothing was trimmed",
                        length=data["length"])  # fmt: skip
    else:
        pieces = len(locations) + (0 if closed else 1)
        readback.expect(data["count"] == pieces, "count", f"NX made {data['count']} pieces",
                        expected=pieces)  # fmt: skip
        readback.expect(abs(data["length"] - source) <= limit * pieces, "length",
                        "The pieces do not add up to the curve", length=data["length"])  # fmt: skip
        if segments is not None:
            goal = source / int(segments)
            worst = max(abs(r["length"] - goal) for r in data["curves"])
            readback.expect(worst <= max(limit, 1e-4 * goal), "segments",
                            "The pieces are not equal", worst=worst)  # fmt: skip
    return {**data, "source_length": source, "locations": [cc.clean(p) for p in locations]}


def _foot(tx, obj, point):
    return [float(v) for v in kcurve.closest(tx, obj, list(point))[0]]
