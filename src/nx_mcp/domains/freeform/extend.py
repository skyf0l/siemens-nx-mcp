"""Sheet extensions: Extend Sheet, Trim and Extend (make corner) and Law Extension.

NX 2406 lessons kept here (probed live on a 20 x 10 planar sheet):

- ``TrimFeatureCollection.CreateExtendSheetBuilder`` and ``CreateTrimAndExtendBuilder``
  need the basic freeform license (``nx_freeform_1``); ``CreateLawExtensionBuilder``
  needs Modeling's (``solid_modeling``). NX's refusal is reported naming the one tried.
- Extend Sheet fills its own ``TargetBoundaryEdges`` collector with sheet boundary
  edges. ``LimitMethod`` ``Offset`` uses the ``Offset`` expression (signed: 5 grows the
  sheet 20 -> 25 long, area 200 -> 250; -3 trims it back to 17, area 170);
  ``UntilSelected`` extends to the faces in ``LimitTools`` (a wall face at x 30: area
  300). ``BodyOutputOption``: ``ExtendOriginalSheet`` changes the sheet (one face),
  ``ExtendasNewFace`` adds a second face to it, ``ExtendasNewSheet`` leaves it and makes
  a new 50 mm^2 sheet (``Feature.GetBodies()`` lists the new sheet only). The surface
  shape options (natural curvature, natural tangent, mirrored) agree on a plane.
- Trim and Extend ``MakeCorner`` with an edge of each sheet extends both until they
  meet and trims the overhang: a horizontal 20 x 10 sheet and a vertical 10 x 10 sheet
  5 above and 10 beyond it give 300 + 150; ``CombineOption`` (the default) sews them into
  one two-face sheet, else two sheets. ``UntilSelected`` with every face of a solid
  extends through to the far face and splits at the near one, so "extend to a face" is
  Extend Sheet's ``UntilSelected`` here.
- Law Extension fills its own ``BaseProfile`` section; ``Type`` ``Faces`` measures the
  angle from the reference faces' tangent plane (0: continues the sheet, 90: along its
  normal), ``Vector`` from the vector (0: along it). ``LengthLaw``/``AngleLaw`` are
  ``LawBuilder``s: ``Constant`` uses ``Value``, ``Linear`` ``StartValue``/``EndValue``.
  A 10 long base edge, length 5: area 50 whatever the angle.
- A native refusal is ``NX_BUILD_FAILED`` with ``nx_code``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Angle, Doc, EdgeRef, FaceRef, Items, Length, Point3, RefTo, Vec3
from nx_mcp.domains.freeform import common
from nx_mcp.domains.freeform import curves_common as cc
from nx_mcp.kit import bodies as kbodies
from nx_mcp.kit import (
    builders,
    curve,
    expr,
    facts,
    geom,
    measure,
    readback,
    select,
    units,
)

FREEFORM = "nx_freeform_1"
MODELING = "solid_modeling"
MAX_EDGES = 100
LimitRef = Annotated[str, RefTo("face", "datum_plane", doc="Face or datum plane 'fc:7'/'dp:3'")]
BaseRef = Annotated[str, RefTo("curve", "edge", doc="Curve or edge 'c:7'/'e:21'")]

SHAPES = {
    "natural_curvature": "NaturalCurvature",
    "natural_tangent": "NaturalTangent",
    "mirrored": "Mirrored",
}
OUTPUTS = {"extend": "ExtendOriginalSheet", "new_face": "ExtendasNewFace",
           "new_sheet": "ExtendasNewSheet"}  # fmt: skip
# NX 2406 "Unable to perform Boolean.": an until limit the extension never reaches.
NOT_REACHED = 670033
UNTIL_HINT = (
    "until faces and planes must lie across the sheet's extension beyond the edges (not "
    "parallel to the sheet, not behind the edges)"
)


def _new_bodies(part, before, feature):
    """The feature's bodies, then any other new body of the part."""
    out = common.feature_bodies(feature)
    known = {int(b.Tag) for b in out}
    return out + [b for b in part.Bodies if int(b.Tag) not in before | known]


def _sheet_edges(tx, refs, field):
    """Boundary edges of sheet bodies (one face each) and their bodies."""
    edges = select.edges(tx, refs, field)
    owners = []
    for i, edge in enumerate(edges):
        faces = list(edge.GetFaces())
        body = faces[0].GetBody() if len(faces) == 1 else None
        if body is None or not common.is_sheet(body):
            fail(
                "NX_WRONG_GEOMETRY",
                "Select an open boundary edge of a sheet body (one face, not a solid edge)",
                field=f"{field}/{i}",
                hint="nx_find_geometry kind 'edge' on the sheet lists its edges",
            )
        if all(int(b.Tag) != int(body.Tag) for b in owners):
            owners.append(body)
    return edges, owners


