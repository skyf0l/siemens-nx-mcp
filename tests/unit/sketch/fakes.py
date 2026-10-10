"""A fake NX sketch environment on top of ``tests.kit.fake_nx`` (sketch domain only).

It is a seam with a tiny solver, not a sketch kernel: lines, arcs and ellipses
with the native read members; persistent constraints and dimensions; a drag
that moves the dragged point and satisfies horizontal/vertical constraints; the
``SketchMake*Builder`` relations for the cases the tests use; driving dimensions
that move a line end or an arc radius unless the curve is fully fixed (then the
sketch turns OverConstrained, like NX 2406); UF Eval and minimum distance by
sampling.
"""

from __future__ import annotations

import itertools
import math
from types import SimpleNamespace as NS

from tests.kit.fake import FakeNX
from tests.kit.objects import Edge, Expression, Feature, Object, matrix, point
from tests.kit.objects import Sketch as BaseSketch

CT = NS(
    NoCon=0,
    Fixed=1,
    Horizontal=2,
    Vertical=3,
    Parallel=4,
    Perpendicular=5,
    Coincident=6,
    Concentric=7,
    EqualLength=8,
    EqualRadius=9,
    Tangent=10,
    Midpoint=11,
    PointOnCurve=12,
    Mirror=13,
    ParallelDim=20,
    HorizontalDim=21,
    VerticalDim=22,
    PerpendicularDim=23,
    AngularDim=24,
    RadiusDim=25,
    DiameterDim=26,
)
DIMENSIONS = {20, 21, 22, 23, 24, 25, 26}


def xyz(p):
    return [float(p.X), float(p.Y), float(p.Z)]


def P(v):  # noqa: N802 - mirrors NXOpen.Point3d
    return point(float(v[0]), float(v[1]), float(v[2]) if len(v) > 2 else 0.0)


class Curve(Object):
    IsReference = False


class Line(Curve):
    def __init__(self, a, b):
        super().__init__("LINE")
        self.StartPoint, self.EndPoint = P(xyz(a)), P(xyz(b))

    def GetLength(self):
        return math.dist(xyz(self.StartPoint), xyz(self.EndPoint))

    def SetEndpoints(self, a, b):
        self.StartPoint, self.EndPoint = P(xyz(a)), P(xyz(b))

    def at(self, t):
        s, e = xyz(self.StartPoint), xyz(self.EndPoint)
        return [s[i] + t * (e[i] - s[i]) for i in range(3)], [e[i] - s[i] for i in range(3)]


class Arc(Curve):
    def __init__(self, center, x, y, radius, start, end):
        super().__init__("ARC")
        z = [x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2], x[0] * y[1] - x[1] * y[0]]
        self.CenterPoint = P(xyz(center))
        self.Matrix = NS(Element=matrix(x, y, z))
        self.Radius, self.StartAngle, self.EndAngle = float(radius), float(start), float(end)

    def axes(self):
        m = self.Matrix.Element
        return [m.Xx, m.Xy, m.Xz], [m.Yx, m.Yy, m.Yz]

    def GetLength(self):
        return self.Radius * (self.EndAngle - self.StartAngle)

    def SetParameters(self, radius, center, start, end):
        self.Radius, self.CenterPoint = float(radius), P(xyz(center))
        self.StartAngle, self.EndAngle = float(start), float(end)

    def at(self, t):
        a = self.StartAngle + t * (self.EndAngle - self.StartAngle)
        (x, y), c, r = self.axes(), xyz(self.CenterPoint), self.Radius
        p = [c[i] + r * (math.cos(a) * x[i] + math.sin(a) * y[i]) for i in range(3)]
        d = [-math.sin(a) * x[i] + math.cos(a) * y[i] for i in range(3)]
        return p, d


