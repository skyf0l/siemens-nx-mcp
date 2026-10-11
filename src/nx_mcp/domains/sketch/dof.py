"""Remaining degrees of freedom of a sketch, from its persistent constraints.

NX 2406 has no scalar DOF query: ``Sketch.GetStatus()`` (also per work region)
counts the curves that are not fully constrained, ``UF_SKET_ask_sketch_status``
answers NotEvaluated for modern-solver sketches (and its initialize/terminate pair
deactivates the active sketch), ``SketchFindMovableObjectsBuilder`` commits nothing
and the drag builder's solver export is empty. So the DOF is computed here.

Every sketch curve is a parameter vector in the sketch plane (line: its end points;
arc: center, radius, end angles; circle: center, radius; point), every persistent
constraint and driving dimension one or more equations on them. The rank of the
equations' Jacobian (central differences, each connected cluster of curves on its
own) gives the freedom left: total = parameters - rank; a curve's own DOF is the
rank of the null space restricted to its parameters, i.e. how many independent ways
the curve can still move (freedom shared by connected curves counts on each).
Geometry outside the sketch (datum axes, other curves, points) is fixed.

The result is checked against NX: as many curves must have DOF > 0 as NX counts as
not fully constrained, else it is withheld (None), as it is for a curve or constraint
kind not modelled here (ellipses, splines, mirror, offset, pattern, slope...).
"""

from __future__ import annotations

import math

from nx_mcp.kit import geom
from nx_mcp.kit import sketch as ksketch
from nx_mcp.kit.result import enum_name

# NX point kinds as results name them ("curve": the whole curve).
CONSTRAINT_POINTS = {
    "NotSet": "curve",
    "StartVertex": "start",
    "EndVertex": "end",
    "ArcCenter": "center",
    "MidVertex": "mid",
    "SplineDefiningPoint": "spline_point",
    "Anchor": "anchor",
    "SplinePole": "pole",
}
DIMENSION_POINTS = {
    "NotSet": "curve",
    "StartPoint": "start",
    "EndPoint": "end",
    "ArcCenter": "center",
    "Midpoint": "mid",
    "Tangency": "tangency",
    "CurvePoint": "curve_point",
    "AnchorPoint": "anchor",
}
NOT_DRIVING = ("Reference", "Preview")
MAX_CLUSTER = 240  # parameters of one connected cluster (dense elimination is cubic)
PIVOT_TOL = 1e-7
SIZES = {"line": 4, "arc": 5, "circle": 3, "point": 2}


class Unsupported(Exception):
    """A curve or constraint kind this model does not cover."""


def geometry(tx, constraint):
    """``[(object, point)]`` a constraint or dimension uses, in NX order. ``point`` is
    start, end, center, mid, curve (the whole curve) or another NX point kind; None
    when NX does not say."""
    nx = tx.nx
    get = getattr(constraint, "GetDimensionGeometry", None)
    if get is not None:
        names, enum, attr = DIMENSION_POINTS, nx.Sketch.AssocType, "AssocType"
    else:
        get = getattr(constraint, "GetGeometry", None)
        names, enum, attr = CONSTRAINT_POINTS, nx.Sketch.ConstraintPointType, "PointType"
    if get is None:
        return None
    try:
        items = list(get() or [])
    except Exception:  # NX refuses for some constraint kinds: nothing to report
        return None
    out = []
    for g in items:
        obj = getattr(g, "Geometry", None)
        if obj is None:
            continue
        name = enum_name(getattr(g, attr, None), enum, list(names))
        out.append((obj, names.get(name, ksketch.snake(name))))
    return out


def is_dimension(constraint) -> bool:
    return getattr(constraint, "GetDimensionGeometry", None) is not None


def driving(tx, constraint) -> bool:
    """A geometric constraint or a driving dimension (reference dimensions measure only;
    NX 2406 gives them no expression)."""
    if not is_dimension(constraint):
        return True
    state = getattr(constraint, "DimensionState", None)
    holder = getattr(tx.nx, "SketchDimensionalConstraint", None)
    enum = getattr(holder, "DimensionStateType", None)
    if state is not None and enum is not None:
        return enum_name(state, enum) not in NOT_DRIVING
    return getattr(constraint, "AssociatedExpression", None) is not None


