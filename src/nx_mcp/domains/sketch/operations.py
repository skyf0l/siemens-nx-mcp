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

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Length, RefTo
from nx_mcp.domains.sketch import common
from nx_mcp.domains.sketch.common import CurveRef, CurveRow, SketchRef
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


def _fillet(tx, sk, a, b, radius):
    nx = tx.nx
    if any(ksketch.curve_kind(tx, c) not in ("line", "arc") for c in (a, b)):
        fail("NX_WRONG_GEOMETRY", "A fillet joins sketch lines and arcs", field="/first")
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
    readback.expect(
        all(within),
        "fillet_within_curves",
        message="The radius is too large for this corner (NX would extend the curves)",
        within_original_curves=within,
    )
    return {"corner": row}


def _chamfer(tx, sk, a, b, d1, d2):
    nx, part, session = tx.nx, tx.part, tx.session
    if any(ksketch.curve_kind(tx, c) != "line" for c in (a, b)):
        fail("NX_WRONG_GEOMETRY", "A sketch chamfer joins two lines", field="/first")
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
    sketch: SketchRef,  # type: ignore[valid-type]
    first: CurveRef,  # type: ignore[valid-type]
    second: CurveRef,  # type: ignore[valid-type]
    kind: Literal["fillet", "chamfer"],
    radius: Length | None = None,
    distance: Annotated[Length | None, Doc("Chamfer cut along first")] = None,
    distance2: Annotated[Length | None, Doc("Chamfer cut along second (default distance)")] = None,
) -> CornerData:
    """Fillet or chamfer the corner where two sketch curves meet; both curves are trimmed. The
    corner is at their nearest ends; a radius or distance too large for it is refused."""
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


def _offset(tx, sk, inputs, d, side):
    part, nx = tx.part, tx.nx
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
            if outputs and _side_of(tx, sk.frame, inputs[0], outputs) != side:
                b.ReverseOffsetDirectionOfChain(inputs[0])
                b.EvaluateOffset()
        b.Commit()
    with native(default="NX_UPDATE_FAILED"):
        sk.sketch.Update()
    created = [c for c in ksketch.new_geometry(sk, before) if isinstance(c, nx.Curve)]
    readback.expect(bool(created), "offset_created")
    measured = [min(kcurve.min_distance(tx, i, c)[0] for i in inputs) for c in created]
    actual_side = _side_of(tx, sk.frame, inputs[0], created)
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

    def reflect(p):
        dx, dy = b[0] - a[0], b[1] - a[1]
        t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)
        fx, fy = a[0] + t * dx, a[1] + t * dy
        return [2 * fx - p[0], 2 * fy - p[1]]

    sources = [
        sorted(tuple(reflect(ksketch.to_local(f, p))) for p in kcurve.ends(tx, c)) for c in inputs
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
    sketch: SketchRef,  # type: ignore[valid-type]
    curves: Annotated[list[CurveRef], Items(1, 100)],  # type: ignore[valid-type]
    mode: Literal["offset", "mirror"],
    distance: Length | None = None,
    side: Annotated[
        Literal["auto", "left", "right"], Doc("Offset side of the first curve's direction")
    ] = "auto",
    mirror_line: RefTo("curve", "datum_axis") | None = None,  # type: ignore[valid-type]
) -> CopyData:
    """Copy sketch curves as an associative offset or mirror. Offsets of ellipses and splines
    are approximated to 0.001 mm; a mirror line is a line of this sketch or a datum axis in its
    plane."""
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


def _model_objects(tx, sk, refs, kinds):
    owned = ksketch.tags(sk)
    out = []
    for i, ref in enumerate(refs):
        obj = select.one(tx, ref, *kinds, field=f"/objects/{i}")
        if int(obj.Tag) in owned:
            fail(
                "NX_INVALID_ARGUMENT",
                "The curve already belongs to this sketch",
                field=f"/objects/{i}",
            )
        out.append(obj)
    return out


def _project(tx, sk, refs):
    part, nx = tx.part, tx.nx
    items = _model_objects(tx, sk, refs, ("edge", "curve"))
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


def _intersect(tx, sk, refs):
    part, nx = tx.part, tx.nx
    faces = _model_objects(tx, sk, refs, ("face",))
    factory = part.Sketches.CreateIntersectionCurveBuilder
    with builders.build(tx, factory, nx.SketchIntersectionCurve.Null) as b:
        b.Associative = True
        b.JoinCurves = False
        b.IgnoreHoles = False
        b.FaceCollector.ReplaceRules([part.ScRuleFactory.CreateRuleFaceDumb(faces)], False)
        with common.rejected_inputs("The faces do not cross the sketch plane"):
            b.CollectorUpdated()
        return b.CommitFeature()


@tool(
    effect="model",
    label="Project model geometry into sketch",
    summary={
        "project": "Project {objects} into sketch {sketch}",
        "intersect": "Intersect {objects} with sketch {sketch}",
    },
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
    sketch: SketchRef,  # type: ignore[valid-type]
    objects: Annotated[
        list[RefTo("edge", "curve", "face")],  # type: ignore[valid-type]
        Items(1, 200),
        Doc("project: edges or curves; intersect: faces"),
    ],
    mode: Literal["project", "intersect"],
) -> ProjectionData:
    """Bring model geometry into a sketch as associative curves that follow model changes.
    project: edges or curves onto the sketch plane; intersect: faces with the sketch plane."""
    args.distinct("/objects", objects, "objects")
    tx.frame = "sketch_local"
    with ksketch.editing(tx, sketch) as sk:
        before = ksketch.tags(sk)
        feature = (_project if mode == "project" else _intersect)(tx, sk, objects)
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
        data = {"sketch": tx.ref(sk.sketch, "sketch")}
        if feature is not None:
            data["feature"] = tx.ref(feature, "feature")
        data["curves"] = [common.curve_row(tx, f, c) for c in created]
        return data
