"""Loft through sections (tool-surface 3.6): NX Through Curves, or Swept with guides.

NX 2406 lessons kept here:

- ``Section.SetStartAndDirection(element, point, direction)`` starts a loop of several
  curves at any of its vertices (``element`` is the curve leaving it), but a single closed
  curve (a circle) keeps its own start point: only its direction follows.
- Alignment ``Points`` (``AlignmentMethodBuilder`` after ``SetSections`` and
  ``ComputeDefaultPoints``) gives every section the same number of points, section after
  section in ``GetAllPoints``. A point switched to ``IsPercentUsed`` takes a percent of its
  loop from the section start, and the percents may wrap (62.5, 87.5, 12.5, 37.5): that
  moves a circle's correspondence to any start.
- Measured: a circle r 10 at z 0 to a square 20 at z 30, starts 135 deg apart, lofts to
  4607 mm^3 (twisted); with matched starts 10726 (moved start) or 10757 (shifted points).
- ``FirstSectionContinuity``/``LastSectionContinuity`` (``GeometricUtilities.Continuity``):
  G1 with ``ConstraintFaces`` (an ``ScCollector`` filled by rules) makes the loft tangent
  to faces at that end; ``NormalToEndSections`` leaves both end sections square.
- Through Curves and Swept have no Boolean option: combine through a Boolean feature.
- Swept takes several sections and 1-3 guides (``SectionList``, ``GuideList``) with the same
  ``AlignmentMethod``.
- A section is any ``Section``: a sketch (curve feature rule) or curves and edges (dumb
  rules, ``builders.curve_section``). A section of curves has no sketch frame: its plane
  is the loop's own (Newell normal, centroid), and an open one has none (the loft
  direction then runs from its centre to the next section's).
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal, cast

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Combine, Doc, Items, Point3, RefTo
from nx_mcp.domains.modeling import common
from nx_mcp.kit import args, builders, curve, geom, measure, readback, select, units
from nx_mcp.kit import sketch as ksketch

GuideRef = Annotated[str, RefTo("sketch", "curve", "edge")]
SectionRef = (
    Annotated[str, RefTo("sketch", "curve", "edge")]
    | Annotated[list[Annotated[str, RefTo("curve", "edge")]], Items(1, 50)]
)
# A solid loft below this share of its sections' trapezoid volume estimate is twisted (an
# untwisted ruled loft keeps at least 2/3 of it, a cone's share).
LOFT_TWIST = 0.5
SAMPLES = 16  # points per curve of a loop of several curves (and the area estimate)
SINGLE_SAMPLES = 360  # points along a single closed curve (its start shift)
SHIFT_MIN = 1e-4  # a start shift below this fraction of the loop is left alone


# ---------------------------------------------------------------------------
# Section loops (pure geometry on sampled points)


def chain(pieces, tol) -> list | None:
    """``[(curve, points)]`` chained head to tail into one closed loop: ``[(curve, forward,
    points in loop order)]``, or None for an open chain, several loops or a gap."""
    if not pieces:
        return None

    def meets(p, q):
        return geom.dist(p, q) <= max(tol, geom.tol(p, q))

    rest = list(pieces)
    first, points = rest.pop(0)
    loop = [(first, True, list(points))]
    while rest:
        end = loop[-1][2][-1]
        hits = [k for k, (_c, pts) in enumerate(rest) if meets(pts[0], end) or meets(pts[-1], end)]
        if not hits:
            return None
        c, pts = rest.pop(hits[0])
        forward = meets(pts[0], end)
        loop.append((c, forward, list(pts) if forward else list(pts)[::-1]))
    return loop if meets(loop[0][2][0], loop[-1][2][-1]) else None


def ring(loop) -> tuple[list, list[int]]:
    """``(points, vertex indices)`` of a chained loop without the closing repeat: the
    vertices are where its curves meet (index 0 only for a single closed curve)."""
    points: list = []
    vertices = []
    for _c, _forward, pts in loop:
        vertices.append(len(points))
        points += pts[:-1]
    return points, vertices


def polygon(points) -> tuple[list[float], list[float]]:
    """``(area vector, centroid)`` of a closed planar polygon (Newell; triangle fan)."""
    area = [0.0, 0.0, 0.0]
    for p, q in zip(points, points[1:] + points[:1], strict=True):
        area = geom.add(area, geom.scale(geom.cross(p, q), 0.5))
    n = geom.unit(area) if geom.norm(area) > 0 else [0.0, 0.0, 1.0]
    total, weighted = 0.0, [0.0, 0.0, 0.0]
    p0 = points[0]
    for p, q in zip(points[1:], points[2:], strict=False):
        a = geom.dot(geom.cross(geom.sub(p, p0), geom.sub(q, p0)), n) / 2
        total += a
        weighted = geom.add(weighted, geom.scale(geom.add(geom.add(p0, p), q), a / 3))
    centroid = geom.scale(weighted, 1 / total) if abs(total) > 1e-12 else points[0]
    return area, centroid


def _flat(v, n):
    """``v`` without its component along unit ``n``."""
    return geom.sub(v, geom.scale(n, geom.dot(v, n)))


def _travel(points, start, forward):
    """Indices of a ring visited from ``start`` on, forwards or backwards."""
    k = len(points)
    step = 1 if forward else -1
    return [(start + step * i) % k for i in range(k + 1)]


def plan(loops, axis, targets=None) -> list[dict]:
    """Where each closed loop starts and which way it runs so that a loft through them in
    order does not twist: every loop runs counter-clockwise about ``axis`` and starts at
    the same angle around its centroid as the anchor (the first loop with corners), or at
    the ``targets`` point given for it (a loop of several curves: its nearest vertex).

    Returns per loop ``{piece, start, direction, shift}``: ``piece`` indexes the loop
    curve to start on, ``shift`` the fraction of a single closed curve from its own start
    to the wanted one (NX cannot move that start; alignment points are shifted instead)."""
    rings = [ring(loop) for loop in loops]
    shapes = [polygon(points) for points, _v in rings]
    ccw = [geom.dot(area, axis) >= 0 for area, _c in shapes]
    normals = [geom.unit(axis) for _ in loops]
    anchor = next((i for i, (_p, v) in enumerate(rings) if len(v) > 1), 0)
    a_points, a_vertices = rings[anchor]
    a_target = None if targets is None else targets[anchor]
    a_best = 0 if a_target is None else _nearest(a_points, a_vertices, a_target)
    u = _flat(geom.sub(a_points[a_best], shapes[anchor][1]), normals[anchor])
    out = []
    for i, (loop, (points, vertices)) in enumerate(zip(loops, rings, strict=True)):
        centre, n = shapes[i][1], normals[i]
        candidates = vertices if len(vertices) > 1 else range(len(points))
        target = None if targets is None else targets[i]
        if target is not None:
            best = _nearest(points, vertices, target)
        elif i == anchor:
            best = a_best
        else:
            w = _flat(u, n)

            def angle(k, points=points, centre=centre, n=n, w=w):
                d = _flat(geom.sub(points[k], centre), n)
                size = geom.norm(d) * geom.norm(w)
                return geom.dot(d, w) / size if size > 0 else -2.0

            best = max(candidates, key=angle)
        out.append(_start(loop, points, vertices, best, ccw[i]))
    return out


def _nearest(points, vertices, target) -> int:
    """Index of the loop vertex (any point of a single closed curve) nearest ``target``."""
    candidates = vertices if len(vertices) > 1 else range(len(points))
    return min(candidates, key=lambda k: geom.dist(points[k], target))


def _start(loop, points, vertices, best, forward) -> dict:
    if len(vertices) > 1:
        j = vertices.index(best)
        if forward:
            _c, _f, pts = loop[j]
            direction = geom.sub(pts[1], pts[0])
        else:
            j = (j - 1) % len(loop)
            _c, _f, pts = loop[j]
            direction = geom.sub(pts[-2], pts[-1])
        return {"piece": j, "start": points[best], "direction": geom.unit(direction), "shift": 0.0}
    order = _travel(points, 0, forward)
    lengths = [geom.dist(points[a], points[b]) for a, b in zip(order, order[1:], strict=False)]
    total = sum(lengths) or 1.0
    walked = sum(lengths[: order.index(best)]) if best else 0.0
    direction = geom.sub(points[order[1]], points[0])
    return {"piece": 0, "start": points[0], "direction": geom.unit(direction),
            "shift": walked / total}  # fmt: skip


# ---------------------------------------------------------------------------
# NX side


class Section:
    """One loft section: a sketch, or curves and edges, with the plane ``frame``
    (``origin``, ``normal``; ``normal`` None for an open section of curves)."""

    def __init__(self, tx, curves, sketch=None):
        self.curves = list(curves)
        self.sketch = sketch
        self.frame = ksketch.frame(sketch) if sketch is not None else _curve_frame(tx, curves)

    def build(self, tx):
        if self.sketch is not None:
            return builders.section(tx, self.sketch)
        return builders.curve_section(tx, self.curves)


def _curve_frame(tx, curves) -> dict:
    """``{origin, normal}`` of a section of curves: its loop's plane (Newell normal and
    centroid), or the centre of its sample points and no normal when it is not one loop."""
    pieces = _pieces(tx, curves)
    loop = chain(pieces, units.part_tolerance(tx))
    if loop is not None:
        points, _vertices = ring(loop)
        if len(points) >= 3:
            area, centroid = polygon(points)
            if geom.norm(area) > 0:
                return {"origin": centroid, "normal": geom.unit(area)}
    samples = [q for _c, pts in pieces for q in pts] or [[0.0, 0.0, 0.0]]
    centre = geom.scale([sum(q[i] for q in samples) for i in range(3)], 1 / len(samples))
    return {"origin": centre, "normal": None}


def _section(tx, ref, field) -> Section:
    """A section from a sketch ref, a curve or edge ref, or a list of curve and edge refs."""
    if isinstance(ref, str):
        try:
            obj = select.resolve(tx, ref, "sketch", field=field)
        except NXError as error:
            if error.code != "NX_WRONG_KIND":
                raise
        else:
            sk = ksketch.resolve(tx, obj, field)
            return Section(tx, common.sketch_curves(sk), sk)
        refs, fields = [ref], [field]
    else:
        refs, fields = list(ref), [f"{field}/{k}" for k in range(len(ref))]
        args.distinct(field, refs, "curves")
    curves = [
        select.owned(tx, select.resolve(tx, r, "curve", "edge", field=f), f, "curves")
        for r, f in zip(refs, fields, strict=True)
    ]
    return Section(tx, curves)


def _pieces(tx, curves) -> list:
    curves = list(curves)
    n = SINGLE_SAMPLES if len(curves) == 1 else SAMPLES
    out = []
    for c in curves:
        try:
            with curve.evaluator(tx, c) as e:
                out.append((c, [e.at(i / n)[0] for i in range(n + 1)]))
        except Exception:  # a sketch point
            continue
    return out


def _loops(tx, sections) -> list:
    tol = units.part_tolerance(tx)
    return [chain(_pieces(tx, s.curves), tol) for s in sections]


def _axis(tx, sections) -> list[float]:
    """The loft direction: the first section's normal turned toward the second section
    (from the first section's centre to the second's when it has no normal)."""
    frames = [s.frame for s in sections]
    ahead = geom.sub(frames[1]["origin"], frames[0]["origin"])
    if frames[0]["normal"] is None:
        return geom.unit(ahead) if geom.norm(ahead) > 0 else [0.0, 0.0, 1.0]
    n = geom.unit(frames[0]["normal"])
    return geom.scale(n, -1.0) if geom.dot(ahead, n) < 0 else n


def _starts(tx, sections, loops, axis, targets) -> dict[int, float]:
    """Start and direct every closed loop section (``plan``); returns the start shifts of
    single closed curves that NX cannot start elsewhere (by section index)."""
    usable = [i for i, loop in enumerate(loops) if loop is not None]
    if len(usable) < 2:
        return {}
    picked = plan(
        [loops[i] for i in usable],
        axis,
        None if targets is None else [targets[i] for i in usable],
    )
    shifts: dict[int, float] = {}
    for i, p in zip(usable, picked, strict=True):
        element = loops[i][p["piece"]][0]
        # NX wants the exact tangent there ("Cannot set the direction" for a chord).
        foot, tangent = curve.closest(tx, element, p["start"])
        if geom.dot(tangent, p["direction"]) < 0:
            tangent = geom.scale(tangent, -1.0)
        with native(default="NX_BUILD_FAILED"):
            sections[i].SetStartAndDirection(
                element, tx.nx.Point3d(*foot), tx.nx.Vector3d(*geom.unit(tangent))
            )
        if SHIFT_MIN < p["shift"] < 1 - SHIFT_MIN:
            shifts[i] = p["shift"]
    return shifts


def _shift(tx, alignment, sections, shifts) -> None:
    """Alignment by points, each shifted section's points moved on by its shift (percent of
    its loop; the sections are in the builder's list already)."""
    kinds = tx.nx.GeometricUtilities.AlignmentMethodBuilder.Type
    alignment.SetSections(list(sections))
    alignment.AlignType = kinds.Points
    alignment.ComputeDefaultPoints()
    points, count = alignment.GetAllPoints()
    per = len(points) // max(1, int(count))
    for i, shift in shifts.items():
        for q in points[i * per : (i + 1) * per]:
            q.IsPercentUsed = True
            q.Expression.RightHandSide = repr((float(q.Expression.Value) + 100.0 * shift) % 100.0)


