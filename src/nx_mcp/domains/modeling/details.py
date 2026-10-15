"""Detail features: blend, chamfer, shell, draft (tool-surface 3.6).

NX 2406 lessons kept here: a variable blend adds its radius points with
``AddVariableRadiusDataNew`` at points placed by percent arc length (on a
closed edge 0 and 1 are the same point: NX 670075); ``ChamferBuilder`` takes
``SymmetricOffsets``/``TwoOffsets``/``OffsetAndAngle`` with ``ReverseOffsets``
swapping the faces; builders get a 0.001 mm distance tolerance; a draft needs
its faces on the stationary face's body. Both take the edges given (a dumb rule): a chamfer
or blend of part of a tangent-continuous chain (the straight top edges of a plate with
rounded corners, without the arcs) stops in a step, so the left-out chain edges are
reported, or added with ``chain``. A shell's alternate thickness (``FaceThicknesses``,
one expression collector set per zone) grows outward for a positive value whatever
``DefaultThicknessFlip`` says, so an inward shell sends it negated.
"""

from __future__ import annotations

import functools
import math
from typing import Annotated, Literal, TypedDict, cast

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
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
from nx_mcp.kit import args, builders, curve, expr, geom, measure, readback, result, select, units

EdgeList = Annotated[list[EdgeRef], Items(1, 200)]
FaceList = Annotated[list[FaceRef], Items(1, 200)]
ChainFlag = Annotated[bool, Doc("Add the edges tangent-continuous with the given ones")]
# Edges meeting at a vertex continue a tangent chain when their tangents there differ by
# less than this many degrees.
TANGENT_DEGREES = 1.0
# Bodies with more edges than this are not searched for a tangent chain.
CHAIN_EDGE_LIMIT = 5000


def _edges(tx, refs):
    edges = select.edges(tx, refs, "/edges")
    return edges, common.one_body(edges, "/edges")


def _vertex_key(point) -> tuple:
    return tuple(round(float(v), 5) for v in point)


def tangent_chain(tx, body, selected) -> list:
    """The edges of ``body`` not in ``selected`` that continue it tangentially: reached
    through shared vertices where the tangents agree within ``TANGENT_DEGREES``."""
    edges = list(body.GetEdges())
    if len(edges) > CHAIN_EDGE_LIMIT:
        return []
    by_vertex: dict[tuple, list] = {}
    vertices: dict[int, list] = {}
    for edge in edges:
        try:
            points = [geom.xyz(v) for v in edge.GetVertices()]
        except Exception:  # an edge without vertices to read
            continue
        vertices[int(edge.Tag)] = points
        for key in {_vertex_key(p) for p in points}:
            by_vertex.setdefault(key, []).append(edge)
    cache: dict[int, list] = {}

    def ends_of(edge) -> list:
        """``[(vertex key, unit tangent)]`` at the edge's ends ([] if not evaluable); each
        evaluated end is keyed by the nearest of the edge's own vertices."""
        tag = int(edge.Tag)
        if tag not in cache:
            cache[tag] = []
            points = vertices.get(tag) or []
            try:
                with curve.evaluator(tx, edge) as e:
                    found = [e.at(0.0), e.at(1.0)]
            except Exception:
                found = []
            for at, d in found:
                if points and geom.norm(d) > 0:
                    near = min(points, key=functools.partial(geom.dist, at))
                    cache[tag].append((_vertex_key(near), geom.unit(d)))
        return cache[tag]

    cos = math.cos(math.radians(TANGENT_DEGREES))
    chosen = {int(e.Tag) for e in selected}
    frontier, found = list(selected), []
    while frontier:
        for key, tangent in ends_of(frontier.pop()):
            for other in by_vertex.get(key, []):
                if int(other.Tag) in chosen:
                    continue
                if any(k == key and abs(geom.dot(tangent, d)) >= cos for k, d in ends_of(other)):
                    chosen.add(int(other.Tag))
                    found.append(other)
                    frontier.append(other)
    return found


def chain_edges(tx, selected, body, chain: bool, what: str) -> list:
    """``selected`` plus its tangent chain when ``chain``; otherwise ``selected``, with a
    note when it leaves edges of a tangent-continuous chain out."""
    missing = tangent_chain(tx, body, selected)
    if chain:
        return [*selected, *missing]
    if missing:
        tx.warn(
            "NX_NOTE",
            f"The edges are part of a tangent-continuous chain and {len(missing)} edge(s) of "
            f"it are left out, so the {what} ends in a step there; pass chain true to "
            "include them (or add them to edges)",
        )
    return selected


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
    chain: ChainFlag = False,
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
    if variable is None:
        selected = chain_edges(tx, selected, body, chain, "blend")
    elif chain:
        fail("NX_INVALID_ARGUMENT", "A variable blend takes one edge, not a chain",
             field="/chain")  # fmt: skip
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