class Ellipse(Curve):
    def __init__(self, center, a, b, start, end, rotation, orientation):
        super().__init__("ELLIPSE")
        self.CenterPoint = P(xyz(center))
        self.MajorRadius, self.MinorRadius = float(a), float(b)
        self.StartAngle, self.EndAngle, self.RotationAngle = (
            float(start),
            float(end),
            float(rotation),
        )
        self.Matrix = orientation

    def at(self, t):
        m = self.Matrix.Element
        x, y = [m.Xx, m.Xy, m.Xz], [m.Yx, m.Yy, m.Yz]
        polar = self.StartAngle + t * (self.EndAngle - self.StartAngle)
        a, b, r = self.MajorRadius, self.MinorRadius, self.RotationAngle
        rho = a * b / math.hypot(b * math.cos(polar), a * math.sin(polar))
        u, v = rho * math.cos(polar), rho * math.sin(polar)
        lx, ly = u * math.cos(r) - v * math.sin(r), u * math.sin(r) + v * math.cos(r)
        c = xyz(self.CenterPoint)
        return [c[i] + lx * x[i] + ly * y[i] for i in range(3)], [1.0, 0.0, 0.0]

    def GetLength(self):
        n = 4000
        pts = [self.at(k / n)[0] for k in range(n + 1)]
        return sum(math.dist(pts[k], pts[k + 1]) for k in range(n))


class Constraint(Object):
    def __init__(self, kind, geometry, sketch):
        super().__init__("CONSTRAINT")
        self.ConstraintType = kind
        self.geometry = list(geometry)  # [(curve, point type name)]
        self.sketch = sketch

    @property
    def curves(self):
        return [g for g, _ in self.geometry]


class Dimension(Constraint):
    def __init__(self, kind, geometry, sketch, expression, reference):
        super().__init__(kind, geometry, sketch)
        self.AssociatedExpression = expression
        self.reference = reference
        self.AssociatedDimension = NS()

    def measure(self):
        g = self.geometry
        if self.ConstraintType in (CT.RadiusDim, CT.DiameterDim):
            r = g[0][0].Radius
            return r if self.ConstraintType == CT.RadiusDim else 2 * r
        if self.ConstraintType == CT.AngularDim:
            u, v = (_dir(c) for c, _ in g)
            return math.degrees(
                math.atan2(abs(u[0] * v[1] - u[1] * v[0]), u[0] * v[0] + u[1] * v[1])
            )
        if self.ConstraintType == CT.PerpendicularDim:
            line, (curve, which) = g[0][0], g[1]
            p, s, d = _pt(curve, which), xyz(line.StartPoint), _dir(line)
            return abs(d[0] * (p[1] - s[1]) - d[1] * (p[0] - s[0])) / math.hypot(d[0], d[1])
        p, q = _pt(*g[0]), _pt(*g[1])
        if self.ConstraintType == CT.HorizontalDim:
            return abs(q[0] - p[0])
        if self.ConstraintType == CT.VerticalDim:
            return abs(q[1] - p[1])
        return math.dist(p, q)


def _dir(line):
    s, e = xyz(line.StartPoint), xyz(line.EndPoint)
    return [e[i] - s[i] for i in range(3)]


def _pt(curve, which):
    if which in ("StartPoint", "StartVertex"):
        return xyz(curve.StartPoint) if isinstance(curve, Line) else curve.at(0.0)[0]
    if which in ("EndPoint", "EndVertex"):
        return xyz(curve.EndPoint) if isinstance(curve, Line) else curve.at(1.0)[0]
    if which in ("ArcCenter", "Center"):
        return xyz(curve.CenterPoint)
    if which == "Midpoint":
        return curve.at(0.5)[0]
    raise ValueError(which)


def _set_pt(curve, which, p):
    if isinstance(curve, Line):
        if which in ("StartPoint", "StartVertex"):
            curve.StartPoint = P(p)
        elif which in ("EndPoint", "EndVertex"):
            curve.EndPoint = P(p)
        return
    if which in ("ArcCenter", "Center"):
        curve.CenterPoint = P(p)