def _guide(tx, ref, field):
    try:
        obj = select.resolve(tx, ref, "sketch", field=field)
    except NXError as error:
        if error.code != "NX_WRONG_KIND":
            raise
        c = select.owned(tx, select.resolve(tx, ref, "curve", "edge", field=field), field, "curves")
        return lambda: builders.curve_section(tx, [c])
    sk = ksketch.resolve(tx, obj, field)
    return lambda: builders.section(tx, sk)


def _touches(tx, face, curves, tol) -> bool:
    return all(curve.min_distance(tx, face, c)[0] <= tol for c in curves)


def _behind(tx, body, f, axis, end) -> bool:
    """Whether ``body`` lies outside the loft at this end: behind the first section (against
    the loft direction) or beyond the last one (its bounding box centre)."""
    low, high = measure.bounds(tx, [body])
    centre = geom.scale(geom.add(low, high), 0.5)
    ahead = geom.dot(geom.sub(centre, f["origin"]), axis)
    return ahead < 0 if end == 0 else ahead > 0


def _tangent_ends(tx, sections, axis) -> dict[int, list]:
    """``{0: faces, 1: faces}``: for the first and last section, the faces of the work-part
    solids outside the loft at that end whose boundary holds all the section's curves,
    except faces in the section's plane (the body the loft continues from: its side
    faces). Neither end on a body is refused."""
    tol = max(10 * units.part_tolerance(tx), 1e-6)
    ends: dict[int, list] = {}
    solids = [b for b in tx.part.Bodies if getattr(b, "IsSolidBody", True)]
    for end, section in ((0, sections[0]), (1, sections[-1])):
        curves = section.curves
        f = section.frame
        normal = geom.unit(f["normal"]) if f["normal"] is not None else None
        found = []
        for body in solids:
            if not _behind(tx, body, f, axis, end):
                continue
            for face in body.GetFaces():
                kind, point, direction = select.face_data(tx, face)
                flat = (
                    normal is not None
                    and kind == select.FACE_PLANE
                    and geom.parallel(geom.unit(direction), normal, 1e-6)
                )
                if flat and abs(ksketch.height(f, point)) <= tol:
                    continue  # the cap the section lies in
                if curves and _touches(tx, face, curves, tol):
                    found.append(face)
        if found:
            ends[end] = found
    if not ends:
        fail(
            "NX_WRONG_GEOMETRY",
            "tangent 'faces': neither end section lies on the edges of a solid body",
            field="/tangent",
            hint="Draw the first or last section on the end of the body the loft continues "
            "from (e.g. a sketch on its end face, on its edge loop), or use 'normal'",
        )
    return ends