class ChamferData(common.FeatureData, total=False):
    offset_face: Annotated[result.Row, Doc("The face offset runs along (first edge)")]
    offset2_face: Annotated[result.Row, Doc("The other face (offset2, or offset x tan angle)")]


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
    chain: ChainFlag = False,
) -> ChamferData:
    """Chamfer edges of one body by a symmetric offset, two offsets (offset2), or an offset
    and angle (0..90 deg). NX picks the face that takes offset from the edge direction;
    offset_face reports it (a straight edge between planar faces); reverse swaps them."""
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
    selected = chain_edges(tx, selected, body, chain, "chamfer")
    before = measure.volume(tx, body)
    sides = _edge_sides(tx, selected[0]) if chosen != "symmetric" else None
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
    out = cast(ChamferData, common.result_of(tx, feature, [body]))
    common.changed(before, out["volume"], "less")
    if sides is not None:
        if chosen == "offset_angle":
            second = float(offset) * math.tan(math.radians(angle_value))
        pair = _offset_faces(tx, sides, float(offset), second)
        if pair is not None:
            out["offset_face"], out["offset2_face"] = pair
    return out


def _edge_sides(tx, edge):
    """``(start, unit direction, [face, face])`` of a straight edge between two planar
    faces, else None: what the chamfer read-back measures."""
    try:
        if edge.SolidEdgeType != tx.nx.Edge.EdgeType.Linear:
            return None
        faces = list(edge.GetFaces())
        start, end = (geom.xyz(p) for p in edge.GetVertices())
        if len(faces) != 2 or geom.dist(start, end) <= geom.tol(start, end):
            return None
        if any(select.face_data(tx, f)[0] != select.FACE_PLANE for f in faces):
            return None
        return start, geom.unit(geom.sub(end, start)), faces
    except Exception:  # noqa: BLE001 - no edge data: nothing reported
        return None


def _set_back(tx, face, start, direction) -> float | None:
    """How far the chamfer moved ``face``'s boundary from the old edge line: the distance
    to its nearest straight edge parallel to that line."""
    best = None
    for e in face.GetEdges():
        a, b = (geom.xyz(p) for p in e.GetVertices())
        run = geom.sub(b, a)
        if geom.norm(run) <= geom.tol(a, b) or not geom.parallel(run, direction, 1e-6):
            continue
        off = geom.sub(a, start)
        gap = geom.norm(geom.sub(off, geom.scale(direction, geom.dot(off, direction))))
        best = gap if best is None else min(best, gap)
    return best


def _offset_faces(tx, sides, first, second) -> tuple | None:
    """``(offset face row, offset2 face row)`` measured after the chamfer, or None when
    unclear."""
    start, direction, faces = sides
    try:
        gaps = [_set_back(tx, f, start, direction) for f in faces]
    except Exception:  # noqa: BLE001 - a face NX replaced: nothing reported
        return None
    if None in gaps:
        return None
    tol = max(1e-4, 1e-3 * max(first, second))
    for i in (0, 1):
        if abs(gaps[i] - first) <= tol and abs(gaps[1 - i] - second) <= tol:
            return tx.ref(faces[i], kind="face"), tx.ref(faces[1 - i], kind="face")
    return None


class ThicknessZone(TypedDict):
    faces: FaceList
    thickness: Annotated[
        Length, Doc("This zone's wall thickness, > 0 (stored negated for an inward shell)")
    ]


forbid_extra(ThicknessZone)


def _own_faces(faces, target, field):
    for i, f in enumerate(faces):
        if int(f.GetBody().Tag) != int(target.Tag):
            fail("NX_WRONG_OWNER", "The faces must belong to body", field=f"{field}/{i}",
                 hint="Find the faces with nx_find_geometry owner=<body>")  # fmt: skip


def _zone_set(tx, faces, thickness, outward: bool):
    """One alternate-thickness zone: its faces and a length expression in part units (an
    empty unit name made a unitless expression)."""
    collector = builders.collector(tx, faces, "Face")
    rhs = _zone_rhs(thickness, outward)
    unit = expr.UNIT_NAMES.get(units.part_units(tx)) or ""
    try:
        return tx.part.CreateExpressionCollectorSet(collector, rhs, unit, 0)
    except Exception:  # noqa: BLE001 - a unit name NX does not take: unitless as before
        return tx.part.CreateExpressionCollectorSet(collector, rhs, "", 0)


