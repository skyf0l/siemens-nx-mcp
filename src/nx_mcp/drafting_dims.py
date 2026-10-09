"""NX-side handlers: Drawing dimensions beyond linear (radial, diameter, angular, ordinate) and drawing standard (projection angle, templates).

Conventions: datum_features.py is the reference implementation. Validate every
argument before touching NXOpen; builders create -> configure -> commit ->
Destroy() in finally; read back what NX built and raise NX_VERIFICATION_FAILED
on mismatch so the operation's undo mark rolls it back.

Ordinate dimensions use the documented UF drafting calls (ordinate origin ->
margin -> ordinate dimension): on NX 2406 the OrdinateDimensionBuilder commits
only the origin plus placeholder dimensions without a margin, so it is not used.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

from nx_mcp.runtime import NXToolError
from nx_mcp.workspace import WorkspaceViolation

SHEET_SIZES = {
    "A0": (1189.0, 841.0),
    "A1": (841.0, 594.0),
    "A2": (594.0, 420.0),
    "A3": (420.0, 297.0),
    "A4": (297.0, 210.0),
}
PROJECTIONS = {"first_angle": "First", "third_angle": "Third"}
MAX_ORDINATES = 50
ANGLE_TOL_DEG = 1e-3
TRUE_SHAPE_TOL_DEG = 0.01  # Max tilt of an arc plane against the drawing plane.
MARGIN_GAP_MM = 15.0  # Between the view border and a default ordinate margin.
RADIAL_GAP_MM = 12.0  # Default radial text distance outside the arc.
ANGULAR_OFFSET_MM = 10.0  # Default angular text offset from the edges' mid-point.


# ---- pure helpers (unit tested) ---------------------------------------------
def _number(value, name, positive=False):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or (positive and value <= 0)
    ):
        raise NXToolError(
            "NX_INVALID_ARGUMENT", f"{name} must be finite" + (" and positive" if positive else "")
        )
    return float(value)


def sheet_xy(value, name="origin"):
    """[x, y] sheet coordinates, or None."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise NXToolError("NX_INVALID_ARGUMENT", f"{name} must be [x, y] in sheet units")
    return [_number(v, name) for v in value]


def choice(value, allowed, name):
    if value not in allowed:
        raise NXToolError(
            "NX_INVALID_ARGUMENT", f"{name} must be one of {', '.join(sorted(allowed))}"
        )
    return value


def object_id(value, name):
    if not isinstance(value, str) or not value.strip():
        raise NXToolError("NX_INVALID_ARGUMENT", f"{name} must be an object ID")
    return value


def acute_angle_deg(a, b):
    """Acute angle (0-90 degrees) between two 2D line directions; None if degenerate."""
    na, nb = math.hypot(*a), math.hypot(*b)
    if na <= 1e-12 or nb <= 1e-12:
        return None
    cos = abs(a[0] * b[0] + a[1] * b[1]) / (na * nb)
    return math.degrees(math.acos(min(1.0, cos)))


def angle_matches(computed, acute, tol=ANGLE_TOL_DEG):
    """NX may dimension the acute angle, its supplement or the reflex equivalents."""
    value = float(computed) % 360.0
    candidates = (acute, 180.0 - acute, 180.0 + acute, 360.0 - acute)
    return any(abs(value - c) <= tol for c in candidates)


def projected_ordinate(origin, point, axis):
    """Signed distance from origin to point along a unit axis (any dimension)."""
    return sum((p - o) * a for p, o, a in zip(point, origin, axis, strict=True))


def template_candidates(template, base_dir):
    """Ordered candidate paths for an installed NX sheet template name."""
    name = template.strip()
    root = Path(base_dir) / "DRAFTING" / "templates"
    if name.upper() in SHEET_SIZES:
        name = f"Drawing-{name.upper()}-Size2D-template"
    if not name.lower().endswith(".prt"):
        name += ".prt"
    return [root / name]


