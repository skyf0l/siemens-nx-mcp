"""Through Curve Mesh and bridge surfaces.

NX 2406 lessons kept here:

- Bridge Surface (``CreateBridgeSurfaceBuilder``) needs a Studio freeform license
  that some seats lack (NXException 949895 when the builder is created). The
  equivalent native construction is a Through Curves sheet over the two edges
  with G0/G1/G2 continuity to the edges' adjacent faces; ``construction`` in the
  result says which was built. The second section starts at its end nearest the
  first edge's start (an untwisted bridge); ``reverse_second`` starts it at the
  other end. A twisting bridge is refused by NX (670929) and reported as
  ``NX_BUILD_FAILED`` with a hint.
- Native refusals of a mesh network (curves not intersecting within tolerance,
  670946) are ``NX_BUILD_FAILED`` with ``nx_code``.
- Section and builder tolerances are 0.001 mm in part units (inch parts scale).
- A mesh section is one curve ref or a list of curve refs (a chain); a flat list of
  refs is one curve per section.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, EdgeRef, FaceRef, Items, Length, RefTo
from nx_mcp.domains.freeform import common
from nx_mcp.kit import builders, curve, facts, geom, readback, select, surface, units

MAX_SECTION = 100
MeshRef = Annotated[str, RefTo("curve", "edge", doc="Curve or edge 'c:7'/'e:21'")]
Section = Annotated[list[MeshRef], Items(1, MAX_SECTION)] | MeshRef
LOOSE_MM = 0.1  # a mesh tolerance above this lets the sheet visibly miss its curves
MESH_MISS = 670946  # NX: "Strings do not intersect within tolerance"
MAX_CURVES = 1000

# Read-back tolerances: angle in degrees; curvature 0.01 1/mm (scaled to part units).
ANGLE_TOLERANCE = 0.1
CURVATURE_TOLERANCE_MM = 0.01
GAP_TOLERANCE_MM = 0.01
BRIDGE_HINT = (
    "Check that the edges face each other; toggle reverse_second if the bridge would twist"
)


# ---------------------------------------------------------------------------
# Through Curve Mesh


def _groups(raw, field):
    """``[(refs, field of each ref)]`` of the sections (a single ref is a one-curve one)."""
    out = []
    for i, section in enumerate(raw):
        if isinstance(section, str):
            out.append(([section], [f"{field}/{i}"]))
        else:
            out.append((list(section), [f"{field}/{i}/{j}" for j in range(len(section))]))
    return out


def _distinct(groups, seen):
    for refs, fields in groups:
        for ref, where in zip(refs, fields, strict=True):
            if ref in seen:
                fail("NX_INVALID_ARGUMENT", f"Curve {ref} is listed twice", field=where)
            seen.add(ref)


def _sections(tx, groups):
    return [
        [
            select.one(tx, ref, "curve", "edge", field=where)
            for ref, where in zip(refs, fields, strict=True)
        ]
        for refs, fields in groups
    ]


class MeshGaps(TypedDict):
    primary: list[float]  # per primary section: largest distance from its curves to the sheet
    cross: list[float]


Side = Literal["first_primary", "last_primary", "first_cross", "last_cross"]
CONSTRAINTS = {
    "first_primary": ("FirstPrimaryContinuity", 0, 0),
    "last_primary": ("LastPrimaryContinuity", 0, -1),
    "first_cross": ("FirstCrossContinuity", 1, 0),
    "last_cross": ("LastCrossContinuity", 1, -1),
}  # side -> (builder constraint, network, section index)
MESH_ANGLE = 0.5  # degrees: G1/G2 boundary read-back (NX's default tangent tolerance)


class _Boundary(TypedDict):
    side: Annotated[Side, Doc("The outer section the condition applies to")]
    continuity: Annotated[Literal["g0", "g1", "g2"], Doc("g1 tangent, g2 curvature")]


class MeshBoundary(_Boundary, total=False):
    faces: Annotated[list[FaceRef], Items(1, 100), Doc("G1/G2: faces; default the edges' faces")]


class BoundaryFit(TypedDict):
    side: str
    continuity: str
    angle: float | None  # largest normal angle to the faces, degrees
    curvature: float | None  # largest curvature difference, 1/part unit


class MeshData(common.SheetData):
    tolerance: float
    gaps: MeshGaps
    deviation: float  # largest of gaps
    boundaries: list[BoundaryFit]


def _constraint_faces(tx, rows, networks) -> list:
    """Per boundary row: the faces its G1/G2 condition follows (given, or the one face of
    each sheet boundary edge of that section); ``[]`` for G0."""
    seen: set[str] = set()
    out: list = []
    for i, row in enumerate(rows):
        if row["side"] in seen:
            fail("NX_INVALID_ARGUMENT", f"{row['side']} is listed twice",
                 field=f"/boundaries/{i}/side")  # fmt: skip
        seen.add(row["side"])
        _name, network, index = CONSTRAINTS[row["side"]]
        if row["continuity"] == "g0":
            if row.get("faces"):
                fail("NX_INVALID_ARGUMENT", "faces apply to g1 and g2",
                     field=f"/boundaries/{i}/faces")  # fmt: skip
            out.append([])
            continue
        if row.get("faces"):
            out.append(select.faces(tx, row["faces"], f"/boundaries/{i}/faces"))
            continue
        faces: list = []
        for obj in networks[network][index]:
            adjacent = list(obj.GetFaces()) if facts.is_edge(obj) else []
            if len(adjacent) != 1:
                fail("NX_INVALID_ARGUMENT", f"{row['side']} needs faces: its section is not "
                     "made of sheet boundary edges", field=f"/boundaries/{i}/faces",
                     hint="Pass faces: the faces the mesh must be tangent to")  # fmt: skip
            if all(int(adjacent[0].Tag) != int(f.Tag) for f in faces):
                faces.append(adjacent[0])
        out.append(faces)
    return out


def _boundary_fits(tx, rows, faces, networks, bodies) -> list[BoundaryFit]:
    """Sampled angle and curvature between the mesh and each G1/G2 boundary's faces."""
    edges = [e for body in bodies for e in body.GetEdges()]
    out: list[BoundaryFit] = []
    for row, given in zip(rows, faces, strict=True):
        fit: BoundaryFit = {"side": row["side"], "continuity": row["continuity"],
                            "angle": None, "curvature": None}  # fmt: skip
        _name, network, index = CONSTRAINTS[row["side"]]
        angles, curvatures = [], []
        for obj in networks[network][index] if given else []:
            tx.checkpoint()
            middle = curve.point_at(tx, obj, 0.5)
            mesh_edge = min(edges, key=lambda e: curve.point_distance(tx, e, middle)[0])
            other = min(given, key=lambda f: curve.point_distance(tx, f, middle)[0])
            report = surface.edge_continuity(tx, mesh_edge, list(mesh_edge.GetFaces())[0], obj,
                                             other)  # fmt: skip
            if report["angle"] is not None:
                angles.append(report["angle"])
                curvatures.append(report["curvature"])
        fit["angle"] = max(angles) if angles else None
        fit["curvature"] = max(curvatures) if curvatures else None
        out.append(fit)
    return out