class Sketch(BaseSketch):
    """``NXOpen.Sketch`` with the enums and members the sketch domain uses."""

    UsesLegacySolver = False
    ConstraintType = CT
    ConstraintClass = NS(Any=0, Dimension=1)
    ConstraintPointType = NS(
        NotSet="NotSet", StartVertex="StartVertex", EndVertex="EndVertex", ArcCenter="ArcCenter"
    )
    AssocType = NS(
        NotSet="NotSet",
        StartPoint="StartPoint",
        EndPoint="EndPoint",
        ArcCenter="ArcCenter",
        Midpoint="Midpoint",
    )
    PointType = NS(StartPoint="StartPoint", EndPoint="EndPoint", Center="Center")
    Status = NS(
        UnderConstrained=2, WellConstrained=3, OverConstrained=4, InconsistentlyConstrained=5
    )
    DimensionOption = NS(CreateAsDriving="driving", CreateAsReference="reference")
    TrimInputOption = NS(TrueValue=True)
    CreateDimensionOption = NS(FalseValue=False)
    AlternateSolutionOption = NS(FalseValue=False)
    ConstraintGeometryHelpType = NS(Point="Point")
    PlaneOption = NS(Inferred="Inferred", NewPlane="NewPlane")
    ViewReorient = NS(FalseValue=False, TrueValue=True)
    UpdateLevel = NS(Model="model")
    InferConstraintsOption = NS(InferNoConstraints=False)

    @staticmethod
    def ConstraintGeometry():  # noqa: N802
        return NS(Geometry=None, PointType="NotSet", SplineDefiningPointIndex=0)

    @staticmethod
    def DimensionGeometry():  # noqa: N802
        return NS(Geometry=None, AssocType="NotSet", AssocValue=0)

    @staticmethod
    def SketchGeometry():  # noqa: N802
        return NS(Geometry=None, PointType=None, PointIndex=0)

    @staticmethod
    def ConstraintGeometryHelp():  # noqa: N802
        return NS(Type=None, Point=None, Parameter=0.0)

    def __init__(self, fake, name="SKETCH_000"):
        super().__init__(fake.session, name)
        self.fake = fake
        self.updates = []
        self.over = False
        self.refuse = None  # an exception raised by the next Create*Constraint

    # -- geometry and constraints --------------------------------------------------
    def Update(self):
        self.updates.append("Update")

    def LocalUpdate(self):
        self.updates.append("LocalUpdate")

    def GetConstraintsForGeometry(self, curve, *_):
        return [c for c in self.constraints if any(g is curve for g in c.curves)]

    def CalculateStatus(self):
        free = sum(not self.GetConstraintsForGeometry(c) for c in self.geometry)
        self.status = (Sketch.Status.OverConstrained, 0) if self.over else (2, free)

    def _add(self, kind, *geometry):
        if self.refuse is not None:
            error, self.refuse = self.refuse, None
            raise error
        c = Constraint(kind, [(g.Geometry, g.PointType) for g in geometry], self)
        self.constraints.append(c)
        return c

    def CreateFixedConstraint(self, g):
        return self._add(CT.Fixed, g)

    def CreateFullyFixedConstraints(self, g):
        if not hasattr(g, "Geometry"):
            raise TypeError("Expecting NXOpen.Sketch.ConstraintGeometry type.")
        return [self._add(CT.Fixed, g), self._add(CT.Fixed, g)]

    def CreateHorizontalConstraint(self, g):
        return self._add(CT.Horizontal, g)

    def CreateVerticalConstraint(self, g):
        return self._add(CT.Vertical, g)

    def CreateCoincidentConstraint(self, a, b):
        return self._add(CT.Coincident, a, b)

    def CreateMidpointConstraint(self, a, b):
        return self._add(CT.Midpoint, a, b)

    def CreatePointOnCurveConstraint(self, a, b, _help):
        return self._add(CT.PointOnCurve, a, b)

    def fully_fixed(self, curve):
        return sum(c.ConstraintType == CT.Fixed for c in self.GetConstraintsForGeometry(curve)) >= 2

    def _dimension(self, kind, geometry, mode):
        if self.refuse is not None:
            error, self.refuse = self.refuse, None
            raise error
        part = self.fake.part
        exp = Expression(f"p{len(part.Expressions)}", "0")
        exp.OwningPart, exp.IsOccurrence = part, False
        part.Expressions.append(exp)
        d = Dimension(kind, geometry, self, exp, mode == "reference")
        exp.dimension = d
        if mode == "reference":  # NX 2406: reference dimensions have no expression
            d.AssociatedExpression = None
            part.Expressions.remove(exp)
        exp.Value = d.measure()
        exp.RightHandSide = repr(exp.Value)
        d.AssociatedDimension.ComputedSize = d.measure()
        self.constraints.append(d)
        return d

    def CreateDimension(self, kind, one, two, _origin, _help, mode):
        return self._dimension(
            kind, [(one.Geometry, one.AssocType), (two.Geometry, two.AssocType)], mode
        )

    def CreateRadialDimension(self, one, _origin, _help, mode):
        return self._dimension(CT.RadiusDim, [(one.Geometry, one.AssocType)], mode)

    def CreateDiameterDimension(self, one, _origin, _help, mode):
        return self._dimension(CT.DiameterDim, [(one.Geometry, one.AssocType)], mode)

    def DeleteObjects(self, objs):
        for obj in objs:
            if obj in self.geometry:
                self.geometry.remove(obj)
                self.constraints[:] = [c for c in self.constraints if obj not in c.curves]
            elif obj in self.constraints:
                self.constraints.remove(obj)
        return NS(Length=0, Dispose=lambda: None)

    def Fillet(self, a, b, ha, hb, radius, *_):
        return self.fake.fillet(self, a, b, radius), []

    # -- the tiny solver ----------------------------------------------------------------
    def solve(self):
        for c in self.constraints:
            if c.ConstraintType in (CT.Horizontal, CT.Vertical):
                line = c.curves[0]
                s, e = xyz(line.StartPoint), xyz(line.EndPoint)
                if c.ConstraintType == CT.Horizontal:
                    line.StartPoint = P([s[0], e[1], s[2]])
                else:
                    line.StartPoint = P([e[0], s[1], s[2]])

    def drive(self, dim, value):
        """A driving dimension edit: move the free curve unless it is fully fixed."""
        (curve, _), *_rest = dim.geometry
        if self.fully_fixed(curve) or self.over:
            self.over = True
            return
        if dim.ConstraintType in (CT.RadiusDim, CT.DiameterDim):
            curve.Radius = value if dim.ConstraintType == CT.RadiusDim else value / 2
        elif dim.ConstraintType == CT.ParallelDim and isinstance(curve, Line):
            s, d = xyz(curve.StartPoint), _dir(curve)
            n = math.hypot(*d)
            curve.EndPoint = P([s[i] + d[i] * value / n for i in range(3)])
        elif dim.ConstraintType == CT.HorizontalDim and dim.geometry[0][0] is dim.geometry[1][0]:
            s, e = xyz(curve.StartPoint), xyz(curve.EndPoint)
            curve.EndPoint = P([s[0] + math.copysign(value, e[0] - s[0]), e[1], e[2]])
        elif dim.ConstraintType == CT.PerpendicularDim:
            line, (other, which) = curve, dim.geometry[1]
            s, d = xyz(line.StartPoint), _dir(line)
            n = math.hypot(d[0], d[1])
            normal = [-d[1] / n, d[0] / n, 0.0]
            p = _pt(other, which)
            side = math.copysign(1.0, normal[0] * (p[0] - s[0]) + normal[1] * (p[1] - s[1]))
            now = dim.measure()
            shift = [normal[i] * side * (value - now) for i in range(3)]
            if isinstance(other, Line):
                other.StartPoint = P(
                    [a + b for a, b in zip(xyz(other.StartPoint), shift, strict=True)]
                )
                other.EndPoint = P([a + b for a, b in zip(xyz(other.EndPoint), shift, strict=True)])
            else:
                other.CenterPoint = P(
                    [a + b for a, b in zip(xyz(other.CenterPoint), shift, strict=True)]
                )
        dim.AssociatedDimension.ComputedSize = dim.measure()


