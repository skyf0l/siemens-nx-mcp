"""NX-side handlers: Sketch dimensions and constraints between points and curves: point-to-point and two-curve dimensions, midpoint.

Conventions: datum_features.py is the reference implementation. Validate every
argument before touching NXOpen; builders create -> configure -> commit ->
Destroy() in finally; read back what NX built and raise NX_VERIFICATION_FAILED
on mismatch so the operation's undo mark rolls it back.

NX 2406 sketches use the modern solver (Sketch.UsesLegacySolver is False).
Native findings that shape this module:
- Sketch.CreateMidpointConstraint / CreatePointOnCurveConstraint create
  persistent constraints but do not move geometry: Sketch.Update() does not
  solve them. Only a driving change (a dimension expression edit or a sketch
  drag) runs the solver. Geometric constraints are therefore followed by a
  native SketchDragGeometryBuilder drag of the constrained point to its
  target, which solves the whole sketch with every persistent constraint.
- NX's midpoint constraint only makes the point equidistant from the target's
  two ends (perpendicular bisector); "on the midpoint" also needs point-on-curve.
- ConstraintPointType.MidVertex is rejected as a midpoint target
  ("Invalid object type"); the target must be the curve itself (NotSet).
"""

from __future__ import annotations

import contextlib
import math

from nx_mcp.kit.geom import cross2 as _cross2
from nx_mcp.kit.geom import dot as _dot
from nx_mcp.kit.geom import norm as _norm
from nx_mcp.kit.geom import sub as _sub
from nx_mcp.kit.geom import tol as tolerance
from nx_mcp.kit.geom import xyz as _xyz
from nx_mcp.kit.result import enum_name
from nx_mcp.legacy_kit import finite
from nx_mcp.runtime import NXToolError

POINTS = ("start", "end", "center", "mid")
VERTEX_POINTS = ("start", "end", "center")
POINT_KINDS = {"distance": "ParallelDim", "horizontal": "HorizontalDim", "vertical": "VerticalDim"}
CURVE_KINDS = ("angle", "parallel_distance", "perpendicular_distance")
DIM_ASSOC = {"start": "StartPoint", "end": "EndPoint", "center": "ArcCenter", "mid": "Midpoint"}
CON_POINT = {"start": "StartVertex", "end": "EndVertex", "center": "ArcCenter"}
DRAG_POINT = {"start": "StartPoint", "end": "EndPoint", "center": "Center"}
FULL_TURN = 2 * math.pi


# ---- pure geometry (sketch-local 2D unless noted) ----------------------------


def check_value(kind, value):
    """None (measure and fix) or a finite positive size; angles 0 < value < 180."""
    if value is None:
        return None
    value = finite(value, "value", True)
    if kind == "angle" and value >= 180.0:
        raise NXToolError("NX_INVALID_ARGUMENT", "angle value must be between 0 and 180 degrees")
    return value


def point_metric(kind, p1, p2):
    """Point-to-point size in sketch-local coordinates."""
    if kind == "horizontal":
        return abs(p2[0] - p1[0])
    if kind == "vertical":
        return abs(p2[1] - p1[1])
    return math.dist(p1[:2], p2[:2])


def line_point_distance(start, end, point):
    """Distance from point to the infinite line through start and end."""
    d = _sub(end[:2], start[:2])
    length = _norm(d)
    if length == 0:
        raise NXToolError("NX_INVALID_ARGUMENT", "Degenerate (zero-length) line")
    return abs(_cross2(d, _sub(point[:2], start[:2]))) / length


def direction_angle(d1, d2):
    """Unsigned angle in degrees [0, 180] between two 2D directions."""
    n = _norm(d1) * _norm(d2)
    if n == 0:
        raise NXToolError("NX_INVALID_ARGUMENT", "Degenerate (zero-length) line")
    return math.degrees(math.atan2(abs(_cross2(d1, d2)), _dot(d1, d2)))


def are_parallel(d1, d2, tol=1e-6):
    return abs(_cross2(d1, d2)) <= tol * _norm(d1) * _norm(d2)


