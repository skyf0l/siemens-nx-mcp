"""Sketch corner, copy and projection tools (tool-surface 3.5).

NX 2406 findings kept here:

- ``Sketch.Fillet`` with trim silently *extends* the inputs when the radius is
  too large for the corner; both arc ends must lie on the original curves.
- ``SketchChamferBuilder`` assigns Distance1 to a line by its own rule, not by
  selection order: an attempt with swapped distances is undone (inner invisible
  mark) and repeated with the values exchanged.
- ``SketchOffsetBuilder.Tolerance`` defaults to 0.0: lines and arcs offset
  exactly, ellipses and splines fail with "Cannot approximate offset curve"
  (1720072) unless a tolerance is set before ``UpdateLoopsAndCopies()``.
  ``ReverseOffsetDirection()`` raises "Sketch is not initialized";
  ``ReverseOffsetDirectionOfChain(curve)`` works.
- A mirror line must be a line of this sketch or a datum axis in the sketch
  plane (another sketch's line would create an undocumented external reference).
- ``SketchProjectBuilder`` raises "Invalid data" unless ``Tolerance`` and the
  section tolerances are set; an edge normal to the plane is refused (674275).
  A face that misses the plane is refused by the intersection builder (3680011).
- Builder factories need typed nulls (``SketchOffset.Null``...), not ``None``.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import CurveRef, Doc, Items, Length, RefTo, SketchRef
from nx_mcp.domains.sketch import common
from nx_mcp.domains.sketch.common import CurveRow
from nx_mcp.kit import args, builders, geom, readback, result, select, units
from nx_mcp.kit import curve as kcurve
from nx_mcp.kit import sketch as ksketch

OFFSET_TOLERANCE_MM = 0.001


def _rule_section(tx, section, curves):
    part, nx = tx.part, tx.nx
    rule = part.ScRuleFactory.CreateRuleCurveDumb(list(curves))
    section.AddToSection(
        [rule], curves[0], None, None, nx.Point3d(0.0, 0.0, 0.0), nx.Section.Mode.Create, False
    )


def _corner(tx, a, b):
    """Closest end pair of two curves: ``(t of a, t of b)`` with t 0 = start, 1 = end."""
    best = None
    ends_a, ends_b = kcurve.ends(tx, a), kcurve.ends(tx, b)
    for ta, pa in zip((0.0, 1.0), ends_a, strict=True):
        for tb, pb in zip((0.0, 1.0), ends_b, strict=True):
            gap = geom.dist(pa, pb)
            if best is None or gap < best[2]:
                best = (ta, tb, gap)
    return best[0], best[1]


def _help_point(tx, curve, t_end, fraction=0.25):
    """A point on ``curve`` a fraction of its parameter range in from end ``t_end``."""
    return kcurve.point_at(tx, curve, fraction if t_end == 0.0 else 1.0 - fraction)


# ---------------------------------------------------------------------------
# Corner: fillet or chamfer


class CornerData(TypedDict, total=False):
    sketch: result.Row
    corner: CurveRow
    trimmed: list[CurveRow]
    distances: list[float]


def _line_corner(tx, sk, a, b):
    """``(corner, room_a, room_b, angle)`` of two sketch lines: their intersection, the length
    of each from it to its far end, and the angle between them (radians); None if parallel."""
    ta, tb = _corner(tx, a, b)
    f = sk.frame
    far_a = ksketch.to_local(f, kcurve.point_at(tx, a, 1.0 - ta))
    far_b = ksketch.to_local(f, kcurve.point_at(tx, b, 1.0 - tb))
    near_a = ksketch.to_local(f, kcurve.point_at(tx, a, ta))
    near_b = ksketch.to_local(f, kcurve.point_at(tx, b, tb))
    u, v = geom.sub(far_a, near_a), geom.sub(far_b, near_b)
    cross = geom.cross2(u, v)
    if abs(cross) <= 1e-9 * geom.norm(u) * geom.norm(v):
        return None
    t = geom.cross2(geom.sub(near_b, near_a), v) / cross
    corner = geom.add(near_a, geom.scale(u, t))
    da, db = geom.sub(far_a, corner), geom.sub(far_b, corner)
    room_a, room_b = geom.norm(da), geom.norm(db)
    if min(room_a, room_b) <= 1e-12:
        return None
    angle = math.acos(max(-1.0, min(1.0, geom.dot(da, db) / (room_a * room_b))))
    return corner, room_a, room_b, angle


def _too_large(message, field, **details):
    fail(
        "NX_INVALID_ARGUMENT",
        message,
        field=field,
        hint="Use a smaller value, or lengthen the curves",
        details=details,
    )


def _fillet(tx, sk, a, b, radius):
    nx = tx.nx
    if any(ksketch.curve_kind(tx, c) not in ("line", "arc") for c in (a, b)):
        fail("NX_WRONG_GEOMETRY", "A fillet joins sketch lines and arcs", field="/first")
    lines = all(ksketch.curve_kind(tx, c) == "line" for c in (a, b))
    corner = _line_corner(tx, sk, a, b) if lines else None
    if lines and corner is None:
        fail("NX_WRONG_GEOMETRY", "The lines are parallel: they make no corner", field="/second")
    if corner is not None:
        # The arc touches each line r / tan(angle / 2) from the corner.
        biggest = min(corner[1], corner[2]) * math.tan(corner[3] / 2)
        if radius > biggest * (1 + 1e-9):
            _too_large(
                f"radius {radius:g} is too large for this corner: at most {biggest:.6g}",
                "/radius",
                max_radius=biggest,
            )
    ta, tb = _corner(tx, a, b)
    originals = [ksketch.read(tx, sk.frame, c) for c in (a, b)]
    with native(default="NX_BUILD_FAILED"):
        arcs, _constraints = sk.sketch.Fillet(
            a,
            b,
            nx.Point3d(*_help_point(tx, a, ta)),
            nx.Point3d(*_help_point(tx, b, tb)),
            float(radius),
            nx.Sketch.TrimInputOption.TrueValue,
            nx.Sketch.CreateDimensionOption.FalseValue,
            nx.Sketch.AlternateSolutionOption.FalseValue,
        )
        sk.sketch.Update()
    arcs = list(arcs)
    readback.expect(len(arcs) == 1, "fillet_count", count=len(arcs))
    row = common.curve_row(tx, sk.frame, arcs[0])
    tol = geom.tol(row["center"], [radius, radius])
    readback.expect_close(row["radius"], radius, "fillet_radius", tol)
    # Tangency: each trimmed input touches the arc at one of its ends.
    arc_ends = kcurve.ends(tx, arcs[0])
    gaps = [min(geom.dist(e, f) for e in kcurve.ends(tx, c) for f in arc_ends) for c in (a, b)]
    readback.expect(max(gaps) <= 1e-4, "fillet_touches_curves", gaps=gaps)
    # Trim only: both arc ends lie on the ORIGINAL curves (NX would extend them).
    ends = [row["start"], row["end"]]
    within = [
        any(common.contains(o, p, 1e-6 * max(1.0, radius, *map(abs, p))) for p in ends)
        for o in originals
    ]
    if not all(within):
        _too_large(
            f"radius {radius:g} is too large for this corner (NX would extend the curves)",
            "/radius",
            check="fillet_within_curves",
            within_original_curves=within,
        )
    return {"corner": row}


def _chamfer(tx, sk, a, b, d1, d2):
    nx, part, session = tx.nx, tx.part, tx.session
    if any(ksketch.curve_kind(tx, c) != "line" for c in (a, b)):
        fail("NX_WRONG_GEOMETRY", "A sketch chamfer joins two lines", field="/first")
    meet = _line_corner(tx, sk, a, b)
    if meet is None:
        fail("NX_WRONG_GEOMETRY", "The lines are parallel: they make no corner", field="/second")
    for d, room, field in ((d1, meet[1], "/distance"), (d2, meet[2], "/distance2")):
        if d > room * (1 + 1e-9):
            _too_large(
                f"{field[1:]} {d:g} is longer than the line from the corner ({room:.6g})",
                field,
                max_distance=room,
            )
    f = sk.frame
    ta, tb = _corner(tx, a, b)
    pa, pb = kcurve.point_at(tx, a, ta), kcurve.point_at(tx, b, tb)
    corner = geom.scale(geom.add(pa, pb), 0.5)
    # A help point inside the corner (between both lines) selects the quadrant to cut.
    ha, hb = _help_point(tx, a, ta, 0.1), _help_point(tx, b, tb, 0.1)
    inside = [corner[i] + 0.5 * (ha[i] - corner[i]) + 0.5 * (hb[i] - corner[i]) for i in range(3)]
    segments = [ksketch.read(tx, f, c) for c in (a, b)]
    corner_local = ksketch.to_local(f, corner)
    tol = 1e-6 * max(1.0, d1, d2, *map(abs, corner_local))
    symmetric = abs(d1 - d2) <= 1e-6 * max(1.0, d1)
    before = ksketch.tags(sk)

    def attempt(first, second):
        with builders.build(tx, part.Sketches.CreateSketchChamferBuilder) as builder:
            options = nx.SketchChamferBuilder.ChamferOptions
            builder.ChamferOption = options.Symmetric if symmetric else options.Asymmetric
            builder.TrimInputCurves = True
            builder.CurvesToChamfer.Add(a)
            builder.CurvesToChamfer.Add(b)
            builder.HelpPoint = part.Points.CreatePoint(nx.Point3d(*inside))
            builder.Distance1.RightHandSide = repr(float(first))
            if not symmetric:
                builder.Distance2.RightHandSide = repr(float(second))
            builder.Commit()
        sk.sketch.Update()
        created = [
            c for c in ksketch.new_geometry(sk, before) if ksketch.curve_kind(tx, c) == "line"
        ]
        readback.expect(len(created) == 1, "chamfer_count", count=len(created))
        row = ksketch.read(tx, f, created[0])
        # Each chamfer end lies on the ORIGINAL segment of its line (a cut, not an
        # outside extension); measure the cut-back along first and second.
        measured = [None, None]
        for k, seg in enumerate(segments):
            hits = [p for p in (row["start"], row["end"]) if common.contains(seg, p, tol)]
            if hits:
                measured[k] = min(geom.dist(corner_local, p) for p in hits)
        return created[0], row, measured

    def matches(measured, x, y):
        return None not in measured and abs(measured[0] - x) <= tol and abs(measured[1] - y) <= tol

    mark = session.SetUndoMark(nx.Session.MarkVisibility.Invisible, "NX MCP sketch chamfer try")
    try:
        line, row, measured = attempt(d1, d2)
        if not symmetric and matches(measured, d2, d1):
            session.UndoToMark(mark, None)
            line, row, measured = attempt(d2, d1)
    finally:
        session.DeleteUndoMark(mark, None)
    readback.expect(
        matches(measured, d1, d2),
        "chamfer_distances",
        message="NX chamfer differs from the request (do the lines meet?)",
        distances_along_curves=measured,
    )
    return {"corner": common.curve_row(tx, f, line), "distances": measured}


@tool(
    effect="model",
    label="Fillet or chamfer sketch corner",
    summary={
        "fillet": "Fillet {first} and {second} R{radius}",
        "chamfer": "Chamfer {first} and {second} {distance}",
    },
    keywords=("sketch fillet", "sketch chamfer", "round corner", "corner", "trim corner"),
    requires=("work_part",),
    variants="kind",
    cost="light",
    batchable=True,
    previewable=True,
    invalidates="deleted",
    example={"sketch": "s:3", "first": "c:7", "second": "c:8", "kind": "fillet", "radius": 5},
)
def nx_create_sketch_corner(
    tx: Tx,
    *,
    sketch: SketchRef,
    first: CurveRef,
    second: CurveRef,
    kind: Literal["fillet", "chamfer"],
    radius: Annotated[Length | None, Doc("Required for fillet")] = None,
    distance: Annotated[Length | None, Doc("Required for chamfer: cut along first")] = None,
    distance2: Annotated[Length | None, Doc("Chamfer cut along second (default distance)")] = None,
) -> CornerData:
    """Fillet (radius) or chamfer (distance) the corner where two sketch curves meet; both are
    trimmed. The corner is at their nearest ends; a size too large for it is refused."""
    if first == second:
        fail("NX_INVALID_ARGUMENT", "first and second must be different curves", field="/second")
    if kind == "fillet":
        args.need(radius=radius)
        if distance is not None or distance2 is not None:
            fail("NX_INVALID_ARGUMENT", "A fillet takes radius only", field="/distance")
        args.finite(radius, "/radius", positive=True)
    else:
        args.need(distance=distance)
        if radius is not None:
            fail("NX_INVALID_ARGUMENT", "A chamfer takes distance and distance2", field="/radius")
        args.finite(distance, "/distance", positive=True)
        if distance2 is not None:
            args.finite(distance2, "/distance2", positive=True)
    tx.frame = "sketch_local"
    with ksketch.editing(tx, sketch) as sk:
        a = common.owned_curve(tx, sk, first, "/first")
        b = common.owned_curve(tx, sk, second, "/second")
        if kind == "fillet":
            data = _fillet(tx, sk, a, b, radius)
        else:
            data = _chamfer(tx, sk, a, b, distance, distance if distance2 is None else distance2)
        alive = ksketch.tags(sk)
        data["trimmed"] = [common.curve_row(tx, sk.frame, c) for c in (a, b) if int(c.Tag) in alive]
        return {"sketch": tx.ref(sk.sketch, "sketch"), **data}


# ---------------------------------------------------------------------------
# Copy: offset or mirror


class CopyData(TypedDict, total=False):
    sketch: result.Row
    curves: list[CurveRow]
    measured_distance: float
    side: str
    mirror_line: dict


def _side_of(tx, f, source, outputs):
    """``left``/``right`` of the source curve's direction, seen from the sketch normal."""
    _, p_in, p_out = min((kcurve.min_distance(tx, source, o) for o in outputs), key=lambda r: r[0])
    _, tangent = kcurve.closest(tx, source, p_in)
    return (
        "left" if geom.dot(geom.cross(tangent, geom.sub(p_out, p_in)), f["normal"]) > 0 else "right"
    )