def _section_gap(tx, a, b) -> float:
    """Smallest distance between two sections (lists of curves or edges)."""
    return min(curve.min_distance(tx, x, y)[0] for x in a for y in b)


def _miss(tx, error, networks, tol):
    """A mesh refusal (curves not meeting within tolerance) with the worst pairs."""
    pairs = []
    for i, first in enumerate(networks[0][: common.MAX_DIAGNOSED]):
        for j, second in enumerate(networks[1][: common.MAX_DIAGNOSED]):
            tx.checkpoint()
            pairs.append({"primary": i, "cross": j, "gap": _section_gap(tx, first, second)})
    pairs.sort(key=lambda p: -p["gap"])
    worst = pairs[0] if pairs else None
    if worst is None or worst["gap"] <= tol:
        return common.explain(error, "Each primary section must cross every cross section")
    hint = (
        f"primary {worst['primary']} and cross {worst['cross']} are {worst['gap']:.4g} apart "
        f"(tolerance {tol:.4g}): make the curves meet, or send tolerance {worst['gap'] * 1.1:.3g}"
        " (the sheet then misses them by up to that much)"
    )
    error.field = f"/cross/{worst['cross']}"
    return common.explain(error, hint, pairs=[p for p in pairs if p["gap"] > tol][:5])


