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
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import CurveRef, Doc, EdgeRef, Items, Length
from nx_mcp.domains.freeform import common
from nx_mcp.kit import builders, curve, geom, readback, select, surface, units

Section = Annotated[list[CurveRef], Items(1, 100), Doc("Curves of one section, in order")]
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


def _distinct(groups, field, seen):
    for i, refs in enumerate(groups):
        for j, ref in enumerate(refs):
            if ref in seen:
                fail(
                    "NX_INVALID_ARGUMENT", f"Curve {ref} is listed twice", field=f"{field}/{i}/{j}"
                )
            seen.add(ref)


def _sections(tx, groups, field):
    return [
        [select.one(tx, ref, "curve", field=f"{field}/{i}/{j}") for j, ref in enumerate(refs)]
        for i, refs in enumerate(groups)
    ]


@tool(
    effect="model",
    label="Create mesh surface",
    summary=lambda a, say: f"Mesh surface {len(a['primary'])} x {len(a['cross'])} sections",
    keywords=("through curve mesh", "mesh surface", "curve network", "surface from curves"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"primary": [["c:1"], ["c:2"]], "cross": [["c:3"], ["c:4"]]},
)
def nx_create_mesh_surface(
    tx: Tx,
    *,
    primary: Annotated[list[Section], Items(2, 100)],
    cross: Annotated[list[Section], Items(2, 100)],
    tolerance: Annotated[Length, Doc("Position and intersection tolerance")] | None = None,
) -> common.SheetData:
    """Create a Through Curve Mesh sheet from primary and cross curve sections that form an
    intersecting network. Use nx_create_loft for sketch sections."""
    total = sum(len(s) for s in [*primary, *cross])
    if total > MAX_CURVES:
        fail("NX_TOO_MANY_OBJECTS", f"At most {MAX_CURVES} curves", field="/primary")
    seen: set[str] = set()
    _distinct(primary, "/primary", seen)
    _distinct(cross, "/cross", seen)
    with tx.checks():
        tol = common.given_tolerance(tx, tolerance)
        networks = [_sections(tx, primary, "/primary"), _sections(tx, cross, "/cross")]
    F = tx.nx.Features
    with builders.build(tx, tx.part.Features.CreateThroughCurveMeshBuilder, None) as b:
        b.BodyPreference = F.ThroughCurveMeshBuilder.BodyPreferenceTypes.Sheet
        b.PositionTolerance = tol
        b.IntersectionTolerance = tol
        for sections, target in zip(
            networks, (b.PrimaryCurvesList, b.CrossCurvesList), strict=True
        ):
            for section in sections:
                target.Append(builders.curve_section(tx, section))
        feature = b.commit()
    out = common.result_of(tx, feature)
    bodies = common.feature_bodies(feature)
    limit = 2 * tol + geom.tol(out["bounds"]["min"], out["bounds"]["max"])
    for i, c in enumerate(x for sections in networks for s in sections for x in s):
        gap = min(curve.min_distance(tx, c, body)[0] for body in bodies)
        readback.expect(gap <= limit, f"curve[{i}]", "The sheet misses a curve", gap=gap)
    return out


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
            return None  # no Studio license on this seat: build Through Curves instead
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
) -> BridgeData:
    """Bridge two sheet edges with a new sheet that meets their faces with G0, G1 or G2
    continuity. The measured gap, angle and curvature per end are returned; G1/G2 need sheet
    boundary edges."""
    if first == second:
        fail("NX_INVALID_ARGUMENT", "Select two different edges", field="/second")
    with tx.checks():
        edges = [select.one(tx, first, "edge", field="/first")]
        edges.append(select.one(tx, second, "edge", field="/second"))
        if int(edges[0].Tag) == int(edges[1].Tag):
            fail("NX_INVALID_ARGUMENT", "Select two different edges", field="/second")
        faces = [_adjacent(e) for e in edges]
        kind = continuity.upper()
        if kind != "G0":
            for adjacent, field in zip(faces, ("/first", "/second"), strict=True):
                if len(adjacent) != 1:
                    fail(
                        "NX_WRONG_GEOMETRY",
                        "G1/G2 bridging needs a sheet boundary edge with one adjacent face",
                        field=field,
                    )
    feature = _native_bridge(tx, edges, kind, reverse_second)
    construction = "bridge_surface"
    if feature is None:
        feature = _through_curves(tx, edges, faces, kind, reverse_second)
        construction = "through_curves"
    out = common.result_of(tx, feature)
    bodies = common.feature_bodies(feature)
    measured = _measure(tx, edges, faces, bodies[0])
    _verify(tx, measured, kind)
    return {**out, "construction": construction, "continuity": measured}
