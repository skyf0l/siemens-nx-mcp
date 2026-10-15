"""Sketch activation, local <-> part coordinates, solving and curve ownership.

    with sketch.editing(tx, ref) as sk:
        line = tx.part.Curves.CreateLine(sk.point(p0), sk.point(p1))
        ...
        state = sketch.solve(tx, sk, edited_expressions)

NX 2406 findings encoded here:

- ``Sketch.Update()`` (and a model update) leaves an *active* sketch unsolved
  after a driving expression changed; ``LocalUpdate()`` solves it now.
- ``GetStatus()`` returns NX's degrees-of-freedom indicator, which on 2406 is
  the number of curves that are not fully constrained, not scalar DOF.
- Solver status needs the work region set to the entire sketch; that change is
  made under an invisible undo mark and undone.
- ``GetStatus()`` can answer NotEvaluated for a sketch NX has not solved in this
  session (reported right after an NX start): :func:`status` solves it once
  (:func:`update`) and reads again.
- NX has one active sketch per session and refuses model calls in every part while it
  is active. No sketch stays active between calls: an unfinished sketch is *parked*
  (deactivated with a model update, ``core/parking.py``) and ``editing`` activates it
  for each call. ``open_sketch`` is the part's open sketch: NX's active one, else the
  parked one.
- ``Sketch.Activate`` in a part that is not displayed (a quiet work part, or a part in
  a background NX tab) raised a General Fault Exception, then NX died, for a sketch
  with a dimension or an offset annotation; geometric constraints alone survived. Every
  tool that activates an existing sketch (``editing``, finishing a parked one) is
  ``display_bound``: its part is displayed for the call and the shown tab comes back.
- Opening a drawing sheet from the modeling display re-activates that sheet's last sheet
  sketch (``IsDraftingSketch``), also in NX's own UI. It is not a model sketch the agent
  opened: :func:`release_drafting_sketch` deactivates it quietly (gate 4 and the drawing
  tools do), a model sketch is left alone.
"""

from __future__ import annotations

import contextlib
import math
import re

from nx_mcp.core import parking
from nx_mcp.core.errors import NXError, fail
from nx_mcp.kit import geom
from nx_mcp.kit.result import enum_name

FAILED_STATUSES = frozenset({"OverConstrained", "InconsistentlyConstrained"})
SOLVED_DOF_STATUSES = frozenset({"UnderConstrained", "WellConstrained"})
# A status NX answers before it solved the sketch: solved once, then read again.
UNEVALUATED = frozenset({"NotEvaluated"})


def frame(sketch):
    """Sketch frame in part coordinates: origin, x_axis, y_axis, normal."""
    m = sketch.Orientation.Element
    return {
        "origin": geom.xyz(sketch.Origin),
        "x_axis": [float(m.Xx), float(m.Xy), float(m.Xz)],
        "y_axis": [float(m.Yx), float(m.Yy), float(m.Yz)],
        "normal": [float(m.Zx), float(m.Zy), float(m.Zz)],
    }


def to_part(f, p2):
    """Part (work-part absolute) coordinates of a sketch-local [x, y]."""
    return [f["origin"][i] + p2[0] * f["x_axis"][i] + p2[1] * f["y_axis"][i] for i in range(3)]


def to_part_direction(f, t2):
    """Part direction of a sketch-local direction [x, y] (no origin shift)."""
    return [t2[0] * f["x_axis"][i] + t2[1] * f["y_axis"][i] for i in range(3)]


def to_local(f, p3):
    """Sketch-local [x, y] of a part point (list or anything with X, Y, Z)."""
    d = geom.sub(geom.xyz(p3) if hasattr(p3, "X") else list(p3), f["origin"])
    return [geom.dot(d, f["x_axis"]), geom.dot(d, f["y_axis"])]


def height(f, p3):
    """Signed distance of a part point from the sketch plane."""
    d = geom.sub(geom.xyz(p3) if hasattr(p3, "X") else list(p3), f["origin"])
    return geom.dot(d, f["normal"])


class Editing:
    """An activated sketch: its object, frame and coordinate conversions."""

    def __init__(self, tx, sketch, activated):
        self.tx = tx
        self.sketch = sketch
        self.activated = activated
        self.frame = frame(sketch)

    def to_part(self, p2):
        return to_part(self.frame, p2)

    def to_local(self, p3):
        return to_local(self.frame, p3)

    def point(self, p2):
        """An NX ``Point3d`` for a sketch-local point."""
        return self.tx.nx.Point3d(*self.to_part(p2))


@contextlib.contextmanager
def at_field(field):
    """A classified error raised inside without a field (a stale, unknown or wrong-kind
    ref) gets ``field``, the JSON pointer of the argument that named it."""
    try:
        yield
    except NXError as error:
        if error.field is None and field:
            error.field = field
        raise


def resolve(tx, ref, field="/sketch"):
    """A sketch of the work part (ref or live object)."""
    with at_field(field):
        sk = tx.resolve(ref, "sketch") if isinstance(ref, str) else ref
    if getattr(sk, "IsOccurrence", False) or getattr(sk, "OwningPart", tx.part) != tx.part:
        fail("NX_WRONG_OWNER", "Select a sketch of the work part", field=field)
    return sk


# NX shows the modal "Large Sketch Performance" question ("Shaded Region can be slow for
# large sketches") once a sketch grows to several hundred curves, and NX then blocks every
# caller until a person answers it (seen live on NX 2406 at about 800-1000 curves). Tools
# that add curves keep every sketch at or below this many objects.
MAX_CURVES = 500


