"""Offset curves: in the curves' plane by a distance, drafted out of it (height and angle),
or 3D axial (along an axis direction).

NX 2406 lessons kept here (``FeatureCollection.CreateOffsetCurveBuilder``):

- ``CurvesToOffset`` is the builder's own section; ``OffsetDistance``, ``DraftHeight``,
  ``DraftAngle`` and ``Offset3dDistance`` are expressions; ``AxialOffsetDirection`` a
  smart direction. Corners are extended (``TrimMethod`` ``ExtendTangents``, the default).
- ``ComputeOffsetDirection()`` returns NX's in-plane offset direction and its seed point on
  the curves; it ignores ``ReverseDirection``. ``ReverseDirection`` flips the in-plane
  side of distance and draft offsets (not the draft height); a negative draft height flips
  both. The top-face loop of a 100 x 50 block at z 20 offset 5 went inward (90 x 40);
  reversed, outward (110 x 60, corners extended); draft height 10 at 45 deg went to z 10
  and inward 10 (80 x 30): the default height side follows NX's loop orientation. Editing
  the committed feature (negated height, toggled reverse) did not move it, so a trial
  build under ``tx.scratch()`` finds NX's height side and the real build negates the
  height (and toggles the reverse) when it is against ``direction``.
- ``ComputeOffsetDirection`` does not predict the side for every input order: the four
  top edges of a 50 x 30 block, listed x = 250, x = 200, y = 0, y = 30, offset to the side
  opposite the one it reported (listed from a y edge they worked). So the curves are put
  in chain order, and the side is measured: a trial build on NX's side (under
  ``tx.scratch()``) is measured where the curves come nearest ``toward``, and the real
  build reverses when that is the wrong side. ``ComputeOffsetDirection`` still runs first:
  it refuses a straight line before the commit.
- Distance and draft need curves that span a plane: a single straight line is refused
  ("Input curves form straight line, unable to determine offset plane").
- 3D axial moves a line normal to the axis by the signed distance along the axis
  (``ReverseDirection`` has no effect): +5 along z from z 0 gave z 5.
- ``CurveFeatureCollection.CreateOffset3dCurveBuilder`` (Offset 3D Curve) is not used: a
  100 mm line offset 5 with view direction z came out 98 mm long (ends trimmed by 1 mm).
"""

from __future__ import annotations

import math
from typing import Annotated, Literal

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Angle, Direction, Doc, Items, Length, Point3, RefTo
from nx_mcp.domains.freeform import curves_common as cc
from nx_mcp.kit import builders, curve, expr, geom, readback, select, units

StringRef = Annotated[str, RefTo("curve", "edge", doc="Curve or edge 'c:7'/'e:21'")]
TYPES = {"distance": "Distance", "draft": "Draft", "axial_3d": "Axial3d"}
PLANE_HINT = "distance and draft need curves spanning a plane; for one straight line use axial_3d"
# NX 2406: "Input curves form straight line, unable to determine offset plane".
STRAIGHT_LINE = 1395069
STRING_HINT = "distance and draft need one planar string of curves meeting end to end"


class OffsetData(cc.CurvesData):
    expected: float  # distance from the source curves that every offset point should have
    measured_min: float  # nearest source distance over the sampled offset points
    measured_max: float