def line_intersection(a0, a1, b0, b1):
    """Intersection of two infinite 2D lines, or None when parallel."""
    da, db = _sub(a1, a0), _sub(b1, b0)
    den = _cross2(da, db)
    if abs(den) <= 1e-12 * _norm(da) * _norm(db):
        return None
    t = _cross2(_sub(b0, a0), db) / den
    return [a0[0] + t * da[0], a0[1] + t * da[1]]


def bisector_projection(point, end1, end2):
    """Closest point to `point` that is equidistant from end1 and end2."""
    mid = [(a + b) / 2 for a, b in zip(end1, end2, strict=True)]
    chord = _sub(end2, end1)
    length = _norm(chord)
    if length == 0:
        raise NXToolError("NX_INVALID_ARGUMENT", "Target curve ends coincide")
    c = [v / length for v in chord]
    t = _dot(_sub(point, mid), c)
    return [p - t * v for p, v in zip(point, c, strict=True)]


def equidistance_residual(point, end1, end2):
    return abs(math.dist(point, end1) - math.dist(point, end2))


def angle_label_origin(a0, a1, b0, b1):
    """Annotation point inside the angle between the rays toward each line's middle."""
    corner = line_intersection(a0, a1, b0, b1)
    ma = [(x + y) / 2 for x, y in zip(a0, a1, strict=True)]
    mb = [(x + y) / 2 for x, y in zip(b0, b1, strict=True)]
    size = min(math.dist(a0, a1), math.dist(b0, b1))
    if corner is None:
        return [(x + y) / 2 for x, y in zip(ma, mb, strict=True)]
    rays = []
    for middle, s, e in ((ma, a0, a1), (mb, b0, b1)):
        v = _sub(middle, corner)
        if _norm(v) <= 1e-9 * max(1.0, size):
            v = _sub(e, s)
        rays.append([x / _norm(v) for x in v])
    bis = [x + y for x, y in zip(*rays, strict=True)]
    if _norm(bis) <= 1e-9:
        bis = [-rays[0][1], rays[0][0]]
    r = 0.3 * max(size, 1e-3)
    return [corner[i] + r * bis[i] / _norm(bis) for i in range(2)]