# ---------------------------------------------------------------------------
# 2D vectors: kit.geom (sub, dot, cross2, norm) takes them as they are.


def _unit2d(a):
    """Unit 2D vector; a zero-length line is a sketch this model does not analyse."""
    n = geom.norm(a)
    if n <= 0.0:
        raise Unsupported("degenerate line")
    return (a[0] / n, a[1] / n)


# ---------------------------------------------------------------------------
# The model


class _Entry:
    __slots__ = ("kind", "offset", "values")

    def __init__(self, kind, values, offset):
        self.kind, self.values, self.offset = kind, values, offset


class Model:
    """Parameters of the sketch curves and equations of the constraints on them."""

    def __init__(self, tx, sk):
        self.tx = tx
        s = sk.sketch if isinstance(sk, ksketch.Editing) else sk
        self.frame = ksketch.frame(s)
        self.curves = ksketch.geometry(s)
        self.entries: dict[int, _Entry] = {}
        self.x0: list[float] = []
        self.owner: list[int] = []  # curve tag of each parameter
        self.angle: list[bool] = []  # angle parameters (scaled by the sketch size)
        extent = [1.0]
        for c in self.curves:
            kind, values = self._read(c)
            self.entries[int(c.Tag)] = _Entry(kind, values, len(self.x0))
            self.x0 += values
            self.owner += [int(c.Tag)] * len(values)
            self.angle += [kind == "arc" and i >= 3 for i in range(len(values))]
            extent += [abs(v) for i, v in enumerate(values) if not (kind == "arc" and i >= 3)]
        self.scale = max(extent)
        self.equations: list[tuple[object, list[int]]] = []  # (residual fn, curve tags)
        for con in ksketch.constraints(tx, s):
            if driving(tx, con):
                self._add(con)

    # -- reading geometry ---------------------------------------------------------------
    def _local(self, p3):
        u = ksketch.to_local(self.frame, p3)
        return (float(u[0]), float(u[1]))

    def _read(self, obj):
        tx, nx = self.tx, self.tx.nx
        point_class = getattr(nx, "Point", None)
        if isinstance(point_class, type) and isinstance(obj, point_class):
            return "point", list(self._local(geom.xyz(obj.Coordinates)))
        kind = ksketch.curve_kind(tx, obj)
        if kind == "line":
            s, e = self._local(geom.xyz(obj.StartPoint)), self._local(geom.xyz(obj.EndPoint))
            return "line", [s[0], s[1], e[0], e[1]]
        if kind == "circle":
            c = self._local(geom.xyz(obj.CenterPoint))
            return "circle", [c[0], c[1], float(obj.Radius)]
        if kind == "arc":
            c = self._local(geom.xyz(obj.CenterPoint))
            ends = [self._local(ksketch.point(tx, obj, w)) for w in ("start", "end", "mid")]
            a0, a1, am = (math.atan2(p[1] - c[1], p[0] - c[0]) for p in ends)
            # counterclockwise or clockwise in the sketch frame: the sweep whose middle
            # is the arc's middle point
            ccw = (a1 - a0) % (2 * math.pi) or 2 * math.pi
            sweep = min(
                (ccw, ccw - 2 * math.pi),
                key=lambda w: abs(math.remainder(a0 + w / 2 - am, 2 * math.pi)),
            )
            return "arc", [c[0], c[1], float(obj.Radius), a0, a0 + sweep]
        axis = getattr(nx, "DatumAxis", None)
        if isinstance(axis, type) and isinstance(obj, axis):
            o = geom.xyz(obj.Origin)
            d = geom.xyz(obj.Direction)
            s, e = self._local(o), self._local(geom.add(o, d))
            return "line", [s[0], s[1], e[0], e[1]]
        raise Unsupported(f"{kind} geometry")

    def _entry(self, obj):
        tag = int(obj.Tag)
        entry = self.entries.get(tag)
        if entry is None:  # geometry outside the sketch: fixed
            kind, values = self._read(obj)
            entry = self.entries[tag] = _Entry(kind, values, None)
        return entry

    # -- evaluating geometry at parameters x ---------------------------------------------
    def _values(self, x, obj):
        e = self._entry(obj)
        if e.offset is None:
            return e.kind, e.values
        return e.kind, x[e.offset : e.offset + SIZES[e.kind]]

    def point(self, x, obj, which):
        kind, v = self._values(x, obj)
        if kind == "point":
            return (v[0], v[1])
        if which == "center" and kind in ("arc", "circle"):
            return (v[0], v[1])
        if kind == "line" and which in ("start", "end", "mid"):
            if which == "mid":
                return ((v[0] + v[2]) / 2, (v[1] + v[3]) / 2)
            return (v[0], v[1]) if which == "start" else (v[2], v[3])
        if kind == "arc" and which in ("start", "end", "mid"):
            a = {"start": v[3], "end": v[4], "mid": (v[3] + v[4]) / 2}[which]
            return (v[0] + v[2] * math.cos(a), v[1] + v[2] * math.sin(a))
        raise Unsupported(f"{which} of a {kind}")

    def line(self, x, obj):
        kind, v = self._values(x, obj)
        if kind != "line":
            raise Unsupported(f"a {kind} as a line")
        return (v[0], v[1]), (v[2] - v[0], v[3] - v[1])

    def round(self, x, obj):
        kind, v = self._values(x, obj)
        if kind not in ("arc", "circle"):
            raise Unsupported(f"a {kind} as an arc")
        return (v[0], v[1]), v[2]

    def length(self, x, obj):
        kind, v = self._values(x, obj)
        if kind == "line":
            return math.hypot(v[2] - v[0], v[3] - v[1])
        if kind == "arc":
            return v[2] * abs(v[4] - v[3])
        if kind == "circle":
            return 2 * math.pi * v[2]
        raise Unsupported(f"length of a {kind}")

    # -- equations ----------------------------------------------------------------------
    def _add(self, con):
        entries = geometry(self.tx, con)
        if not entries:
            raise Unsupported("constraint geometry")
        kind = ksketch.constraint_type(self.tx, con)
        build = EQUATIONS.get(kind)
        if build is None:
            raise Unsupported(f"{kind} constraint")
        for obj, which in entries:  # every object must be readable
            self._entry(obj)
            if which not in ("curve", "start", "end", "center", "mid"):
                raise Unsupported(f"{which} point")
        fn = build(self, entries)
        x0 = list(self.x0)
        fn(x0)  # evaluable at the current geometry, else Unsupported
        tags = [int(o.Tag) for o, _ in entries if self.entries[int(o.Tag)].offset is not None]
        self.equations.append((fn, tags))