@tool(
    effect="model",
    label="Create mesh surface",
    summary=lambda a, say: f"Mesh surface {len(a['primary'])} x {len(a['cross'])} sections",
    keywords=("through curve mesh", "mesh surface", "curve network", "surface from curves"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"primary": ["c:1", "c:2"], "cross": [["c:3"], ["c:4", "c:5"]]},
)
def nx_create_mesh_surface(
    tx: Tx,
    *,
    primary: Annotated[list[Section], Items(2, 100)],
    cross: Annotated[list[Section], Items(2, 100)],
    tolerance: Annotated[Length, Doc("Position and intersection tolerance")] | None = None,
    boundaries: Annotated[list[MeshBoundary], Items(1, 4), Doc("G0/G1/G2 at outer sections")]
    | None = None,
) -> MeshData:
    """Create a Through Curve Mesh sheet from intersecting primary and cross sections.
    Each section: a curve or edge ref or a list of chained refs. boundaries: G0/G1/G2 at the
    outer sections (default the edges' faces). Returns each section's gap to the sheet and
    each boundary's angle; curves that miss each other are reported per pair."""
    groups = [_groups(primary, "/primary"), _groups(cross, "/cross")]
    total = sum(len(refs) for network in groups for refs, _ in network)
    if total > MAX_CURVES:
        fail("NX_TOO_MANY_OBJECTS", f"At most {MAX_CURVES} curves", field="/primary")
    seen: set[str] = set()
    for network in groups:
        _distinct(network, seen)
    with tx.checks():
        tol = common.given_tolerance(tx, tolerance)
        networks = [_sections(tx, network) for network in groups]
        rows = list(boundaries or [])
        constraint_faces = _constraint_faces(tx, rows, networks)
    F = tx.nx.Features
    kinds = tx.nx.GeometricUtilities.Continuity.ContinuityTypes
    try:
        with builders.build(tx, tx.part.Features.CreateThroughCurveMeshBuilder, None) as b:
            b.BodyPreference = F.ThroughCurveMeshBuilder.BodyPreferenceTypes.Sheet
            b.PositionTolerance = tol
            b.IntersectionTolerance = tol
            for sections, target in zip(
                networks, (b.PrimaryCurvesList, b.CrossCurvesList), strict=True
            ):
                for section in sections:
                    target.Append(builders.curve_section(tx, section))
            for row, faces in zip(rows, constraint_faces, strict=True):
                constraint = getattr(b, CONSTRAINTS[row["side"]][0])
                constraint.ContinuityType = getattr(kinds, row["continuity"].upper())
                if faces:
                    builders.fill(tx, constraint.ConstraintFaces, faces, "Face")
            feature = b.commit()
    except NXError as error:
        if error.code == "NX_BUILD_FAILED" and error.nx_code == MESH_MISS:
            raise _miss(tx, error, networks, tol) from None
        raise
    out = common.result_of(tx, feature)
    bodies = common.feature_bodies(feature)
    limit = 2 * tol + geom.tol(out["bounds"]["min"], out["bounds"]["max"])
    gaps: MeshGaps = {"primary": [], "cross": []}
    index = 0
    for name, sections in zip(("primary", "cross"), networks, strict=True):
        for section in sections:
            worst = 0.0
            for c in section:
                tx.checkpoint()
                gap = min(curve.min_distance(tx, c, body)[0] for body in bodies)
                readback.expect(gap <= limit, f"curve[{index}]", "The sheet misses a curve",
                                gap=gap)  # fmt: skip
                worst = max(worst, gap)
                index += 1
            gaps[name].append(worst)  # type: ignore[literal-required]
    deviation = max(gaps["primary"] + gaps["cross"])
    if tol > units.part_tolerance(tx, LOOSE_MM):
        tx.warn("NX_APPROXIMATED", f"tolerance {tol:.4g} lets the sheet miss its curves (largest "
                f"gap {deviation:.4g}): close the curves' gaps and use a finer tolerance")  # fmt: skip
    fits = _boundary_fits(tx, rows, constraint_faces, networks, bodies)
    for fit in fits:
        if fit["continuity"] != "g0":
            readback.expect(fit["angle"] is not None and fit["angle"] <= MESH_ANGLE,
                            f"{fit['side']}.g1", "The mesh is not tangent to the boundary faces",
                            angle=fit["angle"])  # fmt: skip
    return {**out, "tolerance": tol, "gaps": gaps, "deviation": deviation, "boundaries": fits}


# ---------------------------------------------------------------------------
# Bridge surface