class LoftData(common.FeatureData, total=False):
    area: Annotated[float, Doc("Sheet loft: the sheets' total area")]
    start_points: Annotated[list[Point3], Doc("The section corners the loft started at")]


@tool(
    effect="model",
    label="Loft through sections",
    summary="Loft through {sections}",
    keywords=("loft", "through curves", "blend sections", "transition", "ruled", "guide curves"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"sections": ["s:3", "s:4"], "combine": {"operation": "unite", "targets": ["b:2"]}},
)
def nx_create_loft(
    tx: Tx,
    *,
    sections: Annotated[
        list[SectionRef],
        Items(2, 20),
        Doc("Each a sketch, a curve or edge (e.g. a closed spline), or a list of curves/edges"),
    ],
    solid: bool = True,
    start_points: Annotated[list[Point3], Items(2, 20)] | None = None,
    guides: Annotated[list[GuideRef], Items(1, 3)] | None = None,
    tangent: Annotated[
        Literal["faces", "normal"],
        Doc("faces: G1 to a body at an end; normal: square"),
    ]
    | None = None,
    combine: Combine | None = None,
) -> LoftData:
    """Loft a body through 2-20 sections in order (NX Through Curves; Swept along guides). A
    section is a sketch, a curve or edge, or a list of curves and edges forming one chain.
    Closed sections start at matching corners: no twist."""
    args.distinct("/sections", sections, "sections")
    common.check_one_target(combine, "loft")
    if start_points is not None and len(start_points) != len(sections):
        fail("NX_INVALID_ARGUMENT", "start_points needs one point per section",
             field="/start_points", hint="List a start corner for every section, in order")  # fmt: skip
    if guides is not None and tangent is not None:
        fail("NX_INVALID_ARGUMENT", "A loft along guides takes no end tangency",
             field="/tangent", hint="Drop guides or tangent")  # fmt: skip
    if combine is not None and not solid:
        fail("NX_INVALID_ARGUMENT", "A sheet loft cannot combine", field="/combine")
    with tx.checks():
        made_sections = [_section(tx, s, f"/sections/{i}") for i, s in enumerate(sections)]
        loops = _loops(tx, made_sections)
        targets = None
        if start_points is not None:
            targets = [args.vec3(p, f"/start_points/{i}") for i, p in enumerate(start_points)]
            for i, loop in enumerate(loops):
                if loop is None:
                    fail("NX_INVALID_ARGUMENT",
                         f"sections[{i}] is not one closed loop: it has no start to move",
                         field=f"/start_points/{i}")  # fmt: skip
        if solid:
            _refuse_open(tx, made_sections, loops)
        fills = [_guide(tx, g, f"/guides/{i}") for i, g in enumerate(guides or [])]
        axis = _axis(tx, made_sections)
        ends = _tangent_ends(tx, made_sections, axis) if tangent == "faces" else {}
        starts = _snapped(tx, loops, axis, targets) if targets is not None else None
        targets_body = common.one_target(tx, combine, "loft")
    before = measure.volume(tx, targets_body) if targets_body else 0.0
    swept = guides is not None
    factory = tx.part.Features.CreateSweptBuilder if swept else (
        tx.part.Features.CreateThroughCurvesBuilder)  # fmt: skip
    try:
        feature = _build(tx, factory, swept, solid, made_sections, loops, axis, targets, fills, ends,
                         tangent)  # fmt: skip
    except NXError as error:
        _align_hint(error, start_points is not None)
        raise
    bodies = common.feature_bodies(feature)
    readback.expect(bool(bodies), "bodies", "The loft built no body")
    common.sound(tx, bodies)
    readback.expect(
        all(bool(getattr(x, "IsSolidBody", solid)) == solid for x in bodies),
        "body_type",
        "NX built a different body type than requested",
    )
    if combine is not None:
        made = cast(LoftData, common.combine_new(tx, feature, bodies, combine, targets_body[0],
                                                 before))  # fmt: skip
    else:
        made = cast(LoftData, common.result_of(tx, feature))
        if solid:
            common.changed(0.0, made["volume"], "positive")
            _warn_twist(tx, made_sections, made["volume"])
        else:
            faces = [f for b in bodies for f in b.GetFaces()]
            made["area"] = measure.area(tx, faces) if faces else 0.0
            readback.expect(made["area"] > 0, "area", "NX built sheets of no area")
    if starts is not None:
        made["start_points"] = starts
    return made


