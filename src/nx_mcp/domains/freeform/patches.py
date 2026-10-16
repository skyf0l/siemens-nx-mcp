"""N-sided fill patches and ruled surfaces between two section strings.

NX 2406 lessons kept here:

- ``CreateNSidedSurfaceBuilder`` and ``CreateRuledBuilder`` need the basic freeform
  license (``nx_freeform_1``); NX's refusal is reported naming it
  (``kit.licenses.attempt``); neither enters the Studio application.
- N-sided: the boundary goes into the builder's own ``OuterLoop`` section; ``Type``
  ``TrimmedPatch`` (one face trimmed to the boundary, ``TrimToBoundary``) or
  ``TriangularPatch`` (one face per boundary segment; a single closed circle gives
  two). Position, tangent and curvature tolerances default to 0 and are set. G1 sets
  ``Continuity.ContinuityType`` and fills ``ConstraintFaces``: the triangular type
  then bulges tangent to the faces (a circle on a vertical tube: a dome), while the
  trimmed type stays flat whatever the options (TrimToBoundary, UV orientation, flow
  direction, either face collector: probed), so G1 needs the triangular type.
  The triangular G1 dome on a single closed circle r 20 lies 0.0239 mm off the
  circle whatever ``PositionTolerance`` (0.05, 0.01, 0.001 probed); the gap read-back
  limit is 2 x tolerance + 0.01 mm, so such a patch needs tolerance 0.012 or more (the
  refusal says so). Straight 4-edge boundaries fit exactly; two half circles fit to
  0.0011 mm. The G1 cap's bulge is NX's choice and not stable: the same rim gave
  z 24.8..30, 26.4..39.8 (tolerance 0.01) and 2.5..46 (0.001) in different runs, all
  tangent within 0.5 deg: a patch reaching over half its boundary span outside the
  boundary box gets a warning.
- Ruled: assigning new sections to ``FirstSection``/``SecondSection`` leaves the
  builder empty ("No profile selected", 671434): the builder's own sections are
  filled. The second starts at its end nearest the first section's start (an
  untwisted ruling), ``reverse_second`` at the other end (crossing rulings between two
  coplanar arcs: NX refuses, 670178). ``BodyPreference`` is a ``FeatureOptions``
  (``BodyType`` = ``FeatureOptions.BodyStyle.Sheet``/``Solid``). ``IsShapePreserved``
  (the default) allows only Parameter alignment: arc-length alignment turns it off and
  approximates (a quarter annulus: 235.6177 instead of 235.6194). Two circles make a
  B-surface tube (area 3769.804 for 3769.911).
- A native refusal is ``NX_BUILD_FAILED`` with ``nx_code``.
"""

from __future__ import annotations

import contextlib
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, FaceRef, Items, Length, RefTo
from nx_mcp.domains.freeform import common, surface_loops
from nx_mcp.kit import bodies as kbodies
from nx_mcp.kit import (
    builders,
    curve,
    facts,
    geom,
    measure,
    readback,
    result,
    select,
    surface,
    units,
)

StringRef = Annotated[str, RefTo("curve", "edge", doc="Curve or edge 'c:7'/'e:21'")]
MAX_SECTION = 100
Section = Annotated[list[StringRef], Items(1, MAX_SECTION)] | StringRef
SAMPLES = 9  # points per boundary or section curve
ANGLE_TOLERANCE = 0.5  # degrees: N-sided G1 read-back (NX's default tangent tolerance)


def _distinct(refs, field):
    seen: set[str] = set()
    for i, ref in enumerate(refs):
        if ref in seen:
            fail("NX_INVALID_ARGUMENT", f"{ref} is listed twice", field=f"{field}/{i}")
        seen.add(ref)


def _objects(tx, refs, field):
    return [
        select.one(tx, ref, "curve", "edge", field=f"{field}/{i}") for i, ref in enumerate(refs)
    ]


def _chain(tx, objs, tol):
    """``(walk, closed, worst)``: the curves walked end to end as ``[(curve, start, end)]``
    from the first curve (greedy: the nearest free end next; the first curve turns when
    its start meets the next curve); ``worst`` is the largest step gap (two separate
    chains give a large one) and ``closed`` whether the walk ends where it starts."""
    ends = [list(curve.ends(tx, c)) for c in objs]
    if len(objs) > 1:
        a, b = ends[0]
        nearest = [min(geom.dist(p, q) for k in range(1, len(objs)) for q in ends[k])
                   for p in (a, b)]  # fmt: skip
        if nearest[0] < nearest[1]:
            ends[0] = [b, a]
    rest = list(range(1, len(objs)))
    walk = [(objs[0], *ends[0])]
    worst = 0.0
    while rest:
        here = walk[-1][2]
        gap, k, s = min(
            ((geom.dist(here, ends[k][s]), k, s) for k in rest for s in (0, 1)),
            key=lambda x: x[0],
        )
        worst = max(worst, gap)
        rest.remove(k)
        start, end = ends[k] if s == 0 else ends[k][::-1]
        walk.append((objs[k], start, end))
    closing = geom.dist(walk[-1][2], walk[0][1])
    return walk, closing <= tol, worst


