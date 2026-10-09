"""NX-side handlers: Sketch curves: polygon, ellipse, sketch fillet/chamfer, offset/mirror, construction toggle, project/intersection curves.

Conventions: datum_features.py is the reference implementation. Validate every
argument before touching NXOpen; builders create -> configure -> commit ->
Destroy() in finally; read back what NX built and raise NX_VERIFICATION_FAILED
on mismatch so the operation's undo mark rolls it back.
"""

from __future__ import annotations

import math

from nx_mcp.runtime import NXToolError

TOL = 1e-6
OFFSET_APPROX_TOL = 0.001  # mm; approximation tolerance for offsets of ellipses/splines


# ---- pure helpers (no NXOpen) -------------------------------------------------
def finite(value, name, positive=False):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or (positive and value <= 0)
    ):
        raise NXToolError(
            "NX_INVALID_ARGUMENT", name + " must be finite" + (" and positive" if positive else "")
        )
    return float(value)


def point2(value, name="center"):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise NXToolError("NX_INVALID_ARGUMENT", name + " must be local [x, y]")
    return [finite(v, name) for v in value]


def polygon_vertices(center, sides, radius, inscribed=True, rotation=0.0):
    """Counterclockwise vertices of a regular polygon in local [x, y]."""
    if type(sides) is not int or not 3 <= sides <= 64:
        raise NXToolError("NX_INVALID_ARGUMENT", "sides must be an integer 3-64")
    cx, cy = point2(center)
    radius = finite(radius, "radius", True)
    rotation = finite(rotation, "rotation")
    if not isinstance(inscribed, bool):
        raise NXToolError("NX_INVALID_ARGUMENT", "inscribed must be true or false")
    r = radius if inscribed else radius / math.cos(math.pi / sides)
    return [
        [
            cx + r * math.cos(math.radians(rotation) + 2 * math.pi * k / sides),
            cy + r * math.sin(math.radians(rotation) + 2 * math.pi * k / sides),
        ]
        for k in range(sides)
    ]


def ellipse_args(center, major_radius, minor_radius, rotation, start_angle, end_angle):
    c = point2(center)
    a = finite(major_radius, "major_radius", True)
    b = finite(minor_radius, "minor_radius", True)
    if b > a:
        raise NXToolError("NX_INVALID_ARGUMENT", "major_radius must be >= minor_radius")
    rot = finite(rotation, "rotation")
    s, e = finite(start_angle, "start_angle"), finite(end_angle, "end_angle")
    if not 0 < e - s <= 360:
        raise NXToolError("NX_INVALID_ARGUMENT", "Require 0 < end_angle - start_angle <= 360")
    return c, a, b, rot, s, e