LOOP_SAMPLES = 32


def _closed_polygon(tx, sk, inputs):
    """Sketch-local segments sampled along ``inputs``, which must form one closed loop
    (inside/outside need one); the segments' order does not matter to a ray test."""
    f = sk.frame
    rows = [ksketch.read(tx, f, c) for c in inputs]
    chains = ksketch.loops(rows)
    if len(chains) != 1 or not chains[0]["closed"]:
        fail(
            "NX_INVALID_ARGUMENT",
            "side inside/outside needs curves that form one closed loop",
            field="/side",
            hint="Pass every curve of the loop, or use side left/right (of the first curve's "
            "direction) or auto",
        )
    segments = []
    for c in inputs:
        points = [
            ksketch.to_local(f, kcurve.point_at(tx, c, k / LOOP_SAMPLES))
            for k in range(LOOP_SAMPLES + 1)
        ]
        segments += list(zip(points, points[1:], strict=False))
    return segments


def _inside(segments, p):
    """True when sketch-local ``p`` lies inside the closed polygon of ``segments``."""
    inside = False
    for a, b in segments:
        if (a[1] > p[1]) != (b[1] > p[1]):
            x = a[0] + (p[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if x > p[0]:
                inside = not inside
    return inside


def _placed(tx, sk, inputs, outputs, side, polygon):
    """Where the offset ``outputs`` lie, in the words of ``side``: left/right of the first
    curve's direction, or inside/outside of the closed loop."""
    if side not in ("inside", "outside"):
        return _side_of(tx, sk.frame, inputs[0], outputs)
    _, _, p_out = min((kcurve.min_distance(tx, inputs[0], o) for o in outputs), key=lambda r: r[0])
    return "inside" if _inside(polygon, ksketch.to_local(sk.frame, p_out)) else "outside"


def _offset(tx, sk, inputs, d, side):
    part, nx = tx.part, tx.nx
    polygon = _closed_polygon(tx, sk, inputs) if side in ("inside", "outside") else None
    before = ksketch.tags(sk)
    approx = units.part_tolerance(tx, OFFSET_TOLERANCE_MM)
    with builders.build(tx, part.Sketches.CreateSketchOffsetBuilder, nx.SketchOffset.Null) as b:
        _rule_section(tx, b.CreateSection(), inputs)
        b.Tolerance = approx  # before UpdateLoopsAndCopies (ellipses/splines, NX 1720072)
        b.UpdateLoopsAndCopies()
        b.Distance.RightHandSide = repr(float(d))
        b.CreateDimension = True
        b.NumberOfCopies = 1
        b.IsSymmetric = False
        b.ConvertToReference = False
        b.EvaluateOffset()
        if side != "auto":
            outputs = [
                o
                for o in b.GetOutputCurvesOfOffset()
                if isinstance(o, nx.Curve) and int(o.Tag) not in before
            ]
            if outputs and _placed(tx, sk, inputs, outputs, side, polygon) != side:
                b.ReverseOffsetDirectionOfChain(inputs[0])
                b.EvaluateOffset()
        b.Commit()
    with native(default="NX_UPDATE_FAILED"):
        sk.sketch.Update()
    created = [c for c in ksketch.new_geometry(sk, before) if isinstance(c, nx.Curve)]
    readback.expect(bool(created), "offset_created")
    measured = [min(kcurve.min_distance(tx, i, c)[0] for i in inputs) for c in created]
    actual_side = _placed(tx, sk, inputs, created, side, polygon)
    exact = all(ksketch.curve_kind(tx, c) in ("line", "arc", "circle") for c in created)
    limit = 1e-5 * max(1.0, d) if exact else approx
    readback.expect(
        all(abs(m - d) <= limit for m in measured), "offset_distance", distances=measured
    )
    readback.expect(side in ("auto", actual_side), "offset_side", side=actual_side)
    if not exact:
        tx.warn("NX_APPROXIMATED", f"Offsets of ellipses and splines are within {approx:g}")
    return created, {"measured_distance": max(measured), "side": actual_side}


def _mirror(tx, sk, inputs, mirror_line):
    nx, part = tx.nx, tx.part
    f = sk.frame
    axis = tx.resolve(mirror_line, "curve", "datum_axis")
    if isinstance(axis, nx.Curve):
        if int(axis.Tag) not in ksketch.tags(sk):
            fail(
                "NX_WRONG_OWNER",
                "mirror_line must be a line of this sketch or a datum axis",
                field="/mirror_line",
            )
        if ksketch.curve_kind(tx, axis) != "line":
            fail("NX_WRONG_GEOMETRY", "mirror_line must be a straight line", field="/mirror_line")
        a, b = ksketch.to_local(f, axis.StartPoint), ksketch.to_local(f, axis.EndPoint)
    else:
        select.owned(tx, axis, "/mirror_line", "datum axis")
        o, d = geom.xyz(axis.Origin), geom.xyz(axis.Direction)
        if abs(geom.dot(d, f["normal"])) > 1e-6 * geom.norm(d):
            fail(
                "NX_INVALID_ARGUMENT",
                "The datum axis must lie in (or parallel to) the sketch plane",
                field="/mirror_line",
            )
        a, b = ksketch.to_local(f, o), ksketch.to_local(f, geom.add(o, d))
    if geom.dist(a, b) <= 1e-9:
        fail("NX_INVALID_ARGUMENT", "mirror_line has zero length", field="/mirror_line")
    if any(int(c.Tag) == int(axis.Tag) for c in inputs):
        fail("NX_INVALID_ARGUMENT", "mirror_line cannot be mirrored itself", field="/mirror_line")
    before = ksketch.tags(sk)
    factory = part.Sketches.CreateSketchMirrorPatternBuilder
    with builders.build(tx, factory, nx.SketchPattern.Null) as builder:
        _rule_section(tx, builder.Section, inputs)
        builder.UpdateInputSection()
        builder.DirectionObject.Value = axis
        builder.UpdateDirectionObject()
        builder.ConvertToReference = False
        builder.ConvertInputToReference = False
        builder.Commit()
    with native(default="NX_UPDATE_FAILED"):
        sk.sketch.Update()
    created = [c for c in ksketch.new_geometry(sk, before) if isinstance(c, nx.Curve)]
    readback.expect(
        len(created) == len(inputs), "mirror_count", created=len(created), requested=len(inputs)
    )

    line = geom.sub(b, a)
    sources = [
        sorted(
            tuple(geom.reflect_across_line(ksketch.to_local(f, p), a, line))
            for p in kcurve.ends(tx, c)
        )
        for c in inputs
    ]
    tol = 1e-6 * max(1.0, *[abs(v) for e in sources for p in e for v in p])
    unmatched = []
    for c in created:
        ends = sorted(tuple(ksketch.to_local(f, p)) for p in kcurve.ends(tx, c))
        if not any(
            all(geom.dist(p, q) <= tol for p, q in zip(ends, src, strict=True)) for src in sources
        ):
            unmatched.append(tx.ref(c, "curve")["id"])
    readback.expect(not unmatched, "mirror_reflection", unmatched=unmatched)
    return created, {"mirror_line": {"start": a, "end": b}}


@tool(
    effect="model",
    label="Offset or mirror sketch curves",
    summary={
        "offset": "Offset {curves} by {distance}",
        "mirror": "Mirror {curves} about {mirror_line}",
    },
    keywords=("offset curve", "mirror curve", "sketch offset", "sketch mirror", "copy curves"),
    requires=("work_part",),
    variants="mode",
    cost="light",
    batchable=True,
    previewable=True,
    invalidates="deleted",
    example={"sketch": "s:3", "curves": ["c:7", "c:8"], "mode": "offset", "distance": 2},
)
def nx_create_sketch_copy(
    tx: Tx,
    *,
    sketch: SketchRef,
    curves: Annotated[list[CurveRef], Items(1, 100)],
    mode: Literal["offset", "mirror"],
    distance: Annotated[Length | None, Doc("Required for offset")] = None,
    side: Annotated[
        Literal["auto", "left", "right", "inside", "outside"],
        Doc("left/right of the first curve, or inside/outside a closed loop"),
    ] = "auto",
    mirror_line: Annotated[
        str, RefTo("curve", "datum_axis", doc="Required for mirror: sketch line or datum axis")
    ]
    | None = None,
) -> CopyData:
    """Copy sketch curves as an associative offset or mirror. Offsets of ellipses and splines
    are approximated to 0.001 mm."""
    args.distinct("/curves", curves, "curves")
    if mode == "offset":
        args.need(distance=distance)
        args.finite(distance, "/distance", positive=True)
        if mirror_line is not None:
            fail("NX_INVALID_ARGUMENT", "mirror_line applies to mirror only", field="/mirror_line")
    else:
        args.need(mirror_line=mirror_line)
        if distance is not None or side != "auto":
            fail("NX_INVALID_ARGUMENT", "distance and side apply to offset", field="/distance")
        if mirror_line in curves:
            fail("NX_INVALID_ARGUMENT", "mirror_line cannot be in curves", field="/mirror_line")
    tx.frame = "sketch_local"
    with ksketch.editing(tx, sketch) as sk:
        inputs = [common.owned_curve(tx, sk, c, f"/curves/{i}") for i, c in enumerate(curves)]
        if mode == "offset":
            created, extra = _offset(tx, sk, inputs, distance, side)
        else:
            created, extra = _mirror(tx, sk, inputs, mirror_line)
        rows = [common.curve_row(tx, sk.frame, c) for c in created]
        return {"sketch": tx.ref(sk.sketch, "sketch"), "curves": rows, **extra}


# ---------------------------------------------------------------------------
# Projection: project edges/curves or intersect faces with the sketch plane


class ProjectionData(TypedDict, total=False):
    sketch: result.Row
    feature: result.Row
    curves: list[CurveRow]


def _is_face(tx, obj):
    return not isinstance(obj, (tx.nx.Edge, tx.nx.Curve))


def _model_objects(tx, refs, mode):
    """The objects and the mode: omitted, faces mean intersect and edges/curves project;
    a face to project or an edge to intersect is refused with the mode it needs."""
    objs = [
        select.one(tx, ref, "edge", "curve", "face", field=f"/objects/{i}")
        for i, ref in enumerate(refs)
    ]
    faces = [_is_face(tx, o) for o in objs]
    if mode is None:
        if all(faces):
            return objs, "intersect"
        if not any(faces):
            return objs, "project"
        fail(
            "NX_INVALID_ARGUMENT",
            "objects mix faces with edges or curves",
            field=f"/objects/{faces.index(True)}",
            hint="Faces need mode 'intersect', edges and curves mode 'project': make two calls",
        )
    for i, face in enumerate(faces):
        if face and mode == "project":
            fail(
                "NX_WRONG_KIND",
                "A face cannot be projected: project takes edges or curves",
                field=f"/objects/{i}",
                hint="Use mode 'intersect' to cut faces with the sketch plane, or project "
                "the face's edges",
            )
        if not face and mode == "intersect":
            fail(
                "NX_WRONG_KIND",
                "intersect takes faces",
                field=f"/objects/{i}",
                hint="Use mode 'project' for edges and curves",
            )
    return objs, mode


def _not_in_sketch(sk, objs):
    owned = ksketch.tags(sk)
    for i, obj in enumerate(objs):
        if int(obj.Tag) in owned:
            fail(
                "NX_INVALID_ARGUMENT",
                "The curve already belongs to this sketch",
                field=f"/objects/{i}",
            )


def _project(tx, sk, items):
    part, nx = tx.part, tx.nx
    edges = [o for o in items if isinstance(o, nx.Edge)]
    curves = [o for o in items if not isinstance(o, nx.Edge)]
    with builders.build(tx, part.Sketches.CreateProjectBuilder, nx.Features.Feature.Null) as b:
        b.Associativity = True
        b.ProjectAsDumbFixedCurves = False
        b.CurveType = nx.SketchProjectBuilder.OutputCurve.Original
        tol = units.part_tolerance(tx, 0.01)
        b.Tolerance = tol
        b.Section.DistanceTolerance = tol
        b.Section.ChainingTolerance = tol * 0.95
        b.Section.SetAllowedEntityTypes(nx.Section.AllowTypes.CurvesAndPoints)
        rules = []
        if edges:
            rules.append(part.ScRuleFactory.CreateRuleEdgeDumb(edges))
        if curves:
            rules.append(part.ScRuleFactory.CreateRuleCurveDumb(curves))
        b.Section.AddToSection(
            rules, items[0], None, None, nx.Point3d(0.0, 0.0, 0.0), nx.Section.Mode.Create, False
        )
        with common.rejected_inputs(
            "NX cannot project these objects onto the sketch plane (an edge normal to the "
            "plane projects to a point)"
        ):
            b.Commit()
        return b.GetOperation()


def _intersect(tx, sk, faces):
    part, nx = tx.part, tx.nx
    factory = part.Sketches.CreateIntersectionCurveBuilder
    with builders.build(tx, factory, nx.SketchIntersectionCurve.Null) as b:
        b.Associative = True
        b.JoinCurves = False
        b.IgnoreHoles = False
        builders.fill(tx, b.FaceCollector, faces, "Face")
        with common.rejected_inputs("The faces do not cross the sketch plane"):
            b.CollectorUpdated()
        return b.CommitFeature()


def _projection_summary(a, say):
    if a.get("mode") == "intersect":
        return f"Intersect {say('objects')} with sketch {say('sketch')}"
    return f"Project {say('objects')} into sketch {say('sketch')}"


@tool(
    effect="model",
    label="Project model geometry into sketch",
    summary=_projection_summary,
    keywords=("project curve", "project edge", "intersection curve", "include geometry", "sketch"),
    requires=("work_part",),
    variants="mode",
    cost="light",
    batchable=True,
    previewable=True,
    invalidates="deleted",
    example={"sketch": "s:3", "objects": ["e:12"], "mode": "project"},
)
def nx_create_sketch_projection(
    tx: Tx,
    *,
    sketch: SketchRef,
    objects: Annotated[
        list[Annotated[str, RefTo("edge", "curve", "face")]],
        Items(1, 200),
        Doc("project: edges or curves; intersect: faces"),
    ],
    mode: Annotated[
        Literal["project", "intersect"] | None,
        Doc("Default: intersect for faces, else project"),
    ] = None,
) -> ProjectionData:
    """Bring model geometry into a sketch as associative curves that follow model changes.
    project: edges or curves onto the sketch plane; intersect: faces with the sketch plane."""
    args.distinct("/objects", objects, "objects")
    with tx.checks():
        objs, mode = _model_objects(tx, objects, mode)
    tx.frame = "sketch_local"
    with ksketch.editing(tx, sketch) as sk:
        _not_in_sketch(sk, objs)
        before = ksketch.tags(sk)
        feature = (_project if mode == "project" else _intersect)(tx, sk, objs)
        with native(default="NX_UPDATE_FAILED"):
            sk.sketch.Update()
        created = [c for c in ksketch.new_geometry(sk, before) if isinstance(c, tx.nx.Curve)]
        readback.expect(bool(created), "curves_created", message="NX created no curves")
        f = sk.frame
        if mode == "intersect":
            tol = 1e-6 * max(1.0, *map(abs, f["origin"]))
            off = [
                tx.ref(c, "curve")["id"]
                for c in created
                for t in (0.0, 0.5, 1.0)
                if abs(ksketch.height(f, kcurve.point_at(tx, c, t))) > tol
            ]
            readback.expect(not off, "curves_in_plane", curves=off)
        data: ProjectionData = {"sketch": tx.ref(sk.sketch, "sketch")}
        if feature is not None:
            data["feature"] = tx.ref(feature, "feature")
        data["curves"] = [common.curve_row(tx, f, c) for c in created]
        return data