def _loose(tol):
    """Chaining tolerance: 0.001 mm in part units with a floor for round-off."""
    return max(10 * tol, 1e-6)


def _gap_to(tx, objs, bodies):
    """Largest distance from ``SAMPLES`` points of each curve to the nearest body."""
    worst = 0.0
    for c in objs:
        tx.checkpoint()
        with curve.evaluator(tx, c) as e:
            points = [e.at((i + 0.5) / SAMPLES)[0] for i in range(SAMPLES)]
        for p in points:
            worst = max(worst, min(curve.point_distance(tx, b, p)[0] for b in bodies))
    return worst


# ---------------------------------------------------------------------------
# N-sided surface


class BoundaryFit(TypedDict):
    boundary: str  # the boundary curve or edge ref
    gap: float  # largest sampled distance from it to the patch
    edge: result.Row | None  # the patch edge on it
    angle: float | None  # G1: largest normal angle to its adjacent face, degrees


class NSidedData(common.SheetData):
    type: Literal["trimmed", "triangular"]
    continuity: Literal["g0", "g1"]
    gap: float  # largest boundary gap
    boundary: list[BoundaryFit]


def _adjacent(tx, objs, refs, faces, kind):
    """The faces the patch is G1 to: ``faces`` when given, else the one face of each
    boundary edge (``NX_WRONG_GEOMETRY`` for a curve or a non-boundary edge)."""
    if kind == "g0":
        return []
    if faces is not None:
        return select.faces(tx, faces, "/faces")
    out = []
    for i, obj in enumerate(objs):
        adjacent = list(obj.GetFaces()) if facts.is_edge(obj) else []
        if len(adjacent) != 1:
            fail(
                "NX_WRONG_GEOMETRY",
                f"{refs[i]}: G1 needs sheet boundary edges (one adjacent face) or faces",
                field=f"/boundary/{i}",
                hint="Pass faces: the faces the patch must be tangent to",
            )
        if int(adjacent[0].Tag) not in {int(f.Tag) for f in out}:
            out.append(adjacent[0])
    return out


def _fits(tx, objs, refs, bodies, adjacent, limit) -> list[BoundaryFit]:
    """Per boundary curve: sampled gap to the patch, the patch edge on it and (G1) the
    largest normal angle between the patch and the boundary face next to it.

    The patch edges on a boundary curve are those whose midpoint lies on it (a
    triangular patch splits one closed curve into several edges and has interior edges
    meeting the boundary at vertices); G1 samples each of them onto the curve."""
    middles = [(e, curve.point_at(tx, e, 0.5)) for b in bodies for e in b.GetEdges()]
    out: list[BoundaryFit] = []
    for obj, ref in zip(objs, refs, strict=True):
        tx.checkpoint()
        gap = _gap_to(tx, [obj], bodies)
        fit: BoundaryFit = {"boundary": ref, "gap": gap, "edge": None, "angle": None}
        ranked = sorted(
            (curve.point_distance(tx, obj, m)[0], i) for i, (_e, m) in enumerate(middles)
        )
        on = [middles[i] for d, i in ranked if d <= 2 * gap + limit]
        if on:
            fit["edge"] = tx.ref(on[0][0], kind="edge")
        if on and adjacent:
            own = list(obj.GetFaces()) if facts.is_edge(obj) else []
            if len(own) != 1:  # a curve: the given G1 face nearest it
                middle = curve.point_at(tx, obj, 0.5)
                own = [min(adjacent, key=lambda f: curve.point_distance(tx, f, middle)[0])]
            angles = []
            for patch_edge, _m in on:
                patch_face = list(patch_edge.GetFaces())[0]
                report = surface.edge_continuity(tx, patch_edge, patch_face, obj, own[0])
                if report["angle"] is not None:
                    angles.append(report["angle"])
            fit["angle"] = max(angles) if angles else None
        out.append(fit)
    return out