def require_room(tx, sk, adding, field="/curves") -> int:
    """Refuse adding ``adding`` objects to ``sk`` (a sketch or :class:`Editing`) when the
    sketch would hold more than :data:`MAX_CURVES`; returns the count it holds now."""
    held = len(geometry(sk)) if sk is not None else 0
    if held + int(adding) > MAX_CURVES:
        fail(
            "NX_TOO_MANY_OBJECTS",
            f"The sketch would hold {held + int(adding)} curves ({held} now, {int(adding)} "
            f"new); a sketch takes at most {MAX_CURVES}: NX slows down and opens a blocking "
            "dialog for larger sketches",
            field=field,
            hint="Split the profile over several sketches (one per feature or region), or "
            "use fewer polygon sides",
            details={"curves": held, "adding": int(adding), "limit": MAX_CURVES},
        )
    return held


def _parked(tx):
    """The work part's parked sketch (``core/parking.py``), or None."""
    services = getattr(tx, "services", None)
    return None if services is None else parking.parked(services, tx.part)


def open_sketch(tx):
    """The open sketch of the work part: NX's active sketch, else the parked one."""
    active = tx.session.ActiveSketch
    return active if active is not None else _parked(tx)


def open_details(tx, sk) -> dict:
    """``{"open_sketch": ref id, "open_sketch_name": name}`` of an open sketch, for the
    details of an ``NX_SKETCH_ACTIVE`` error (best effort: empty when NX cannot say)."""
    out: dict = {}
    with contextlib.suppress(Exception):
        out["open_sketch"] = tx.ref(sk, "sketch")["id"]
    with contextlib.suppress(Exception):
        out["open_sketch_name"] = str(sk.Name)
    return out


def release_drafting_sketch(session, nx) -> bool:
    """Deactivate the active sketch when it is a drafting (sheet) sketch NX re-activated
    on opening a sheet: no view change, model update level. A model sketch is left
    alone. Returns whether one was deactivated."""
    active = getattr(session, "ActiveSketch", None)
    if active is None or not bool(getattr(active, "IsDraftingSketch", False)):
        return False
    active.Deactivate(nx.Sketch.ViewReorient.FalseValue, nx.Sketch.UpdateLevel.Model)
    return True


def same(a, b) -> bool:
    """Same NX object (by tag: NXOpen may hand out different wrappers)."""
    return a is not None and b is not None and int(a.Tag) == int(b.Tag)


def park(tx, sk) -> None:
    """Leave the active sketch ``sk`` open but deactivated between calls (model update,
    no view change) and record it as its part's open sketch (``core/parking.py``)."""
    nx = tx.nx
    sk.Deactivate(nx.Sketch.ViewReorient.FalseValue, nx.Sketch.UpdateLevel.Model)
    services = getattr(tx, "services", None)
    if services is not None:
        parking.park(services, getattr(sk, "OwningPart", None) or tx.part, sk)


@contextlib.contextmanager
def editing(tx, ref, field="/sketch"):
    """Activate a sketch for editing and restore the previous state afterwards.

    A different open sketch of the part (active, or parked) is ``NX_SKETCH_ACTIVE``. A
    sketch that was already active stays active; one activated here (a parked one too)
    is deactivated with a model update level, even when the block fails.
    """
    sk = resolve(tx, ref, field)
    session, nx = tx.session, tx.nx
    active = session.ActiveSketch
    other = active if active is not None else _parked(tx)
    if other is not None and not same(other, sk):
        fail(
            "NX_SKETCH_ACTIVE",
            "Finish the other active sketch first",
            hint="Finish the active sketch (details.open_sketch) with nx_finish_sketch, then retry",
            details=open_details(tx, other),
        )
    activated = not active
    if activated:
        sk.Activate(nx.Sketch.ViewReorient.FalseValue)
    try:
        yield Editing(tx, sk, activated)
    finally:
        if activated and session.ActiveSketch == sk:
            sk.Deactivate(nx.Sketch.ViewReorient.FalseValue, nx.Sketch.UpdateLevel.Model)


def _sketch_of(sk):
    return sk.sketch if isinstance(sk, Editing) else sk


def _expression_closure(expressions):
    """Edited expressions plus every expression that references them (by tag)."""
    tags, pending = set(), list(expressions)
    while pending and len(tags) < 10000:
        exp = pending.pop()
        if int(exp.Tag) not in tags:
            tags.add(int(exp.Tag))
            query = getattr(exp, "GetReferencingExpressions", None)
            pending.extend(list(query()) if query is not None else [])
    return tags


def update(tx, sk):
    """Solve geometry now: ``LocalUpdate()`` for the active sketch, then ``Update()``."""
    s = _sketch_of(sk)
    if tx.session.ActiveSketch == s and hasattr(s, "LocalUpdate"):
        s.LocalUpdate()
    s.Update()


def status(tx, sk):
    """``{"status": name, "dof": n or None, "native_dof": n}`` for the whole sketch."""
    s = _sketch_of(sk)
    session, nx = tx.session, tx.nx
    mark = session.SetUndoMark(nx.Session.MarkVisibility.Invisible, "NX MCP sketch status")
    try:
        region = tx.part.Sketches.CreateWorkRegionBuilder()
        try:
            region.Scope = nx.SketchWorkRegionBuilder.ScopeType.EntireSketch
            region.Commit()
        finally:
            region.Destroy()
        s.CalculateStatus()
        value, dof = s.GetStatus()
        if enum_name(value, nx.Sketch.Status) in UNEVALUATED:
            update(tx, s)  # NX had not solved it yet (seen right after an NX start)
            s.CalculateStatus()
            value, dof = s.GetStatus()
    finally:
        session.UndoToMark(mark, None)
        session.DeleteUndoMark(mark, None)
    name = enum_name(value, nx.Sketch.Status)
    return {"status": name, "dof": dof if name in SOLVED_DOF_STATUSES else None, "native_dof": dof}


