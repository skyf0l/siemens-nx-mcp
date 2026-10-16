"""Projected curves (onto faces or a plane) and combined projections (a 3D curve from two
curves seen in two directions: sheer and chine lines).

NX 2406 lessons kept here:

- ``FeatureCollection.CreateProjectCurveBuilder`` (Modeling's Project Curve, not the
  Draw Shapes one): ``FaceToProjectTo`` is a selection list that refuses faces, bodies
  and datum planes added directly ("Invalid faces or planes to project to", whatever the
  direction or work part); a face collector added to it works. ``PlaneToProjectTo`` takes
  a smart plane. ``AlongFaceNormal`` onto a plane projects along its normal.
- ``CreateCombinedProjectionBuilder``: ``Curve1``/``Curve2`` are sections,
  ``Direction1``/``Direction2`` ``ProjectionOptions`` (``ProjectDirectionMethod``
  ``CrvPlaneNormal`` or ``Vector`` with ``ProjectVector``). ``CrvPlaneNormal`` fails for a
  straight line ("Unable to project. Check first projection direction"): the tool asks for
  the direction before NX. A line (0,0,0)-(100,50,0) seen along z and a line
  (0,-10,0)-(100,-10,30) seen along y combine to (0,0,0)-(100,50,30), length 115.75837.
"""

from __future__ import annotations

from typing import Annotated, Literal

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Direction, Doc, Items, Plane, RefTo
from nx_mcp.domains.freeform import curves_common as cc
from nx_mcp.kit import args, builders, geom, readback, result, select, units

StringRef = Annotated[str, RefTo("curve", "edge", doc="Curve or edge 'c:7'/'e:21'")]
Strings = Annotated[list[StringRef], Items(1, 200)]
SolidRef = Annotated[str, RefTo("body", "face", doc="Body or face 'b:3'/'fc:7'")]


class ProjectedData(cc.CurvesData):
    gap: float  # largest sampled distance of a curve from the target faces or plane
    ray_gap: float | None  # largest distance of a projection ray from the source curves
    far_curves: list[result.Row]  # vector: curves behind a nearer hit (a closed face's far side)


MAX_STACKED = 50  # output curves compared for far-side hits


def _far_side(tx, made, direction) -> list:
    """Output curves lying behind another output curve along ``direction`` (a vector
    projection onto a closed face also hits its far side)."""
    limit = cc.gap_tolerance(tx)
    far: list = []
    shown = made[:MAX_STACKED]
    for a in shown:
        tx.checkpoint()
        points = cc.samples(tx, a, 5)[1:-1]
        for b in shown:
            if b is a or any(b is f for f in far):
                continue
            middle = cc.samples(tx, b, 3)[1]
            for p in points:
                if cc.line_gap(tx, b, p, direction) <= limit:
                    if geom.dot(geom.sub(middle, p), direction) > limit:
                        far.append(b)
                    break
    return far


def _ray_gap(tx, curves, sources, direction) -> float:
    """Largest distance, over sample points p of ``curves``, of the line through p along
    ``direction`` from the nearest source curve (0: every point is a projection)."""
    worst = 0.0
    for c in curves[: cc.MAX_SAMPLED]:
        tx.checkpoint()
        for p in cc.samples(tx, c):
            worst = max(worst, min(cc.line_gap(tx, s, p, direction) for s in sources))
    return worst


