"""Detail features: blend, chamfer, shell, draft (tool-surface 3.6).

NX 2406 lessons kept here: a variable blend adds its radius points with
``AddVariableRadiusDataNew`` at points placed by percent arc length (on a
closed edge 0 and 1 are the same point: NX 670075); ``ChamferBuilder`` takes
``SymmetricOffsets``/``TwoOffsets``/``OffsetAndAngle`` with ``ReverseOffsets``
swapping the faces; builders get a 0.001 mm distance tolerance; a draft needs
its faces on the stationary face's body.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Angle, Direction, Doc, Items, Length, Range, RefTo
from nx_mcp.domains.modeling import common
from nx_mcp.kit import args, builders, measure, select, units

EdgeList = Annotated[list[RefTo("edge")], Items(1, 200)]  # type: ignore[valid-type]
FaceList = Annotated[list[RefTo("face")], Items(1, 200)]  # type: ignore[valid-type]


def _edges(tx, refs):
    edges = select.edges(tx, refs, "/edges")
    return edges, common.one_body(edges, "/edges")


class RadiusPoint(TypedDict):
    at: Annotated[float, Range(ge=0, le=1), Doc("Fraction of arc length from the edge start")]
    radius: Length


RadiusPoint.__pydantic_config__ = {"extra": "forbid"}  # type: ignore[attr-defined]


@tool(
    effect="model",
    label="Blend edges",
    summary=lambda a, say: (
        f"Blend {say('edges')} "
        + (f"R{say('radius')}" if a.get("radius") is not None else "variable")
    ),
    keywords=("fillet", "round", "edge blend", "variable radius", "rounding"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"edges": ["e:21"], "radius": "2 mm"},
)
def nx_create_blend(
    tx: Tx,
    *,
    edges: EdgeList,
    radius: Length | None = None,
    variable: Annotated[list[RadiusPoint], Items(2, 20)] | None = None,
) -> common.FeatureData:
    """Blend (fillet) edges of one body: a constant radius, or a radius varying along one
    edge."""
    args.exactly_one(radius=radius, variable=variable)
    points = []
    if radius is not None and radius <= 0:
        fail("NX_INVALID_ARGUMENT", "radius must be positive", field="/radius")
    if variable is not None:
        if len(edges) != 1:
            fail("NX_INVALID_ARGUMENT", "A variable blend takes one edge", field="/edges")
        points = sorted((float(p["at"]), float(p["radius"])) for p in variable)
        if any(r <= 0 for _, r in points):
            fail("NX_INVALID_ARGUMENT", "radii must be positive", field="/variable")
        if any(b[0] - a[0] < 1e-6 for a, b in zip(points, points[1:], strict=False)):
            fail("NX_INVALID_ARGUMENT", "radius points need distinct positions", field="/variable")
    selected, body = _edges(tx, edges)
    before = measure.volume(tx, body)
    part = tx.part
    update = tx.nx.SmartObject.UpdateOption.WithinModeling
    with builders.build(tx, part.Features.CreateEdgeBlendBuilder, None) as b:
        b.Tolerance = units.part_tolerance(tx)
        collector = builders.collector(tx, selected, "Edge")
        b.AddChainset(collector, repr(float(radius if radius is not None else points[0][1])))
        for at, value in points:
            scalar = part.Scalars.CreateScalar(
                at * 100.0, tx.nx.Scalar.DimensionalityType.NotSet, update
            )
            where = part.Points.CreatePoint(
                selected[0],
                scalar,
                tx.nx.PointCollection.PointOnCurveLocationOption.PercentArcLength,
                None,
                update,
            )
            b.AddVariableRadiusDataNew(
                selected[0], repr(at * 100.0), repr(value), where, False, False
            )
        feature = b.commit()
    out = common.result_of(tx, feature, [body])
    common.changed(before, out["volume"], "differs")
    return out


@tool(
    effect="model",
    label="Chamfer edges",
    summary=lambda a, say: f"Chamfer {say('edges')} {say('offset')}",
    keywords=("chamfer", "bevel", "edge break", "offset angle chamfer", "45 degree"),
    requires=("work_part", "no_active_sketch"),
    variants="method",
    cost="heavy",
    previewable=True,
    example={"edges": ["e:21"], "method": "symmetric", "offset": 1},
)
def nx_create_chamfer(
    tx: Tx,
    *,
    edges: EdgeList,
    method: Literal["symmetric", "two_offsets", "offset_angle"],
    offset: Length,
    offset2: Length | None = None,
    angle: Angle | None = None,
    reverse: Annotated[bool, Doc("Swap which face takes offset (asymmetric methods)")] = False,
) -> common.FeatureData:
    """Chamfer edges of one body by a symmetric offset, two offsets, or an offset and angle.
    NX picks the first face from the edge direction; reverse swaps the faces."""
    if offset <= 0:
        fail("NX_INVALID_ARGUMENT", "offset must be positive", field="/offset")
    if method == "symmetric":
        if offset2 is not None or angle is not None or reverse:
            fail("NX_INVALID_ARGUMENT", "symmetric takes only offset", field="/method")
    elif method == "two_offsets":
        args.need(offset2=offset2)
        if angle is not None or offset2 <= 0:
            fail("NX_INVALID_ARGUMENT", "two_offsets takes a positive offset2", field="/offset2")
    else:
        args.need(angle=angle)
        if offset2 is not None or not 0 < angle < 90:
            fail("NX_INVALID_ARGUMENT", "offset_angle takes an angle in 0..90", field="/angle")
    selected, body = _edges(tx, edges)
    before = measure.volume(tx, body)
    options = tx.nx.Features.ChamferBuilder.ChamferOption
    with builders.build(tx, tx.part.Features.CreateChamferBuilder, None) as b:
        b.Tolerance = units.part_tolerance(tx)
        b.SmartCollector = builders.collector(tx, selected, "Edge")
        if method == "symmetric":
            b.Option = options.SymmetricOffsets
        elif method == "two_offsets":
            b.Option = options.TwoOffsets
            b.SecondOffsetExp.RightHandSide = repr(float(offset2))
        else:
            b.Option = options.OffsetAndAngle
            b.AngleExp.RightHandSide = repr(float(angle))
        b.FirstOffsetExp.RightHandSide = repr(float(offset))
        b.ReverseOffsets = bool(reverse)
        feature = b.commit()
    out = common.result_of(tx, feature, [body])
    common.changed(before, out["volume"], "less")
    return out


@tool(
    effect="model",
    label="Shell body",
    summary="Shell {body} {thickness}",
    keywords=("shell", "hollow", "wall thickness", "thin wall", "open box"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"body": "b:3", "thickness": 2, "remove_faces": ["fc:9"]},
)
def nx_create_shell(
    tx: Tx,
    *,
    body: RefTo("body"),  # type: ignore[valid-type]
    thickness: Length,
    remove_faces: Annotated[FaceList, Doc("Faces to open; omit for a closed hollow")] | None = None,
    outward: Annotated[bool, Doc("Grow the wall outside the original faces")] = False,
) -> common.FeatureData:
    """Hollow a solid body to a wall thickness, opening the removed faces."""
    if thickness <= 0:
        fail("NX_INVALID_ARGUMENT", "thickness must be positive", field="/thickness")
    target = common.solid(tx, body)
    faces = select.faces(tx, remove_faces or [], "/remove_faces")
    for i, f in enumerate(faces):
        if int(f.GetBody().Tag) != int(target.Tag):
            fail("NX_WRONG_OWNER", "Removed faces must belong to body", field=f"/remove_faces/{i}")
    before = measure.volume(tx, target)
    with builders.build(tx, tx.part.Features.CreateShellBuilder, None) as b:
        b.Tolerance = units.part_tolerance(tx)
        b.Body = target
        b.DefaultThickness.RightHandSide = repr(float(thickness))
        b.DefaultThicknessFlip = not outward
        if faces:
            b.RemovedFacesCollector = builders.collector(tx, faces, "Face")
        feature = b.commit()
    out = common.result_of(tx, feature, [target])
    common.changed(before, out["volume"], "differs" if outward else "less")
    return out


@tool(
    effect="model",
    label="Draft faces",
    summary="Draft {faces} {angle}",
    keywords=("draft", "taper", "draft angle", "mold draft", "pull direction"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"faces": ["fc:4"], "stationary_face": "fc:2", "direction": [0, 0, 1], "angle": 5},
)
def nx_create_draft(
    tx: Tx,
    *,
    faces: FaceList,
    stationary_face: RefTo("face"),  # type: ignore[valid-type]
    direction: Direction,
    angle: Angle,
) -> common.FeatureData:
    """Draft faces about a stationary face; a positive angle tapers them inward along the draw
    direction."""
    if not 0 < abs(angle) < 89:
        fail("NX_INVALID_ARGUMENT", "The draft angle must be within 0..89 degrees", field="/angle")
    selected = select.faces(tx, faces, "/faces")
    fixed = select.one(tx, stationary_face, "face", field="/stationary_face")
    body = fixed.GetBody()
    for i, f in enumerate(selected):
        if int(f.Tag) == int(fixed.Tag):
            fail("NX_INVALID_ARGUMENT", "Exclude the stationary face", field=f"/faces/{i}")
        if int(f.GetBody().Tag) != int(body.Tag):
            fail(
                "NX_WRONG_OWNER", "Faces must share the stationary face's body", field=f"/faces/{i}"
            )
    vector = select.direction(tx, direction, "/direction")
    before = measure.volume(tx, body)
    part = tx.part
    with builders.build(tx, part.Features.CreateDraftBuilder, None) as b:
        b.AngleTolerance = 0.1
        b.DistanceTolerance = units.part_tolerance(tx)
        b.TypeOfDraft = b.Type.Face
        b.Direction = builders.direction(tx, vector, field="/direction")
        stationary = builders.collector(tx, [fixed], "Face")
        b.StationaryReference.ReplaceRules(stationary.GetRules(), False)
        group = part.CreateExpressionCollectorSet(
            builders.collector(tx, selected, "Face"), repr(float(angle)), "Angle", 0
        )
        b.FaceSetAngleExpressionList.Append(group)
        feature = b.commit()
    out = common.result_of(tx, feature, [body])
    common.changed(before, out["volume"], "differs")
    return out
