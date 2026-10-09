"""Freeform extensions of the shared fake for the freeform unit tests.

``freeform_nx(units)`` is the modeling fake (auto-vivifying NXOpen namespace,
bounding boxes, mass properties) plus polyline curves and edges with UF ``Eval``
(exact closest points), UF minimum distance to points, polylines and boxes, face
derivatives for continuity (``AskFaceParm``/``EvaluateFace``), face areas and
Studio Spline builders whose committed curve is the polyline through the
defining points.
"""

from __future__ import annotations

import math
from types import SimpleNamespace as NS

from tests.kit.objects import Body, Edge, Face, Object, point
from tests.unit.modeling.fakes import feature, modeling_nx


def _sub(a, b):
    return [x - y for x, y in zip(a, b, strict=True)]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


class Polyline:
    """Shape of a fake curve or edge: a polyline with normalised parameter 0..1."""

    def __init__(self, points):
        self.points = [[float(c) for c in p] for p in points]

    @property
    def segments(self):
        return len(self.points) - 1

    def at(self, t):
        n = self.segments
        i = min(int(t * n), n - 1)
        local = t * n - i
        a, b = self.points[i], self.points[i + 1]
        return [a[k] + local * (b[k] - a[k]) for k in range(3)], _sub(b, a)

    def closest(self, p):
        best = None
        for i in range(self.segments):
            a, b = self.points[i], self.points[i + 1]
            d = _sub(b, a)
            s = max(0.0, min(1.0, _dot(_sub(p, a), d) / (_dot(d, d) or 1.0)))
            q = [a[k] + s * d[k] for k in range(3)]
            gap = math.dist(p, q)
            if best is None or gap < best[0]:
                best = (gap, (i + s) / self.segments, q)
        return best  # (distance, t, point)


def box_point(box, p):
    return [min(max(p[i], box[i]), box[i + 3]) for i in range(3)]


class Spline(Object):
    def __init__(self, points, degree, periodic):
        super().__init__("SPLINE")
        self.shape = Polyline(points)
        self.Order = degree + 1
        self.Periodic = periodic
        self.poles = [point(*p) for p in points]

    def Get3DPoles(self):
        return list(self.poles)


class StudioSpline(Object):
    FeatureType = "SPLINE"


def freeform_nx(units="mm"):
    nx = modeling_nx(units)
    objects = {}
    nx.objects = objects

    base = nx.remember

    def remember(obj):
        objects[int(obj.Tag)] = obj
        return base(obj)

    nx.remember = nx.keep = remember

    # ---- UF Eval over shapes ---------------------------------------------------------
    def evaluate(h, t):
        p, d = objects[h].shape.at(float(t))
        length = math.sqrt(_dot(d, d)) or 1.0
        return (p, [v / length for v in d])

    nx.uf.Eval = NS(
        Initialize=lambda tag: tag,
        AskLimits=lambda h: (0.0, 1.0),
        EvaluateUnitVectors=evaluate,
        EvaluateClosestPoint=lambda h, p: (objects[h].shape.closest(p)[1],),
        Free=lambda h: None,
    )

    def to_point(obj, p):
        if hasattr(obj, "shape"):
            gap, _t, q = obj.shape.closest(p)
            return gap, q
        q = box_point(obj.box, p)
        return math.dist(p, q), q

    def min_dist(a, b, _g1, _p1, given2, p2):
        first = objects[a]
        if not b:
            gap, q = to_point(first, list(p2))
            return (gap, q, list(p2))
        second = objects[b]
        samples = [first.shape.at(i / 50)[0] for i in range(51)] + first.shape.points
        gap, p = min((to_point(second, s)[0], s) for s in samples)
        return (gap, p, to_point(second, p)[1])

    nx.uf.Modeling.AskMinimumDist = min_dist

    def face_parm(tag, p):
        return (list(p), list(p))  # uv are the point itself; derivatives come from the face

    def evaluate_face(tag, _mode, uv):
        du, dv, duu, duv, dvv = objects[tag].derivatives(uv)
        return NS(SrfDu=du, SrfDv=dv, SrfD2u=duu, SrfDudv=duv, SrfD2v=dvv)

    conservative = nx.uf.ModlGeneral.AskBoundingBox

    def exact_box(tag, _csys):
        box = conservative(tag)
        return (box[:3], [1, 0, 0, 0, 1, 0, 0, 0, 1], [box[i + 3] - box[i] for i in range(3)])

    nx.uf.ModlGeneral.AskBoundingBoxExact = exact_box
    nx.uf.Modeling.AskFaceParm = face_parm
    nx.uf.Modeling.EvaluateFace = evaluate_face
    mass = nx.part.MeasureManager.NewMassProperties
    nx.part.MeasureManager = NS(
        NewMassProperties=mass,
        NewFaceProperties=lambda _a, _l, _acc, faces: NS(
            Area=sum(f.area for f in faces), Dispose=lambda: None
        ),
    )
    nx.part.CreateRegionPoint = lambda p, body: NS(kind="region", point=p, body=body)
    nx.nx.Features.StudioSpline = StudioSpline

    # ---- Studio Spline builder: the committed curve is the polyline ----------------------
    def spline_commit(b):
        points = [
            [
                float(c.Point.Coordinates.X),
                float(c.Point.Coordinates.Y),
                float(c.Point.Coordinates.Z),
            ]
            for c in (call[0][0] for call in b.ConstraintManager.Append.calls)
        ]
        b.Curve = remember(Spline(points, b.Degree, b.IsPeriodic))
        existing = b.args[0] if b.args else None
        if existing is not None:
            return existing
        f = studio_spline(nx)
        return f

    nx.on_commit("StudioSplineBuilderEx", spline_commit)
    return nx