@tool(
    effect="model",
    label="Create projected curve",
    summary={
        "face_normal": "Project {curves} along the face normals",
        "vector": "Project {curves} along a direction",
    },
    keywords=("project curve", "curve on surface", "wrap curve", "project onto face", "drape"),
    requires=("work_part", "no_active_sketch"),
    variants="along",
    required_by={"vector": ("direction",)},
    cost="heavy",
    previewable=True,
    example={"curves": ["c:4"], "faces": ["fc:9"], "along": "vector", "direction": "-z"},
)
def nx_create_projected_curve(
    tx: Tx,
    *,
    curves: Annotated[Strings, Doc("Curves or edges to project")],
    faces: Annotated[list[SolidRef], Items(1, 200), Doc("Target faces or bodies; or plane")]
    | None = None,
    plane: Annotated[Plane, Doc("Target plane instead of faces")] | None = None,
    along: Literal["face_normal", "vector"] = "face_normal",
    direction: Annotated[Direction, Doc("vector: projection direction")] | None = None,
    both_sides: Annotated[bool, Doc("vector: also project against direction")] = False,
) -> ProjectedData:
    """Project curves or edges onto faces or a plane as an associative curve feature. along:
    face_normal (onto a plane: its normal) or vector (direction; every face hit is projected,
    a closed face's far side too: far_curves). Returns each curve (length, ends), gap to the
    target and ray_gap (along a direction). Into a sketch instead:
    nx_create_sketch_projection."""
    args.exactly_one(faces=faces, plane=plane)
    if along == "vector":
        args.need(direction=direction)
    with tx.checks():
        sources = cc.strings(tx, curves, "/curves")
        if faces is not None:
            _objs, targets = cc.solids(tx, faces, "/faces")
            cut = None
        else:
            targets = []
            _o, origin, normal = select.plane(tx, plane, "/plane")
            cut = (origin, normal)
        ray = select.direction(tx, direction, "/direction") if along == "vector" else None
    F = tx.nx.Features.ProjectCurveBuilder
    tol = units.part_tolerance(tx)
    with (
        cc.licensed(tx),
        builders.build(tx, tx.part.Features.CreateProjectCurveBuilder, None) as b,
    ):
        b.Tolerance = tol
        b.CurveFitData.Tolerance = tol
        builders.curve_section(tx, sources, into=b.SectionToProject)
        if cut is None:
            # Faces added directly are refused (module notes): a collector is accepted.
            b.FaceToProjectTo.Add(builders.collector(tx, targets, "Face"))
        else:
            b.PlaneToProjectTo = cc.plane_object(tx, *cut)
        if ray is None:
            b.ProjectionDirectionMethod = F.DirectionType.AlongFaceNormal
        else:
            b.ProjectionDirectionMethod = F.DirectionType.AlongVector
            b.ProjectionVector = builders.direction(tx, ray)
            if both_sides:
                b.ProjectionOption = F.ProjectionOptionType.ProjectBothSides
        feature = b.commit()
    made = cc.output_curves(tx, feature)
    limit = cc.gap_tolerance(tx)
    gap = cc.worst_gap(tx, made, targets) if cut is None else cc.plane_gap(tx, made, *cut)
    readback.expect(gap <= limit, "on_target", "A projected curve is off the target", gap=gap)
    rays = ray if ray is not None else (cut[1] if cut is not None else None)
    ray_gap = None
    if rays is not None:
        ray_gap = _ray_gap(tx, made, sources, rays)
        readback.expect(ray_gap <= limit, "direction", "A point is not a projection along the "
                        "direction", gap=ray_gap)  # fmt: skip
    far = _far_side(tx, made, ray) if ray is not None and not both_sides else []
    if far:
        tx.warn("NX_UPDATE_WARNING", f"{len(far)} projected curve(s) lie behind a nearer hit "
                "along the direction (the far side of a closed face): see far_curves")  # fmt: skip
    return {
        **cc.describe(tx, feature, made),
        "gap": gap,
        "ray_gap": ray_gap,
        "far_curves": [tx.ref(c, kind="curve") for c in far],
    }


# ---------------------------------------------------------------------------
# Combined projection


class CombinedData(cc.CurvesData):
    first_direction: list[float]
    second_direction: list[float]
    gap: float  # largest distance of a sight line through the result from its source


def _view(tx, value, objs, field):
    """The unit view direction: the given one, else the normal of the curves' plane."""
    if value is None:
        return cc.chain_normal(tx, objs, field)
    return select.direction(tx, value, field)


def _options(tx, options, given, unit):
    """Configure one ``ProjectionOptions``: the curve plane's normal or a vector."""
    T = tx.nx.GeometricUtilities.ProjectionOptions.DirectionType
    if given is None:
        options.ProjectDirectionMethod = T.CrvPlaneNormal
        return
    options.ProjectDirectionMethod = T.Vector
    options.ProjectVector = builders.direction(tx, unit)


@tool(
    effect="model",
    label="Create combined projection",
    summary="Combine {first} and {second} into a 3D curve",
    keywords=("combined projection", "3d curve from two views", "sheer line", "chine line"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"first": ["c:3"], "second": ["c:5"], "first_direction": "z", "second_direction": "y"},
)
def nx_create_combined_projection(
    tx: Tx,
    *,
    first: Annotated[Strings, Doc("Chained curves/edges seen in the first view")],
    second: Annotated[Strings, Doc("Chained curves/edges seen in the second view")],
    first_direction: Annotated[Direction, Doc("First view direction; default its plane normal")]
    | None = None,
    second_direction: Annotated[Direction, Doc("Second view direction; default its plane normal")]
    | None = None,
) -> CombinedData:
    """Create the 3D curve whose views along two directions are the two given curves (e.g. a
    sheer line from its side and top views). An omitted direction is the normal of that
    curve's plane (a straight line needs one)."""
    with tx.checks():
        one = cc.strings(tx, first, "/first")
        two = cc.strings(tx, second, "/second")
        if {int(o.Tag) for o in one} & {int(o.Tag) for o in two}:
            fail("NX_INVALID_ARGUMENT", "first and second share a curve", field="/second")
        d1 = _view(tx, first_direction, one, "/first_direction")
        d2 = _view(tx, second_direction, two, "/second_direction")
    tol = units.part_tolerance(tx)
    factory = tx.part.Features.CreateCombinedProjectionBuilder
    with (
        cc.licensed(tx),
        builders.build(tx, factory, None) as b,
    ):
        b.CurveFitData.Tolerance = tol
        builders.curve_section(tx, one, into=b.Curve1)
        builders.curve_section(tx, two, into=b.Curve2)
        _options(tx, b.Direction1, first_direction, d1)
        _options(tx, b.Direction2, second_direction, d2)
        feature = b.commit()
    made = cc.output_curves(tx, feature)
    gap = max(_ray_gap(tx, made, one, d1), _ray_gap(tx, made, two, d2))
    readback.expect(gap <= cc.gap_tolerance(tx), "views", "The curve does not match a view",
                    gap=gap)  # fmt: skip
    return {
        **cc.describe(tx, feature, made),
        "first_direction": cc.clean(d1),
        "second_direction": cc.clean(d2),
        "gap": gap,
    }