def _too_thick(tx, error, target, thickness, outward) -> None:
    """Explain an inward shell NX refuses because the wall leaves no cavity."""
    if error.code != "NX_BUILD_FAILED" or outward:
        return
    try:
        low, high = measure.bounds(tx, target)
    except Exception:  # noqa: BLE001 - no bounds: NX's message stands
        return
    smallest = min(high[k] - low[k] for k in range(3))
    if float(thickness) >= smallest / 2:
        error.field = error.field or "/thickness"
        error.hint = (
            f"thickness {float(thickness):g} leaves no cavity in a body {smallest:g} thick at "
            f"its thinnest; use less than {smallest / 2:g} (or outward)"
        )
        error.details = {**(error.details or {}), "smallest_extent": smallest}


def _zone_rhs(thickness, outward: bool) -> str:
    """An alternate thickness as NX takes it: NX 2406 grows a zone's wall outward for a
    positive value whatever ``DefaultThicknessFlip`` says, so an inward shell negates it."""
    text = expr.rhs(thickness)
    return text if outward else f"-({text})"


def _zones(tx, alternate, target, removed) -> list:
    """``[(faces, thickness)]`` of the alternate-thickness zones, checked: faces of
    ``target``, none removed, none in two zones."""
    zones, seen = [], {int(f.Tag) for f in removed}
    for k, zone in enumerate(alternate or []):
        field = f"/alternate/{k}"
        if float(zone["thickness"]) <= 0:
            fail("NX_INVALID_ARGUMENT", "thickness must be positive", field=field + "/thickness")
        faces = select.faces(tx, zone["faces"], field + "/faces")
        _own_faces(faces, target, field + "/faces")
        for i, f in enumerate(faces):
            if int(f.Tag) in seen:
                fail(
                    "NX_INVALID_ARGUMENT",
                    "A face is removed or in another zone already",
                    field=f"{field}/faces/{i}",
                    hint="Give each face one thickness; removed faces take none",
                )
            seen.add(int(f.Tag))
        zones.append((faces, zone["thickness"]))
    return zones


@tool(
    effect="model",
    label="Shell body",
    summary="Shell {body} {thickness}",
    keywords=("shell", "hollow", "wall thickness", "thin wall", "open box", "alternate thickness"),
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
    alternate: Annotated[
        list[ThicknessZone], Items(1, 20), Doc("Faces whose wall takes its own thickness")
    ]
    | None = None,
) -> common.FeatureData:
    """Hollow a solid body to a wall thickness, opening the removed faces. alternate gives
    listed faces their own wall thickness (laminate zones); the others take thickness."""
    if float(thickness) <= 0:
        fail("NX_INVALID_ARGUMENT", "thickness must be positive", field="/thickness",
             hint="Send e.g. 'thickness': '2 mm'; outward grows the wall outside")  # fmt: skip
    with tx.checks():
        target = common.solid(tx, body)
        faces = select.faces(tx, remove_faces or [], "/remove_faces")
        _own_faces(faces, target, "/remove_faces")
        zones = _zones(tx, alternate, target, faces)
    before = measure.volume(tx, target)
    try:
        with builders.build(tx, tx.part.Features.CreateShellBuilder, None) as b:
            b.Tolerance = units.part_tolerance(tx)
            b.Body = target
            expr.drive(b.DefaultThickness, thickness)  # an expression keeps the wall driven
            b.DefaultThicknessFlip = not outward
            if faces:
                b.RemovedFacesCollector = builders.collector(tx, faces, "Face")
            for zone_faces, zone_thickness in zones:
                b.FaceThicknesses.Append(_zone_set(tx, zone_faces, zone_thickness, outward))
            feature = b.commit()
    except NXError as error:
        _too_thick(tx, error, target, thickness, outward)
        raise
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
    direction: Direction,
    angle: Angle,
) -> common.FeatureData:
    """Draft (taper) wall faces about a stationary face along the draw (pull) direction. A
    positive angle tapers them inward, a negative one outward. The walls must run roughly
    along the direction (a face leaning over 45 deg off it is refused); each planar wall's
    angle is read back."""
    a = float(angle)
    if not 0 < abs(a) < 89:
        fail(
            "NX_INVALID_ARGUMENT",
            "The draft angle must be non-zero and within -89..89 degrees",
            field="/angle",
            hint="Send e.g. 'angle': 5 or '3 deg'; negative tapers outward",
        )
    with tx.checks():  # lookups and refusals: nothing changed yet (not_started)
        selected = select.faces(tx, faces, "/faces")
        fixed = select.one(tx, stationary_face, "face", field="/stationary_face")
        body = fixed.GetBody()
        for i, f in enumerate(selected):
            if int(f.Tag) == int(fixed.Tag):
                fail("NX_INVALID_ARGUMENT", "Exclude the stationary face", field=f"/faces/{i}",
                     hint="faces are the walls to taper; stationary_face stays put")  # fmt: skip
            if int(f.GetBody().Tag) != int(body.Tag):
                fail("NX_WRONG_OWNER", "Faces must share the stationary face's body",
                     field=f"/faces/{i}")  # fmt: skip
        vector = select.direction(tx, common.principal(direction), "/direction")
        walls = _draft_walls(tx, selected, vector)
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
            builders.collector(tx, selected, "Face"), expr.rhs(angle), "Angle", 0
        )
        b.FaceSetAngleExpressionList.Append(group)
        feature = b.commit()
    out = common.result_of(tx, feature, [body])
    common.changed(before, out["volume"], "differs")
    _drafted_angles(tx, feature, walls, vector, abs(a))
    if abs(out["volume"] - before) > DRAFT_VOLUME_SHARE * before:
        tx.warn(
            "NX_UPDATE_WARNING",
            f"The draft changed the body's volume by {out['volume'] - before:+.6g} "
            f"({100 * (out['volume'] - before) / before:+.3g}%): check the shape "
            "(nx_measure_bounds); undo if the walls did not just tilt",
        )
    return out