# Each builder returns residual(x) -> list of floats; values at the current geometry
# (self.x0) are the reference of fixed/constant constraints.


def _points(m, entries):
    return [(o, w) for o, w in entries if w != "curve" or m._entry(o).kind == "point"]


def _curves(m, entries):
    return [(o, w) for o, w in entries if w == "curve" and m._entry(o).kind != "point"]


def _fixed(m, entries):
    parts = []
    for obj, which in entries:
        kind = m._entry(obj).kind
        if which != "curve" or kind == "point":
            p0 = m.point(m.x0, obj, which)
            parts.append(lambda x, o=obj, w=which, p0=p0: list(geom.sub(m.point(x, o, w), p0)))
        elif kind == "line":  # NX's line fix holds the infinite line only
            s0, d0 = m.line(m.x0, obj)
            u0 = _unit2d(d0)
            parts.append(
                lambda x, o=obj, s0=s0, u0=u0: [
                    geom.cross2(_unit2d(m.line(x, o)[1]), u0),
                    geom.cross2(u0, geom.sub(m.line(x, o)[0], s0)),
                ]
            )
        else:
            c0, r0 = m.round(m.x0, obj)
            parts.append(
                lambda x, o=obj, c0=c0, r0=r0: [
                    *geom.sub(m.round(x, o)[0], c0),
                    m.round(x, o)[1] - r0,
                ]
            )
    return lambda x: [v for part in parts for v in part(x)]