class EndContinuity(TypedDict):
    gap: float
    angle: float | None
    curvature: float | None


class Continuity(TypedDict):
    first: EndContinuity
    second: EndContinuity


class BridgeData(common.SheetData):
    construction: str
    continuity: Continuity


def _adjacent(edge):
    return list(edge.GetFaces())


def _chosen_faces(tx, faces, given, kind) -> list:
    """The face each edge's bridge side follows when given (``None``: the edge's own)."""
    out: list = []
    for adjacent, ref, name in zip(faces, given, ("first", "second"), strict=True):
        if ref is None:
            out.append(None)
            continue
        if kind == "G0":
            fail("NX_INVALID_ARGUMENT", f"{name}_face applies to g1 and g2", field=f"/{name}_face")
        face = select.one(tx, ref, "face", field=f"/{name}_face")
        if all(int(face.Tag) != int(f.Tag) for f in adjacent):
            fail("NX_INVALID_ARGUMENT", f"{name}_face is not a face of the {name} edge",
                 field=f"/{name}_face")  # fmt: skip
        out.append(face)
    return out


def _native_bridge(tx, edges, kind, reverse_second):
    G = tx.nx.GeometricUtilities
    F = tx.nx.Features
    entered = False
    try:
        with builders.build(tx, tx.part.Features.CreateBridgeSurfaceBuilder, None) as b:
            entered = True
            b.FirstEndObjectType = F.BridgeSurfaceBuilder.EndObjectType.Edge
            b.SecondEndObjectType = F.BridgeSurfaceBuilder.EndObjectType.Edge
            b.FirstEdgeSelection.Value = edges[0]
            b.SecondEdgeSelection.Value = edges[1]
            b.FirstEdgeContinuity.ContinuityType = getattr(G.Continuity.ContinuityTypes, kind)
            b.SecondEdgeContinuity.ContinuityType = getattr(G.Continuity.ContinuityTypes, kind)
            b.IsSecondEdgeReversed = bool(reverse_second)
            b.IsFirstEdgeLimitEndToEnd = True
            b.IsSecondEdgeLimitEndToEnd = True
            return b.commit(validate=True)
    except NXError as error:
        if not entered and error.code == "NX_LICENSE_UNAVAILABLE":
            return None  # no Studio license: build Through Curves instead
        raise


def _through_curves(tx, edges, faces, kind, reverse_second):
    G = tx.nx.GeometricUtilities
    F = tx.nx.Features
    part = tx.part
    first_start = curve.ends(tx, edges[0])[0]
    ends = sorted(curve.ends(tx, edges[1]), key=lambda p: geom.dist(p, first_start))
    starts = [first_start, ends[1] if reverse_second else ends[0]]
    with builders.build(tx, part.Features.CreateThroughCurvesBuilder, None) as b:
        b.BodyPreference = F.ThroughCurvesBuilder.BodyPreferenceTypes.Sheet
        b.PositionTolerance = units.part_tolerance(tx)
        sections = [
            builders.curve_section(tx, [edge], start)
            for edge, start in zip(edges, starts, strict=True)
        ]
        for section in sections:
            b.SectionsList.Append(section)
        b.Alignment.SetSections(sections)
        for constraint, adjacent in (
            (b.FirstSectionContinuity, faces[0]),
            (b.LastSectionContinuity, faces[1]),
        ):
            constraint.ContinuityType = getattr(G.Continuity.ContinuityTypes, kind)
            if kind != "G0":
                rule = part.ScRuleFactory.CreateRuleFaceDumb(adjacent)
                constraint.ConstraintFaces.ReplaceRules([rule], False)
        try:
            return b.commit(validate=True)
        except NXError as error:
            if error.code == "NX_BUILD_FAILED" and not error.hint:
                error.hint = BRIDGE_HINT
            raise


def _bridge_edge(tx, edge, bridge_edges):
    """The bridge boundary edge lying on ``edge`` (nearest to its midpoint)."""
    middle = curve.point_at(tx, edge, 0.5)
    return min(bridge_edges, key=lambda e: curve.point_distance(tx, e, middle)[0])