# A face within this many degrees of square to the pull direction is not a wall of it.
WALL_DEGREES = 45.0
# Read-back tolerance of a drafted planar wall's angle, degrees.
DRAFT_ANGLE_TOL = 0.05
# A draft changing the body's volume by more than this share gets a warning.
DRAFT_VOLUME_SHARE = 0.25


def _draft_walls(tx, selected, vector) -> list:
    """The planar walls of ``selected`` (their index and frame), refusing a face that is no
    wall of the pull direction: a plane within ``WALL_DEGREES`` of square to it, or a
    cylinder whose axis leans further than that from it. Such a draft collapses the body."""
    walls = []
    for i, face in enumerate(selected):
        frame = readback.face_frame(tx, face)
        if frame is None:
            continue
        if frame["type"] == readback.PLANE:  # degrees off running along the direction
            lean = readback.draft_angle(frame["dir"], vector)
        elif frame["type"] == readback.CYLINDER:  # its axis: 0 when along the direction
            lean = 90.0 - readback.draft_angle(frame["dir"], vector)
        else:
            continue
        if lean > WALL_DEGREES:
            fail(
                "NX_INVALID_ARGUMENT",
                f"faces[{i}] leans {lean:.3g} deg off the draw direction: it is not a wall of "
                "that pull, and drafting it would collapse the body",
                field="/direction",
                hint="direction is the pull (draw) direction: the walls to taper run along it "
                "and the stationary face is roughly square to it (e.g. 'z' for vertical walls "
                "standing on an XY floor)",
                details={"face": i, "lean": lean},
            )
        if frame["type"] == readback.PLANE:
            walls.append((i, face))
    return walls


def _drafted_angles(tx, feature, walls, vector, angle: float) -> None:
    """Read back the draft angle of each planar wall NX kept (else of the feature's own
    planar faces): at least one must lie at ``angle`` to the pull direction."""
    found = []
    for _i, face in walls:
        frame = readback.face_frame(tx, face)
        if frame is not None and frame["type"] == readback.PLANE:
            found.append(readback.draft_angle(frame["dir"], vector))
    if not found and hasattr(feature, "GetFaces"):
        for face in feature.GetFaces():
            frame = readback.face_frame(tx, face)
            if frame is not None and frame["type"] == readback.PLANE:
                found.append(readback.draft_angle(frame["dir"], vector))
    if not found:
        return  # cylinders or cones only: the volume read-back stands
    off = [x for x in found if abs(x - angle) > DRAFT_ANGLE_TOL]
    readback.expect(
        len(off) < len(found),
        "angle",
        f"No drafted wall lies at {angle:g} deg to the draw direction (NX gave "
        f"{', '.join(f'{x:.4g}' for x in found[:5])} deg)",
        angles=found,
    )