def _axis(index):
    def build(m, entries):
        pts = _points(m, entries)
        if len(pts) == 2:
            (a, wa), (b, wb) = pts
            return lambda x: [m.point(x, b, wb)[index] - m.point(x, a, wa)[index]]
        (obj, _), *_rest = entries
        return lambda x: [_unit2d(m.line(x, obj)[1])[index]]

    return build


def _two_lines(fn):
    def build(m, entries):
        (a, _), (b, _) = entries[:2]
        return lambda x: [fn(_unit2d(m.line(x, a)[1]), _unit2d(m.line(x, b)[1]))]

    return build


def _collinear(m, entries):
    (a, _), (b, _) = entries[:2]

    def residual(x):
        sa, da = m.line(x, a)
        sb, db = m.line(x, b)
        u = _unit2d(da)
        return [geom.cross2(u, _unit2d(db)), geom.cross2(u, geom.sub(sb, sa)) / m.scale]

    return residual


def _equal_length(m, entries):
    (a, _), (b, _) = entries[:2]
    return lambda x: [m.length(x, a) - m.length(x, b)]


def _equal_radius(m, entries):
    (a, _), (b, _) = entries[:2]
    return lambda x: [m.round(x, a)[1] - m.round(x, b)[1]]


def _constant_length(m, entries):
    (a, _), *_rest = entries
    n0 = m.length(m.x0, a)
    return lambda x: [m.length(x, a) - n0]


def _constant_angle(m, entries):
    (a, _), *_rest = entries
    u0 = _unit2d(m.line(m.x0, a)[1])
    return lambda x: [geom.cross2(_unit2d(m.line(x, a)[1]), u0)]


def _coincident(m, entries):
    (a, wa), (b, wb) = entries[:2]
    wa = "center" if wa == "curve" and m._entry(a).kind in ("arc", "circle") else wa
    wb = "center" if wb == "curve" and m._entry(b).kind in ("arc", "circle") else wb
    return lambda x: list(geom.sub(m.point(x, a, wa), m.point(x, b, wb)))


def _point_and_curve(m, entries):
    pts, curves = _points(m, entries), _curves(m, entries)
    if len(pts) != 1 or len(curves) != 1:
        raise Unsupported("point and curve")
    return pts[0], curves[0][0]


def _on_curve(m, x, p, curve):
    if m._entry(curve).kind == "line":
        s, d = m.line(x, curve)
        return geom.cross2(_unit2d(d), geom.sub(p, s))
    c, r = m.round(x, curve)
    return geom.norm(geom.sub(p, c)) - r


def _point_on_curve(m, entries):
    (obj, which), curve = _point_and_curve(m, entries)
    return lambda x: [_on_curve(m, x, m.point(x, obj, which), curve)]


def _midpoint(m, entries):
    # NX's midpoint holds the point on the perpendicular bisector of the curve's ends
    (obj, which), curve = _point_and_curve(m, entries)

    def residual(x):
        p = m.point(x, obj, which)
        s, e = m.point(x, curve, "start"), m.point(x, curve, "end")
        return [geom.norm(geom.sub(p, s)) - geom.norm(geom.sub(p, e))]

    return residual