def solve(tx, sk, expressions=()):
    """Solve the sketch and verify it; returns ``status()``.

    Each driving dimension whose expression is in ``expressions`` (or depends
    on one) must measure its value afterwards, else ``NX_CONSTRAINT_UNSATISFIED``.
    An over- or inconsistently constrained sketch is ``NX_SKETCH_OVERCONSTRAINED``.
    """
    s = _sketch_of(sk)
    update(tx, s)
    if expressions:
        tags = _expression_closure(expressions)
        nx = tx.nx
        for c in s.GetAllConstraintsOfType(
            nx.Sketch.ConstraintClass.Dimension, nx.Sketch.ConstraintType.NoCon
        ):
            exp = getattr(c, "AssociatedExpression", None)
            if exp is None or int(exp.Tag) not in tags:
                continue
            wanted = float(exp.Value)
            measured = float(c.AssociatedDimension.ComputedSize)
            if abs(measured - wanted) > 1e-6 * max(1.0, abs(wanted)):
                fail(
                    "NX_CONSTRAINT_UNSATISFIED",
                    "NX could not solve the sketch to the new dimension value",
                    details={"expression": exp.Name, "requested": wanted, "measured": measured},
                )
    state = status(tx, s)
    if state["status"] in FAILED_STATUSES:
        fail(
            "NX_SKETCH_OVERCONSTRAINED",
            f"The sketch is {state['status']} after the change",
            hint="Delete one of the constraints that already hold these curves (nx_get_sketch "
            "lists them), or make the new dimension a reference",
            details=state,
        )
    return state


def owns(tx, sk, curve, field="/curve"):
    """``curve`` (ref or object) when it is geometry of the sketch, else ``wrong_owner``."""
    s = _sketch_of(sk)
    with at_field(field):
        obj = tx.resolve(curve, "curve") if isinstance(curve, str) else curve
    if int(obj.Tag) not in {int(c.Tag) for c in s.GetAllGeometry()}:
        fail("NX_WRONG_OWNER", "The curve is not owned by this sketch", field=field)
    return obj


# ---------------------------------------------------------------------------
# Sketch curves: kinds, points, read-back, constraint geometry, solving by drag
#
# NX 2406 findings (modern solver, ``Sketch.UsesLegacySolver`` is False):
#
# - ``Create<Kind>Constraint`` creates a persistent constraint but does not move
#   geometry; ``Update()``/``LocalUpdate()`` do not solve it. A drag of one curve
#   point (``SketchDragGeometryBuilder``, also a zero-length drag) solves the
#   whole sketch with every persistent constraint (NX may move any free point,
#   the dragged one too: live, a horizontal line kept its start, not its end).
# - ``CreateFixedConstraint`` on a line fixes the infinite line only: a later
#   length dimension still slides its end points. ``CreateFullyFixedConstraints``
#   (with a ``ConstraintGeometry``) fixes the curve and its points; a dimension
#   that would move it then leaves the sketch OverConstrained.
# - ``ConstraintPointType.MidVertex`` is refused as a constraint target; drag
#   points use the ``Sketch.PointType`` enum (StartPoint/EndPoint/Center).
# - NXOpen point constructors refuse Python ``int`` ("Expecting double type").

POINTS = ("start", "end", "center", "mid")
CONSTRAINT_POINT = {"start": "StartVertex", "end": "EndVertex", "center": "ArcCenter"}
DIMENSION_POINT = {
    "start": "StartPoint",
    "end": "EndPoint",
    "center": "ArcCenter",
    "mid": "Midpoint",
}
DRAG_POINT = {"start": "StartPoint", "end": "EndPoint", "center": "Center"}
FULL_TURN = 2 * math.pi


def snake(name):
    """``ParallelDim`` -> ``parallel_dim`` (enum names as lower snake case)."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", str(name)).lower()


def curve_kind(tx, curve):
    """``line``, ``arc``, ``circle``, ``ellipse``, ``spline`` or ``other``."""
    nx = tx.nx
    if isinstance(curve, nx.Line):
        return "line"
    if isinstance(curve, nx.Arc):
        full = abs(float(curve.EndAngle) - float(curve.StartAngle)) >= FULL_TURN - 1e-9
        return "circle" if full else "arc"
    if isinstance(curve, nx.Ellipse):
        return "ellipse"
    spline = getattr(nx, "Spline", None)
    if isinstance(spline, type) and isinstance(curve, spline):
        return "spline"
    return "other"


def add(tx, sk, curve):
    """Add a curve to the sketch without inferred constraints; returns the curve."""
    _sketch_of(sk).AddGeometry(curve, tx.nx.Sketch.InferConstraintsOption.InferNoConstraints)
    return curve


def geometry(sk):
    """The sketch's curves and points, in NX order."""
    return list(_sketch_of(sk).GetAllGeometry())


def tags(sk):
    """Tags of the sketch's geometry."""
    return {int(c.Tag) for c in _sketch_of(sk).GetAllGeometry()}


def new_geometry(sk, before):
    """Sketch geometry whose tag is not in ``before``, in NX order."""
    return [c for c in _sketch_of(sk).GetAllGeometry() if int(c.Tag) not in before]


def constraints(tx, sk):
    """Every persistent constraint and dimension of the sketch."""
    nx = tx.nx
    return list(
        _sketch_of(sk).GetAllConstraintsOfType(
            nx.Sketch.ConstraintClass.Any, nx.Sketch.ConstraintType.NoCon
        )
    )