class SketchConstraintsMixin:
    # ---- curve points ------------------------------------------------------
    def _skc_kind(self, curve):
        if isinstance(curve, self.nxopen.Line):
            return "line"
        if isinstance(curve, self.nxopen.Arc):
            return "arc"
        raise NXToolError(
            "NX_OBJECT_TYPE_MISMATCH", "Only sketch lines, arcs and circles are supported"
        )

    def _skc_closed(self, arc):
        return abs(float(arc.EndAngle) - float(arc.StartAngle)) >= FULL_TURN - 1e-9

    def _skc_check_point(self, curve, point, allowed, label):
        if point not in allowed:
            raise NXToolError("NX_INVALID_ARGUMENT", f"{label} must be one of {', '.join(allowed)}")
        kind = self._skc_kind(curve)
        if kind == "line" and point == "center":
            raise NXToolError("NX_INVALID_ARGUMENT", f"{label}: a line has no center")
        if kind == "arc" and point != "center" and self._skc_closed(curve):
            raise NXToolError(
                "NX_INVALID_ARGUMENT", f"{label}: a full circle has only a center point"
            )

    def _skc_arc_point(self, arc, angle):
        m = arc.Matrix.Element
        c, r = arc.CenterPoint, float(arc.Radius)
        x, y = [m.Xx, m.Xy, m.Xz], [m.Yx, m.Yy, m.Yz]
        cos, sin = math.cos(angle), math.sin(angle)
        return [
            float(c.X) + r * (cos * x[0] + sin * y[0]),
            float(c.Y) + r * (cos * x[1] + sin * y[1]),
            float(c.Z) + r * (cos * x[2] + sin * y[2]),
        ]

    def _skc_point(self, curve, point):
        """Work-part coordinates of a curve point."""
        if isinstance(curve, self.nxopen.Line):
            s, e = _xyz(curve.StartPoint), _xyz(curve.EndPoint)
            return {"start": s, "end": e, "mid": [(a + b) / 2 for a, b in zip(s, e, strict=True)]}[
                point
            ]
        if point == "center":
            return _xyz(curve.CenterPoint)
        a0, a1 = float(curve.StartAngle), float(curve.EndAngle)
        return self._skc_arc_point(curve, {"start": a0, "end": a1, "mid": (a0 + a1) / 2}[point])

    def _skc_local(self, sketch, xyz):
        f = self._sketch_frame(sketch)
        d = _sub(xyz, f["origin"])
        return [_dot(d, f["x_axis"]), _dot(d, f["y_axis"])]

    def _skc_world(self, sketch, local):
        f = self._sketch_frame(sketch)
        return self.nxopen.Point3d(
            *[
                f["origin"][i] + local[0] * f["x_axis"][i] + local[1] * f["y_axis"][i]
                for i in range(3)
            ]
        )

    def _skc_line_local(self, sketch, line):
        return self._skc_local(sketch, _xyz(line.StartPoint)), self._skc_local(
            sketch, _xyz(line.EndPoint)
        )

    def _skc_point_record(self, sketch, curve, point):
        xyz = self._skc_point(curve, point)
        return {
            "curve": self._reference(curve, "curve", self._work_part(), "Curve"),
            "point": point,
            "local": self._skc_local(sketch, xyz),
            "work_part": xyz,
        }

    # ---- native helpers ----------------------------------------------------
    def _skc_sketch(self, sketch_id):
        sketch = self._resolve(sketch_id, {"sketch"})
        if getattr(sketch, "IsOccurrence", False) or sketch.OwningPart != self._work_part():
            raise NXToolError("NX_OBJECT_OWNER_MISMATCH", "Select a sketch of the work part")
        return sketch

    def _skc_constraints(self, sketch):
        return list(
            sketch.GetAllConstraintsOfType(
                self.nxopen.Sketch.ConstraintClass.Any, self.nxopen.Sketch.ConstraintType.NoCon
            )
        )

    def _skc_dim_geometry(self, curve, point):
        g = self.nxopen.Sketch.DimensionGeometry()
        g.Geometry = curve
        g.AssocType = getattr(self.nxopen.Sketch.AssocType, DIM_ASSOC[point] if point else "NotSet")
        g.AssocValue = 0
        return g

    def _skc_con_geometry(self, curve, point):
        g = self.nxopen.Sketch.ConstraintGeometry()
        g.Geometry = curve
        g.PointType = getattr(
            self.nxopen.Sketch.ConstraintPointType, CON_POINT[point] if point else "NotSet"
        )
        g.SplineDefiningPointIndex = 0
        return g

    def _skc_create_dimension(self, sketch, ctype, one, two, origin_local, value):
        """Create a driving dimension; edit it to value (which solves the sketch)."""
        with self._skc_native("dimension"):
            constraint = sketch.CreateDimension(
                getattr(self.nxopen.Sketch.ConstraintType, ctype),
                one,
                two,
                self._skc_world(sketch, origin_local),
                None,
                self.nxopen.Sketch.DimensionOption.CreateAsDriving,
            )
        exp = constraint.AssociatedExpression
        if exp is None:
            raise NXToolError("NX_VERIFICATION_FAILED", "NX dimension has no expression")
        measured_by_nx = float(exp.Value)
        if value is not None:
            self._work_part().Expressions.EditExpression(exp, repr(float(value)))
        sketch.Update()
        return constraint, exp, measured_by_nx

    @contextlib.contextmanager
    def _skc_native(self, what):
        """Map NX's refusal of a duplicate/contradicting relation to a stable code."""
        try:
            yield
        except NXToolError:
            raise
        except Exception as exc:
            text = str(exc)
            if "already exists" in text.lower() or "constrain" in text.lower():
                raise NXToolError(
                    "NX_SKETCH_CONFLICT",
                    f"NX refused the {what}: {text}",
                    details={"nx_code": getattr(exc, "ErrorCode", None), "nx_message": text},
                ) from exc
            raise

    def _skc_drag(self, sketch, curve, point, start_xyz, target_local):
        """Solve the sketch by dragging one curve point to target_local."""
        sg = self.nxopen.Sketch.SketchGeometry()
        sg.Geometry = curve
        sg.PointType = getattr(self.nxopen.Sketch.PointType, DRAG_POINT[point])
        sg.PointIndex = 0
        builder = self._work_part().Sketches.CreateDragGeometryBuilder()
        try:
            builder.SetDragGeometry([sg])
            builder.InitializeDrag(self.nxopen.Point3d(*start_xyz))
            builder.DragToPoint(self._skc_world(sketch, target_local))
            raw = builder.GetSolverStatus()
            try:  # Readable name (e.g. UpdateFailed) instead of NX's numeric str().
                status = enum_name(raw, self.nxopen.SketchDragGeometryBuilder.SolverStatus)
            except Exception:
                status = str(raw)
            builder.Commit()
        finally:
            builder.Destroy()
        sketch.Update()
        return status

    def _skc_finish(self, sketch_id, sketch, before, label):
        after = self._skc_constraints(sketch)
        tags = {int(c.Tag) for c in after}
        if not before <= tags:
            raise NXToolError(
                "NX_CONSTRAINT_REMOVED",
                "Native solver removed an existing constraint; rolling back",
            )
        created = [c for c in after if int(c.Tag) not in before]
        diagnostics = self._skc_check_solver(sketch)
        part = self._work_part()
        return [self._reference(c, "constraint", part, label) for c in created], diagnostics

    def _skc_solver_status(self, sketch):
        """Native whole-sketch solver status; temporary work-region change is undone."""
        part = self._work_part()
        mark = self.session.SetUndoMark(
            self.nxopen.Session.MarkVisibility.Invisible, "NX MCP sketch status"
        )
        try:
            region = part.Sketches.CreateWorkRegionBuilder()
            try:
                region.Scope = self.nxopen.SketchWorkRegionBuilder.ScopeType.EntireSketch
                region.Commit()
            finally:
                region.Destroy()
            sketch.CalculateStatus()
            status, dof = sketch.GetStatus()
        finally:
            self.session.UndoToMark(mark, None)
            self.session.DeleteUndoMark(mark, None)
        name = enum_name(status, self.nxopen.Sketch.Status)
        return {
            "solver_status": name,
            "remaining_degrees_of_freedom": dof
            if name in {"UnderConstrained", "WellConstrained"}
            else None,
        }

    def _skc_check_solver(self, sketch):
        # Same rule as nx_sketch_dimension/_check_sketch_result, without the
        # per-constraint expression records (Expression.GetExpressionParents
        # does not exist on NX 2406, which breaks _sketch_diagnostics there).
        self._update_model()
        diagnostics = self._skc_solver_status(sketch)
        explicit = self._explicit_constraint_conflicts(sketch)
        if (
            diagnostics["solver_status"] in {"OverConstrained", "InconsistentlyConstrained"}
            or explicit
        ):
            raise NXToolError(
                "NX_SKETCH_CONFLICT",
                "Constraint edit conflicts with the sketch; rolling back",
                details={"diagnostics": diagnostics, "explicit_conflicts": len(explicit)},
            )
        return diagnostics

    def _skc_expression(self, exp):
        """Expression record for a dimension (ID usable with nx_set_expression)."""
        units = getattr(exp, "Units", None)
        return {
            "object": self._reference(exp, "expression", self._work_part(), "Expression"),
            "name": exp.Name,
            "formula": exp.RightHandSide,
            "value": exp.GetValueUsingUnits(self.nxopen.Expression.UnitsOption.Expression),
            "units": units.Abbreviation if units else "unitless",
        }

    def _skc_verify_dimension(self, sketch, ok, **details):
        """NX keeps the old geometry when it cannot reach a driving value."""
        if not ok:
            details.update(self._skc_solver_status(sketch))
            raise NXToolError(
                "NX_SKETCH_CONFLICT",
                "NX could not solve the sketch to the requested value (conflicting or "
                "over-constrained); rolling back",
                details=details,
            )

    def _skc_verify(self, ok, message, **details):
        if not ok:
            raise NXToolError("NX_CONSTRAINT_UNSATISFIED", message, details=details)

    # ---- point-to-point dimension -------------------------------------------
    def _sketch_point_dimension(
        self, sketch_id, first, first_point, second, second_point, kind="distance", value=None
    ):
        if kind not in POINT_KINDS:
            raise NXToolError(
                "NX_INVALID_ARGUMENT", "kind must be distance, horizontal or vertical"
            )
        value = check_value(kind, value)
        sketch = self._skc_sketch(sketch_id)
        a, b = self._owned_curve(sketch, first), self._owned_curve(sketch, second)
        self._skc_check_point(a, first_point, POINTS, "first_point")
        self._skc_check_point(b, second_point, POINTS, "second_point")
        if int(a.Tag) == int(b.Tag) and first_point == second_point:
            raise NXToolError("NX_INVALID_ARGUMENT", "Select two different points")
        p1 = self._skc_local(sketch, self._skc_point(a, first_point))
        p2 = self._skc_local(sketch, self._skc_point(b, second_point))
        before_size = point_metric(kind, p1, p2)
        tol = tolerance(p1, p2)
        if value is None and before_size <= tol:
            raise NXToolError(
                "NX_INVALID_ARGUMENT",
                "The points have no measurable size in this direction; give a value",
            )
        mid = [(x + y) / 2 for x, y in zip(p1, p2, strict=True)]
        offset = 0.15 * max(math.dist(p1, p2), 1.0) + 1.0
        origin = [mid[0] + offset, mid[1]] if kind == "vertical" else [mid[0], mid[1] + offset]
        with self._editing_sketch(sketch):
            before = {int(c.Tag) for c in self._skc_constraints(sketch)}
            constraint, exp, measured_by_nx = self._skc_create_dimension(
                sketch,
                POINT_KINDS[kind],
                self._skc_dim_geometry(a, first_point),
                self._skc_dim_geometry(b, second_point),
                origin,
                value,
            )
            if abs(measured_by_nx - before_size) > tolerance(p1, p2) * 10:
                raise NXToolError(
                    "NX_VERIFICATION_FAILED",
                    "NX measured different points than requested",
                    details={"nx_value": measured_by_nx, "expected": before_size},
                )
            target = before_size if value is None else value
            q1 = self._skc_local(sketch, self._skc_point(a, first_point))
            q2 = self._skc_local(sketch, self._skc_point(b, second_point))
            actual = point_metric(kind, q1, q2)
            self._skc_verify_dimension(
                sketch,
                abs(actual - target) <= tolerance(q1, q2) * 10
                and abs(float(exp.Value) - target) <= tolerance(q1, q2) * 10,
                requested=target,
                measured=actual,
                expression_value=float(exp.Value),
            )
            refs, diagnostics = self._skc_finish(sketch_id, sketch, before, "Dimension")
            first_rec = self._skc_point_record(sketch, a, first_point)
            second_rec = self._skc_point_record(sketch, b, second_point)
        part = self._work_part()
        return {
            "constraint": self._reference(constraint, "constraint", part, "Dimension"),
            "created": refs,
            "expression": self._skc_expression(exp),
            "kind": kind,
            "requested_value": value,
            "value_before": before_size,
            "measured_value": actual,
            "points": {"first": first_rec, "second": second_rec},
            "solver_status": diagnostics["solver_status"],
            "remaining_degrees_of_freedom": diagnostics["remaining_degrees_of_freedom"],
            "units": self._units(),
            "coordinate_frame": "sketch_local_and_work_part",
        }

    # ---- two-curve dimension ---------------------------------------------------
    def _sketch_curve_dimension(
        self, sketch_id, first, second, kind, value=None, second_point=None
    ):
        if kind not in CURVE_KINDS:
            raise NXToolError(
                "NX_INVALID_ARGUMENT",
                "kind must be angle, parallel_distance or perpendicular_distance",
            )
        value = check_value(kind, value)
        if second_point is not None and kind != "perpendicular_distance":
            raise NXToolError(
                "NX_INVALID_ARGUMENT", "second_point applies only to perpendicular_distance"
            )
        if second_point is not None and second_point not in POINTS:
            raise NXToolError(
                "NX_INVALID_ARGUMENT", "second_point must be start, end, center or mid"
            )
        sketch = self._skc_sketch(sketch_id)
        a, b = self._owned_curve(sketch, first), self._owned_curve(sketch, second)
        if int(a.Tag) == int(b.Tag):
            raise NXToolError("NX_INVALID_ARGUMENT", "Select two different curves")
        if self._skc_kind(a) != "line":
            raise NXToolError("NX_INVALID_ARGUMENT", f"{kind} requires `first` to be a line")
        a0, a1 = self._skc_line_local(sketch, a)
        if kind in {"angle", "parallel_distance"}:
            if self._skc_kind(b) != "line":
                raise NXToolError("NX_INVALID_ARGUMENT", f"{kind} requires two lines")
            b0, b1 = self._skc_line_local(sketch, b)
            if kind == "angle":
                before_size = direction_angle(_sub(a1, a0), _sub(b1, b0))
                origin = angle_label_origin(a0, a1, b0, b1)
                if value is None and are_parallel(_sub(a1, a0), _sub(b1, b0)):
                    raise NXToolError(
                        "NX_INVALID_ARGUMENT", "The lines are parallel; give an angle value"
                    )
            else:
                if not are_parallel(_sub(a1, a0), _sub(b1, b0)):
                    raise NXToolError(
                        "NX_INVALID_ARGUMENT",
                        "parallel_distance requires parallel lines; make them parallel first",
                    )
                before_size = line_point_distance(a0, a1, b0)
                mb = [(x + y) / 2 for x, y in zip(b0, b1, strict=True)]
                ma = [(x + y) / 2 for x, y in zip(a0, a1, strict=True)]
                origin = [(x + y) / 2 for x, y in zip(ma, mb, strict=True)]
            point = None if kind == "angle" else "start"
        else:
            point = second_point or "center"
            self._skc_check_point(b, point, POINTS, "second_point")
            p = self._skc_local(sketch, self._skc_point(b, point))
            before_size = line_point_distance(a0, a1, p)
            ma = [(x + y) / 2 for x, y in zip(a0, a1, strict=True)]
            origin = [(x + y) / 2 for x, y in zip(ma, p, strict=True)]
        tol = tolerance(a0, a1, origin)
        if value is None and before_size <= tol:
            raise NXToolError(
                "NX_INVALID_ARGUMENT", "The current size is zero; give a value to drive"
            )
        ctype = {
            "angle": "AngularDim",
            "parallel_distance": "PerpendicularDim",
            "perpendicular_distance": "PerpendicularDim",
        }[kind]
        tol_value = 1e-6 if kind == "angle" else tol * 10
        with self._editing_sketch(sketch):
            before = {int(c.Tag) for c in self._skc_constraints(sketch)}
            constraint, exp, measured_by_nx = self._skc_create_dimension(
                sketch,
                ctype,
                self._skc_dim_geometry(a, None),
                self._skc_dim_geometry(b, point),
                origin,
                value,
            )
            a0, a1 = self._skc_line_local(sketch, a)
            if kind == "perpendicular_distance":
                actual = line_point_distance(
                    a0, a1, self._skc_local(sketch, self._skc_point(b, point))
                )
                matches = [actual]
            else:
                b0, b1 = self._skc_line_local(sketch, b)
                if kind == "angle":
                    actual = direction_angle(_sub(a1, a0), _sub(b1, b0))
                    matches = [actual, 180.0 - actual]
                else:
                    self._skc_verify(
                        are_parallel(_sub(a1, a0), _sub(b1, b0)),
                        "Lines are no longer parallel after the solve; rolling back",
                    )
                    actual = line_point_distance(a0, a1, b0)
                    matches = [actual]
            target = measured_by_nx if value is None else value
            if value is None:
                expected = [before_size, 180.0 - before_size] if kind == "angle" else [before_size]
                if min(abs(measured_by_nx - e) for e in expected) > max(tol_value, 1e-6) * 10:
                    raise NXToolError(
                        "NX_VERIFICATION_FAILED",
                        "NX measured a different size than requested",
                        details={"nx_value": measured_by_nx, "expected": before_size},
                    )
            self._skc_verify_dimension(
                sketch,
                min(abs(m - target) for m in matches) <= max(tol_value, 1e-6) * 10
                and abs(float(exp.Value) - target) <= max(tol_value, 1e-6) * 10,
                requested=target,
                measured=actual,
                expression_value=float(exp.Value),
            )
            refs, diagnostics = self._skc_finish(sketch_id, sketch, before, "Dimension")
        part = self._work_part()
        result = {
            "constraint": self._reference(constraint, "constraint", part, "Dimension"),
            "created": refs,
            "expression": self._skc_expression(exp),
            "kind": kind,
            "requested_value": value,
            "value_before": before_size,
            "measured_value": float(exp.Value) if kind == "angle" else actual,
            "solver_status": diagnostics["solver_status"],
            "remaining_degrees_of_freedom": diagnostics["remaining_degrees_of_freedom"],
            "units": "deg" if kind == "angle" else self._units(),
        }
        if kind == "angle":
            result["line_direction_angle"] = actual
        if point:
            result["second_point"] = self._skc_point_record(sketch, b, point)
        return result

    # ---- midpoint -------------------------------------------------------------
    def _skc_vertex_args(self, sketch_id, point_curve, point, target_ref):
        sketch = self._skc_sketch(sketch_id)
        src = self._owned_curve(sketch, point_curve)
        target = self._owned_curve(sketch, target_ref)
        if int(src.Tag) == int(target.Tag):
            raise NXToolError("NX_INVALID_ARGUMENT", "Select a point of a different curve")
        self._skc_check_point(src, point, VERTEX_POINTS, "point")
        self._skc_kind(target)
        return sketch, src, target

    def _sketch_midpoint(self, sketch_id, point_curve, point, target_curve, on_curve=True):
        if point not in VERTEX_POINTS:
            raise NXToolError("NX_INVALID_ARGUMENT", "point must be start, end or center")
        if type(on_curve) is not bool:
            raise NXToolError("NX_INVALID_ARGUMENT", "on_curve must be boolean")
        sketch, src, target = self._skc_vertex_args(sketch_id, point_curve, point, target_curve)
        if self._skc_kind(target) == "arc" and self._skc_closed(target):
            raise NXToolError("NX_INVALID_ARGUMENT", "A full circle has no midpoint")
        p = self._skc_local(sketch, self._skc_point(src, point))
        e1 = self._skc_local(sketch, self._skc_point(target, "start"))
        e2 = self._skc_local(sketch, self._skc_point(target, "end"))
        m = self._skc_local(sketch, self._skc_point(target, "mid"))
        goal = m if on_curve else bisector_projection(p, e1, e2)
        with self._editing_sketch(sketch):
            before = {int(c.Tag) for c in self._skc_constraints(sketch)}
            with self._skc_native("midpoint constraint"):
                sketch.CreateMidpointConstraint(
                    self._skc_con_geometry(src, point), self._skc_con_geometry(target, None)
                )
                if on_curve:
                    help_ = self.nxopen.Sketch.ConstraintGeometryHelp()
                    help_.Type = self.nxopen.Sketch.ConstraintGeometryHelpType.Point
                    help_.Point = self._skc_world(sketch, m)
                    help_.Parameter = 0.0
                    sketch.CreatePointOnCurveConstraint(
                        self._skc_con_geometry(src, point),
                        self._skc_con_geometry(target, None),
                        help_,
                    )
            drag_status = self._skc_drag(sketch, src, point, self._skc_point(src, point), goal)
            q = self._skc_local(sketch, self._skc_point(src, point))
            f1 = self._skc_local(sketch, self._skc_point(target, "start"))
            f2 = self._skc_local(sketch, self._skc_point(target, "end"))
            mid = self._skc_local(sketch, self._skc_point(target, "mid"))
            tol = tolerance(q, f1, f2)
            residuals = {"equidistance": equidistance_residual(q, f1, f2)}
            if on_curve:
                residuals["to_midpoint"] = math.dist(q, mid)
            self._skc_verify(
                all(r <= tol * 10 for r in residuals.values()),
                "Native midpoint constraint was not satisfied after the solve; rolling back",
                residuals=residuals,
                drag_solver_status=drag_status,
            )
            refs, diagnostics = self._skc_finish(sketch_id, sketch, before, "Midpoint")
            self._skc_verify(
                len(refs) == (2 if on_curve else 1),
                "NX did not create the expected persistent constraints",
                created=len(refs),
            )
            point_rec = self._skc_point_record(sketch, src, point)
        return {
            "constraint": refs[0],
            "constraints": refs,
            "on_curve": on_curve,
            "point": point_rec,
            "target_midpoint": {"local": mid, "work_part": _xyz(self._skc_world(sketch, mid))},
            "residuals": residuals,
            "solver_status": diagnostics["solver_status"],
            "remaining_degrees_of_freedom": diagnostics["remaining_degrees_of_freedom"],
            "units": self._units(),
        }

    # ---- point on curve ---------------------------------------------------------
    def _skc_curve_projection(self, sketch, curve, p):
        """Closest point of the (extended) curve to local point p, and p's distance to it."""
        if self._skc_kind(curve) == "line":
            s, e = self._skc_line_local(sketch, curve)
            d = _sub(e, s)
            t = _dot(_sub(p, s), d) / _dot(d, d)
            foot = [s[0] + t * d[0], s[1] + t * d[1]]
            return foot, math.dist(p, foot)
        c = self._skc_local(sketch, _xyz(curve.CenterPoint))
        r = float(curve.Radius)
        v = _sub(p, c)
        n = _norm(v)
        if n <= 1e-12:
            v, n = [1.0, 0.0], 1.0
        foot = [c[0] + r * v[0] / n, c[1] + r * v[1] / n]
        return foot, abs(math.dist(p, c) - r)

    def _sketch_point_on_curve(self, sketch_id, point_curve, point, curve):
        if point not in VERTEX_POINTS:
            raise NXToolError("NX_INVALID_ARGUMENT", "point must be start, end or center")
        sketch, src, target = self._skc_vertex_args(sketch_id, point_curve, point, curve)
        p = self._skc_local(sketch, self._skc_point(src, point))
        goal, _ = self._skc_curve_projection(sketch, target, p)
        with self._editing_sketch(sketch):
            before = {int(c.Tag) for c in self._skc_constraints(sketch)}
            help_ = self.nxopen.Sketch.ConstraintGeometryHelp()
            help_.Type = self.nxopen.Sketch.ConstraintGeometryHelpType.Point
            help_.Point = self._skc_world(sketch, goal)
            help_.Parameter = 0.0
            with self._skc_native("point-on-curve constraint"):
                sketch.CreatePointOnCurveConstraint(
                    self._skc_con_geometry(src, point), self._skc_con_geometry(target, None), help_
                )
            drag_status = self._skc_drag(sketch, src, point, self._skc_point(src, point), goal)
            q = self._skc_local(sketch, self._skc_point(src, point))
            _, residual = self._skc_curve_projection(sketch, target, q)
            self._skc_verify(
                residual <= tolerance(q, goal) * 10,
                "Native point-on-curve constraint was not satisfied after the solve; rolling back",
                residual=residual,
                drag_solver_status=drag_status,
            )
            refs, diagnostics = self._skc_finish(sketch_id, sketch, before, "Point on curve")
            self._skc_verify(len(refs) == 1, "NX did not create the constraint", created=len(refs))
            point_rec = self._skc_point_record(sketch, src, point)
        return {
            "constraint": refs[0],
            "point": point_rec,
            "residual": residual,
            "solver_status": diagnostics["solver_status"],
            "remaining_degrees_of_freedom": diagnostics["remaining_degrees_of_freedom"],
            "units": self._units(),
        }