def studio_spline(nx):
    f = feature(nx, "SPLINE")
    f.__class__ = type("StudioSpline", (StudioSpline, type(f)), {})
    return f


def curve(nx, *points, kind="curve"):
    """A work-part curve along ``points`` (a polyline)."""
    c = Object("CURVE")
    c.shape = Polyline(points)
    c.OwningPart, c.IsOccurrence = nx.part, False
    nx.keep(c)
    c.ref = nx.ref_of(c, kind)
    return c


def face(nx, derivatives, area=0.0, normal=(0, 0, 1), body=None, kind="plane"):
    f = Face(f"FACE({kind})")
    f.OwningPart, f.IsOccurrence = nx.part, False
    f.derivatives = derivatives
    f.area = area
    f.normal = list(normal)
    f.GetBody = lambda: body
    f.contains = lambda p: 1
    nx.face_data[f.Tag] = (kind, [0, 0, 0], list(normal))
    nx.keep(f)
    f.ref = nx.ref_of(f, "face")
    return f


def flat(du=(1, 0, 0), dv=(0, 1, 0)):
    """Derivatives of a plane spanned by du, dv (no curvature)."""
    zero = [0.0, 0.0, 0.0]
    return lambda uv: (list(du), list(dv), zero, zero, zero)


def edge(nx, a, b, faces=()):
    e = Edge("EDGE")
    e.shape = Polyline([a, b])
    e.SolidEdgeType = nx.nx.Edge.EdgeType.Linear
    e.OwningPart, e.IsOccurrence = nx.part, False
    e.GetVertices = lambda: (point(*a), point(*b))
    e.GetFaces = lambda: list(faces)
    nx.keep(e)
    e.ref = nx.ref_of(e, "edge")
    return e


def sheet(nx, box, faces=(), edges=()):
    """A sheet body with ``box`` [x0, y0, z0, x1, y1, z1]."""
    body = Body("SHEET", list(box))
    body.IsSolidBody, body.IsSheetBody = False, True
    body.OwningPart, body.IsOccurrence = nx.part, False
    body.volume = 0.0
    body.faces, body.edges = list(faces), list(edges)
    for f in body.faces:
        f.GetBody = lambda body=body: body
    nx.keep(body)
    nx.part.Bodies.append(body)
    body.ref = nx.ref_of(body, "body")
    return body


def made(nx, kind, bodies):
    """A committed feature owning ``bodies``."""
    return feature(nx, kind, list(bodies))