@tool(
    effect="model",
    label="Create N-sided surface",
    summary="N-sided {type} patch on {boundary}",
    keywords=(
        "n-sided surface",
        "fill hole",
        "patch",
        "close corner",
        "fill boundary",
        "cap opening",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    variants="type",
    cost="heavy",
    previewable=True,
    example={"boundary": ["e:4", "e:5", "e:6", "e:7"], "type": "trimmed", "continuity": "g1"},
)
def nx_create_n_sided_surface(
    tx: Tx,
    *,
    boundary: Annotated[list[StringRef], Items(1, 100), Doc("One closed loop of curves/edges")],
    type: Annotated[
        Literal["trimmed", "triangular"],
        Doc("trimmed: one face (G0); triangular: one face per side (G0 or G1)"),
    ] = "trimmed",
    continuity: Annotated[Literal["g0", "g1"], Doc("g1: tangent to the boundary faces")] = "g0",
    faces: Annotated[list[FaceRef], Items(1, 100), Doc("G1 faces; default: edges' faces")]
    | None = None,
    tolerance: Annotated[Length, Doc("Position tolerance")] | None = None,
) -> NSidedData:
    """Fill a closed loop of curves or sheet edges with an N-sided sheet, G0 or (triangular)
    G1 to the boundary faces. Returns each side's gap, patch edge and G1 angle (for
    nx_check_continuity)."""
    _distinct(boundary, "/boundary")
    if continuity == "g1" and type == "trimmed":
        fail(
            "NX_INVALID_ARGUMENT",
            "NX builds the trimmed N-sided type without tangency: G1 needs type triangular",
            field="/continuity",
            hint='Send type "triangular" for a patch tangent to the boundary faces',
        )
    if faces is not None and continuity == "g0":
        fail("NX_INVALID_ARGUMENT", "faces are the G1 faces: send continuity g1",
             field="/faces")  # fmt: skip
    with tx.checks():
        tol = common.given_tolerance(tx, tolerance)
        objs = _objects(tx, boundary, "/boundary")
        tags = [int(o.Tag) for o in objs]
        if len(set(tags)) != len(tags):
            fail("NX_INVALID_ARGUMENT", "A boundary curve is listed twice", field="/boundary")
        loops = surface_loops.loops_of(tx, objs, _loose(tol))  # open_at, to, gap if open
        if len(loops) != 1:
            fail("NX_WRONG_GEOMETRY", f"The boundary forms {len(loops)} closed loops: fill one "
                 "opening per call", field="/boundary",
                 hint="List every curve or edge around one opening, end to end")  # fmt: skip
        adjacent = _adjacent(tx, objs, boundary, faces, continuity)
    F = tx.nx.Features
    G = tx.nx.GeometricUtilities
    with (
        common.licensed(tx),
        builders.build(tx, tx.part.Features.CreateNSidedSurfaceBuilder, None) as b,
    ):
        types = F.NSidedSurfaceBuilder.SurfaceType
        b.Type = types.TrimmedPatch if type == "trimmed" else types.TriangularPatch
        b.PositionTolerance = tol
        b.TangentTolerance = ANGLE_TOLERANCE
        b.CurvatureTolerance = ANGLE_TOLERANCE
        if type == "trimmed":
            b.TrimToBoundary = True
        builders.curve_section(tx, objs, into=b.OuterLoop)
        if adjacent:
            b.Continuity.ContinuityType = G.Continuity.ContinuityTypes.G1
            builders.fill(tx, b.ConstraintFaces, adjacent, "Face")
        feature = b.commit()
    bodies = common.feature_bodies(feature)
    out = common.result_of(tx, feature, bodies)
    kbodies.expect_sound(tx, bodies)
    limit = 2 * tol + 10 * units.part_tolerance(tx)
    fits = _fits(tx, objs, boundary, bodies, adjacent, limit)
    gap = max(f["gap"] for f in fits)
    if gap > limit:
        fail(
            "NX_VERIFICATION_FAILED",
            f"The patch is {gap:.3g} from the boundary (limit {limit:.3g}); rolled back",
            hint=f"NX fits this patch {gap:.3g} off the boundary whatever its position "
            f"tolerance: send tolerance {gap / 2:.3g} or more to accept it",
            details={"check": "boundary", "actual": {"gap": gap}},
        )
    if continuity == "g1":
        angles = [f["angle"] for f in fits if f["angle"] is not None]
        readback.expect(bool(angles), "g1", "No boundary side could be checked for G1")
        readback.expect(max(angles) <= ANGLE_TOLERANCE, "g1",
                        "The patch is not tangent to the boundary faces", angle=max(angles))  # fmt: skip
    _warn_bulge(tx, objs, out["bounds"])
    return {**out, "type": type, "continuity": continuity, "gap": gap, "boundary": fits}


BULGE = 0.5  # a patch reaching this share of its boundary's span outside its box is odd


def _warn_bulge(tx, objs, bounds) -> None:
    """Warn when the patch reaches far outside its boundary's box (NX 2406 built a
    tangent but wild G1 cap, z 2.5..46 on a 40 mm opening at z 30, at a 0.001 mm
    tolerance and a shallow one at 0.01)."""
    points = []
    for c in objs:
        with curve.evaluator(tx, c) as e:
            points += [e.at(i / SAMPLES)[0] for i in range(SAMPLES + 1)]
    low = [min(p[k] for p in points) for k in range(3)]
    high = [max(p[k] for p in points) for k in range(3)]
    span = max(h - lo for lo, h in zip(low, high, strict=True))
    out = max(
        max(lo - p, p - h, 0.0)
        for corner in (bounds["min"], bounds["max"])
        for p, lo, h in zip(corner, low, high, strict=True)
    )
    if span > 0 and out > BULGE * span:
        tx.warn(
            "NX_UPDATE_WARNING",
            f"The patch reaches {out:.4g} outside its boundary (span {span:.4g}): check its "
            "shape (nx_capture_view); another tolerance or more boundary edges change it",
        )


# ---------------------------------------------------------------------------
# Ruled


class SectionGaps(TypedDict):
    first: float
    second: float


class RuledData(common.SheetData):
    volume: float | None  # solid only
    alignment: Literal["parameter", "arc_length"]
    gaps: SectionGaps  # largest sampled distance from each section to the result


def _refs(raw):
    return [raw] if isinstance(raw, str) else list(raw)


def _start(tx, objs, tol, field):
    """``(walk, closed)`` of one section (``NX_WRONG_GEOMETRY`` unless one chain)."""
    walk, closed, worst = _chain(tx, objs, tol)
    if worst > tol:
        fail("NX_WRONG_GEOMETRY", "The section curves do not form one chain", field=field,
             hint="List curves that meet end to end", details={"largest_gap": worst})  # fmt: skip
    return walk, closed


RULED_HINT = (
    "The rulings cross: the sections run in opposite directions or their starts do not line "
    "up; toggle reverse_second, or check that closed sections run the same way round"
)
# NX 2406 "Self-intersecting geometry detected.", crossing rulings between two arcs and
# "Cannot create B-surface body by ruled method." (two lines, the second reversed).
CROSSING = frozenset({875316, 670178, 670982})


@contextlib.contextmanager
def _ruled_hint():
    """A self-intersecting ruled result (refused by NX, or an unsound body) gets the
    ruled-surface hint instead of a generic one."""
    try:
        yield
    except NXError as error:
        crossing = error.nx_code in CROSSING or "self-intersect" in error.message.lower()
        if error.code == "NX_BUILD_FAILED" and (crossing or "faults" in error.details):
            error.hint = RULED_HINT
        raise


def _turn(section, objs, tx):
    """The turning sense of a closed section's loop: ``direction x (centre - start)`` at
    its start, NX's own start and direction (``GetStartAndDirectionOfLoop``)."""
    start, direction = section.GetStartAndDirectionOfLoop(0)
    points = [p for c in objs for p in surface_loops.curve_points(tx, c)]
    centre = [sum(p[k] for p in points) / len(points) for k in range(3)]
    return geom.cross(geom.xyz(direction), geom.sub(centre, geom.xyz(start)))


def _align_loops(tx, first, second, objs, reverse) -> None:
    """Make two closed sections run the same way round (``reverse``: the opposite way).
    NX 2406 keeps each loop's own sense: the top and bottom circles of a cylinder ran
    opposite ways and every ruled sheet or solid between them self-intersected (875316),
    whatever the start point."""
    same = geom.dot(_turn(first, objs[0], tx), _turn(second, objs[1], tx)) >= 0
    if same == bool(reverse):
        second.ReverseDirectionOfLoop(0)


def _second_start(walk, closed, start, reverse):
    """``(seed curve, start point)`` of the second section: its end (open) or vertex
    (closed) nearest ``start``; ``reverse``: the far end of an open section."""
    if closed:
        candidates = [(c, s) for c, s, _e in walk]
    else:
        candidates = [(walk[0][0], walk[0][1]), (walk[-1][0], walk[-1][2])]
    candidates.sort(key=lambda x: geom.dist(x[1], start))
    return candidates[-1] if reverse and not closed else candidates[0]


@tool(
    effect="model",
    label="Create ruled surface",
    summary="Ruled {first} to {second}",
    keywords=(
        "ruled surface",
        "ruled solid",
        "straight-line surface",
        "surface between curves",
        "developable",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    variants="alignment",
    cost="heavy",
    previewable=True,
    example={"first": "c:3", "second": ["c:4", "c:5"], "alignment": "parameter"},
)
def nx_create_ruled(
    tx: Tx,
    *,
    first: Section,
    second: Section,
    alignment: Annotated[
        Literal["parameter", "arc_length"], Doc("How points along the two sections pair up")
    ] = "parameter",
    solid: Annotated[bool, Doc("Closed sections: a solid instead of a sheet")] = False,
    reverse_second: Annotated[
        bool, Doc("Open: start the second at its other end; closed: run it the other way round")
    ] = False,
    tolerance: Annotated[Length, Doc("Position tolerance")] | None = None,
) -> RuledData:
    """Create a ruled sheet or solid of straight lines between two section strings.
    Each section: a curve/edge ref or a chain of refs. Returns area, volume (solid) and
    each section's gap to the result."""
    sections = [_refs(first), _refs(second)]
    _distinct(sections[0], "/first")
    _distinct(sections[1], "/second")
    for i, ref in enumerate(sections[1]):
        if ref in sections[0]:
            fail("NX_INVALID_ARGUMENT", f"{ref} is in both sections", field=f"/second/{i}")
    with tx.checks():
        tol = common.given_tolerance(tx, tolerance)
        objs = [_objects(tx, refs, f) for refs, f in zip(sections, ("/first", "/second"),
                                                          strict=True)]  # fmt: skip
        tags = [int(o.Tag) for group in objs for o in group]
        if len(set(tags)) != len(tags):
            fail("NX_INVALID_ARGUMENT", "A curve is in both sections", field="/second")
        walks = [_start(tx, group, _loose(tol), f)
                 for group, f in zip(objs, ("/first", "/second"), strict=True)]  # fmt: skip
        if solid and not (walks[0][1] and walks[1][1]):
            fail("NX_INVALID_ARGUMENT", "A ruled solid needs two closed sections",
                 field="/solid", hint="Send solid false for a ruled sheet")  # fmt: skip
    first_walk, _closed = walks[0]
    seed0, start0 = first_walk[0][0], first_walk[0][1]
    second_walk, second_closed = walks[1]
    seed1, start1 = _second_start(second_walk, second_closed, start0, reverse_second)
    order = [
        [seed0, *[o for o in objs[0] if o is not seed0]],
        [seed1, *[o for o in objs[1] if o is not seed1]],
    ]
    G = tx.nx.GeometricUtilities
    with (
        common.licensed(tx),
        builders.build(tx, tx.part.Features.CreateRuledBuilder, None) as b,
    ):
        builders.curve_section(tx, order[0], start0, into=b.FirstSection)
        builders.curve_section(tx, order[1], start1, into=b.SecondSection)
        if walks[0][1] and second_closed:
            _align_loops(tx, b.FirstSection, b.SecondSection, objs, reverse_second)
        b.PositionTolerance = tol
        kinds = G.AlignmentMethodBuilder.Type
        if alignment == "arc_length":
            b.IsShapePreserved = False
        b.AlignmentMethod.AlignType = kinds.Parameter if alignment == "parameter" else (
            kinds.ArcLength)  # fmt: skip
        style = G.FeatureOptions.BodyStyle
        b.BodyPreference.BodyType = style.Solid if solid else style.Sheet
        with _ruled_hint():
            feature = b.commit()
    bodies = common.feature_bodies(feature)
    out = common.result_of(tx, feature, bodies)
    with _ruled_hint():
        kbodies.expect_sound(tx, bodies)
    readback.expect(
        all(common.is_sheet(x) != solid for x in bodies),
        "body_type",
        "NX built a different body type than requested",
    )
    volume = measure.volume(tx, bodies) if solid else None
    if volume is not None:
        readback.expect(volume > 0, "volume", "The ruled solid has no volume", volume=volume)
    limit = 2 * tol + 10 * units.part_tolerance(tx)
    measured = [_gap_to(tx, group, bodies) for group in objs]
    for name, gap in zip(("first", "second"), measured, strict=True):
        readback.expect(gap <= limit, f"{name}_section", "The ruled body misses a section",
                        gap=gap)  # fmt: skip
    gaps: SectionGaps = {"first": measured[0], "second": measured[1]}
    return {**out, "volume": volume, "alignment": alignment, "gaps": gaps}
