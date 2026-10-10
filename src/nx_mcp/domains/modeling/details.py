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
from nx_mcp.core.types import (
    Angle,
    BodyRef,
    Direction,
    Doc,
    EdgeRef,
    FaceRef,
    Items,
    Length,
    Range,
    forbid_extra,
)
from nx_mcp.domains.modeling import common
from nx_mcp.kit import args, builders, expr, measure, select, units

EdgeList = Annotated[list[EdgeRef], Items(1, 200)]
FaceList = Annotated[list[FaceRef], Items(1, 200)]


def _edges(tx, refs):
    edges = select.edges(tx, refs, "/edges")
    return edges, common.one_body(edges, "/edges")


class RadiusPoint(TypedDict):
    at: Annotated[float, Range(ge=0, le=1), Doc("Fraction of arc length from the edge start")]
    radius: Length


forbid_extra(RadiusPoint)


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
    edge. Edge refs expire after each feature: re-find them."""
    args.exactly_one(radius=radius, variable=variable)
    points = []
    if radius is not None and float(radius) <= 0:
        fail("NX_INVALID_ARGUMENT", "radius must be positive", field="/radius",
             hint="Send e.g. 'radius': '2 mm'")  # fmt: skip
    if variable is not None:
        if len(edges) != 1:
            fail("NX_INVALID_ARGUMENT", "A variable blend takes one edge", field="/edges",
                 hint="Blend one edge per call, or use radius for a constant blend")  # fmt: skip
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
        b.AddChainset(collector, expr.rhs(radius if radius is not None else points[0][1]))
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


ChamferMethod = Literal["symmetric", "two_offsets", "offset_angle"]


def chamfer_method(method, offset2, angle) -> ChamferMethod:
    """The chamfer method: ``method`` as given, or inferred from the fields when omitted
    (``angle`` -> offset_angle, ``offset2`` -> two_offsets, else symmetric)."""
    if method is not None:
        return method
    if offset2 is not None and angle is not None:
        fail(
            "NX_INVALID_ARGUMENT",
            "Give offset2 (two_offsets) or angle (offset_angle), not both",
            field="/angle",
            hint="Drop one of them, or set method",
        )
    if angle is not None:
        return "offset_angle"
    return "two_offsets" if offset2 is not None else "symmetric"


@tool(
    effect="model",
    label="Chamfer edges",
    summary=lambda a, say: f"Chamfer {say('edges')} {say('offset')}",
    keywords=("chamfer", "bevel", "edge break", "offset angle chamfer", "45 degree"),
    requires=("work_part", "no_active_sketch"),
    variants="method",
    cost="heavy",
    previewable=True,
    example={"edges": ["e:21"], "offset": "1 mm"},
)
def nx_create_chamfer(
    tx: Tx,
    *,
    edges: EdgeList,
    method: Annotated[ChamferMethod, Doc("Omit: inferred from offset2 or angle")] | None = None,
    offset: Length,
    offset2: Length | None = None,
    angle: Angle | None = None,
    reverse: Annotated[bool, Doc("Swap which face takes offset (asymmetric methods)")] = False,
) -> common.FeatureData:
    """Chamfer edges of one body by a symmetric offset, two offsets (offset2), or an offset
    and angle (0..90 deg). NX picks the first face from the edge direction; reverse swaps the
    faces."""
    if float(offset) <= 0:
        fail("NX_INVALID_ARGUMENT", "offset must be positive", field="/offset")
    chosen = chamfer_method(method, offset2, angle)
    second = angle_value = 0.0
    if chosen == "symmetric":
        extra = "offset2" if offset2 is not None else "angle" if angle is not None else None
        if extra:
            fail(
                "NX_INVALID_ARGUMENT",
                f"symmetric takes only offset, not {extra}",
                field=f"/{extra}",
                hint="Use method 'two_offsets' with offset2, 'offset_angle' with angle, "
                "or omit method",
            )
        if reverse:
            fail(
                "NX_INVALID_ARGUMENT",
                "reverse applies to two_offsets and offset_angle only",
                field="/reverse",
                hint="Omit reverse for a symmetric chamfer",
            )
    elif chosen == "two_offsets":
        if offset2 is None or angle is not None or float(offset2) <= 0:
            fail(
                "NX_INVALID_ARGUMENT",
                "two_offsets takes a positive offset2 and no angle",
                field="/angle" if angle is not None else "/offset2",
                hint="Send e.g. {'offset': 2, 'offset2': 3}",
            )
        second = expr.number(offset2)
    else:
        if angle is None or offset2 is not None or not 0 < float(angle) < 90:
            fail(
                "NX_INVALID_ARGUMENT",
                "offset_angle takes an angle strictly between 0 and 90 degrees and no offset2",
                field="/offset2" if offset2 is not None else "/angle",
                hint="Send e.g. {'offset': 2, 'angle': 30}",
            )
        angle_value = expr.number(angle)
    selected, body = _edges(tx, edges)
    before = measure.volume(tx, body)
    options = tx.nx.Features.ChamferBuilder.ChamferOption
    with builders.build(tx, tx.part.Features.CreateChamferBuilder, None) as b:
        b.Tolerance = units.part_tolerance(tx)
        b.SmartCollector = builders.collector(tx, selected, "Edge")
        if chosen == "symmetric":
            b.Option = options.SymmetricOffsets
        elif chosen == "two_offsets":
            b.Option = options.TwoOffsets
            expr.drive(b.SecondOffsetExp, second)
        else:
            b.Option = options.OffsetAndAngle
            expr.drive(b.AngleExp, angle_value)
        expr.drive(b.FirstOffsetExp, offset)  # an expression value keeps the chamfer driven
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
    body: BodyRef,
    thickness: Length,
    remove_faces: Annotated[FaceList, Doc("Faces to open; omit for a closed hollow")] | None = None,
    outward: Annotated[bool, Doc("Grow the wall outside the original faces")] = False,
) -> common.FeatureData:
    """Hollow a solid body to a wall thickness, opening the removed faces. To drive thickness
    by an expression, edit it afterwards with nx_edit_feature."""
    if float(thickness) <= 0:
        fail("NX_INVALID_ARGUMENT", "thickness must be positive", field="/thickness",
             hint="Send e.g. 'thickness': '2 mm'; outward grows the wall outside")  # fmt: skip
    target = common.solid(tx, body)
    faces = select.faces(tx, remove_faces or [], "/remove_faces")
    for i, f in enumerate(faces):
        if int(f.GetBody().Tag) != int(target.Tag):
            fail("NX_WRONG_OWNER", "Removed faces must belong to body", field=f"/remove_faces/{i}",
                 hint="Find the faces with nx_find_geometry owner=<body>")  # fmt: skip
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
    stationary_face: FaceRef,
    direction: common.PrincipalName | Direction,
    angle: Angle,
) -> common.FeatureData:
    """Draft faces about a stationary face; a positive angle tapers them inward along the draw
    direction. A negative angle tapers outward."""
    if not 0 < abs(float(angle)) < 89:
        fail(
            "NX_INVALID_ARGUMENT",
            "The draft angle must be non-zero and within -89..89 degrees",
            field="/angle",
            hint="Send e.g. 'angle': 5 or '3 deg'; negative tapers outward",
        )
    selected = select.faces(tx, faces, "/faces")
    fixed = select.one(tx, stationary_face, "face", field="/stationary_face")
    body = fixed.GetBody()
    for i, f in enumerate(selected):
        if int(f.Tag) == int(fixed.Tag):
            fail("NX_INVALID_ARGUMENT", "Exclude the stationary face", field=f"/faces/{i}",
                 hint="faces are the walls to taper; stationary_face stays put")  # fmt: skip
        if int(f.GetBody().Tag) != int(body.Tag):
            fail(
                "NX_WRONG_OWNER", "Faces must share the stationary face's body", field=f"/faces/{i}"
            )
    vector = select.direction(tx, common.principal(direction), "/direction")
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