def default_radial_origin(center, sheet_radius, mm):
    """Up-right of the arc center at 45 degrees, RADIAL_GAP_MM outside the arc (sheet units)."""
    reach = (sheet_radius + RADIAL_GAP_MM * mm) * math.sqrt(0.5)
    return [center[0] + reach, center[1] + reach]


def default_ordinate_margin(bounds, horizontal, mm):
    """Margin line (sheet units) MARGIN_GAP_MM above / left of view borders [x0, y0, x1, y1]."""
    gap = MARGIN_GAP_MM * mm
    return bounds[3] + gap if horizontal else bounds[0] - gap


def is_unit_transfer_error(error):
    """NX refuses to copy template objects into a part with other base units."""
    return "units are different" in str(error)


def ordinate_snap(kind, snap, label, closed=False):
    """Resolve auto snap and validate it for a curve kind ("line", "arc", "other")."""
    if snap == "auto":
        snap = "center" if kind == "arc" else "start"
    if snap == "center" and kind != "arc":
        raise NXToolError("NX_OBJECT_TYPE_MISMATCH", f"{label}: center needs a circular edge")
    if closed and snap in {"start", "end"}:
        # NX measures a closed circle at its center whatever end point is requested.
        raise NXToolError(
            "NX_OBJECT_TYPE_MISMATCH",
            f"{label}: a full circle has no end vertices; use center (or auto)",
        )
    return snap


def arc_plane_normal(center, points):
    """Unit normal of an arc's plane from points on it; None if they are degenerate."""
    vectors = [[p[i] - center[i] for i in range(3)] for p in points]
    best = (0.0, None)
    for i, a in enumerate(vectors):
        for b in vectors[i + 1 :]:
            c = [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]
            norm = math.sqrt(sum(x * x for x in c))
            if norm > best[0]:
                best = (norm, c)
    if best[1] is None or best[0] <= 1e-12:
        return None
    return [x / best[0] for x in best[1]]


def seen_true_shape(normal, view_z, tol_deg=TRUE_SHAPE_TOL_DEG):
    """True if a plane with this normal is parallel to the drawing plane of the view."""
    dot = abs(sum(a * b for a, b in zip(normal, view_z, strict=True)))
    return math.degrees(math.acos(min(1.0, dot))) <= tol_deg