def _total_area(tx, bodies):
    faces = [f for b in bodies for f in b.GetFaces()]
    return measure.area(tx, faces) if faces else 0.0


def _plane_of(tx, obj):
    """``(origin, unit normal)`` of a datum plane or planar face, else None."""
    if hasattr(obj, "Normal") and hasattr(obj, "Origin"):
        return geom.xyz(obj.Origin), geom.unit(geom.xyz(obj.Normal))
    kind, point, normal = select.face_data(tx, obj)
    return (point, geom.unit(normal)) if kind == select.FACE_PLANE else None


def _reachable(tx, edges, limits, refs) -> None:
    """Refuse an until plane that the extension of a planar sheet's edges runs parallel to
    (it never reaches it; NX answers "Unable to perform Boolean")."""
    runs = []  # (edge midpoint, extension line direction up to sign) of planar sheets
    for edge in edges:
        sheet = select.face_data(tx, list(edge.GetFaces())[0])
        if sheet[0] != select.FACE_PLANE:
            return  # a curved sheet extends along its curvature: NX decides
        with curve.evaluator(tx, edge) as e:
            middle, tangent = e.at(0.5)
        runs.append((middle, geom.unit(geom.cross(tangent, sheet[2]))))
    tol = 10 * units.part_tolerance(tx)
    for i, obj in enumerate(limits):
        plane = _plane_of(tx, obj)
        if plane is None:
            continue
        origin, normal = plane
        if all(
            abs(geom.dot(d, normal)) <= 1e-6 and abs(geom.dot(geom.sub(m, origin), normal)) > tol
            for m, d in runs
        ):
            fail("NX_INVALID_ARGUMENT", f"The extension never reaches {refs[i]}: the sheet "
                 "runs parallel to it", field=f"/until/{i}", hint=UNTIL_HINT)  # fmt: skip


def _limit_rules(tx, objects):
    part = tx.part
    datums = [o for o in objects if hasattr(o, "Normal") and not hasattr(o, "GetBody")]
    faces = [o for o in objects if o not in datums]
    rules = []
    if faces:
        rules.append(part.ScRuleFactory.CreateRuleFaceDumb(faces))
    if datums:
        rules.append(part.ScRuleFactory.CreateRuleFaceDatum(datums))
    return rules


class ExtensionData(common.SheetData):
    method: Literal["distance", "until", "corner"]
    area_before: float  # total area of the extended sheets before
    added_area: float  # area now minus area_before (negative: trimmed back)
    edges: int  # edges of the result bodies
    gaps: list[float]  # until/corner: distance of the result to each limit/tool