def attached(tx, sk, curve):
    """Constraints that reference ``curve``."""
    return list(_sketch_of(sk).GetConstraintsForGeometry(curve, tx.nx.Sketch.ConstraintClass.Any))


def constraint_type(tx, constraint):
    """Lower snake case NX constraint type (``horizontal``, ``parallel_dim``...)."""
    return snake(enum_name(constraint.ConstraintType, tx.nx.Sketch.ConstraintType))


def _arc_point(arc, angle):
    m = arc.Matrix.Element
    c, r = geom.xyz(arc.CenterPoint), float(arc.Radius)
    x, y = [m.Xx, m.Xy, m.Xz], [m.Yx, m.Yy, m.Yz]
    cos, sin = math.cos(angle), math.sin(angle)
    return [c[i] + r * (cos * x[i] + sin * y[i]) for i in range(3)]


def point(tx, curve, which):
    """Work-part coordinates of ``start``/``end``/``center``/``mid`` of a line or arc."""
    kind = curve_kind(tx, curve)
    if kind == "line":
        if which == "center":
            fail("NX_INVALID_ARGUMENT", "A line has no center point")
        s, e = geom.xyz(curve.StartPoint), geom.xyz(curve.EndPoint)
        return {"start": s, "end": e, "mid": geom.scale(geom.add(s, e), 0.5)}[which]
    if kind in ("arc", "circle"):
        if which == "center":
            return geom.xyz(curve.CenterPoint)
        a0, a1 = float(curve.StartAngle), float(curve.EndAngle)
        return _arc_point(curve, {"start": a0, "end": a1, "mid": (a0 + a1) / 2}[which])
    fail("NX_WRONG_GEOMETRY", "Only sketch lines, arcs and circles have named points")


def check_point(tx, curve, which, field, allowed=POINTS):
    """Refuse a point the curve does not have (line center, start of a full circle)."""
    kind = curve_kind(tx, curve)
    if kind not in ("line", "arc", "circle"):
        fail("NX_WRONG_GEOMETRY", "Select a sketch line, arc or circle", field=field)
    if which not in allowed:
        fail("NX_INVALID_ARGUMENT", f"point must be one of {', '.join(allowed)}", field=field)
    if kind == "line" and which == "center":
        fail("NX_INVALID_ARGUMENT", "A line has no center point", field=field)
    if kind == "circle" and which != "center":
        fail("NX_INVALID_ARGUMENT", "A full circle has only a center point", field=field)


def read(tx, f, curve):
    """Read a sketch curve back in the sketch frame ``f`` (sketch-local coordinates).

    Lines and arcs come from their native data, ellipses and splines from UF Eval.
    Arc and ellipse angles are counterclockwise degrees in the sketch frame (NX
    stores them relative to the curve's own matrix, which a mirror flips).
    """
    from nx_mcp.kit import curve as kcurve

    kind = curve_kind(tx, curve)
    if kind == "line":
        s, e = geom.xyz(curve.StartPoint), geom.xyz(curve.EndPoint)
    elif kind in ("arc", "circle"):
        s, e = point(tx, curve, "start"), point(tx, curve, "end")
    else:
        s, e = kcurve.ends(tx, curve)
    row = {"type": kind, "start": to_local(f, s), "end": to_local(f, e)}
    if geom.dist(s, e) <= geom.tol(s, e):
        row["closed"] = True  # the curve closes on itself (circle, full ellipse); see loops()
    length = getattr(curve, "GetLength", None)
    if length is not None:
        row["length"] = float(length())
    if kind in ("arc", "circle"):
        m = curve.Matrix.Element
        center = to_local(f, curve.CenterPoint)
        sweep = math.degrees(float(curve.EndAngle) - float(curve.StartAngle))
        ccw = geom.dot([m.Zx, m.Zy, m.Zz], f["normal"]) > 0
        first = row["start"] if ccw else row["end"]
        angle = math.degrees(math.atan2(first[1] - center[1], first[0] - center[0])) % 360.0
        row.update(center=center, radius=float(curve.Radius))
        row.update(start_angle=angle, end_angle=angle + sweep)
    elif kind == "ellipse":
        m = curve.Matrix.Element
        rotation, start, end = ellipse_in_frame(
            [m.Xx, m.Xy, m.Xz],
            [m.Yx, m.Yy, m.Yz],
            [m.Zx, m.Zy, m.Zz],
            math.degrees(float(curve.RotationAngle)),
            math.degrees(float(curve.StartAngle)),
            math.degrees(float(curve.EndAngle)),
            f,
        )
        row.update(
            center=to_local(f, curve.CenterPoint),
            major_radius=float(curve.MajorRadius),
            minor_radius=float(curve.MinorRadius),
            rotation=rotation,
            start_angle=start,
            end_angle=end,
        )
    row["construction"] = bool(getattr(curve, "IsReference", False))
    return row


def loops(rows):
    """Chains of connected curves from read rows (``start``/``end``, ``closed``), in row
    order: ``[{"closed": bool, "curves": n}]`` plus ``open_ends`` for an open chain and
    ``branched`` where more than two curve ends meet. Pure; points in any one frame."""
    out = []
    for chain in chains([(r["start"], r["end"]) for r in rows]):
        loop = {"closed": chain["closed"], "curves": len(chain["curves"])}
        if chain["free_ends"]:
            loop["open_ends"] = len(chain["free_ends"])
        if chain["branched"]:
            loop["branched"] = True
        out.append(loop)
    return out