class FakeSketchNX(FakeNX):
    """``fake_nx`` plus sketch geometry, builders and UF Eval."""

    def __init__(self, units="mm"):
        super().__init__(units)
        f = self
        ns = f.nx
        ns.Sketch = Sketch
        Edge.EdgeType = ns.Edge.EdgeType  # NXOpen.Edge is the class and holds the enum
        ns.Line, ns.Arc, ns.Ellipse, ns.Curve, ns.Edge = Line, Arc, Ellipse, Curve, Edge
        ns.Spline = type("Spline", (Curve,), {})
        ns.Matrix3x3 = lambda: NS()
        ns.SketchDragGeometryBuilder = NS(
            SolverStatus=NS(Unknown=0, Initializing=1, Configured=2, Updated=4, UpdateFailed=5)
        )
        ns.ConvertToFromReferenceBuilder = NS(OutputType=NS(Reference="Reference", Active="Active"))
        ns.SketchChamferBuilder = NS(
            ChamferOptions=NS(Symmetric="Symmetric", Asymmetric="Asymmetric")
        )
        ns.SketchMakeEqualBuilder = NS(EqualTypes=NS(Length="Length", Radius="Radius"))
        ns.InferSnapType = NS(SnapType=NS(Start="Start", End="End", Center="Center"))
        ns.SketchOffset = NS(Null=None)
        ns.SketchPattern = NS(Null=None)
        ns.SketchIntersectionCurve = NS(Null=None)
        ns.SketchProjectBuilder = NS(OutputCurve=NS(Original="Original"))
        ns.Section = NS(Mode=NS(Create=1), AllowTypes=NS(CurvesAndPoints=2))
        ns.SketchAlongPathBuilder = NS(
            PlaneOrientationType=NS(NormalToPath="NormalToPath"),
            SketchOrientationType=NS(Automatic="Automatic", RelativeToFace="RelativeToFace"),
        )
        ns.GeometricUtilities = NS(OnPathDimensionBuilder=NS(UpdateReason=NS(Path="Path")))
        ns.PlaneTypes = NS(MethodType=NS(Coincident="Coincident"))
        ns.OriginMethod = NS(SpecifyPoint="SpecifyPoint")
        ns.Session.MarkVisibility = NS(Visible=1, Invisible=0)
        part = f.part
        part.Curves.CreateLine = self.create_line
        part.Curves.CreateArc = self.create_arc
        part.Curves.CreateEllipse = self.create_ellipse
        part.CoordinateSystems.CreateCoordinateSystem = lambda o, m, _t: NS(origin=o, matrix=m)
        part.Planes = NS(
            CreatePlane=lambda o, n, _u: NS(
                SetMethod=lambda m: None, SetGeometry=lambda g: None, Evaluate=lambda: None
            )
        )
        part.Expressions.EditExpression = self.edit_expression
        part.Sketches.CreateSketchInPlaceBuilder2 = lambda *a: self.builder(
            "SketchInPlaceBuilder2", *a
        )
        part.ModelingViews.WorkView = NS(Name="Work")
        part.ScRuleFactory.CreateRuleCurveDumb = lambda objs: ("CurveDumb", list(objs))
        f.session.Parts.Display = part
        f.session.UpdateManager.DoUpdate.return_value = 0
        self.by_tag = {}
        self.uf.Eval = NS(
            Initialize=lambda tag: tag,
            AskLimits=lambda h: [0.0, 1.0],
            EvaluateUnitVectors=self.eval_at,
            EvaluateClosestPoint=self.closest,
            Free=lambda h: None,
        )
        self.uf.Modeling.AskMinimumDist = self.min_dist
        f.on_commit("SketchInPlaceBuilder2", self.commit_sketch)
        f.on_commit("SketchAlongPathBuilder", self.commit_sketch)
        f.on_commit("DragGeometryBuilder", self.commit_drag)
        f.on_commit("ConvertToFromReferenceBuilder", self.commit_reference)
        for kind in (
            "SketchMakeParallelBuilder",
            "SketchMakePerpendicularBuilder",
            "SketchMakeEqualBuilder",
            "SketchMakeCoincidentBuilder",
            "SketchMakeTangentBuilder",
        ):
            f.on_commit(kind, lambda b, kind=kind: self.commit_relation(kind, b))
        self.next_sketch = None  # frame of the next committed sketch (origin, x, y)
        # Undo marks also restore sketch geometry and constraint lists.
        self.sketch_marks = {}
        base_set, base_undo = self.session.SetUndoMark, self.session.UndoToMark

        def set_mark(*args):
            mark = base_set(*args)
            self.sketch_marks[mark] = [
                (s, list(s.geometry), list(s.constraints), s.over) for s in self.part.Sketches
            ]
            return mark

        def undo(mark, *args):
            base_undo(mark, *args)
            for s, geometry, constraints, over in self.sketch_marks.get(mark, []):
                s.geometry[:], s.constraints[:], s.over = geometry, constraints, over

        self.session.SetUndoMark, self.session.UndoToMark = set_mark, undo
        self.status = None  # forced solver status of the next CalculateStatus

    def builder(self, kind, *args):
        b = super().builder(kind, *args)
        object.__setattr__(b, "FindRelations", lambda: [])
        if kind == "DragGeometryBuilder":
            object.__setattr__(b, "GetSolverStatus", lambda: self.drag_status)
        return b

    # -- helpers for tests --------------------------------------------------------------
    def sketch(self, origin=(0, 0, 0), x_axis=(1, 0, 0), y_axis=(0, 1, 0), active=False):
        s = Sketch(self)
        z = (
            x_axis[1] * y_axis[2] - x_axis[2] * y_axis[1],
            x_axis[2] * y_axis[0] - x_axis[0] * y_axis[2],
            x_axis[0] * y_axis[1] - x_axis[1] * y_axis[0],
        )
        s.Origin, s.Orientation = point(*origin), NS(Element=matrix(x_axis, y_axis, z))
        s.Feature = Feature("SKETCH")
        s.Feature.OwningPart, s.Feature.IsOccurrence = self.part, False
        s.OwningPart, s.IsOccurrence = self.part, False
        s.ref = self.ref_of(s, "sketch")
        self.part.Sketches.append(s)
        if active:
            s.Activate()
        return s

    def cref(self, obj, kind="curve"):
        return self.ref_of(obj, kind)

    def _own_curve(self, curve):
        curve.OwningPart, curve.IsOccurrence = self.part, False
        self.by_tag[curve.Tag] = curve
        self.part.Curves.append(curve)
        return curve

    def line(self, sk, a, b):
        c = self._own_curve(Line(P(a), P(b)))
        sk.geometry.append(c)
        return c

    def arc(self, sk, center, radius, start_deg, end_deg):
        c = self._own_curve(
            Arc(
                P(center),
                [1, 0, 0],
                [0, 1, 0],
                radius,
                math.radians(start_deg),
                math.radians(end_deg),
            )
        )
        sk.geometry.append(c)
        return c

    def model_edge(self, a, b):
        e = self.edge(a, b)
        e.at = lambda t: (
            [a[i] + t * (b[i] - a[i]) for i in range(3)],
            [b[i] - a[i] for i in range(3)],
        )
        self.by_tag[e.Tag] = e
        return e

    # -- factories --------------------------------------------------------------------
    def create_line(self, a, b):
        return self._own_curve(Line(a, b))

    def create_arc(self, c, x, y, r, s, e):
        return self._own_curve(Arc(c, xyz(x), xyz(y), r, s, e))

    def create_ellipse(self, c, a, b, s, e, rot, orientation):
        return self._own_curve(Ellipse(c, a, b, s, e, rot, orientation))

    def commit_sketch(self, b):
        origin, x, y = self.next_sketch or ((0, 0, 0), (1, 0, 0), (0, 1, 0))
        csys = getattr(b, "Csystem", None)
        if isinstance(csys, NS) and not self.next_sketch:
            m = csys.matrix
            origin = xyz(csys.origin)
            x, y = (m.Xx, m.Xy, m.Xz), (m.Yx, m.Yy, m.Yz)
        s = self.sketch(origin, x, y)
        return s

    def commit_drag(self, b):
        (g,) = b.SetDragGeometry.calls[-1][0][0]
        target = xyz(b.DragToPoint.calls[-1][0][0])
        sketch = next(s for s in self.part.Sketches if g.Geometry in s.geometry)
        if not sketch.fully_fixed(g.Geometry):
            _set_pt(g.Geometry, g.PointType, target)
        sketch.solve()
        return None

    drag_status = 4

    def commit_reference(self, b):
        on = b.OutputState == "Reference"
        for curve in b.InputObjects.Add.calls[-1][0][0]:
            curve.IsReference = on

    def commit_relation(self, kind, b):
        sketch = self.session.ActiveSketch
        if kind == "SketchMakeCoincidentBuilder":
            sa, a, _v, _p, *_ = b.StationaryObject.SetValue.calls[-1][0]
            sb, c, _v2, _p2, *_ = b.MotionPoints.Add.calls[-1][0]
            names = {"Start": "StartVertex", "End": "EndVertex", "Center": "ArcCenter"}
            _set_pt(c, names[sb], _pt(a, names[sa]))
            sketch.constraints.append(
                Constraint(
                    CT.Concentric if sa == "Center" else CT.Coincident, [(a, sa), (c, sb)], sketch
                )
            )
            return None
        a = b.StationaryObject.Value
        (c,) = b.MotionObjects.Add.calls[-1][0]
        if kind in ("SketchMakeParallelBuilder", "SketchMakePerpendicularBuilder"):
            d = _dir(a)
            n = math.hypot(d[0], d[1])
            u = [d[0] / n, d[1] / n]
            if kind.endswith("PerpendicularBuilder"):
                u = [-u[1], u[0]]
            s, length = xyz(c.StartPoint), c.GetLength()
            if (_dir(c)[0] * u[0] + _dir(c)[1] * u[1]) < 0:
                u = [-u[0], -u[1]]
            c.EndPoint = P([s[0] + u[0] * length, s[1] + u[1] * length, s[2]])
            ctype = CT.Parallel if "Parallel" in kind else CT.Perpendicular
        elif kind == "SketchMakeEqualBuilder":
            if b.EqualType == "Radius":
                c.Radius = a.Radius
                ctype = CT.EqualRadius
            else:
                s, d = xyz(c.StartPoint), _dir(c)
                n, length = math.hypot(*d), a.GetLength()
                c.EndPoint = P([s[i] + d[i] * length / n for i in range(3)])
                ctype = CT.EqualLength
        else:  # tangent: line a, circle c moves along the line normal
            s, d = xyz(a.StartPoint), _dir(a)
            n = math.hypot(d[0], d[1])
            normal = [-d[1] / n, d[0] / n]
            p = xyz(c.CenterPoint)
            dist = normal[0] * (p[0] - s[0]) + normal[1] * (p[1] - s[1])
            shift = math.copysign(c.Radius, dist) - dist
            c.CenterPoint = P([p[0] + normal[0] * shift, p[1] + normal[1] * shift, p[2]])
            ctype = CT.Tangent
        sketch.constraints.append(Constraint(ctype, [(a, "NotSet"), (c, "NotSet")], sketch))
        return None

    def fillet(self, sketch, a, b, radius):
        """A fillet arc of ``radius`` between two perpendicular lines meeting at a corner."""
        ends = [(t, xyz(p)) for t, p in ((0, a.StartPoint), (1, a.EndPoint))]
        corner = None
        for ta, pa in ends:
            for tb, pb in ((0, b.StartPoint), (1, b.EndPoint)):
                if math.dist(pa, xyz(pb)) < 1e-9:
                    corner = (ta, tb, pa)
        ta, tb, c = corner
        far_a = xyz(a.EndPoint if ta == 0 else a.StartPoint)
        far_b = xyz(b.EndPoint if tb == 0 else b.StartPoint)
        ua = [(far_a[i] - c[i]) / math.dist(far_a, c) for i in range(3)]
        ub = [(far_b[i] - c[i]) / math.dist(far_b, c) for i in range(3)]
        pa = [c[i] + ua[i] * radius for i in range(3)]
        pb = [c[i] + ub[i] * radius for i in range(3)]
        center = [c[i] + (ua[i] + ub[i]) * radius for i in range(3)]
        _set_pt(a, "StartPoint" if ta == 0 else "EndPoint", pa)
        _set_pt(b, "StartPoint" if tb == 0 else "EndPoint", pb)
        s_ang = math.atan2(pa[1] - center[1], pa[0] - center[0])
        e_ang = math.atan2(pb[1] - center[1], pb[0] - center[0])
        if (e_ang - s_ang) % (2 * math.pi) > math.pi:
            s_ang, e_ang = e_ang, s_ang
        e_ang = s_ang + (e_ang - s_ang) % (2 * math.pi)
        arc = self._own_curve(Arc(P(center), [1, 0, 0], [0, 1, 0], radius, s_ang, e_ang))
        sketch.geometry.append(arc)
        return [arc]

    def edit_expression(self, exp, formula):
        exp.RightHandSide = formula
        value = float(formula)
        dim = getattr(exp, "dimension", None)
        exp.Value = value
        if dim is not None:
            dim.sketch.drive(dim, value)

    # -- UF ------------------------------------------------------------------------------
    def eval_at(self, handle, t):
        p, d = self.by_tag[handle].at(t)
        n = math.sqrt(sum(v * v for v in d)) or 1.0
        return p, [v / n for v in d], [0, 0, 0], [0, 0, 0]

    def _samples(self, obj, n=400):
        return [(k / n, obj.at(k / n)[0]) for k in range(n + 1)]

    def closest(self, handle, p):
        obj = self.by_tag[handle]
        lo, hi = 0.0, 1.0
        for _ in range(40):
            ts = [lo + (hi - lo) * k / 20 for k in range(21)]
            best = min(ts, key=lambda t: math.dist(obj.at(t)[0], p))
            step = (hi - lo) / 20
            lo, hi = max(0.0, best - step), min(1.0, best + step)
        t = (lo + hi) / 2
        return t, obj.at(t)[0]

    def min_dist(self, a, b, *_):
        oa = self.by_tag[a]
        best = None
        for _, p in self._samples(oa, 200):
            t, q = self.closest(b, p)
            d = math.dist(p, q)
            if best is None or d < best[0]:
                best = (d, p, q)
        return best


def sketch_nx(units="mm"):
    return FakeSketchNX(units)


__all__ = ["CT", "Arc", "Ellipse", "FakeSketchNX", "Line", "Sketch", "sketch_nx", "itertools"]