def _check(method, distance, height, angle, direction):
    if method in ("distance", "axial_3d"):
        if distance is None:
            fail("NX_INVALID_ARGUMENT", f"{method} needs distance", field="/distance")
        if method == "distance" and not float(distance) > 0:
            fail("NX_INVALID_ARGUMENT", "distance must be positive (pick the side with toward)",
                 field="/distance")  # fmt: skip
        if method == "axial_3d" and geom.close(float(distance), 0.0, 1e-12):
            fail("NX_INVALID_ARGUMENT", "distance must not be zero", field="/distance")
        if method == "axial_3d" and direction is None:
            fail("NX_INVALID_ARGUMENT", "axial_3d needs direction (the axis)", field="/direction")
        return abs(float(distance))
    if height is None or angle is None:
        fail("NX_INVALID_ARGUMENT", "draft needs height and angle",
             field="/height" if height is None else "/angle")  # fmt: skip
    if not float(height) > 0:
        fail("NX_INVALID_ARGUMENT", "height must be positive (pick its side with direction)",
             field="/height")  # fmt: skip
    if not -90.0 < float(angle) < 90.0:
        fail("NX_INVALID_ARGUMENT", "angle must be between -90 and 90 degrees", field="/angle")
    return float(height) * math.hypot(1.0, math.tan(math.radians(float(angle))))


def _or_zero(value) -> float:
    """A checked numeric argument as a float (0.0 when absent)."""
    return 0.0 if value is None else float(value)


def _configure(tx, b, method, sources, distance, height, angle, axis):
    b.Type = getattr(tx.nx.Features.OffsetCurveBuilder.Types, TYPES[method])
    tol = units.part_tolerance(tx)
    b.Tolerance = tol
    b.CurveFitData.Tolerance = tol
    builders.curve_section(tx, sources, into=b.CurvesToOffset)
    if method == "distance":
        expr.drive(b.OffsetDistance, distance)
    elif method == "draft":
        expr.drive(b.DraftHeight, height)
        expr.drive(b.DraftAngle, angle)
    else:
        expr.drive(b.Offset3dDistance, distance)
        b.AxialOffsetDirection = builders.direction(tx, axis)


def _side(tx, b):
    """``(unit in-plane offset direction, seed point)`` NX computes for the curves."""
    try:
        with native(default="NX_BUILD_FAILED"):
            vector, seed = b.ComputeOffsetDirection()
    except NXError as error:
        line = error.nx_code == STRAIGHT_LINE or "straight line" in error.message
        error.hint = PLANE_HINT if line else STRING_HINT
        raise
    return geom.unit(geom.xyz(vector)), geom.xyz(seed)


def _nearest(tx, made, point):
    """The point of the offset curves nearest ``point``."""
    return min((curve.point_distance(tx, c, point) for c in made), key=lambda x: x[0])[1]


def _orientations(tx, sources) -> list:
    """Per source: +1 or -1 when its parameter runs with or against the chain through
    ``sources[0]`` (ends matched within the read-back tolerance), None when not chained."""
    ends = [curve.ends(tx, s) for s in sources]
    limit = cc.gap_tolerance(tx, *(p for pair in ends for p in pair))
    flags: list = [None] * len(sources)
    flags[0], todo = 1, [0]
    while todo:
        i = todo.pop()
        tail, head = ends[i] if flags[i] > 0 else ends[i][::-1]
        for j, (start, end) in enumerate(ends):
            if flags[j] is not None:
                continue
            if geom.dist(head, start) <= limit or geom.dist(tail, end) <= limit:
                flags[j] = 1
            elif geom.dist(head, end) <= limit or geom.dist(tail, start) <= limit:
                flags[j] = -1
            else:
                continue
            todo.append(j)
    return flags


def _chained(tx, sources) -> None:
    """Refuse curves that do not form one string end to end (NX: "Input curves must form
    continuous string"), naming the first curve not connected to curves/0."""
    flags = _orientations(tx, sources)
    loose = next((i for i, f in enumerate(flags) if f is None), None)
    if loose is not None:
        fail("NX_INVALID_ARGUMENT", f"curves/{loose} is not connected end to end with the "
             "string through curves/0", field=f"/curves/{loose}",
             hint="List curves that meet end to end (one string); offset separate strings "
             "in separate calls")  # fmt: skip


def _frame(tx, sources, point):
    """``(source index, nearest source point, unit tangent there)`` for ``point``."""
    found = [(curve.point_distance(tx, s, point), i) for i, s in enumerate(sources)]
    (_gap, foot), i = min(found, key=lambda f: f[0][0])
    return i, foot, curve.closest(tx, sources[i], foot)[1]