def _tangent(m, entries):
    (a, _), (b, _) = entries[:2]
    ka, kb = m._entry(a).kind, m._entry(b).kind
    if ka == "line" and kb == "line":
        raise Unsupported("tangent lines")
    if kb == "line":
        a, b, ka, kb = b, a, kb, ka
    if ka == "line":
        s0, d0 = m.line(m.x0, a)
        side = math.copysign(1.0, geom.cross2(_unit2d(d0), geom.sub(m.round(m.x0, b)[0], s0)))

        def residual(x):
            s, d = m.line(x, a)
            c, r = m.round(x, b)
            return [side * geom.cross2(_unit2d(d), geom.sub(c, s)) - r]

        return residual
    (c1, r1), (c2, r2) = m.round(m.x0, a), m.round(m.x0, b)
    gap = geom.norm(geom.sub(c1, c2))
    outside = abs(gap - (r1 + r2)) <= abs(gap - abs(r1 - r2))
    sign = 1.0 if r1 >= r2 else -1.0

    def residual2(x):
        (p, ra), (q, rb) = m.round(x, a), m.round(x, b)
        d = geom.norm(geom.sub(p, q))
        return [d - (ra + rb) if outside else d - sign * (ra - rb)]

    return residual2


def _radius(m, entries):
    (a, _), *_rest = entries
    return lambda x: [m.round(x, a)[1]]


def _axis_dim(index):
    def build(m, entries):
        pts = _points(m, entries)
        if len(pts) == 2:
            (a, wa), (b, wb) = pts
            return lambda x: [m.point(x, b, wb)[index] - m.point(x, a, wa)[index]]
        (obj, _), *_rest = entries
        return lambda x: [m.line(x, obj)[1][index]]

    return build


def _parallel_dim(m, entries):
    pts = _points(m, entries)
    if len(pts) == 2:
        (a, wa), (b, wb) = pts
        return lambda x: [geom.norm(geom.sub(m.point(x, b, wb), m.point(x, a, wa)))]
    (obj, _), *_rest = entries
    return lambda x: [m.length(x, obj)]


def _perpendicular_dim(m, entries):
    lines = [i for i, (o, w) in enumerate(entries) if w == "curve" and m._entry(o).kind == "line"]
    if not lines or len(entries) != 2:
        raise Unsupported("perpendicular dimension")
    line = entries[lines[0]][0]
    obj, which = entries[1 - lines[0]]
    which = "start" if which == "curve" and m._entry(obj).kind == "line" else which
    return lambda x: [_on_curve(m, x, m.point(x, obj, which), line)]


def _angular_dim(m, entries):
    (a, _), (b, _) = entries[:2]

    def residual(x):
        u, v = _unit2d(m.line(x, a)[1]), _unit2d(m.line(x, b)[1])
        return [math.atan2(geom.cross2(u, v), geom.dot(u, v))]

    return residual


def _perimeter_dim(m, entries):
    objs = [o for o, _ in entries]
    return lambda x: [sum(m.length(x, o) for o in objs)]


EQUATIONS = {
    "fixed": _fixed,
    "horizontal": _axis(1),
    "vertical": _axis(0),
    "parallel": _two_lines(geom.cross2),
    "perpendicular": _two_lines(geom.dot),
    "collinear": _collinear,
    "equal_length": _equal_length,
    "equal_radius": _equal_radius,
    "constant_length": _constant_length,
    "constant_angle": _constant_angle,
    "coincident": _coincident,
    "concentric": _coincident,
    "point_on_curve": _point_on_curve,
    "midpoint": _midpoint,
    "tangent": _tangent,
    "radius_dim": _radius,
    "diameter_dim": _radius,
    "horizontal_dim": _axis_dim(0),
    "vertical_dim": _axis_dim(1),
    "parallel_dim": _parallel_dim,
    "perpendicular_dim": _perpendicular_dim,
    "angular_dim": _angular_dim,
    "minor_angular_dim": _angular_dim,
    "major_angular_dim": _angular_dim,
    "perimeter_dim": _perimeter_dim,
}


# ---------------------------------------------------------------------------
# Linear algebra (pure Python: NX's interpreter has no numpy)