@tool(
    effect="model",
    label="Extend sheet",
    summary=lambda a, say: (
        f"Extend {say('edges')} "
        + {"until": "to the limit faces", "corner": "into a corner"}.get(
            a.get("method", "distance"), f"by {say('distance')}"
        )
    ),
    keywords=(
        "extend sheet",
        "extend surface",
        "trim and extend",
        "make corner",
        "extend to face",
        "lengthen surface",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    variants="method",
    required_by={"distance": ("distance",), "until": ("until",), "corner": ("to_edges",)},
    cost="heavy",
    previewable=True,
    example={"edges": ["e:12"], "method": "distance", "distance": 5},
)
def nx_create_sheet_extension(
    tx: Tx,
    *,
    edges: Annotated[list[EdgeRef], Items(1, MAX_EDGES), Doc("Open boundary edges of sheets")],
    method: Annotated[
        Literal["distance", "until", "corner"],
        Doc("distance: by distance; until: to faces/planes; corner: meet the to_edges sheet"),
    ] = "distance",
    distance: Annotated[Length, Doc("distance: along the surface; negative trims back")]
    | None = None,
    until: Annotated[list[LimitRef], Items(1, 20), Doc("until: faces/planes to reach")]
    | None = None,
    to_edges: Annotated[list[EdgeRef], Items(1, MAX_EDGES), Doc("corner: the other sheet's edges")]
    | None = None,
    shape: Annotated[
        Literal["natural_curvature", "natural_tangent", "mirrored"], Doc("Extension surface shape")
    ] = "natural_curvature",
    output: Annotated[
        Literal["extend", "new_face", "new_sheet"],
        Doc("distance/until: grow the sheet, add a face to it, or make a separate sheet"),
    ] = "extend",
    join: Annotated[bool, Doc("corner: sew the two sheets into one")] = True,
) -> ExtensionData:
    """Extend sheet boundary edges by a signed distance, until faces/planes, or into a corner.
    until reaches faces or datum planes (hull to deck/transom); corner extends and trims the
    edges and to_edges until the two sheets meet. Reports the area change and limit gaps."""
    given = {"distance": distance, "until": until, "corner": to_edges}
    for name, value in (("distance", distance), ("until", until), ("to_edges", to_edges)):
        wanted = name == {"distance": "distance", "until": "until", "corner": "to_edges"}[method]
        if value is not None and not wanted:
            fail("NX_INVALID_ARGUMENT", f"{name} is not used by method {method}",
                 field=f"/{name}")  # fmt: skip
    if given[method] is None:
        field = {"distance": "/distance", "until": "/until", "corner": "/to_edges"}[method]
        fail("NX_INVALID_ARGUMENT", f"method {method} needs {field[1:]}", field=field)
    signed = 0.0 if distance is None else float(distance)
    if method == "distance" and geom.close(signed, 0.0, 1e-12):
        fail("NX_INVALID_ARGUMENT", "The distance must not be zero", field="/distance")
    if method == "corner" and output != "extend":
        fail("NX_INVALID_ARGUMENT", "output applies to distance and until; corner uses join",
             field="/output")  # fmt: skip
    with tx.checks():
        targets, owners = _sheet_edges(tx, edges, "/edges")
        limits: list = []
        tools: list = []
        if until is not None:
            limits = [
                select.one(tx, ref, "face", "datum_plane", field=f"/until/{i}")
                for i, ref in enumerate(until)
            ]
            _reachable(tx, targets, limits, until)
        if to_edges is not None:
            tools, others = _sheet_edges(tx, to_edges, "/to_edges")
            mine = {int(b.Tag) for b in owners}
            if any(int(b.Tag) in mine for b in others):
                fail("NX_INVALID_ARGUMENT", "to_edges must belong to another sheet",
                     field="/to_edges")  # fmt: skip
            owners = owners + others
    part = tx.part
    before = {int(b.Tag) for b in part.Bodies}
    area_before = _total_area(tx, owners)
    tol = units.part_tolerance(tx)
    E = tx.nx.Features.ExtendSheetBuilder
    trims = part.Features.TrimFeatureCollection
    if method == "corner":
        T = tx.nx.Features.TrimAndExtendBuilder
        with (
            common.licensed(tx, FREEFORM),
            builders.build(tx, trims.CreateTrimAndExtendBuilder, None) as b,
        ):
            b.Operation = T.OperationType.MakeCorner
            builders.fill(tx, b.TargetSelection, targets, "Edge")
            builders.fill(tx, b.ToolSelection, tools, "Edge")
            b.DistanceTolerance = tol
            b.CombineOption = bool(join)
            b.SurfaceShapeOption = getattr(E.SurfaceShape, SHAPES[shape])
            feature = b.commit()
    else:
        with (
            common.licensed(tx, FREEFORM),
            builders.build(tx, trims.CreateExtendSheetBuilder, None) as b,
        ):
            builders.fill(tx, b.TargetBoundaryEdges, targets, "Edge")
            b.DistanceTolerance = tol
            b.SurfaceShapeOption = getattr(E.SurfaceShape, SHAPES[shape])
            b.BodyOutputOption = getattr(E.BodyOutput, OUTPUTS[output])
            if method == "distance":
                b.LimitMethod = E.Limit.Offset
                expr.drive(b.Offset, distance)
            else:
                b.LimitMethod = E.Limit.UntilSelected
                b.LimitTools.ReplaceRules(_limit_rules(tx, limits), False)
            try:
                feature = b.commit()
            except NXError as error:
                if error.nx_code == NOT_REACHED and method == "until":
                    error.field = "/until"
                    error.hint = UNTIL_HINT
                raise
    bodies = _new_bodies(part, before, feature)
    out = common.result_of(tx, feature, bodies)
    kbodies.expect_sound(tx, bodies)
    live = {int(b.Tag) for b in part.Bodies}
    kept = [
        b for b in owners if int(b.Tag) in live and all(int(b.Tag) != int(x.Tag) for x in bodies)
    ]
    added = _total_area(tx, bodies + kept) - area_before
    limit = 2 * tol + 10 * units.part_tolerance(tx)
    grew = method != "distance" or signed > 0
    if grew:
        readback.expect(added > limit, "area", "The sheet did not grow", added_area=added)
    else:
        readback.expect(added < -limit, "area", "The sheet was not trimmed back", added_area=added)
    gaps = [min(curve.min_distance(tx, b, obj)[0] for b in bodies) for obj in limits]
    if method == "corner" and join:
        readback.expect(len(bodies) == 1, "joined", "NX left the corner sheets apart",
                        bodies=len(bodies))  # fmt: skip
    elif method == "corner":
        gaps = [
            curve.min_distance(tx, a, b)[0] for i, a in enumerate(bodies) for b in bodies[i + 1 :]
        ]
    if gaps:
        readback.expect(max(gaps) <= limit, "reach", "The extension does not reach the "
                        + ("limit faces" if limits else "other sheet"), gaps=gaps)  # fmt: skip
    edge_count = sum(len(b.GetEdges()) for b in bodies)
    return {**out, "method": method, "area_before": area_before, "added_area": added,
            "edges": edge_count, "gaps": gaps}  # fmt: skip


# ---------------------------------------------------------------------------
# Law extension


class LawData(common.SheetData):
    reference: Literal["faces", "vector"]
    base_length: float
    expected_area: float | None  # straight base, one angle: base_length x mean length
    gap: float  # largest distance of a base midpoint from the result
    base_start: Point3  # where length and angle apply (end_length/end_angle at base_end)
    base_end: Point3
    side: list[float] | None  # unit vector from the base middle towards the sheet


def _straight(tx, obj) -> bool:
    start, end = curve.ends(tx, obj)
    return abs(measure.length(tx, [obj]) - geom.dist(start, end)) <= 1e-6 * max(
        1.0, geom.dist(start, end)
    )


def _across(tx, objs, vector) -> None:
    """Refuse a reference vector along a base curve: NX builds no surface there."""
    for i, obj in enumerate(objs):
        with curve.evaluator(tx, obj) as e:
            tangents = [e.at(k / 4)[1] for k in range(5)]
        if all(abs(geom.dot(geom.unit(t), vector)) >= 1 - 1e-6 for t in tangents):
            fail("NX_INVALID_ARGUMENT", f"direction runs along base/{i}: give a direction "
                 "across the base", field="/direction",
                 hint="e.g. the sheet normal or [0, 0, 1] for a base in the XY plane")  # fmt: skip


def _base_end(tx, objs, start):
    """The other free end of the chained base (``start`` when the base is closed)."""
    ends = [p for o in objs for p in curve.ends(tx, o)]
    limit = 10 * units.part_tolerance(tx) + geom.tol(*ends)
    free = [p for p in ends if sum(geom.dist(p, q) <= limit for q in ends) == 1]
    far = [p for p in free if geom.dist(p, start) > limit]
    return far[0] if far else start


def _law(tx, law, start, end):
    types = tx.nx.GeometricUtilities.LawBuilder.Type
    if end is None:
        law.LawType = types.Constant
        expr.drive(law.Value, start)
    else:
        law.LawType = types.Linear
        expr.drive(law.StartValue, start)
        expr.drive(law.EndValue, end)


@tool(
    effect="model",
    label="Create law extension",
    summary="Law extension of {base}, length {length}",
    keywords=(
        "law extension",
        "flange surface",
        "draft surface",
        "extend at angle",
        "skirt surface",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    variants="reference",
    required_by={"vector": ("direction",)},
    cost="heavy",
    previewable=True,
    example={"base": ["e:12"], "reference": "faces", "length": 5, "angle": 90},
)
def nx_create_law_extension(
    tx: Tx,
    *,
    base: Annotated[
        list[BaseRef], Items(1, MAX_EDGES), Doc("Chained curves/sheet edges to grow from")
    ],
    length: Annotated[Length, Doc("Length of the extension (at the base start when end_length)")],
    reference: Annotated[
        Literal["faces", "vector"],
        Doc("faces: angle from the faces' tangent plane; vector: angle from direction"),
    ] = "faces",
    angle: Annotated[
        Angle, Doc("0: continue the faces/along direction; 90: along the normal")
    ] = 0.0,
    faces: Annotated[
        list[FaceRef], Items(1, 100), Doc("faces: reference; default: base edges' faces")
    ]
    | None = None,
    direction: Annotated[Vec3, Doc("vector: the 0-degree direction")] | None = None,
    end_length: Annotated[Length, Doc("Length at the base end (linear law)")] | None = None,
    end_angle: Annotated[Angle, Doc("Angle at the base end (linear law)")] | None = None,
) -> LawData:
    """Grow a new sheet from curves or sheet edges at a length and angle (flange, skirt).
    The angle is from reference faces (0 continues the sheet, 90 along the face normal, -90
    against it) or from direction. end_length/end_angle vary length/angle linearly from
    base_start to base_end. Returns base_start, base_end and side (where the sheet went)."""
    if reference == "faces" and direction is not None:
        fail("NX_INVALID_ARGUMENT", "direction is for reference vector", field="/direction")
    if reference == "vector" and faces is not None:
        fail("NX_INVALID_ARGUMENT", "faces are for reference faces", field="/faces")
    if reference == "vector" and direction is None:
        fail("NX_INVALID_ARGUMENT", "reference vector needs direction", field="/direction",
             hint="direction: [0, 0, 1] grows the sheet up at angle 0")  # fmt: skip
    lengths = [float(length)] + ([] if end_length is None else [float(end_length)])
    if min(lengths) <= 0:
        fail("NX_INVALID_ARGUMENT", "Lengths must be positive",
             field="/length" if float(length) <= 0 else "/end_length")  # fmt: skip
    with tx.checks():
        objs = [
            select.one(tx, ref, "curve", "edge", field=f"/base/{i}") for i, ref in enumerate(base)
        ]
        if len({int(o.Tag) for o in objs}) != len(objs):
            fail("NX_INVALID_ARGUMENT", "A base curve is listed twice", field="/base")
        problem = cc.chain_problem(tx, objs) if len(objs) > 1 else None
        if problem is not None:
            index, kind, details = problem
            fail("NX_INVALID_ARGUMENT", f"base/{index} does not chain end to end with the other "
                 f"base curves ({kind})", field=f"/base/{index}", details=details,
                 hint="List base curves that meet end to end in one string")  # fmt: skip
        vector = None if direction is None else geom.unit(direction, "/direction")
        if vector is not None:
            _across(tx, objs, vector)
        refs = []
        if reference == "faces":
            if faces is not None:
                refs = select.faces(tx, faces, "/faces")
            else:
                for obj in objs:
                    own = list(obj.GetFaces()) if facts.is_edge(obj) else []
                    refs += [f for f in own if all(int(f.Tag) != int(r.Tag) for r in refs)]
                if not refs:
                    fail("NX_INVALID_ARGUMENT", "Curves have no faces: send faces or use "
                         "reference vector", field="/faces")  # fmt: skip
    part = tx.part
    L = tx.nx.Features.LawExtensionBuilder
    start = curve.ends(tx, objs[0])[0]
    with (
        common.licensed(tx, MODELING),
        builders.build(tx, part.Features.CreateLawExtensionBuilder, None) as b,
    ):
        builders.curve_section(tx, objs, start=start, into=b.BaseProfile)
        if reference == "faces":
            b.Type = L.Types.Faces
            builders.fill(tx, b.Faces, refs, "Face")
        else:
            b.Type = L.Types.Vector
            b.Vector = builders.direction(tx, vector, start, "/direction")
        _law(tx, b.LengthLaw, length, end_length)
        _law(tx, b.AngleLaw, angle, end_angle)
        b.DistanceTolerance = units.part_tolerance(tx)
        feature = b.commit()
    bodies = common.feature_bodies(feature)
    out = common.result_of(tx, feature, bodies)
    kbodies.expect_sound(tx, bodies)
    base_length = measure.length(tx, objs)
    limit = 10 * units.part_tolerance(tx) + 1e-6 * base_length
    gap = max(min(curve.point_distance(tx, b, curve.point_at(tx, o, 0.5))[0] for b in bodies)
              for o in objs)  # fmt: skip
    readback.expect(gap <= limit, "attached", "The extension does not start at the base",
                    gap=gap)  # fmt: skip
    expected = None
    if end_angle is None and all(_straight(tx, o) for o in objs):
        expected = base_length * sum(lengths) / len(lengths)
        readback.expect_close(out["area"], expected, "area", tol=1e-3 * expected)
    return {**out, "reference": reference, "base_length": base_length,
            "expected_area": expected, "gap": gap, "base_start": cc.clean(start),
            "base_end": cc.clean(_base_end(tx, objs, start)),
            "side": _grown(tx, objs, out["bounds"])}  # fmt: skip


def _grown(tx, objs, bounds) -> list[float] | None:
    """Unit vector from the middle of the base towards the middle of the new sheet (its
    bounds centre), across the base: the side the extension went."""
    with curve.evaluator(tx, objs[len(objs) // 2]) as e:
        middle, tangent = e.at(0.5)
    centre = geom.scale(geom.add(bounds["min"], bounds["max"]), 0.5)
    away = geom.sub(centre, middle)
    t = geom.unit(tangent)
    across = geom.sub(away, geom.scale(t, geom.dot(away, t)))
    size = geom.norm(across)
    if size <= 1e-9 * max(1.0, geom.norm(away)):
        return None
    return cc.clean([v / size for v in across])