def _in_chain_order(tx, sources) -> list:
    """``sources`` reordered so that each meets the next, starting with ``sources[0]`` (NX
    2406 offset a rim listed out of chain order to the side opposite the one asked for);
    as given when they do not form one string."""
    ends = [curve.ends(tx, s) for s in sources]
    limit = cc.gap_tolerance(tx, *(p for pair in ends for p in pair))
    order = [0]
    left, right = ends[0]
    free = list(range(1, len(sources)))
    grew = True
    while free and grew:
        grew = False
        for j in list(free):
            start, end = ends[j]
            if geom.dist(right, start) <= limit or geom.dist(right, end) <= limit:
                right = end if geom.dist(right, start) <= limit else start
                order.append(j)
            elif geom.dist(left, end) <= limit or geom.dist(left, start) <= limit:
                left = start if geom.dist(left, end) <= limit else end
                order.insert(0, j)
            else:
                continue
            free.remove(j)
            grew = True
    return list(sources) if free else [sources[i] for i in order]


def _toward(tx, sources, made, toward, normal):
    """``(moved, wanted)``: in the curves' plane, how the offset point nearest the source
    point nearest ``toward`` moved from it, and the way to ``toward``."""
    _i, foot, _t = _frame(tx, sources, toward)
    moved = geom.sub(_nearest(tx, made, foot), foot)
    wanted = geom.sub(list(toward), foot)

    def flat(v):
        return geom.sub(v, geom.scale(normal, geom.dot(v, normal)))

    return flat(moved), flat(wanted)


def _build(tx, method, sources, values, axis, reverse=False, flip=False):
    """``(feature, seed)`` of one offset build (``seed``: NX's point on the curves, None for
    axial_3d); ``reverse`` flips the in-plane side, ``flip`` turns the draft height over (a
    negated height flips both sides, so the in-plane side is reversed back)."""
    distance, height, angle = values
    with builders.build(tx, tx.part.Features.CreateOffsetCurveBuilder, None) as b:
        _configure(tx, b, method, sources, distance, height, angle, axis)
        seed = None
        if method != "axial_3d":
            _vector, seed = _side(tx, b)  # also refuses a straight line before the commit
        if flip:
            b.DraftHeight.RightHandSide = f"-({expr.rhs(height)})"
            reverse = not reverse
        b.ReverseDirection = bool(reverse)
        feature = b.commit()
    return feature, seed


def _height_flipped(tx, method, sources, values, axis) -> bool:
    """Whether NX's draft height goes against ``axis``: a trial build, always undone."""
    with tx.scratch():
        feature, seed = _build(tx, method, sources, values, axis)
        moved = geom.sub(_nearest(tx, cc.output_curves(tx, feature), seed), seed)
    return geom.dot(moved, axis) < 0


def _side_reversed(tx, method, sources, values, axis, toward, flip, normal) -> bool:
    """Whether the offset must be reversed to reach ``toward``: a trial build on NX's own
    side (always undone), measured where the curves come nearest ``toward``. NX's
    ``ComputeOffsetDirection`` did not predict the side of a rim listed out of chain order,
    so the side is measured instead."""
    with tx.scratch():
        feature, _seed = _build(tx, method, sources, values, axis, False, flip)
        moved, wanted = _toward(tx, sources, cc.output_curves(tx, feature), toward, normal)
    return geom.dot(moved, wanted) < 0


def _measured(tx, made, sources):
    distances = []
    for c in made[: cc.MAX_SAMPLED]:
        tx.checkpoint()
        for p in cc.samples(tx, c):
            distances.append(min(curve.point_distance(tx, s, p)[0] for s in sources))
    return min(distances), max(distances)