# ---- NX-side mixin --------------------------------------------------------------
class DraftingDimsMixin:
    # helpers ------------------------------------------------------------------
    def _dd_verify(self, ok, message, **actual):
        if not ok:
            raise NXToolError("NX_VERIFICATION_FAILED", message, details={"actual": actual})

    def _dd_uf(self):
        import NXOpen.UF

        return NXOpen.UF.UFSession.GetUFSession()

    def _dd_curve(self, edge):
        """Native curve facts of an edge: kind, start/end points and arc data."""
        uf = self._dd_uf()
        evaluator = uf.Eval.Initialize2(edge.Tag)
        limits = uf.Eval.AskLimits(evaluator)
        start = [float(v) for v in uf.Eval.EvaluateUnitVectors(evaluator, limits[0])[0]]
        end = [float(v) for v in uf.Eval.EvaluateUnitVectors(evaluator, limits[1])[0]]
        facts = {"kind": "other", "start": start, "end": end}
        if uf.Eval.IsLine(evaluator):
            facts["kind"] = "line"
        elif uf.Eval.IsArc(evaluator):
            arc = uf.Eval.AskArc(evaluator)
            radius = float(arc.Radius)
            facts.update(
                kind="arc",
                center=[float(v) for v in arc.Center],
                radius=radius,
                x_axis=[float(v) for v in arc.XAxis],
                samples=[
                    [float(v) for v in uf.Eval.EvaluateUnitVectors(evaluator, t)[0]]
                    for t in (
                        limits[0] + (limits[1] - limits[0]) / 3.0,
                        limits[0] + 2.0 * (limits[1] - limits[0]) / 3.0,
                    )
                ],
                closed=math.dist(start, end) <= 1e-6 * max(1.0, radius),
            )
        return facts

    def _dd_to_sheet(self, view, point):
        """Model point -> drawing-sheet coordinates (sheet units) in a drawing view.

        UF_VIEW_map_model_to_drawing (like UF_DRAW_ask_view_borders) answers in the
        sheet's units; NXOpen origins and UF_DRF placement points take part units.
        """
        mapped = self._dd_uf().View.MapModelToDrawing(view.Tag, [float(v) for v in point])
        return [float(mapped[0]), float(mapped[1])]

    def _dd_mm(self, sheet):
        """Sheet units per millimetre."""
        return 1.0 / 25.4 if self._sheet_units(sheet) == "in" else 1.0

    def _dd_sheet_factor(self, sheet):
        """Sheet units per native drawing (part) unit."""
        return (25.4 if self._units() == "inch" else 1.0) / (
            25.4 if self._sheet_units(sheet) == "in" else 1.0
        )

    def _dd_view_axes(self, view):
        m = view.Matrix
        return [m.Xx, m.Xy, m.Xz], [m.Yx, m.Yy, m.Yz], [m.Zx, m.Zy, m.Zz]

    def _dd_dimension_result(self, result, drawing_view, view, point, **extra):
        sheet = self._view_sheet(drawing_view)
        return {
            "object": self._reference(result, "dimension", self._work_part(), "Drawing dimension"),
            "dimension_name": result.Name,
            "native_type": type(result).__name__,
            "view": view,
            "measured_value": float(result.ComputedSize),
            **self._drawing_coordinates(sheet, point, "origin"),
            **extra,
        }

    # radial / diameter --------------------------------------------------------
    def _add_radial_dimension(self, view, edge, kind="radius", origin=None):
        choice(kind, {"radius", "diameter"}, "kind")
        object_id(view, "view")
        object_id(edge, "edge")
        point = sheet_xy(origin)
        drawing_view = self._drawing_object(view, "drawing_view")
        e = self._drawing_dimension_edge(edge)
        curve = self._dd_curve(e)
        if curve["kind"] != "arc":
            raise NXToolError("NX_OBJECT_TYPE_MISMATCH", "edge must be a circular edge or arc")
        center, radius = curve["center"], curve["radius"]
        normal = arc_plane_normal(center, [curve["start"], *curve["samples"]])
        if normal is None or not seen_true_shape(normal, self._dd_view_axes(drawing_view)[2]):
            raise NXToolError(
                "NX_UNSUPPORTED_ARGUMENT",
                "The arc is not seen true-shape in this view (its plane is not parallel to the "
                "drawing plane); use a view that looks along the arc's axis",
            )
        on_arc = [c + radius * a for c, a in zip(center, curve["x_axis"], strict=True)]
        sheet = self._view_sheet(drawing_view)
        if point is None:
            c2 = self._dd_to_sheet(drawing_view, center)
            sheet_radius = math.dist(c2, self._dd_to_sheet(drawing_view, on_arc))
            point = default_radial_origin(c2, sheet_radius, self._dd_mm(sheet))
        builder = self._work_part().Dimensions.CreateRadialDimensionBuilder(None)
        try:
            methods = builder.Measurement.MeasurementMethod
            builder.Measurement.Method = methods.Radial if kind == "radius" else methods.Diametral
            builder.FirstAssociativity.SetValue(
                self.nxopen.InferSnapType.SnapType.NotSet,
                e,
                drawing_view,
                self.nxopen.Point3d(*on_arc),
                None,
                None,
                self.nxopen.Point3d(0.0, 0.0, 0.0),
            )
            builder.Origin.OriginPoint = self._sheet_point3d(sheet, point)
            result = builder.Commit()
        finally:
            builder.Destroy()
        expected = radius if kind == "radius" else 2.0 * radius
        actual = float(result.ComputedSize)
        self._dd_verify(
            abs(actual - expected) <= 1e-6 * max(1.0, expected),
            f"NX {kind} dimension value differs from the edge geometry",
            computed=actual,
            expected=expected,
            native_type=type(result).__name__,
        )
        return self._dd_dimension_result(
            result,
            drawing_view,
            view,
            point,
            kind=kind,
            expected_value=expected,
            edge_radius=radius,
            edge_center=center,
            units=self._units(),
            association="circular edge (value follows the model)",
        )

    # angular ----------------------------------------------------------------
    def _add_angular_dimension(self, view, first, second, origin=None):
        object_id(view, "view")
        object_id(first, "first")
        object_id(second, "second")
        if first == second:
            raise NXToolError("NX_INVALID_ARGUMENT", "first and second must be different edges")
        point = sheet_xy(origin)
        drawing_view = self._drawing_object(view, "drawing_view")
        edges = [self._drawing_dimension_edge(first), self._drawing_dimension_edge(second)]
        curves = [self._dd_curve(e) for e in edges]
        for label, curve in zip(("first", "second"), curves, strict=True):
            if curve["kind"] != "line":
                raise NXToolError("NX_OBJECT_TYPE_MISMATCH", f"{label} must be a straight edge")
        flat = [
            [self._dd_to_sheet(drawing_view, c["start"]), self._dd_to_sheet(drawing_view, c["end"])]
            for c in curves
        ]
        acute = acute_angle_deg(*[[q[0] - p[0], q[1] - p[1]] for p, q in flat])
        if acute is None:
            raise NXToolError(
                "NX_INVALID_ARGUMENT", "An edge is seen end-on in this view; choose another view"
            )
        if acute < 1e-3:
            raise NXToolError("NX_INVALID_ARGUMENT", "The edges are parallel in this view")
        sheet = self._view_sheet(drawing_view)
        if point is None:
            offset = ANGULAR_OFFSET_MM * self._dd_mm(sheet)
            mids = [[(p[i] + q[i]) / 2.0 for i in range(2)] for p, q in flat]
            point = [(mids[0][i] + mids[1][i]) / 2.0 + offset for i in range(2)]
        mids3 = [[(u + v) / 2.0 for u, v in zip(c["start"], c["end"], strict=True)] for c in curves]
        builder = self._work_part().Dimensions.CreateAngularDimensionBuilder(None)
        try:
            snap = self.nxopen.InferSnapType.SnapType.NotSet
            empty = self.nxopen.Point3d(0.0, 0.0, 0.0)
            builder.FirstAssociativity.SetValue(
                snap, edges[0], drawing_view, self.nxopen.Point3d(*mids3[0]), None, None, empty
            )
            builder.SecondAssociativity.SetValue(
                snap, edges[1], drawing_view, self.nxopen.Point3d(*mids3[1]), None, None, empty
            )
            builder.Origin.OriginPoint = self._sheet_point3d(sheet, point)
            result = builder.Commit()
        finally:
            builder.Destroy()
        actual = float(result.ComputedSize)
        self._dd_verify(
            angle_matches(actual, acute),
            "NX angular dimension differs from the angle between the edges in the view",
            computed=actual,
            view_angle=acute,
        )
        return self._dd_dimension_result(
            result,
            drawing_view,
            view,
            point,
            view_angle_deg=acute,
            supplement_deg=180.0 - acute,
            units="deg",
            association="two straight edges (value follows the model)",
        )

    # ordinate -----------------------------------------------------------------
    def _dd_ordinate_target(self, ref, snap, label):
        edge = self._drawing_dimension_edge(ref)
        curve = self._dd_curve(edge)
        snap = ordinate_snap(curve["kind"], snap, label, curve.get("closed", False))
        if snap == "center":
            return edge, snap, curve["center"], ("ARC_CENTER", 0)
        return edge, snap, curve[snap], ("END_POINT", 1 if snap == "start" else 2)

    def _dd_drf_object(self, edge, view, assoc):
        import NXOpen.UF as U

        data = U.Drf.Object()
        data.ObjectTag = edge.Tag
        data.ObjectViewTag = view.Tag
        data.ObjectAssocType = getattr(U.Drf.AssocType, assoc[0])
        data.ObjectAssocModifier = assoc[1]
        return data

    def _add_ordinate_dimensions(
        self,
        view,
        origin_object,
        objects,
        direction="horizontal",
        origin_snap="start",
        object_snap="auto",
        margin=None,
    ):
        object_id(view, "view")
        object_id(origin_object, "origin_object")
        if not isinstance(objects, list) or not 1 <= len(objects) <= MAX_ORDINATES:
            raise NXToolError("NX_INVALID_ARGUMENT", "objects must list 1-50 edge IDs")
        for i, item in enumerate(objects):
            object_id(item, f"objects[{i}]")
        if len(set(objects)) != len(objects):
            raise NXToolError("NX_INVALID_ARGUMENT", "objects must be unique edge IDs")
        if origin_object in objects:
            raise NXToolError("NX_INVALID_ARGUMENT", "origin_object cannot also be in objects")
        choice(direction, {"horizontal", "vertical"}, "direction")
        choice(origin_snap, {"start", "end", "center"}, "origin_snap")
        choice(object_snap, {"auto", "start", "end", "center"}, "object_snap")
        if margin is not None:
            margin = _number(margin, "margin")
        drawing_view = self._drawing_object(view, "drawing_view")
        sheet = self._view_sheet(drawing_view)
        factor = self._dd_sheet_factor(sheet)
        origin_edge, origin_snap, origin_xyz, origin_assoc = self._dd_ordinate_target(
            origin_object, origin_snap, "origin_object"
        )
        targets = [
            (ref, *self._dd_ordinate_target(ref, object_snap, f"objects[{i}]"))
            for i, ref in enumerate(objects)
        ]
        horizontal = direction == "horizontal"
        axis = self._dd_view_axes(drawing_view)[0 if horizontal else 1]
        uf = self._dd_uf()
        if margin is None:  # Above the view for horizontal, left of it for vertical ordinates.
            bounds = [float(v) for v in uf.Draw.AskViewBorders(drawing_view.Tag)]  # Sheet units.
            margin = default_ordinate_margin(bounds, horizontal, self._dd_mm(sheet))
        # UF_DRF placement points are in part units; the mapped points are in sheet units.
        line = margin / factor
        origin_at = [v / factor for v in self._dd_to_sheet(drawing_view, origin_xyz)]
        import NXOpen.UF as U

        origin_tag = uf.Drf.CreateOrdorigin(
            self._dd_drf_object(origin_edge, drawing_view, origin_assoc), 5, 2, 2, ""
        )
        margin_tag = uf.Drf.CreateOrdinateMargin(
            1 if horizontal else 2,
            origin_tag,
            0,
            [origin_at[0], line] if horizontal else [line, origin_at[1]],
            [1.0, 0.0] if horizontal else [0.0, 1.0],
            0.0,
        )
        lookup = self.nxopen.TaggedObjectManager.GetTaggedObject
        part = self._work_part()
        results = []
        for ref, edge, snap, xyz, assoc in targets:
            at = [v / factor for v in self._dd_to_sheet(drawing_view, xyz)]
            tag = uf.Drf.CreateOrddimension(
                margin_tag,
                1 if horizontal else 2,
                self._dd_drf_object(edge, drawing_view, assoc),
                0.0,
                0.0,
                U.Drf.Text(),
                2,
                [at[0], line, 0.0] if horizontal else [line, at[1], 0.0],
            )
            dimension = lookup(tag)
            expected = abs(projected_ordinate(origin_xyz, xyz, axis))
            actual = float(dimension.ComputedSize)
            self._dd_verify(
                abs(actual - expected) <= 1e-6 * max(1.0, expected)
                and int(dimension.GetOrdinateOrigin().Tag) == int(origin_tag),
                f"NX ordinate dimension for {ref} differs from the model geometry",
                computed=actual,
                expected=expected,
                object=ref,
            )
            results.append(
                {
                    "object": self._reference(dimension, "dimension", part, "Ordinate dimension"),
                    "native_type": type(dimension).__name__,
                    "target": ref,
                    "snap": snap,
                    "model_point": xyz,
                    "measured_value": actual,
                    "expected_value": expected,
                }
            )
        return {
            "ordinate_origin": self._reference(
                lookup(origin_tag), "dimension", part, "Ordinate origin"
            ),
            "origin_snap": origin_snap,
            "origin_model_point": origin_xyz,
            "direction": direction,
            "measurement_axis": axis,
            "view": view,
            "margin": margin,
            "dimensions": results,
            "count": len(results),
            "units": self._units(),
            "sheet_units": self._sheet_units(sheet),
            "coordinate_frame": "drawing_sheet",
        }

    # drawing sheet ------------------------------------------------------------
    def _dd_template_path(self, template):
        text = template.strip()
        if "/" in text or "\\" in text:
            try:  # Workspace-relative (or absolute inside the workspace), like other file tools.
                path = self.workspace.resolve(text)
            except WorkspaceViolation as error:
                raise NXToolError("NX_PATH_OUTSIDE_WORKSPACE", str(error)) from None
            if path.suffix.lower() != ".prt" or not path.is_file():
                raise NXToolError("NX_FILE_NOT_FOUND", f"Template part not found: {text}")
            return path
        base = os.environ.get("UGII_BASE_DIR", "")
        for candidate in template_candidates(text, base) if base else []:
            if candidate.is_file():
                return candidate
        raise NXToolError(
            "NX_NOT_FOUND",
            f"No installed NX drawing template named {text!r} in "
            "UGII_BASE_DIR/DRAFTING/templates; use e.g. 'A3' or 'Drawing-A3-Size2D-template'",
        )

    def _create_drawing_sheet(
        self,
        name="Sheet1",
        size=None,
        scale=1.0,
        units="mm",
        projection="third_angle",
        template=None,
    ):
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            raise NXToolError("NX_INVALID_ARGUMENT", "name must be a nonempty sheet name")
        name = name.strip()
        if size is not None:
            choice(size, set(SHEET_SIZES), "size")
        scale = _number(scale, "scale", positive=True)
        choice(units, {"mm", "in"}, "units")
        choice(projection, set(PROJECTIONS), "projection")
        if template is not None:
            if not isinstance(template, str) or not template.strip():
                raise NXToolError("NX_INVALID_ARGUMENT", "template must be a name or .prt path")
            if template.strip().upper() in SHEET_SIZES and units != "mm":
                raise NXToolError(
                    "NX_INVALID_ARGUMENT", "The installed A0-A4 template sheets are metric (mm)"
                )
        part = self._work_part()
        if any(s.Name.casefold() == name.casefold() for s in part.DrawingSheets):
            raise NXToolError("NX_INVALID_ARGUMENT", f"A drawing sheet named {name!r} exists")
        template_path = self._dd_template_path(template) if template is not None else None
        import NXOpen.Drawings as D

        before = {int(s.Tag) for s in part.DrawingSheets}
        title_blocks = len(list(part.DraftingManager.TitleBlocks))
        b = part.DraftingDrawingSheets.CreateDraftingDrawingSheetBuilder(None)
        try:
            b.Name = name
            b.Units = b.SheetUnits.Metric if units == "mm" else b.SheetUnits.English
            if template_path is None:
                b.Option = b.SheetOption.CustomSize
                length, height = SHEET_SIZES[size or "A3"]
                b.Length = length / (25.4 if units == "in" else 1.0)
                b.Height = height / (25.4 if units == "in" else 1.0)
            else:
                b.Option = b.SheetOption.UseTemplate
                if units == "mm":
                    b.MetricSheetTemplateLocation = str(template_path)
                else:
                    b.EnglishSheetTemplateLocation = str(template_path)
                b.AutoStartViewCreation = False
            b.ScaleNumerator = scale
            b.ScaleDenominator = 1.0
            b.ProjectionAngle = getattr(b.SheetProjectionAngle, PROJECTIONS[projection])
            try:
                b.Commit()
            except Exception as error:
                if template_path is None or not is_unit_transfer_error(error):
                    raise
                raise NXToolError(
                    "NX_UNSUPPORTED_ARGUMENT",
                    "The template part's units differ from the work part's: use a metric "
                    "template (e.g. A0-A4) in a mm part and an inch template (e.g. "
                    "Drawing-B-Size2D-template) in an inch part",
                    nx_code=getattr(error, "ErrorCode", None),
                ) from None
        finally:
            b.Destroy()
        created = [s for s in part.DrawingSheets if int(s.Tag) not in before]
        self._dd_verify(len(created) == 1, "NX did not create exactly one drawing sheet")
        sheet = created[0]
        if sheet.Name != name:
            sheet.SetName(name)  # Template sheets keep the template's own sheet name.
        native_projection = (
            "third_angle"
            if sheet.ProjectionAngle == D.DrawingSheet.ProjectionAngleType.ThirdAngle
            else "first_angle"
        )
        native_scale = sheet.GetScale()
        # Templates carry their own scale and projection angle: apply the request explicitly.
        angle = getattr(D.DrawingSheet.ProjectionAngleType, PROJECTIONS[projection] + "Angle")
        sheet.SetParameters(sheet.Height, sheet.Length, scale, 1.0, sheet.Units, angle)
        self._open_drawing_sheet(sheet)
        actual_units = self._sheet_units(sheet)
        mm = 25.4 if actual_units == "in" else 1.0
        dims_mm = [float(sheet.Length) * mm, float(sheet.Height) * mm]
        actual_size = next(
            (
                k
                for k, (length, height) in SHEET_SIZES.items()
                if abs(dims_mm[0] - length) <= 0.5 and abs(dims_mm[1] - height) <= 0.5
            ),
            None,
        )
        numerator, denominator = sheet.GetScale()
        actual_projection = (
            "third_angle"
            if sheet.ProjectionAngle == D.DrawingSheet.ProjectionAngleType.ThirdAngle
            else "first_angle"
        )
        self._dd_verify(
            actual_projection == projection
            and actual_units == units
            and math.isclose(numerator / denominator, scale, rel_tol=1e-10)
            and (size is None or actual_size == size)
            and sheet.Name == name,
            "NX drawing sheet differs from the request",
            projection=actual_projection,
            units=actual_units,
            scale=numerator / denominator,
            size=actual_size,
            dimensions_mm=dims_mm,
            name=sheet.Name,
        )
        side = "right" if actual_projection == "third_angle" else "left"
        warnings = []
        if template_path is not None and native_projection != projection:
            warnings.append(
                f"The template was drawn for {native_projection.replace('_', ' ')} projection: "
                "the sheet now projects views in "
                f"{projection.replace('_', ' ')}, but the template's printed projection symbol "
                "is static border graphics and still shows the template's convention."
            )
        if template_path is not None and not math.isclose(
            native_scale[0] / native_scale[1], scale, rel_tol=1e-10
        ):
            warnings.append(
                "The sheet scale was changed from the template's; static title-block scale "
                "text from the template is not updated."
            )
        return {
            "object": self._reference(sheet, "drawing_sheet", part, "Drawing sheet"),
            "sheet_name": sheet.Name,
            "size": actual_size,
            "dimensions": [float(sheet.Length), float(sheet.Height)],
            "dimensions_mm": dims_mm,
            "units": actual_units,
            "scale": numerator / denominator,
            "scale_ratio": [numerator, denominator],
            "projection": actual_projection,
            "projection_convention": (
                f"a view projected to the right of its parent shows the part's {side} side"
            ),
            "template": str(template_path) if template_path else None,
            "template_projection": native_projection if template_path else None,
            "warnings": warnings,
            "title_block_added": len(list(part.DraftingManager.TitleBlocks)) > title_blocks,
        }