def ellipse_perimeter(a, b):
    """Exact ellipse perimeter 4a*E(e) via the arithmetic-geometric mean.

    Ramanujan II (used before) is off by 1.2e-5 at b/a = 0.1 and 1.6e-4 at 0.02,
    which made the 1e-6 read-back check reject valid thin ellipses.
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
    """Local point of an ellipse at a POLAR angle (degrees from the major axis, seen from
    the centre), which is how NX 2406 Ellipse start/end angles behave (verified live:
    -45..45 on a 20x10 ellipse ends at radius 12.649, not at the parametric point)."""
    r, t = math.radians(rotation), math.radians(angle)
    radius = a * b / math.hypot(b * math.cos(t), a * math.sin(t))
    u, v = radius * math.cos(t), radius * math.sin(t)
    return [
        center[0] + u * math.cos(r) - v * math.sin(r),
        center[1] + u * math.sin(r) + v * math.cos(r),
    ]


def native_input_error(call, message):
    """Run a native call; an NX rejection of the inputs becomes NX_INVALID_ARGUMENT.

    Keeps the native code as nx_code (live NX 2406: 674275 "Invalid data for Sketch
    Project", 3680011 "No intersection exists between selected faces and sketch plane").
    """
    try:
        return call()
    except NXToolError:
        raise
    except Exception as error:
        code = getattr(error, "ErrorCode", None)
        if code is None:
            raise
        raise NXToolError("NX_INVALID_ARGUMENT", f"{message}: {error}", nx_code=code) from error


def ellipse_in_frame(x, y, z, rotation, start, end, frame):
    """Ellipse rotation/start/end (degrees) in the sketch frame.

    NX stores them relative to the curve's own matrix. Mirrored ellipses get their
    own (often flipped) matrix: live NX 2406 reported rotation 0 for a mirror whose
    major axis lies at 150 degrees in the sketch. A flipped normal reverses the
    angle sense, so the arc [s, e] becomes [-e, -s].
    """
    r = math.radians(rotation)
    axis = [math.cos(r) * x[i] + math.sin(r) * y[i] for i in range(3)]
    local = math.degrees(math.atan2(_dot(axis, frame["y_axis"]), _dot(axis, frame["x_axis"])))
    if _dot(z, frame["normal"]) < 0:
        start, end = -end, -start
    return local, start, end


def reflect2(p, a, b):
    """Reflect local point p about the line through a and b."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)
    fx, fy = a[0] + t * dx, a[1] + t * dy
    return [2 * fx - p[0], 2 * fy - p[1]]


def on_segment(p, segment, tol):
    """True when local point p lies on the 2D segment [a, b]."""
    a, b = segment
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2
    foot = [a[0] + t * dx, a[1] + t * dy]
    slack = tol / math.sqrt(length2)
    return -slack <= t <= 1 + slack and math.dist(p, foot) <= tol


def on_arc(p, arc, tol):
    """True when local point p lies on a 2D arc {center, radius, start, sweep, direction}."""
    cx, cy = arc["center"]
    if abs(math.dist(p, arc["center"]) - arc["radius"]) > tol:
        return False
    phi = math.atan2(p[1] - cy, p[0] - cx)
    delta = ((phi - arc["start"]) * arc["direction"]) % (2 * math.pi)
    slack = tol / arc["radius"]
    return delta <= arc["sweep"] + slack or delta >= 2 * math.pi - slack


def distinct_ids(values, name, minimum=1, maximum=200):
    if (
        not isinstance(values, list)
        or not minimum <= len(values) <= maximum
        or not all(isinstance(v, str) and v for v in values)
    ):
        raise NXToolError("NX_INVALID_ARGUMENT", f"{name} must list {minimum}-{maximum} object IDs")
    if len(set(values)) != len(values):
        raise NXToolError("NX_INVALID_ARGUMENT", f"{name} contains duplicates")
    return values


def _sub(a, b):
    return [x - y for x, y in zip(a, b, strict=True)]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def _xyz(p):
    return [float(p.X), float(p.Y), float(p.Z)]


class SketchCurvesMixin:
    # ---- helpers ------------------------------------------------------------
    def _sc_sketch(self, sketch_id):
        sketch = self._resolve(sketch_id, {"sketch"})
        if getattr(sketch, "IsOccurrence", False) or sketch.OwningPart != self._work_part():
            raise NXToolError("NX_OBJECT_OWNER_MISMATCH", "Select a sketch of the work part")
        active = self.session.ActiveSketch
        if active is not None and active != sketch:
            raise NXToolError("NX_SKETCH_ACTIVE", "Finish the other active sketch first")
        return sketch

    def _sc_curves(self, sketch, refs):
        owned = {int(c.Tag) for c in sketch.GetAllGeometry()}
        curves = []
        for ref in refs:
            curve = self._resolve(ref, {"curve"})
            if int(curve.Tag) not in owned:
                raise NXToolError(
                    "NX_OBJECT_OWNER_MISMATCH", f"Curve {ref} is not owned by this sketch"
                )
            curves.append(curve)
        return curves

    def _sc_world(self, frame, p):
        return self.nxopen.Point3d(
            *[
                frame["origin"][i] + p[0] * frame["x_axis"][i] + p[1] * frame["y_axis"][i]
                for i in range(3)
            ]
        )

    def _sc_local(self, frame, point):
        d = _sub(_xyz(point) if hasattr(point, "X") else list(point), frame["origin"])
        return [_dot(d, frame["x_axis"]), _dot(d, frame["y_axis"])]

    def _sc_evaluator(self, curve):
        import NXOpen.UF

        ev = NXOpen.UF.UFSession.GetUFSession().Eval
        handle = ev.Initialize(curve.Tag)
        return ev, handle, [float(v) for v in ev.AskLimits(handle)]

    def _sc_eval(self, curve, t):
        """World point at normalized parameter t (0 = start, 1 = end) of a curve."""
        ev, handle, (lo, hi) = self._sc_evaluator(curve)
        return [float(v) for v in ev.EvaluateUnitVectors(handle, lo + float(t) * (hi - lo))[0]]

    def _sc_record(self, curve, frame):
        part = self._work_part()
        nx = self.nxopen
        row = {
            "object": self._reference(curve, "curve", part, "Curve"),
            "type": type(curve).__name__,
        }
        start, end = self._sc_eval(curve, 0.0), self._sc_eval(curve, 1.0)
        row["start"] = self._sc_local(frame, start)
        row["end"] = self._sc_local(frame, end)
        row["closed"] = math.dist(start, end) <= TOL * max(1.0, *map(abs, start))
        if hasattr(curve, "GetLength"):
            row["length"] = float(curve.GetLength())
        if isinstance(curve, nx.Arc):
            row.update(
                center=self._sc_local(frame, curve.CenterPoint),
                radius=float(curve.Radius),
                start_angle=math.degrees(curve.StartAngle),
                end_angle=math.degrees(curve.EndAngle),
            )
        elif isinstance(curve, nx.Ellipse):
            m = curve.Matrix.Element
            rotation, start, end = ellipse_in_frame(
                [m.Xx, m.Xy, m.Xz],
                [m.Yx, m.Yy, m.Yz],
                [m.Zx, m.Zy, m.Zz],
                math.degrees(curve.RotationAngle),
                math.degrees(curve.StartAngle),
                math.degrees(curve.EndAngle),
                frame,
            )
            row.update(
                center=self._sc_local(frame, curve.CenterPoint),
                major_radius=float(curve.MajorRadius),
                minor_radius=float(curve.MinorRadius),
                rotation=rotation,
                start_angle=start,
                end_angle=end,
            )
        return row

    def _sc_geometry_tags(self, sketch):
        return {int(c.Tag) for c in sketch.GetAllGeometry()}

    def _sc_new(self, sketch, before):
        return [c for c in sketch.GetAllGeometry() if int(c.Tag) not in before]

    def _sc_result(self, sketch, created, frame, message, **extra):
        part = self._work_part()
        rows = [self._sc_record(c, frame) for c in created]
        refs = [r["object"] for r in rows]
        result = {
            "sketch": self._reference(sketch, "sketch", part, "Sketch"),
            "curves": rows,
            "curve_ids": [r["id"] for r in refs],
            "curve_count": len(rows),
            "created": refs,
            "units": self._units(),
            "coordinate_frame": "sketch_local",
            "message": message,
        }
        result.update(extra)
        return result

    def _sc_verify(self, ok, message, **actual):
        if not ok:
            raise NXToolError("NX_VERIFICATION_FAILED", message, details={"actual": actual})

    # ---- polygon --------------------------------------------------------------
    def _sketch_polygon(self, sketch_id, center, sides, radius, inscribed=True, rotation=0.0):
        vertices = polygon_vertices(center, sides, radius, inscribed, rotation)
        sketch = self._sc_sketch(sketch_id)
        part, nx = self._work_part(), self.nxopen
        frame = self._sketch_frame(sketch)
        lines = []
        with self._editing_sketch(sketch):
            for k in range(sides):
                a, b = vertices[k], vertices[(k + 1) % sides]
                line = part.Curves.CreateLine(self._sc_world(frame, a), self._sc_world(frame, b))
                sketch.AddGeometry(line, nx.Sketch.InferConstraintsOption.InferNoConstraints)
                lines.append(line)
            for k in range(sides):
                g1, g2 = nx.Sketch.ConstraintGeometry(), nx.Sketch.ConstraintGeometry()
                g1.Geometry, g1.PointType = lines[k], nx.Sketch.ConstraintPointType.EndVertex
                g2.Geometry = lines[(k + 1) % sides]
                g2.PointType = nx.Sketch.ConstraintPointType.StartVertex
                g1.SplineDefiningPointIndex = g2.SplineDefiningPointIndex = 0
                sketch.CreateCoincidentConstraint(g1, g2)
            sketch.Update()
        result = self._sc_result(sketch, lines, frame, f"Created a {sides}-sided polygon")
        tol = TOL * max(1.0, abs(radius), *map(abs, center))
        actual = [row["start"] for row in result["curves"]]
        ok = all(math.dist(p, q) <= tol for p, q in zip(actual, vertices, strict=True)) and all(
            math.dist(row["end"], vertices[(k + 1) % sides]) <= tol
            for k, row in enumerate(result["curves"])
        )
        self._sc_verify(ok, "NX polygon vertices differ from the request", vertices=actual)
        result.update(vertices=actual, edge_length=result["curves"][0]["length"], closed=True)
        return result

    # ---- ellipse --------------------------------------------------------------
    def _sketch_ellipse(
        self,
        sketch_id,
        center,
        major_radius,
        minor_radius,
        rotation=0.0,
        start_angle=0.0,
        end_angle=360.0,
    ):
        c, a, b, rot, s, e = ellipse_args(
            center, major_radius, minor_radius, rotation, start_angle, end_angle
        )
        sketch = self._sc_sketch(sketch_id)
        part, nx = self._work_part(), self.nxopen
        frame = self._sketch_frame(sketch)
        with self._editing_sketch(sketch):
            ellipse = part.Curves.CreateEllipse(
                self._sc_world(frame, c),
                a,
                b,
                math.radians(s),
                math.radians(e),
                math.radians(rot),
                sketch.Orientation,
            )
            sketch.AddGeometry(ellipse, nx.Sketch.InferConstraintsOption.InferNoConstraints)
            sketch.Update()
        result = self._sc_result(sketch, [ellipse], frame, "Created sketch ellipse")
        row = result["curves"][0]
        tol = TOL * max(1.0, a, *map(abs, c))
        rot_err = (row["rotation"] - rot) % 180.0
        ok = (
            math.dist(row["center"], c) <= tol
            and abs(row["major_radius"] - a) <= tol
            and abs(row["minor_radius"] - b) <= tol
            and (min(rot_err, 180.0 - rot_err) <= 1e-6 or abs(a - b) <= tol)
        )
        self._sc_verify(ok, "NX ellipse differs from the request", **row)
        full = e - s >= 360.0 - 1e-9
        if not full:
            # An elliptical arc: its ends must be at the requested polar angles (either
            # curve direction), not only the centre, radii and rotation.
            want = [ellipse_point(c, a, b, rot, s), ellipse_point(c, a, b, rot, e)]
            got = [row["start"], row["end"]]
            self._sc_verify(
                any(
                    all(math.dist(p, q) <= tol for p, q in zip(got, order, strict=True))
                    for order in (want, want[::-1])
                ),
                "NX elliptical arc ends differ from the requested angles",
                expected_ends=want,
                **row,
            )
        if full and "length" in row:
            self._sc_verify(
                abs(row["length"] - ellipse_perimeter(a, b)) <= 1e-6 * ellipse_perimeter(a, b),
                "NX ellipse perimeter differs from the request",
                **row,
            )
        result.update(ellipse=row, closed=full)
        return result

    # ---- fillet / chamfer --------------------------------------------------
    def _sc_pair(self, sketch_id, curve1, curve2):
        if not all(isinstance(c, str) and c for c in (curve1, curve2)) or curve1 == curve2:
            raise NXToolError("NX_INVALID_ARGUMENT", "curve1 and curve2 must be distinct curve IDs")
        sketch = self._sc_sketch(sketch_id)
        a, b = self._sc_curves(sketch, [curve1, curve2])
        return sketch, a, b

    def _sc_corner(self, a, b):
        """Closest endpoint pair of two curves: (end parameter of a, of b, gap)."""
        best = None
        for ta in (0.0, 1.0):
            for tb in (0.0, 1.0):
                gap = math.dist(self._sc_eval(a, ta), self._sc_eval(b, tb))
                if best is None or gap < best[2]:
                    best = (ta, tb, gap)
        return best

    def _sc_help_point(self, curve, t_end, fraction=0.25):
        """World point on curve a fraction of its parameter range from end t_end."""
        t = fraction if t_end == 0.0 else 1.0 - fraction
        return self.nxopen.Point3d(*self._sc_eval(curve, t))

    def _sc_extent(self, curve, frame):
        """2D extent of a sketch line (segment) or arc (center, radius, sweep)."""
        if isinstance(curve, self.nxopen.Line):
            return {
                "segment": [
                    self._sc_local(frame, curve.StartPoint),
                    self._sc_local(frame, curve.EndPoint),
                ]
            }
        m = curve.Matrix.Element
        direction = 1.0 if _dot([m.Zx, m.Zy, m.Zz], frame["normal"]) > 0 else -1.0
        center = self._sc_local(frame, curve.CenterPoint)
        start = self._sc_local(frame, self._sc_eval(curve, 0.0))
        return {
            "arc": {
                "center": center,
                "radius": float(curve.Radius),
                "start": math.atan2(start[1] - center[1], start[0] - center[0]),
                "sweep": float(curve.EndAngle - curve.StartAngle),
                "direction": direction,
            }
        }

    @staticmethod
    def _sc_within(p, extent, tol):
        if "segment" in extent:
            return on_segment(p, extent["segment"], tol)
        return on_arc(p, extent["arc"], tol)

    def _sketch_fillet(self, sketch_id, curve1, curve2, radius):
        radius = finite(radius, "radius", True)
        sketch, a, b = self._sc_pair(sketch_id, curve1, curve2)
        nx = self.nxopen
        if not all(isinstance(c, (nx.Line, nx.Arc)) for c in (a, b)):
            raise NXToolError("NX_OBJECT_TYPE_MISMATCH", "Fillet supports sketch lines and arcs")
        frame = self._sketch_frame(sketch)
        ta, tb, _ = self._sc_corner(a, b)
        originals = [self._sc_extent(c, frame) for c in (a, b)]
        with self._editing_sketch(sketch):
            fillets, _constraints = sketch.Fillet(
                a,
                b,
                self._sc_help_point(a, ta),
                self._sc_help_point(b, tb),
                radius,
                nx.Sketch.TrimInputOption.TrueValue,
                nx.Sketch.CreateDimensionOption.FalseValue,
                nx.Sketch.AlternateSolutionOption.FalseValue,
            )
            sketch.Update()
        arcs = list(fillets)
        self._sc_verify(len(arcs) == 1, "NX created no single fillet arc", count=len(arcs))
        arc = arcs[0]
        result = self._sc_result(sketch, [arc], frame, f"Filleted two curves with R{radius:g}")
        row = result["curves"][0]
        tol = TOL * max(1.0, radius, *map(abs, row["center"]))
        # Tangency: each trimmed input touches the arc at one of its ends.
        touches = [
            min(
                math.dist(self._sc_eval(c, t), self._sc_eval(arc, s))
                for t in (0.0, 1.0)
                for s in (0.0, 1.0)
            )
            for c in (a, b)
        ]
        # Trim only: both arc ends must lie on the ORIGINAL curves. NX would otherwise
        # extend the inputs (radius too large for the corner) and break the profile.
        ends = [row["start"], row["end"]]
        trimmed_only = all(
            any(self._sc_within(p, extent, 1e-6 * max(1.0, radius, *map(abs, p))) for p in ends)
            for extent in originals
        )
        self._sc_verify(
            abs(row["radius"] - radius) <= tol and max(touches) <= 1e-4 and trimmed_only,
            "NX fillet arc differs from the request (radius too large for the corner?)",
            radius=row["radius"],
            gaps=touches,
            within_original_curves=trimmed_only,
            fillet=row,
        )
        result.update(
            fillet=row,
            trimmed=[self._sc_record(c, frame) for c in (a, b)],
            modified=[self._reference(c, "curve", self._work_part(), "Curve") for c in (a, b)],
        )
        return result

    def _sketch_chamfer(self, sketch_id, curve1, curve2, distance1, distance2=None):
        d1 = finite(distance1, "distance1", True)
        d2 = d1 if distance2 is None else finite(distance2, "distance2", True)
        sketch, a, b = self._sc_pair(sketch_id, curve1, curve2)
        nx = self.nxopen
        if not all(isinstance(c, nx.Line) for c in (a, b)):
            raise NXToolError("NX_OBJECT_TYPE_MISMATCH", "Chamfer supports two sketch lines")
        part = self._work_part()
        frame = self._sketch_frame(sketch)
        ta, tb, _ = self._sc_corner(a, b)
        corner = [
            (p + q) / 2 for p, q in zip(self._sc_eval(a, ta), self._sc_eval(b, tb), strict=True)
        ]
        # A help point inside the corner (between both lines) selects the quadrant to cut.
        ha, hb = _xyz(self._sc_help_point(a, ta, 0.1)), _xyz(self._sc_help_point(b, tb, 0.1))
        inside = [
            corner[i] + 0.5 * (ha[i] - corner[i]) + 0.5 * (hb[i] - corner[i]) for i in range(3)
        ]
        segments = [
            [self._sc_local(frame, c.StartPoint), self._sc_local(frame, c.EndPoint)] for c in (a, b)
        ]
        corner_local = self._sc_local(frame, corner)
        tol = 1e-6 * max(1.0, d1, d2, *map(abs, corner_local))
        symmetric = abs(d1 - d2) <= TOL * max(1.0, d1)
        before = self._sc_geometry_tags(sketch)

        def chamfer(first, second):
            builder = part.Sketches.CreateSketchChamferBuilder()
            try:
                options = nx.SketchChamferBuilder.ChamferOptions
                builder.ChamferOption = options.Symmetric if symmetric else options.Asymmetric
                builder.TrimInputCurves = True
                builder.CurvesToChamfer.Add(a)
                builder.CurvesToChamfer.Add(b)
                builder.HelpPoint = part.Points.CreatePoint(nx.Point3d(*inside))
                builder.Distance1.RightHandSide = repr(first)
                if not symmetric:
                    builder.Distance2.RightHandSide = repr(second)
                builder.Commit()
            finally:
                builder.Destroy()
            sketch.Update()
            created = [c for c in self._sc_new(sketch, before) if isinstance(c, nx.Line)]
            self._sc_verify(
                len(created) == 1, "NX created no single chamfer line", count=len(created)
            )
            row = self._sc_record(created[0], frame)
            # Each chamfer end lies on the ORIGINAL segment of its line (a cut, not
            # an outside extension); measure the cut-back along curve1 and curve2.
            measured = [None, None]
            for k, seg in enumerate(segments):
                hits = [p for p in (row["start"], row["end"]) if on_segment(p, seg, tol)]
                if hits:
                    measured[k] = min(math.dist(corner_local, p) for p in hits)
            return created, row, measured

        def matches(measured, x, y):
            return (
                None not in measured and abs(measured[0] - x) <= tol and abs(measured[1] - y) <= tol
            )

        with self._editing_sketch(sketch):
            # NX assigns Distance1 to one of the lines by its own geometric rule (not
            # the selection order); if it lands on curve2, undo and swap the values.
            mark = self.session.SetUndoMark(
                nx.Session.MarkVisibility.Invisible, "NX MCP sketch chamfer attempt"
            )
            try:
                created, row, measured = chamfer(d1, d2)
                if not symmetric and matches(measured, d2, d1):
                    self.session.UndoToMark(mark, None)
                    created, row, measured = chamfer(d2, d1)
            finally:
                self.session.DeleteUndoMark(mark, None)
        self._sc_verify(
            matches(measured, d1, d2),
            "NX chamfer differs from the request",
            distances_along_curves=measured,
            chamfer=row,
        )
        result = self._sc_result(sketch, created, frame, "Chamfered two lines")
        row = result["curves"][0]
        result.update(
            chamfer=row,
            trimmed=[self._sc_record(c, frame) for c in (a, b)],
            modified=[self._reference(c, "curve", part, "Curve") for c in (a, b)],
        )
        return result

    # ---- sections / distances ---------------------------------------------------
    def _sc_add_rules(self, section, rules, seed=None):
        nx = self.nxopen
        help_point = nx.Point3d(0.0, 0.0, 0.0)
        section.AddToSection(rules, seed, None, None, help_point, nx.Section.Mode.Create, False)

    def _sc_curve_rule(self, curves):
        return self._work_part().ScRuleFactory.CreateRuleCurveDumb(curves)

    def _sc_min_dist(self, a, b):
        import NXOpen.UF

        modeling = NXOpen.UF.UFSession.GetUFSession().Modeling
        zero = [0.0, 0.0, 0.0]
        out = modeling.AskMinimumDist(a.Tag, b.Tag, 0, zero, 0, zero)
        return float(out[0]), [float(v) for v in out[1]], [float(v) for v in out[2]]

    def _sc_tangent_at(self, curve, point):
        ev, handle, _ = self._sc_evaluator(curve)
        parm = ev.EvaluateClosestPoint(handle, list(point))[0]
        return [float(v) for v in ev.EvaluateUnitVectors(handle, parm)[1]]

    # ---- offset ---------------------------------------------------------------
    def _sketch_offset(self, sketch_id, curves, distance, side="auto"):
        distinct_ids(curves, "curves", 1, 100)
        d = finite(distance, "distance", True)
        if side not in {"auto", "left", "right"}:
            raise NXToolError("NX_INVALID_ARGUMENT", "side must be auto, left or right")
        sketch = self._sc_sketch(sketch_id)
        inputs = self._sc_curves(sketch, curves)
        part, nx = self._work_part(), self.nxopen
        frame = self._sketch_frame(sketch)
        before = self._sc_geometry_tags(sketch)
        approx_tol = OFFSET_APPROX_TOL * (1.0 if self._units() == "mm" else 1 / 25.4)

        def side_of(outputs):
            _, p_in, p_out = min(
                (self._sc_min_dist(inputs[0], o) for o in outputs), key=lambda r: r[0]
            )
            t = self._sc_tangent_at(inputs[0], p_in)
            v = _sub(p_out, p_in)
            cz = [t[1] * v[2] - t[2] * v[1], t[2] * v[0] - t[0] * v[2], t[0] * v[1] - t[1] * v[0]]
            return "left" if _dot(cz, frame["normal"]) > 0 else "right"

        with self._editing_sketch(sketch):
            builder = part.Sketches.CreateSketchOffsetBuilder(nx.SketchOffset.Null)
            try:
                section = builder.CreateSection()
                self._sc_add_rules(section, [self._sc_curve_rule(inputs)], inputs[0])
                # The builder's default Tolerance is 0.0: lines and arcs offset exactly, but
                # ellipses/splines fail with "Cannot approximate offset curve" (NX 1720072).
                builder.Tolerance = approx_tol
                builder.UpdateLoopsAndCopies()
                builder.Distance.RightHandSide = repr(d)
                builder.CreateDimension = True
                builder.NumberOfCopies = 1
                builder.IsSymmetric = False
                builder.ConvertToReference = False
                builder.EvaluateOffset()
                if side != "auto":
                    outputs = [
                        o
                        for o in builder.GetOutputCurvesOfOffset()
                        if isinstance(o, nx.Curve) and int(o.Tag) not in before
                    ]
                    if outputs and side_of(outputs) != side:
                        # ReverseOffsetDirection() raises "Sketch is not initialized"
                        # on NX 2406 sketches; reversing the chain works.
                        builder.ReverseOffsetDirectionOfChain(inputs[0])
                        builder.EvaluateOffset()
                builder.Commit()
            finally:
                builder.Destroy()
            sketch.Update()
        created = [c for c in self._sc_new(sketch, before) if isinstance(c, nx.Curve)]
        self._sc_verify(bool(created), "NX created no offset curves")
        measured = [min(self._sc_min_dist(i, c)[0] for i in inputs) for c in created]
        actual_side = side_of(created)
        exact = all(isinstance(c, (nx.Line, nx.Arc)) for c in created)
        self._sc_verify(
            all(abs(m - d) <= (1e-5 * max(1.0, d) if exact else approx_tol) for m in measured)
            and (side == "auto" or actual_side == side),
            "NX offset differs from the request",
            distances=measured,
            side=actual_side,
        )
        return self._sc_result(
            sketch,
            created,
            frame,
            f"Offset {len(inputs)} curve(s) by {d:g}",
            measured_distance=max(measured),
            side=actual_side,
            associative=True,
        )

    # ---- mirror ---------------------------------------------------------------
    def _sketch_mirror(self, sketch_id, curves, mirror_line):
        distinct_ids(curves, "curves", 1, 100)
        if not isinstance(mirror_line, str) or not mirror_line or mirror_line in curves:
            raise NXToolError(
                "NX_INVALID_ARGUMENT", "mirror_line must be an ID that is not in curves"
            )
        sketch = self._sc_sketch(sketch_id)
        inputs = self._sc_curves(sketch, curves)
        nx, part = self.nxopen, self._work_part()
        axis = self._resolve(mirror_line, {"curve", "datum"})
        if isinstance(axis, nx.Curve) and int(axis.Tag) not in self._sc_geometry_tags(sketch):
            # The documented contract is a line of THIS sketch (or a datum axis); a line of
            # another sketch or a model curve would create an undocumented external reference.
            raise NXToolError(
                "NX_OBJECT_OWNER_MISMATCH",
                "mirror_line must be a line of this sketch or a datum axis",
            )
        frame = self._sketch_frame(sketch)
        if isinstance(axis, nx.Line):
            a, b = self._sc_local(frame, axis.StartPoint), self._sc_local(frame, axis.EndPoint)
        elif hasattr(axis, "Direction") and hasattr(axis, "Origin"):
            o, dvec = _xyz(axis.Origin), _xyz(axis.Direction)
            if abs(_dot(dvec, frame["normal"])) > 1e-6 * math.sqrt(_dot(dvec, dvec)):
                raise NXToolError(
                    "NX_INVALID_ARGUMENT", "The datum axis must lie in the sketch plane"
                )
            a = self._sc_local(frame, o)
            b = self._sc_local(frame, [o[i] + dvec[i] for i in range(3)])
        else:
            raise NXToolError(
                "NX_OBJECT_TYPE_MISMATCH", "mirror_line must be a sketch line or a datum axis"
            )
        if math.dist(a, b) <= TOL:
            raise NXToolError("NX_INVALID_ARGUMENT", "mirror_line has zero length in the sketch")
        before = self._sc_geometry_tags(sketch)
        with self._editing_sketch(sketch):
            builder = part.Sketches.CreateSketchMirrorPatternBuilder(nx.SketchPattern.Null)
            try:
                self._sc_add_rules(builder.Section, [self._sc_curve_rule(inputs)], inputs[0])
                builder.UpdateInputSection()
                builder.DirectionObject.Value = axis
                builder.UpdateDirectionObject()
                builder.ConvertToReference = False
                builder.ConvertInputToReference = False
                builder.Commit()
            finally:
                builder.Destroy()
            sketch.Update()
        created = [c for c in self._sc_new(sketch, before) if isinstance(c, nx.Curve)]
        self._sc_verify(
            len(created) == len(inputs),
            "NX mirrored a different number of curves",
            created=len(created),
            requested=len(inputs),
        )
        result = self._sc_result(sketch, created, frame, f"Mirrored {len(inputs)} curve(s)")
        source_ends = [
            sorted(
                tuple(reflect2(self._sc_local(frame, self._sc_eval(c, t)), a, b))
                for t in (0.0, 1.0)
            )
            for c in inputs
        ]
        tol = 1e-6 * max(1.0, *[abs(v) for e in source_ends for p in e for v in p])
        unmatched = []
        for row in result["curves"]:
            ends = sorted([tuple(row["start"]), tuple(row["end"])])
            if not any(
                all(math.dist(p, q) <= tol for p, q in zip(ends, src, strict=True))
                for src in source_ends
            ):
                unmatched.append(row["object"]["id"])
        self._sc_verify(
            not unmatched, "NX mirror output is not a reflection of the input", unmatched=unmatched
        )
        result.update(mirror_line={"start": a, "end": b}, associative=True)
        return result

    # ---- construction -----------------------------------------------------------
    def _sketch_construction(self, sketch_id, curves, construction=True):
        distinct_ids(curves, "curves", 1, 200)
        if not isinstance(construction, bool):
            raise NXToolError("NX_INVALID_ARGUMENT", "construction must be true or false")
        sketch = self._sc_sketch(sketch_id)
        inputs = self._sc_curves(sketch, curves)
        part, nx = self._work_part(), self.nxopen
        frame = self._sketch_frame(sketch)
        with self._editing_sketch(sketch):
            builder = part.Sketches.CreateConvertToFromReferenceBuilder()
            try:
                kinds = nx.ConvertToFromReferenceBuilder.OutputType
                builder.OutputState = kinds.Reference if construction else kinds.Active
                builder.InputObjects.Add(inputs)
                builder.Commit()
            finally:
                builder.Destroy()
            sketch.Update()
        rows = []
        for curve in inputs:
            row = self._sc_record(curve, frame)
            row["construction"] = bool(curve.IsReference)
            rows.append(row)
        wrong = [r["object"]["id"] for r in rows if r["construction"] != construction]
        self._sc_verify(not wrong, "NX did not change the construction state", curves=wrong)
        return {
            "sketch": self._reference(sketch, "sketch", part, "Sketch"),
            "curves": rows,
            "curve_count": len(rows),
            "construction": construction,
            "modified": [r["object"] for r in rows],
            "units": self._units(),
            "coordinate_frame": "sketch_local",
            "message": ("Converted to construction" if construction else "Converted to active")
            + f": {len(rows)} curve(s)",
        }

    # ---- project / intersection -------------------------------------------------
    def _sc_model_objects(self, sketch, ids, kinds):
        owned = self._sc_geometry_tags(sketch)
        objects = []
        for ref in ids:
            obj = self._resolve(ref, kinds)
            if getattr(obj, "IsOccurrence", False) or obj.OwningPart != self._work_part():
                raise NXToolError(
                    "NX_OBJECT_OWNER_MISMATCH", f"{ref} must be owned by the work part"
                )
            if int(obj.Tag) in owned:
                raise NXToolError("NX_INVALID_ARGUMENT", f"{ref} already belongs to this sketch")
            objects.append(obj)
        return objects

    def _sketch_project(self, sketch_id, objects):
        distinct_ids(objects, "objects", 1, 200)
        sketch = self._sc_sketch(sketch_id)
        items = self._sc_model_objects(sketch, objects, {"edge", "curve"})
        part, nx = self._work_part(), self.nxopen
        frame = self._sketch_frame(sketch)
        edges = [o for o in items if isinstance(o, nx.Edge)]
        curves = [o for o in items if not isinstance(o, nx.Edge)]
        before = self._sc_geometry_tags(sketch)
        with self._editing_sketch(sketch):
            builder = part.Sketches.CreateProjectBuilder(nx.Features.Feature.Null)
            try:
                builder.Associativity = True
                builder.ProjectAsDumbFixedCurves = False
                builder.CurveType = nx.SketchProjectBuilder.OutputCurve.Original
                builder.Tolerance = 0.01
                builder.Section.DistanceTolerance = 0.01
                builder.Section.ChainingTolerance = 0.0095
                builder.Section.SetAllowedEntityTypes(nx.Section.AllowTypes.CurvesAndPoints)
                rules = []
                if edges:
                    rules.append(part.ScRuleFactory.CreateRuleEdgeDumb(edges))
                if curves:
                    rules.append(self._sc_curve_rule(curves))
                self._sc_add_rules(builder.Section, rules, items[0])
                native_input_error(
                    builder.Commit,
                    "NX cannot project these objects onto the sketch plane (an edge normal to the "
                    "plane projects to a point)",
                )
                feature = builder.GetOperation()
            finally:
                builder.Destroy()
            sketch.Update()
        created = [c for c in self._sc_new(sketch, before) if isinstance(c, nx.Curve)]
        self._sc_verify(bool(created), "NX projected no curves")
        extra = {"associative": True}
        if feature is not None:
            extra["feature"] = self._reference(feature, "feature", part, "Projected curve")
        return self._sc_result(sketch, created, frame, f"Projected {len(items)} object(s)", **extra)

    def _sketch_intersection(self, sketch_id, faces):
        distinct_ids(faces, "faces", 1, 200)
        sketch = self._sc_sketch(sketch_id)
        items = self._sc_model_objects(sketch, faces, {"face"})
        part, nx = self._work_part(), self.nxopen
        frame = self._sketch_frame(sketch)
        before = self._sc_geometry_tags(sketch)
        with self._editing_sketch(sketch):
            builder = part.Sketches.CreateIntersectionCurveBuilder(nx.SketchIntersectionCurve.Null)
            try:
                builder.Associative = True
                builder.JoinCurves = False
                builder.IgnoreHoles = False
                builder.FaceCollector.ReplaceRules(
                    [part.ScRuleFactory.CreateRuleFaceDumb(items)], False
                )
                native_input_error(
                    builder.CollectorUpdated, "The faces do not cross the sketch plane"
                )
                feature = builder.CommitFeature()
            finally:
                builder.Destroy()
            sketch.Update()
        created = [c for c in self._sc_new(sketch, before) if isinstance(c, nx.Curve)]
        self._sc_verify(bool(created), "No intersection of the faces with the sketch plane")
        tol = 1e-6 * max(1.0, *map(abs, frame["origin"]))
        off_plane = [
            c
            for c in created
            for t in (0.0, 0.5, 1.0)
            if abs(_dot(_sub(self._sc_eval(c, t), frame["origin"]), frame["normal"])) > tol
        ]
        self._sc_verify(not off_plane, "Intersection curves are not in the sketch plane")
        extra = {"associative": True}
        if feature is not None:
            extra["feature"] = self._reference(feature, "feature", part, "Intersection curve")
        return self._sc_result(
            sketch, created, frame, f"Intersected {len(items)} face(s) with the sketch", **extra
        )