def _open_ends(pieces, tol) -> int:
    """Curve ends of a section that meet no other end (an open chain has two)."""
    ends = [p for _c, pts in pieces for p in (pts[0], pts[-1])]
    free = 0
    for i, p in enumerate(ends):
        if not any(j != i and geom.dist(p, q) <= max(tol, geom.tol(p, q)) for j, q in
                   enumerate(ends)):  # fmt: skip
            free += 1
    return free


def _refuse_open(tx, sections, loops) -> None:
    """Refuse, before NX, an open section in a solid loft (NX builds a sheet instead)."""
    tol = units.part_tolerance(tx)
    for i, (section, loop) in enumerate(zip(sections, loops, strict=True)):
        if loop is not None:
            continue
        pieces = _pieces(tx, section.curves)
        if pieces and _open_ends(pieces, tol):
            fail(
                "NX_WRONG_GEOMETRY",
                f"sections[{i}] is open: a solid loft needs every section closed",
                field=f"/sections/{i}",
                hint="Close the section, or pass solid: false for a sheet loft",
            )


def _snapped(tx, loops, axis, targets) -> list:
    """The section points the loft starts at for ``start_points``; a point off its section
    is warned about (the loft starts at the section's nearest corner)."""
    picked = plan(loops, axis, targets)
    tol = max(10 * units.part_tolerance(tx), 1e-6)
    out = []
    for i, (p, target) in enumerate(zip(picked, targets, strict=True)):
        start = [round(float(v), 9) for v in p["start"]]
        gap = geom.dist(start, target)
        if gap > tol:
            tx.warn(
                "NX_APPROXIMATED",
                f"start_points[{i}] lies {gap:.6g} from its section: the loft starts at its "
                f"nearest corner [{', '.join(f'{v:.6g}' for v in start)}]",
            )
        out.append(start)
    return out