def _rref(rows, n):
    """Reduced row echelon form of ``rows`` (each scaled to max |v| = 1 first); returns
    the nonzero rows and their pivot columns."""
    work = []
    for r in rows:
        top = max((abs(v) for v in r), default=0.0)
        if top > PIVOT_TOL:
            work.append([v / top for v in r])
    pivots: list[int] = []
    rank = 0
    for col in range(n):
        if rank == len(work):
            break
        best = max(range(rank, len(work)), key=lambda i: abs(work[i][col]))
        if abs(work[best][col]) <= PIVOT_TOL:
            continue
        work[rank], work[best] = work[best], work[rank]
        inv = 1.0 / work[rank][col]
        pivot = [v * inv for v in work[rank]]
        work[rank] = pivot
        for i in range(len(work)):
            f = work[i][col]
            if i != rank and f != 0.0:
                work[i] = [a - f * b for a, b in zip(work[i], pivot, strict=True)]
        pivots.append(col)
        rank += 1
    return work[:rank], pivots


def _rank(rows, n):
    return len(_rref(rows, n)[1])


def _jacobian(m, eqs, columns):
    """Rows of d(residual)/d(parameter) over ``columns`` (global parameter indices);
    angle columns are scaled by the sketch size so every column is a length."""
    index = {c: k for k, c in enumerate(columns)}
    h = 1e-6 * m.scale
    rows = []
    for fn, tags in eqs:
        x = list(m.x0)
        base = fn(x)
        block = [[0.0] * len(columns) for _ in base]
        for tag in set(tags):
            e = m.entries[tag]
            for i in range(e.offset, e.offset + SIZES[e.kind]):
                step = h / m.scale if m.angle[i] else h
                x[i] = m.x0[i] + step
                hi = fn(x)
                x[i] = m.x0[i] - step
                lo = fn(x)
                x[i] = m.x0[i]
                weight = m.scale if m.angle[i] else 1.0
                for r in range(len(base)):
                    block[r][index[i]] = (hi[r] - lo[r]) / (2 * step) / weight
        rows += block
    return rows


def _clusters(m):
    """Curves joined by constraints: ``[(curve tags, equations)]``."""
    parent = {t: t for t, e in m.entries.items() if e.offset is not None}

    def root(t):
        while parent[t] != t:
            parent[t] = parent[parent[t]]
            t = parent[t]
        return t

    for _fn, tags in m.equations:
        for t in tags[1:]:
            parent[root(t)] = root(tags[0])
    groups: dict[int, tuple[list[int], list]] = {}
    for t in parent:
        groups.setdefault(root(t), ([], []))[0].append(t)
    for eq in m.equations:
        if eq[1]:
            groups[root(eq[1][0])][1].append(eq)
    return list(groups.values())


def analyse(tx, sk):
    """``{"dof": total, "curves": {tag: dof}}`` of the sketch, or None when a curve or
    constraint kind is not modelled (or a cluster is too large)."""
    try:
        m = Model(tx, sk)
        total, per_curve = 0, {}
        for tags, eqs in _clusters(m):
            columns = [
                i
                for t in tags
                for i in range(m.entries[t].offset, m.entries[t].offset + SIZES[m.entries[t].kind])
            ]
            if len(columns) > MAX_CLUSTER:
                return None
            reduced, pivots = _rref(_jacobian(m, eqs, columns), len(columns))
            free = [c for c in range(len(columns)) if c not in set(pivots)]
            total += len(free)
            basis = []
            for f in free:
                v = [0.0] * len(columns)
                v[f] = 1.0
                for row, p in zip(reduced, pivots, strict=True):
                    v[p] = -row[f]
                top = max(abs(a) for a in v)
                basis.append([a / top for a in v])
            position = {c: k for k, c in enumerate(columns)}
            for t in tags:
                own = [position[i] for i, o in enumerate(m.owner) if o == t]
                per_curve[t] = _rank([[v[k] for k in own] for v in basis], len(own))
        return {"dof": total, "curves": per_curve}
    except (Unsupported, ZeroDivisionError, ValueError):
        return None


def checked(tx, sk, native_count):
    """:func:`analyse` when it agrees with NX's count of curves not fully constrained
    (``native_count``), else None."""
    if native_count is None:
        return None
    found = analyse(tx, sk)
    if found is None:
        return None
    if sum(1 for v in found["curves"].values() if v > 0) != native_count:
        return None
    return found