@tool(
    effect="model",
    label="Create offset curve",
    summary={
        "distance": "Offset {curves} by {distance}",
        "draft": "Draft offset {curves} {height} high",
        "axial_3d": "Offset {curves} {distance} along an axis",
    },
    keywords=("offset curve", "parallel curve", "draft offset", "3d axial offset", "offset edges"),
    requires=("work_part", "no_active_sketch"),
    variants="method",
    required_by={
        "distance": ("distance",),
        "draft": ("height", "angle"),
        "axial_3d": ("distance", "direction"),
    },
    cost="heavy",
    previewable=True,
    example={"curves": ["e:4", "e:5", "e:6", "e:7"], "method": "distance", "distance": 5},
)
def nx_create_offset_curve(
    tx: Tx,
    *,
    curves: Annotated[list[StringRef], Items(1, 200), Doc("Chained curves or edges")],
    method: Literal["distance", "draft", "axial_3d"] = "distance",
    distance: Annotated[Length, Doc("distance: > 0; axial_3d: signed along direction")]
    | None = None,
    height: Annotated[Length, Doc("draft: > 0, out of the curves' plane")] | None = None,
    angle: Annotated[Angle, Doc("draft: from the height direction; 0 = straight")] | None = None,
    direction: Annotated[Direction, Doc("axial_3d: the axis; draft: height side (default NX's)")]
    | None = None,
    toward: Annotated[Point3, Doc("distance/draft: a point on the side to offset to")]
    | None = None,
) -> OffsetData:
    """Create an associative offset curve from chained curves or edges. distance: in their
    plane, toward picks the side (else NX's); draft: height out of the plane (direction picks
    which way) plus height x tan(angle) in it; axial_3d: moved by the signed distance along
    an axis, normal to the tangent. distance/draft need planar curves, not one line; corners
    extend. Faces: nx_create_offset_surface."""
    expected = _check(method, distance, height, angle, direction)
    with tx.checks():
        sources = cc.strings(tx, curves, "/curves")
        _chained(tx, sources)
        axis = select.direction(tx, direction, "/direction") if direction is not None else None
    cc.licensed(tx)
    sources = _in_chain_order(tx, sources)
    values = (distance, height, angle)
    flip = method == "draft" and axis is not None
    flip = flip and _height_flipped(tx, method, sources, values, axis)
    flat = method == "draft" and geom.close(_or_zero(angle), 0.0, 1e-12)  # no in-plane move
    sided = toward is not None and method != "axial_3d" and not flat
    normal = cc.chain_normal(tx, sources, "/curves") if sided else None
    reverse = sided and _side_reversed(tx, method, sources, values, axis, toward, flip, normal)
    feature, seed = _build(tx, method, sources, values, axis, reverse, flip)
    made = cc.output_curves(tx, feature)
    low, high = _measured(tx, made, sources)
    limit = cc.gap_tolerance(tx)
    readback.expect(abs(low - expected) <= limit, "distance", "The offset is not at the distance",
                    expected=expected, measured_min=low)  # fmt: skip
    if sided:
        moved, wanted = _toward(tx, sources, made, toward, normal)
        readback.expect(geom.dot(moved, wanted) > 0, "side", "The offset went to the other side",
                        moved=moved)  # fmt: skip
    if seed is None:  # axial_3d: moved along the axis by the sign of the distance
        seed = cc.samples(tx, sources[0], 3)[1]
    moved = geom.sub(_nearest(tx, made, seed), seed)
    if method == "axial_3d":
        side = geom.scale(axis, 1.0 if _or_zero(distance) > 0 else -1.0)
        readback.expect(geom.dot(moved, side) > 0, "side", "The offset went to the other side",
                        moved=moved)  # fmt: skip
    if method == "draft" and axis is not None:
        readback.expect(geom.dot(moved, axis) > 0, "height", "The draft height went the other "
                        "way", moved=moved)  # fmt: skip
    return {
        **cc.describe(tx, feature, made),
        "expected": expected,
        "measured_min": low,
        "measured_max": high,
    }