# ---------------------------------------------------------------------------
# Chaining: curves joined end to end (shared by every tool that needs a closed loop or
# a connected path: sketch offsets, sweeps, tubes, joined curves, n-sided patches)
#
#     ends = [kcurve.ends(tx, c) for c in curves]       # or sketch-local [(start, end)]
#     order = ksketch.require_chain(ends, "/curves", closed=True, labels=refs)
#     ksketch.chains(ends)  # [{"curves", "closed", "branched", "free_ends", "branch_points"}]


def _nodes(ends, tol):
    """``(points, [(start node, end node)])``: curve ends merged within ``tol`` (default
    ``geom.tol`` of the two points)."""
    points: list = []

    def node(p):
        p = [float(v) for v in p]
        for i, q in enumerate(points):
            if geom.dist(p, q) <= (geom.tol(p, q) if tol is None else tol):
                return i
        points.append(p)
        return len(points) - 1

    return points, [(node(s), node(e)) for s, e in ends]


def chains(ends, tol=None) -> list[dict]:
    """Curves joined end to end. ``ends``: ``[(start, end)]`` per curve, points in any one
    frame (2D or 3D); a curve whose ends coincide is closed on itself (a circle). Pure.

    One dict per connected chain, in the order of its first curve: ``curves`` (indices,
    end to end from a free end when the chain is not branched), ``closed`` (every end
    meets exactly one other), ``branched`` (more than two ends meet somewhere),
    ``free_ends`` (``[(index, "start"|"end")]`` no other curve meets) and
    ``branch_points``. ``tol``: join distance (default 1e-6 relative, ``geom.tol``)."""
    points, nodes = _nodes(ends, tol)
    parent = list(range(len(points)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b in nodes:
        parent[root(a)] = root(b)
    groups: dict = {}
    for k, (a, _b) in enumerate(nodes):
        groups.setdefault(root(a), []).append(k)
    out = []
    for members in groups.values():
        at: dict = {}  # node -> [(curve, which)] of the curves that are not closed on themselves
        for k in members:
            a, b = nodes[k]
            if a == b:
                continue  # a closed curve is its own loop: no free ends
            at.setdefault(a, []).append((k, "start"))
            at.setdefault(b, []).append((k, "end"))
        free = [use for uses in at.values() if len(uses) == 1 for use in uses]
        branch = [n for n, uses in at.items() if len(uses) > 2]
        chain = {
            "curves": members if branch else _walk(members, nodes, at, free),
            "closed": not free and not branch,
            "branched": bool(branch),
            "free_ends": sorted(free),
            "branch_points": [points[n] for n in branch],
        }
        out.append(chain)
    return out


def _walk(members, nodes, at, free):
    """Members of an unbranched chain in order, end to end from a free end."""
    open_members = [k for k in members if nodes[k][0] != nodes[k][1]]
    if not open_members:
        return members
    first, which = free[0] if free else (open_members[0], "start")
    order, used = [], set()
    curve, here = first, nodes[first][0] if which == "start" else nodes[first][1]
    while curve is not None and curve not in used:
        used.add(curve)
        order.append(curve)
        a, b = nodes[curve]
        here = b if here == a else a
        curve = next((k for k, _w in at.get(here, []) if k not in used), None)
    return order + [k for k in members if k not in used]


def nearest_gap(ends, tol=None) -> dict | None:
    """The smallest gap between free curve ends that keeps ``ends`` from one closed chain:
    between two chains, else between the two free ends of one open chain. ``{"gap": d,
    "ends": [(i, which), (j, which)], "points": [p, q]}``, None when nothing is open."""
    found = chains(ends, tol)
    points = {
        (k, w): [float(v) for v in (ends[k][0] if w == "start" else ends[k][1])]
        for chain in found
        for k, w in chain["free_ends"]
    }
    owner = {use: n for n, chain in enumerate(found) for use in chain["free_ends"]}
    uses = list(points)
    for between_chains in (True, False):  # first a gap between two chains, else within one
        best = None
        for i, u in enumerate(uses):
            for v in uses[i + 1 :]:
                if between_chains and owner[u] == owner[v]:
                    continue
                d = geom.dist(points[u], points[v])
                if best is None or d < best["gap"]:
                    best = {"gap": d, "ends": [u, v], "points": [points[u], points[v]]}
        if best is not None:
            return best
    return None


def require_chain(ends, field, *, closed=False, labels=None, tol=None, what="curves") -> list:
    """The order (indices, end to end) of ``ends`` when they form one unbranched chain,
    closed too when ``closed``; else ``NX_INVALID_ARGUMENT`` on ``field`` naming the gap
    (``details.gap``, ``details.between``) or the branch point. ``labels``: a name per
    curve for the message (refs; default ``#i``)."""
    names = list(labels) if labels is not None else [f"#{i}" for i in range(len(ends))]
    found = chains(ends, tol)
    if len(found) == 1 and not found[0]["branched"] and (found[0]["closed"] or not closed):
        return found[0]["curves"]
    details: dict = {"chains": len(found)}
    branched = next((c for c in found if c["branched"]), None)
    if branched is not None:
        details["branch_point"] = branched["branch_points"][0]
        message = f"The {what} branch: more than two ends meet at {_fmt(details['branch_point'])}"
        hint = f"Pass {what} that join end to end without branches (one path)"
    elif (gap := nearest_gap(ends, tol)) is None:  # separate closed loops: no free end
        message = f"The {what} form {len(found)} separate closed loops, not one"
        hint = f"Pass the {what} of one loop per call"
    else:
        between = [
            {"curve": names[k], "end": w, "at": p}
            for (k, w), p in zip(gap["ends"], gap["points"], strict=True)
        ]
        details.update(gap=gap["gap"], between=between)
        where = f"{between[0]['curve']} {between[0]['end']} and {between[1]['curve']} " + str(
            between[1]["end"]
        )
        if len(found) > 1:
            message = (
                f"The {what} form {len(found)} separate chains, not one {'loop' if closed else 'path'}: "
                f"gap {gap['gap']:.6g} between {where}"
            )
        else:
            message = f"The {what} do not close: gap {gap['gap']:.6g} between {where}"
        hint = (
            f"Close the gap (extend, trim or add a curve), or pass every curve of the "
            f"{'loop' if closed else 'path'}"
        )
    fail("NX_INVALID_ARGUMENT", message, field=field, hint=hint, details=details)


def _fmt(p) -> str:
    return "[" + ", ".join(f"{float(v):.6g}" for v in p) + "]"


# ---------------------------------------------------------------------------
# Shapes before and after a solve: which curves the solver moved


def shape(tx, curve) -> list:
    """Coordinates that change when a curve moves or changes size: a line's ends; an arc's
    or circle's center, radius and ends; the ends of any other curve."""
    kind = curve_kind(tx, curve)
    if kind == "line":
        return geom.xyz(curve.StartPoint) + geom.xyz(curve.EndPoint)
    if kind in ("arc", "circle"):
        values = geom.xyz(curve.CenterPoint) + [float(curve.Radius)]
        if kind == "arc":
            values += point(tx, curve, "start") + point(tx, curve, "end")
        return values
    if kind in ("ellipse", "spline"):
        from nx_mcp.kit import curve as kcurve

        with contextlib.suppress(Exception):
            s, e = kcurve.ends(tx, curve)
            return list(s) + list(e)
    return []


def shapes(tx, sk) -> dict:
    """``{tag: (curve, shape)}`` of every sketch curve, read before a change."""
    return {int(c.Tag): (c, shape(tx, c)) for c in geometry(sk) if curve_kind(tx, c) != "other"}


def moved(tx, sk, before, skip=()) -> list:
    """Curves of ``before`` (:func:`shapes`) that still exist and changed shape, in sketch
    order; ``skip``: tags of curves the request itself changed."""
    alive = tags(sk)
    out = []
    for tag, (curve, old) in before.items():
        if tag in skip or tag not in alive or not old:
            continue
        new = shape(tx, curve)
        if len(new) == len(old) and max(abs(a - b) for a, b in zip(new, old, strict=True)) > (
            geom.tol(old, new) * 10
        ):
            out.append(curve)
    return out


def constraint_geometry(tx, curve, which=None):
    """A ``Sketch.ConstraintGeometry`` for a curve (``which`` None) or one of its points."""
    nx = tx.nx
    g = nx.Sketch.ConstraintGeometry()
    g.Geometry = curve
    g.PointType = getattr(nx.Sketch.ConstraintPointType, CONSTRAINT_POINT.get(which, "NotSet"))
    g.SplineDefiningPointIndex = 0
    return g


def dimension_geometry(tx, curve, which=None):
    """A ``Sketch.DimensionGeometry`` for a curve (``which`` None) or one of its points."""
    nx = tx.nx
    g = nx.Sketch.DimensionGeometry()
    g.Geometry = curve
    g.AssocType = getattr(nx.Sketch.AssocType, DIMENSION_POINT.get(which, "NotSet"))
    g.AssocValue = 0
    return g


def relax_found_relations(builder):
    """Relax the relations NX infers for a relation or drag operation; returns their count.

    NX 2406's modern solver also maintains *found* relations during these
    operations: a Make Equal (radius) between circles A and B also resized an
    unrelated circle C that merely had B's radius. Relaxing every found relation
    keeps the change to the persistent constraints and the selected curves.
    """
    find = getattr(builder, "FindRelations", None)
    if find is None:
        return 0
    found = list(find() or [])
    for relation in found:
        builder.SetRelationRelaxState(relation, True)
    return len(found)


def drag(tx, sk, curve, which, target=None):
    """Solve the sketch by dragging one curve point to sketch-local ``target``.

    Without ``target`` the drag has zero length: NX solves every persistent
    constraint (it may still move that point). Returns the drag solver status
    name (``Updated`` when solved, ``UpdateFailed`` when NX could not solve).
    A solve that made NX delete curves fails (``require_kept``).
    """
    from nx_mcp.kit import builders

    s = _sketch_of(sk)
    nx = tx.nx
    g = nx.Sketch.SketchGeometry()
    g.Geometry = curve
    g.PointType = getattr(nx.Sketch.PointType, DRAG_POINT[which])
    g.PointIndex = 0
    start = point(tx, curve, which)
    goal = to_part(frame(s), target) if target is not None else start
    kept = curve_labels(s)
    with builders.build(tx, tx.part.Sketches.CreateDragGeometryBuilder) as b:
        b.SetDragGeometry([g])
        b.InitializeDrag(nx.Point3d(*(float(v) for v in start)))
        relax_found_relations(b)
        b.DragToPoint(nx.Point3d(*(float(v) for v in goal)))
        raw = b.GetSolverStatus()
        b.Commit()
    require_kept(s, kept)
    s.Update()
    return enum_name(raw, nx.SketchDragGeometryBuilder.SolverStatus)


# ---------------------------------------------------------------------------
# Solves that make NX delete curves
#
# NX 2406 finding (live, 2026-10-12): when a drag (``SketchDragGeometryBuilder``) or a
# relation builder (``SketchMake*Builder``) solves to a configuration in which curves
# collapse (a point held on two parallel sides of one rectangle, a point held on two
# sides that meet at a corner, a vertex pulled onto the side its neighbour ends on, two
# ends of one curve made coincident), NX deletes the collapsed curves and their
# constraints when the builder ends, on ``Commit`` *and* on ``Destroy``. Interactive NX
# first opens the modal message box "Edit Sketch: The edit causes curves or dimensions
# to be deleted." [OK], which blocks NX and every bridge call until a person answers it
# (batch NX deletes silently). ``DragToPoint`` itself shows nothing, but its solution
# cannot be taken back: ``RestoreOperation``, a drag back, deleting the new constraints
# and ``UI.LockAccess`` all still delete (and show the box); ``UndoToMark`` with the
# builder alive crashed NX. So the requests that do it are refused before NX solves
# (``collapse_risks``, used by the constraint tool), and every solve checks afterwards
# that no curve vanished (``require_kept``: batch NX, or an engineer who answered OK).


def curve_labels(sk) -> dict:
    """``{tag: label}`` of the sketch's geometry, read before a solve (a deleted curve
    can no longer be asked its name)."""
    labels = {}
    for c in _sketch_of(sk).GetAllGeometry():
        name = ""
        with contextlib.suppress(Exception):
            name = str(getattr(c, "JournalIdentifier", "") or getattr(c, "Name", "") or "")
        labels[int(c.Tag)] = name or f"tag {int(c.Tag)}"
    return labels


def require_kept(sk, labels, field=None) -> None:
    """Fail when a solve deleted sketch geometry ``labels`` (``curve_labels``) held."""
    gone = sorted(label for tag, label in labels.items() if tag not in tags(sk))
    if gone:
        fail(
            "NX_SKETCH_OVERCONSTRAINED",
            f"NX could only solve the change by deleting {len(gone)} sketch curve(s) "
            f"({', '.join(gone)}): the constraints collapse them; rolled back",
            hint="The new constraint conflicts with the existing ones (nx_get_sketch lists "
            "the constraints of these curves): delete or change the conflicting one, or "
            "constrain other geometry",
            field=field,
            details={"deleted_curves": gone},
        )


def ends(tx, curve) -> list:
    """``[(which, part xyz)]`` of the start and end of a line or open arc ([] otherwise)."""
    if curve_kind(tx, curve) not in ("line", "arc"):
        return []
    return [(w, point(tx, curve, w)) for w in ("start", "end")]


def _meet(p, q) -> bool:
    return geom.dist(p, q) <= geom.tol(p, q) * 10


def adjacent(tx, a, b) -> bool:
    """Whether an end of curve ``a`` coincides with an end of curve ``b`` (a corner)."""
    return any(_meet(p, q) for _, p in ends(tx, a) for _, q in ends(tx, b))


def connected(tx, sk, a, b) -> bool:
    """Whether curves ``a`` and ``b`` belong to one chain of curves joined end to end."""
    curves = [c for c in geometry(sk) if ends(tx, c)]
    target, seen, todo = int(b.Tag), {int(a.Tag)}, [a]
    while todo:
        here = todo.pop()
        if int(here.Tag) == target:
            return True
        for c in curves:
            if int(c.Tag) not in seen and adjacent(tx, here, c):
                seen.add(int(c.Tag))
                todo.append(c)
    return False


def curves_at(tx, sk, p) -> list:
    """``[(curve, which)]``: the curve ends of the sketch that lie at part point ``p``."""
    return [(c, w) for c in geometry(sk) for w, q in ends(tx, c) if _meet(p, q)]


def held_on(tx, sk, curve, which) -> list:
    """``[(target curve, constraint)]``: the curves a persistent point-on-curve or
    midpoint constraint holds the point ``which`` of ``curve`` on."""
    nx = tx.nx
    out = []
    for c in attached(tx, sk, curve):
        if constraint_type(tx, c) not in ("point_on_curve", "midpoint"):
            continue
        get = getattr(c, "GetGeometry", None)
        try:
            rows = list(get() or []) if get is not None else []
        except Exception:
            continue
        held = target = None
        for g in rows:
            obj = getattr(g, "Geometry", None)
            if obj is None:
                continue
            name = enum_name(g.PointType, nx.Sketch.ConstraintPointType)
            if int(obj.Tag) == int(curve.Tag) and CONSTRAINT_POINT.get(which) == name:
                held = obj
            elif name not in CONSTRAINT_POINT.values():
                target = obj
        if held is not None and target is not None:
            out.append((target, c))
    return out


def _line_gap(tx, a, b) -> float:
    """Distance between two parallel lines (from ``b``'s start to ``a``'s line)."""
    s, e, p = point(tx, a, "start"), point(tx, a, "end"), point(tx, b, "start")
    d = geom.sub(e, s)
    t = geom.dot(geom.sub(p, s), d) / geom.dot(d, d)
    return geom.dist(p, geom.add(s, geom.scale(d, t)))


def _parallel_lines(tx, a, b) -> bool:
    u = geom.sub(point(tx, a, "end"), point(tx, a, "start"))
    v = geom.sub(point(tx, b, "end"), point(tx, b, "start"))
    return geom.norm(geom.cross(u, v)) <= 1e-9 * geom.norm(u) * geom.norm(v)


def collapse_risks(tx, sk, kind, points, target=None) -> list:
    """Why solving constraint ``kind`` would collapse curves (NX then deletes them behind
    the "Edit Sketch" dialog): ``[{"reason": text, "curves": [...], "constraint": c}]``,
    empty when no known collapse applies. ``points``: ``[(curve, which)]`` of the request;
    ``target``: the curve of a ``point_on_curve`` or ``midpoint`` request.

    Known collapses (each seen live on NX 2406):
    - a point held on line L, put on a line T parallel to L and joined to it by other
      curves (a rectangle's opposite sides): the curves between them collapse;
    - a point held on line L, put on a line T that meets L at a corner: NX deletes curves;
    - a vertex put on line T while a curve at that vertex already ends on T (a rectangle
      corner onto the far side): that curve folds onto T;
    - a midpoint on line T for a point at one of T's own ends: T shrinks to nothing;
    - coincident ends of one curve (also through the curves ending there)."""
    risks: list = []
    if kind == "coincident" and len(points) == 2:
        (a, pa), (b, pb) = points
        here = curves_at(tx, sk, point(tx, a, pa)) if pa != "center" else []
        there = curves_at(tx, sk, point(tx, b, pb)) if pb != "center" else []
        here = here or [(a, pa)]
        there = there or [(b, pb)]
        for c, w in here:
            for d, v in there:
                if int(c.Tag) == int(d.Tag) and w != v:
                    risks.append(
                        {
                            "reason": "both points are ends of the same curve, which would "
                            "shrink to nothing",
                            "curves": [c],
                        }
                    )
        return risks[:1]
    if kind not in ("point_on_curve", "midpoint") or target is None or not points:
        return risks
    if curve_kind(tx, target) != "line":
        return risks
    src, which = points[0]
    for line, constraint in held_on(tx, sk, src, which):
        if int(line.Tag) == int(target.Tag) or curve_kind(tx, line) != "line":
            continue
        if _parallel_lines(tx, line, target):
            gap = _line_gap(tx, line, target)
            if gap > geom.tol(point(tx, line, "start"), point(tx, target, "start")) * 10 and (
                connected(tx, sk, line, target)
            ):
                risks.append(
                    {
                        "reason": f"the point is already held on a parallel line {gap:.6g} away "
                        "that other curves join to this one: the curves between them would "
                        "collapse",
                        "curves": [line],
                        "constraint": constraint,
                    }
                )
        elif adjacent(tx, line, target):
            risks.append(
                {
                    "reason": "the point is already held on a line that meets this one at a "
                    "corner: it could only sit on the corner, which NX solves by deleting "
                    "curves (use a coincident constraint with the corner instead)",
                    "curves": [line],
                    "constraint": constraint,
                }
            )
    if which == "center":
        return risks
    at = point(tx, src, which)
    joined = curves_at(tx, sk, at)
    if kind == "midpoint" and any(int(c.Tag) == int(target.Tag) for c, _ in joined):
        risks.append(
            {
                "reason": "the point is an end of this line: its midpoint there would shrink "
                "the line to nothing",
                "curves": [target],
            }
        )
    for c, w in joined:
        if int(c.Tag) == int(target.Tag):
            continue
        other = point(tx, c, "end" if w == "start" else "start")
        if any(_meet(other, q) for _, q in ends(tx, target)):
            risks.append(
                {
                    "reason": "a curve ending at this point already ends on this line: it "
                    "would fold onto the line",
                    "curves": [c],
                }
            )
    return risks


def set_construction(tx, curves, on):
    """Convert sketch curves to construction (reference) curves, or back to active."""
    from nx_mcp.kit import builders

    nx = tx.nx
    with builders.build(tx, tx.part.Sketches.CreateConvertToFromReferenceBuilder) as b:
        kinds = nx.ConvertToFromReferenceBuilder.OutputType
        b.OutputState = kinds.Reference if on else kinds.Active
        b.InputObjects.Add(list(curves))
        b.Commit()


# ---- ellipses (pure math, sketch-local degrees) ----------------------------------


def ellipse_perimeter(a, b):
    """Exact ellipse perimeter ``4a E(e)`` by the arithmetic-geometric mean.

    Ramanujan II is off by 1.2e-5 at b/a = 0.1, which made a 1e-6 read-back
    reject valid thin ellipses on NX 2406.
    """
    a, b = max(a, b), min(a, b)
    x, g = 1.0, b / a
    s, p = 0.5 * (1.0 - g * g), 0.5
    while abs(x - g) > 1e-15 * x:
        c = (x - g) / 2
        x, g = (x + g) / 2, math.sqrt(x * g)
        p *= 2
        s += p * c * c
    return 2 * math.pi * a / x * (1 - s)


def ellipse_point(center, a, b, rotation, angle):
    """Local point of an ellipse at a POLAR angle (degrees from the major axis, seen
    from the center): NX 2406 ellipse start/end angles are polar, not parametric
    (live: -45..45 on a 20x10 ellipse ends at radius 12.649)."""
    r, t = math.radians(rotation), math.radians(angle)
    radius = a * b / math.hypot(b * math.cos(t), a * math.sin(t))
    u, v = radius * math.cos(t), radius * math.sin(t)
    return [
        center[0] + u * math.cos(r) - v * math.sin(r),
        center[1] + u * math.sin(r) + v * math.cos(r),
    ]


def ellipse_in_frame(x, y, z, rotation, start, end, f):
    """Ellipse rotation/start/end (degrees) in the sketch frame ``f``.

    NX stores them relative to the curve's own matrix; a mirrored ellipse gets
    its own (often flipped) matrix (live NX 2406: rotation 0 for a mirror whose
    major axis lies at 150 degrees). A flipped normal reverses the angle sense,
    so the arc [s, e] becomes [-e, -s].
    """
    r = math.radians(rotation)
    axis = [math.cos(r) * x[i] + math.sin(r) * y[i] for i in range(3)]
    local = math.degrees(math.atan2(geom.dot(axis, f["y_axis"]), geom.dot(axis, f["x_axis"])))
    if geom.dot(z, f["normal"]) < 0:
        start, end = -end, -start
    return local, start, end