# NX 2406 refuses a loft whose section start points do not line up.
ALIGN_FAILED = 670024
ALIGN_HINT = (
    "The sections do not line up: pass start_points, one per section at matching corners "
    "(the same corner of each profile), so NX aligns them without a twist"
)


def _align_hint(error: NXError, given: bool) -> None:
    """Point NX's section alignment failure at ``start_points``."""
    if error.nx_code == ALIGN_FAILED and not error.hint:
        error.hint = (
            "Move start_points to matching corners of the sections (the same corner of each "
            "profile), in section order"
            if given
            else ALIGN_HINT
        )
        error.field = error.field or "/start_points"


def _build(tx, factory, swept, solid, made_sections, loops, axis, targets, fills, ends, tangent):
    """Configure and commit the Through Curves (or Swept, with guides) builder."""
    with builders.build(tx, factory, None) as b:
        if swept:
            b.G0Tolerance = units.part_tolerance(tx)
            b.G1Tolerance = 0.5
            listing, alignment = b.SectionList, b.AlignmentMethod
        else:
            prefer = b.BodyPreferenceTypes
            b.BodyPreference = prefer.Solid if solid else prefer.Sheet
            listing, alignment = b.SectionsList, b.Alignment
        made: list[Any] = []
        for section in made_sections:
            made.append(section.build(tx))
            tx.checkpoint()
        shifts = _starts(tx, made, loops, axis, targets)
        for section in made:
            listing.Append(section)
        if shifts:
            _shift(tx, alignment, made, shifts)
        for fill in fills:
            b.GuideList.Append(fill())
        g1 = tx.nx.GeometricUtilities.Continuity.ContinuityTypes.G1
        for end, faces in ends.items():
            continuity = b.FirstSectionContinuity if end == 0 else b.LastSectionContinuity
            continuity.ContinuityType = g1
            builders.fill(tx, continuity.ConstraintFaces, faces, "Face")
        if tangent == "normal":
            b.NormalToEndSections = True
        return b.commit()