def _measure(tx, edges, faces, body):
    """Sampled continuity between each input edge's face and the bridge sheet."""
    bridge_edges = list(body.GetEdges())
    readback.expect(bool(bridge_edges), "bridge_edges", "The bridge sheet has no edges")
    out = {}
    for name, edge, adjacent in zip(("first", "second"), edges, faces, strict=True):
        other = _bridge_edge(tx, edge, bridge_edges)
        own = adjacent[0] if len(adjacent) == 1 else None
        report = surface.edge_continuity(tx, edge, own, other, _adjacent(other)[0])
        out[name] = {
            "gap": report["gap"],
            "angle": report["angle"],
            "curvature": report["curvature"],
        }
    return out


def _verify(tx, continuity, kind):
    mm = units.MM_PER[units.part_units(tx)]
    gap_tol = GAP_TOLERANCE_MM / mm
    curvature_tol = CURVATURE_TOLERANCE_MM * mm
    for name, end in continuity.items():
        readback.expect(end["gap"] <= gap_tol, f"{name}.g0", "The bridge leaves a gap", **end)
        if kind in ("G1", "G2"):
            angle = end["angle"]
            readback.expect(
                angle is not None and angle <= ANGLE_TOLERANCE,
                f"{name}.g1",
                "The bridge is not tangent to the adjacent face",
                **end,
            )
        if kind == "G2":
            k = end["curvature"]
            readback.expect(
                k is not None and k <= curvature_tol,
                f"{name}.g2",
                "The bridge curvature differs from the adjacent face",
                **end,
            )


@tool(
    effect="model",
    label="Create bridge surface",
    summary="Bridge {first} and {second} ({continuity})",
    keywords=("bridge surface", "blend surface", "connect sheets", "g1 g2 continuity", "fill gap"),
    requires=("work_part", "no_active_sketch"),
    variants="continuity",
    cost="heavy",
    previewable=True,
    example={"first": "e:4", "second": "e:9", "continuity": "g1"},
)
def nx_create_bridge_surface(
    tx: Tx,
    *,
    first: EdgeRef,
    second: EdgeRef,
    continuity: Annotated[
        Literal["g0", "g1", "g2"], Doc("Position, tangent or curvature at both ends")
    ],
    reverse_second: Annotated[bool, Doc("Start the second edge at its other end")] = False,
    first_face: Annotated[FaceRef, Doc("G1/G2: the face of first to follow (solid edge)")]
    | None = None,
    second_face: Annotated[FaceRef, Doc("G1/G2: the face of second to follow (solid edge)")]
    | None = None,
) -> BridgeData:
    """Bridge two edges with a new sheet meeting their faces at G0, G1 or G2. G1/G2 follow
    a sheet boundary edge's face, or first_face/second_face (one of the edge's faces, e.g.
    on a solid). Tangent magnitude is NX's (1). Returns per-end gap, angle (deg) and
    curvature difference (1/unit; proves G2)."""
    if first == second:
        fail("NX_INVALID_ARGUMENT", "Select two different edges", field="/second")
    with tx.checks():
        edges = [select.one(tx, first, "edge", field="/first")]
        edges.append(select.one(tx, second, "edge", field="/second"))
        if int(edges[0].Tag) == int(edges[1].Tag):
            fail("NX_INVALID_ARGUMENT", "Select two different edges", field="/second")
        faces = [_adjacent(e) for e in edges]
        kind = continuity.upper()
        chosen = _chosen_faces(tx, faces, (first_face, second_face), kind)
        faces = [[c] if c is not None else f for c, f in zip(chosen, faces, strict=True)]
        if kind != "G0":
            for adjacent, field in zip(faces, ("/first", "/second"), strict=True):
                if len(adjacent) != 1:
                    fail(
                        "NX_WRONG_GEOMETRY",
                        "G1/G2 bridging needs a sheet boundary edge with one adjacent face",
                        field=field,
                        hint=f"Pass {field[1:]}_face: the face of the edge to follow",
                    )
    # A chosen face of a two-face edge is honoured by the Through Curves construction only.
    pick = any(c is not None and len(_adjacent(e)) > 1 for c, e in zip(chosen, edges, strict=True))
    feature = None if pick else _native_bridge(tx, edges, kind, reverse_second)
    construction = "bridge_surface"
    if feature is None:
        feature = _through_curves(tx, edges, faces, kind, reverse_second)
        construction = "through_curves"
    out = common.result_of(tx, feature)
    bodies = common.feature_bodies(feature)
    measured = _measure(tx, edges, faces, bodies[0])
    _verify(tx, measured, kind)
    return {**out, "construction": construction, "continuity": measured}