def _warn_twist(tx, sections, volume) -> None:
    estimate = _loft_estimate(tx, sections)
    if estimate is None or volume >= LOFT_TWIST * estimate:
        return
    tx.warn(
        "NX_UPDATE_WARNING",
        f"The loft volume {volume:.6g} is under half of {estimate:.6g} estimated from the "
        "section areas: the loft twists. Undo it (nx_undo_operation) and "
        + "pass start_points at matching corners",
    )


# ---------------------------------------------------------------------------
# Volume estimate (twist warning)


def _section_area(tx, section):
    """Area enclosed by a section that is one closed loop, or None."""
    loop = chain(list(_sampled(tx, section.curves)), units.part_tolerance(tx))
    if loop is None:
        return None
    points, _vertices = ring(loop)
    if len(points) < 3:
        return None
    area, _centroid = polygon(points)
    return geom.norm(area)


def _sampled(tx, curves):
    for c in curves:
        try:
            with curve.evaluator(tx, c) as e:
                yield c, [e.at(i / SAMPLES)[0] for i in range(SAMPLES + 1)]
        except Exception:  # a sketch point
            continue


def _loft_estimate(tx, sections):
    """Trapezoid volume estimate of a loft through parallel one-loop sections, or None."""
    try:
        frames = [s.frame for s in sections]
        if any(f["normal"] is None for f in frames):
            return None
        normal = geom.unit(frames[0]["normal"])
        if not all(geom.parallel(geom.unit(f["normal"]), normal, 1e-6) for f in frames):
            return None
        areas = [_section_area(tx, s) for s in sections]
    except Exception:  # an estimate only: never fail a built loft over it
        return None
    if any(a is None or a <= 0 for a in areas):
        return None
    total = 0.0
    for i in range(len(sections) - 1):
        h = abs(geom.dot(geom.sub(frames[i + 1]["origin"], frames[i]["origin"]), normal))
        total += h * (areas[i] + areas[i + 1]) / 2
    return total if total > 0 and math.isfinite(total) else None
