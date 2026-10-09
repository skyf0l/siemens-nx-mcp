"""NX-side handlers: Drawing annotations: notes and leaders, center marks, centerlines, surface finish and weld symbols.

Conventions: datum_features.py is the reference implementation. Validate every
argument before touching NXOpen; builders create -> configure -> commit ->
Destroy() in finally; read back what NX built and raise NX_VERIFICATION_FAILED
on mismatch so the operation's undo mark rolls it back.

Sheet coordinates: origins are [x, y] in the sheet's own units (the frame of
nx_list_drawings and nx_list_annotations positions). Leaders attach to a model
edge/face as shown in one drawing view; the leader point is that object's
evaluated midpoint mapped into the view (native UF_DRAW_map_model_to_drawing).
"""

from __future__ import annotations

import math

from nx_mcp.runtime import NXToolError

MAX_LINES = 50
MAX_LINE_LENGTH = 1024
MAX_RA_UM = 1000.0
SYMBOLS = {
    "basic": "Basic",
    "material_removal_required": "MaterialRemovalRequired",
    "material_removal_prohibited": "MaterialRemovalProhibited",
}
WELDS = {"fillet": "Fillet", "square_butt": "SquareButt", "v_butt": "VButt"}
SIDES = {"arrow": ("arrow",), "other": ("other",), "both": ("arrow", "other")}
CENTERLINE_KINDS = {"2d", "3d"}
# UF face types accepted by a 3D centerline (cylinder, cone).
AXIAL_FACE_TYPES = {16: "cylinder", 17: "cone"}
TRUE_SHAPE_TOL_DEG = 0.01  # Max tilt of a center-marked circle against the drawing plane.


def _invalid(message):
    return NXToolError("NX_INVALID_ARGUMENT", message, details={"mutation_outcome": "not_started"})


def note_lines(text):
    """Validated list of note lines (at least one non-blank)."""
    if not isinstance(text, (list, tuple)) or not 1 <= len(text) <= MAX_LINES:
        raise _invalid(f"text must be a list of 1-{MAX_LINES} lines")
    lines = []
    for line in text:
        if not isinstance(line, str) or len(line) > MAX_LINE_LENGTH:
            raise _invalid(f"Each text line must be a string of at most {MAX_LINE_LENGTH} chars")
        if "\n" in line or "\r" in line:
            raise _invalid("Pass each line as its own list item; lines may not contain newlines")
        lines.append(line)
    if not any(line.strip() for line in lines):
        raise _invalid("text must contain at least one non-blank line")
    return lines


def sheet_point(value, name="origin"):
    """[x, y] sheet coordinates as finite floats."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise _invalid(f"{name} must be [x, y] in sheet units")
    result = []
    for v in value:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise _invalid(f"{name} must contain finite numbers")
        result.append(float(v))
    return result


def positive(value, name, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _invalid(f"{name} must be a finite number")
    if value <= 0 or (maximum is not None and value > maximum):
        limit = f" and at most {maximum:g}" if maximum is not None else ""
        raise _invalid(f"{name} must be positive{limit}")
    return float(value)


def number_text(value):
    """Shortest exact decimal for a drawing callout (3.2 -> '3.2', 5.0 -> '5')."""
    return f"{float(value):.6g}"


def ra_text(roughness):
    return "Ra " + number_text(roughness)


def distinct_ids(values, minimum, maximum, name):
    if not isinstance(values, (list, tuple)) or not minimum <= len(values) <= maximum:
        count = str(minimum) if minimum == maximum else f"{minimum}-{maximum}"
        raise _invalid(f"{name} must list {count} object IDs")
    if any(not isinstance(v, str) or not v for v in values) or len(set(values)) != len(values):
        raise _invalid(f"{name} must be distinct non-empty object IDs")
    return list(values)


def object_id(value, name):
    if not isinstance(value, str) or not value:
        raise _invalid(f"{name} must be an object ID")
    return value


def enum_name(enum_type, value, names):
    """Name of a native enum member among names (NX enums stringify as integers)."""
    for name in names:
        member = getattr(enum_type, name, None)
        if member is None and name == "None":  # Python NXOpen cannot expose a member "None".
            if str(value) == "0":
                return name
        elif member == value:
            return name
    return str(value)


def line_distance(point, origin, direction):
    """Distance from a 2D point to the infinite line through origin along direction."""
    length = math.hypot(*direction)
    if length == 0:
        return math.hypot(point[0] - origin[0], point[1] - origin[1])
    return (
        abs((point[0] - origin[0]) * direction[1] - (point[1] - origin[1]) * direction[0]) / length
    )


def parallel2(a, b, tol=1e-6):
    la, lb = math.hypot(*a), math.hypot(*b)
    return la > 0 and lb > 0 and abs(a[0] * b[1] - a[1] * b[0]) <= tol * la * lb


def uv_samples(steps=8):
    """Normalized (u, v) samples ordered from the parameter-box center outwards."""
    values = [i / steps for i in range(1, steps)]
    grid = [(u, v) for u in values for v in values]
    return sorted(grid, key=lambda uv: ((uv[0] - 0.5) ** 2 + (uv[1] - 0.5) ** 2, uv))


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


def close2(a, b, tol=1e-3):
    return all(abs(x - y) <= tol * max(1.0, abs(y)) for x, y in zip(a, b, strict=True))


class DraftingNotesMixin:
    # ---- helpers ---------------------------------------------------------
    def _dn_sheet(self, sheet):
        part = self._work_part()
        if sheet is None:
            current = part.DrawingSheets.CurrentDrawingSheet
            if current is None:
                raise NXToolError(
                    "NX_NO_DRAWING", "Open a drawing sheet first or pass sheet explicitly"
                )
            return current
        target = self._drawing_object(sheet, "drawing_sheet")
        if part.DrawingSheets.CurrentDrawingSheet != target:
            self._open_drawing_sheet(target)
        return target

    def _dn_view(self, view):
        target = self._drawing_object(view, "drawing_view")
        sheet = self._view_sheet(target)
        if self._work_part().DrawingSheets.CurrentDrawingSheet != sheet:
            self._open_drawing_sheet(sheet)
        return target, sheet

    def _dn_geometry(self, reference, kinds):
        obj = self._resolve(reference, kinds)
        if getattr(obj, "IsOccurrence", False):
            if obj.OwningComponent not in [c for c, _ in self._walk_components(self._work_part())]:
                raise NXToolError(
                    "NX_OBJECT_OWNER_MISMATCH", "Select geometry in the work assembly"
                )
        elif obj.OwningPart != self._work_part():
            raise NXToolError("NX_OBJECT_OWNER_MISMATCH", "Select owned work-part geometry")
        return obj

    def _dn_kind(self, obj):
        return "edge" if type(obj).__name__ == "Edge" or hasattr(obj, "SolidEdgeType") else "face"

    def _dn_model_point(self, obj):
        """A point on the edge (parametric midpoint) or inside the face, in model coordinates."""
        import NXOpen.UF as U

        modeling = U.UFSession.GetUFSession().Modeling
        if self._dn_kind(obj) == "edge":
            return [
                float(v)
                for v in U.UFSession.GetUFSession().ModlGeneral.AskCurveProps(obj.Tag, 0.5)[0]
            ]
        u0, u1, v0, v1 = modeling.AskFaceUvMinmax(obj.Tag)
        step = 1 / 8

        def at(fu, fv):
            uv = [u0 + fu * (u1 - u0), v0 + fv * (v1 - v0)]
            return [float(v) for v in modeling.AskFaceProps(obj.Tag, uv)[0]]

        def inside(fu, fv):
            # 1 = inside the trimmed face (not in a hole, not on its boundary).
            return int(modeling.AskPointContainment(at(fu, fv), obj.Tag)) == 1

        samples = uv_samples()
        interior = [uv for uv in samples if inside(*uv)]
        for fu, fv in interior:  # Prefer a point whose neighbours are inside too (clear of holes).
            near = [(fu + du, fv + dv) for du, dv in ((step, 0), (-step, 0), (0, step), (0, -step))]
            if all(0 < a < 1 and 0 < b < 1 and inside(a, b) for a, b in near):
                return at(fu, fv)
        return at(*(interior or samples)[0])

    def _dn_to_sheet(self, view, model_point):
        import NXOpen.UF as U

        mapped = U.UFSession.GetUFSession().View.MapModelToDrawing(view.Tag, model_point)
        return [float(mapped[0]), float(mapped[1])]

    def _dn_edge_ends(self, view, edge):
        import NXOpen.UF as U

        general = U.UFSession.GetUFSession().ModlGeneral
        return [
            self._dn_to_sheet(view, [float(v) for v in general.AskCurveProps(edge.Tag, t)[0]])
            for t in (0.0, 1.0)
        ]

    def _dn_axis(self, view, face):
        """Projected axis (sheet point, sheet direction) of a cylindrical/conical face."""
        import NXOpen.UF as U

        data = U.UFSession.GetUFSession().Modeling.AskFaceData(face.Tag)
        if int(data[0]) not in AXIAL_FACE_TYPES:
            raise NXToolError(
                "NX_OBJECT_TYPE_MISMATCH", "A 3D centerline needs a cylindrical or conical face"
            )
        point, direction = [float(v) for v in data[1]], [float(v) for v in data[2]]
        a = self._dn_to_sheet(view, point)
        b = self._dn_to_sheet(view, [p + d * 100.0 for p, d in zip(point, direction, strict=True)])
        projected = [b[0] - a[0], b[1] - a[1]]
        if math.hypot(*projected) < 1e-3:
            raise NXToolError(
                "NX_UNSUPPORTED_ARGUMENT",
                "The face axis points out of this view; use nx_add_center_mark on its circular "
                "edge, or a view in which the axis lies in the view plane",
            )
        return a, projected

    def _dn_check_leader(self, reader, geometry, view):
        """Read the first leader back from a reopened builder and verify its attachment."""
        leaders = reader.Leader.Leaders
        attached = leaders.FindItem(0).Leader.GetValue() if leaders.Length else (None, None, None)
        obj, attached_view = attached[0], attached[1]
        if (
            obj is None
            or int(obj.Tag) != int(geometry.Tag)
            or attached_view is None
            or int(attached_view.Tag) != int(view.Tag)
        ):
            raise NXToolError(
                "NX_VERIFICATION_FAILED",
                "The native leader is not attached to the requested object in the view",
            )

    def _dn_leader_info(self, geometry, view, anchor):
        part = self._work_part()
        return {
            "object": self._reference(geometry, self._dn_kind(geometry), part, "Geometry"),
            "view": self._reference(view, "drawing_view", part, "View"),
            "point": anchor,
            "verified": True,
        }

    def _dn_point3d(self, sheet, point):
        return self._sheet_point3d(sheet, point)

    def _dn_leader(self, leader_builder, obj, view, sheet):
        """Append one filled-arrow leader attached to obj as shown in view."""
        import NXOpen.Annotations as A

        part = self._work_part()
        model = self._dn_model_point(obj)
        anchor = self._dn_to_sheet(view, model)
        data = part.Annotations.CreateLeaderData()
        data.StubSize = 5.0
        data.Arrowhead = A.LeaderData.ArrowheadType.FilledArrow
        data.VerticalAttachment = A.LeaderVerticalAttachment.Center
        leader_builder.Leaders.Append(data)
        data.StubSide = A.LeaderSide.Inferred
        # Selection points are model coordinates (part units), not sheet coordinates.
        data.Leader.SetValue(obj, view, self.nxopen.Point3d(*model))
        return anchor

    def _dn_origin(self, origin_builder, sheet, point):
        import NXOpen.Annotations as A

        origin_builder.SetInferRelativeToGeometry(False)
        origin_builder.Plane.PlaneMethod = A.PlaneBuilder.PlaneMethodType.XyPlane
        origin_builder.Origin.SetValue(None, None, self._dn_point3d(sheet, point))

    def _dn_sheet_xy(self, sheet, point3d):
        """Native annotation origin (part units) -> sheet units."""
        factor = (25.4 if self._units() == "inch" else 1.0) / (
            25.4 if self._sheet_units(sheet) == "in" else 1.0
        )
        return [float(point3d.X) * factor, float(point3d.Y) * factor]

    def _dn_commit(self, builder, what):
        if hasattr(builder, "Validate") and not builder.Validate():
            raise NXToolError("NX_ANNOTATION_INVALID", f"Native {what} builder validation failed")
        obj = builder.Commit()
        if obj is None:
            raise NXToolError("NX_VERIFICATION_FAILED", f"NX returned no {what}")
        return obj

    def _dn_verify_origin(self, obj, sheet, point):
        actual = self._dn_sheet_xy(sheet, obj.AnnotationOrigin)
        if not close2(actual, point):
            raise NXToolError(
                "NX_VERIFICATION_FAILED",
                "Native annotation origin differs from the request",
                details={"actual": actual, "requested": point},
            )
        return actual

    def _dn_associations(self, obj):
        """Objects/views the annotation is natively associated with (readback)."""
        part = self._work_part()
        result = []
        count = int(getattr(obj, "NumberOfAssociativities", 0) or 0)
        for i in range(1, count + 1):
            try:
                assoc = obj.GetAssociativity(i)
            except Exception:
                continue
            first = getattr(assoc, "FirstObject", None)
            view = getattr(assoc, "ObjectView", None)
            if first is None:
                continue
            kind = self._dn_kind(first) if type(first).__name__ in {"Edge", "Face"} else None
            result.append(
                {
                    "object": self._reference(first, kind, part, "Geometry")
                    if kind
                    else {"native_type": type(first).__name__},
                    "view": getattr(view, "Name", None),
                }
            )
        return result

    def _dn_result(self, obj, sheet, label, requested, **extra):
        part = self._work_part()
        ref = self._reference(obj, "annotation", part, label)
        position = self._dn_verify_origin(obj, sheet, requested)
        return {
            "annotation": ref,
            "native_type": type(obj).__name__,
            "created": [ref],
            "sheet": self._reference(sheet, "drawing_sheet", part, "Drawing sheet"),
            **self._drawing_coordinates(sheet, position, "origin"),
            **extra,
        }

    # ---- note ------------------------------------------------------------
    def _add_note(self, text, origin, sheet=None, leader_to=None, view=None):
        lines = note_lines(text)
        point = sheet_point(origin)
        if leader_to is not None:
            object_id(leader_to, "leader_to")
            if view is None:
                raise _invalid("leader_to needs view: the drawing view showing that object")
        elif view is not None:
            raise _invalid("view is only used with leader_to")
        if view is not None:
            target_view, target_sheet = self._dn_view(view)
            if sheet is not None and self._drawing_object(sheet, "drawing_sheet") != target_sheet:
                raise NXToolError("NX_OBJECT_OWNER_MISMATCH", "view is not on the given sheet")
            geometry = self._dn_geometry(leader_to, {"edge", "face"})
        else:
            target_sheet, target_view, geometry = self._dn_sheet(sheet), None, None
        import NXOpen.Annotations as A

        part = self._work_part()
        builder = part.Annotations.CreateDraftingNoteBuilder(A.SimpleDraftingAid.Null)
        try:
            builder.Origin.Anchor = A.OriginBuilder.AlignmentPosition.MidLeft
            builder.Text.TextBlock.SetText(lines)
            anchor = None
            if geometry is not None:
                anchor = self._dn_leader(builder.Leader, geometry, target_view, target_sheet)
            self._dn_origin(builder.Origin, target_sheet, point)
            note = self._dn_commit(builder, "note")
        finally:
            builder.Destroy()
        self._update_model()
        stored = [str(s) for s in note.GetText()]
        if stored != lines:
            raise NXToolError(
                "NX_VERIFICATION_FAILED",
                "Native note text differs from the request",
                details={"actual": stored},
            )
        extra = {"text": stored, "leader": None}
        if geometry is not None:
            reader = part.Annotations.CreateDraftingNoteBuilder(note)
            try:
                self._dn_check_leader(reader, geometry, target_view)
            finally:
                reader.Destroy()
            extra["leader"] = self._dn_leader_info(geometry, target_view, anchor)
        return self._dn_result(note, target_sheet, "Note", point, **extra)

    # ---- center mark -----------------------------------------------------
    def _add_center_mark(self, view, edges):
        refs = distinct_ids(edges, 1, 50, "edges")
        target_view, sheet = self._dn_view(view)
        targets = [self._dn_geometry(r, {"edge"}) for r in refs]
        import NXOpen

        circular = NXOpen.Edge.EdgeType.Circular
        if any(e.SolidEdgeType != circular for e in targets):
            raise NXToolError("NX_OBJECT_TYPE_MISMATCH", "Center marks need circular edges")
        m = target_view.Matrix
        view_z = [m.Zx, m.Zy, m.Zz]
        for ref, edge in zip(refs, targets, strict=True):
            normal = arc_plane_normal(self._dn_arc_center(edge), self._dn_arc_points(edge))
            if normal is None or not seen_true_shape(normal, view_z):
                raise NXToolError(
                    "NX_UNSUPPORTED_ARGUMENT",
                    f"Edge {ref} is not seen along its axis in this view (the circle appears "
                    "as a line or ellipse); use a view that looks along the hole/boss axis, "
                    "or nx_add_centerline(kind='3d') in a side view",
                )
        import NXOpen.Annotations as A

        part = self._work_part()
        before = {int(c.Tag) for c in part.Annotations.Centerlines}
        builder = part.Annotations.Centerlines.CreateCenterMarkBuilder(A.CenterMark.Null)
        try:
            builder.MultipleCenterMarks = len(targets) > 1
            centers = []
            for edge in targets:
                center = self._dn_to_sheet(target_view, self._dn_arc_center(edge))
                centers.append(center)
                on_edge = self._dn_model_point(edge)
                builder.Locations.Add(edge, target_view, self.nxopen.Point3d(*on_edge))
            self._dn_commit(builder, "center mark")
        finally:
            builder.Destroy()
        self._update_model()
        created = [c for c in part.Annotations.Centerlines if int(c.Tag) not in before]
        if len(created) != len(targets):
            raise NXToolError(
                "NX_VERIFICATION_FAILED",
                f"NX created {len(created)} center marks for {len(targets)} edges",
            )
        tags = [int(e.Tag) for e in targets]
        marks = [None] * len(targets)
        for obj in created:
            associated = [
                int(obj.GetAssociativity(i).FirstObject.Tag)
                for i in range(1, int(obj.NumberOfAssociativities) + 1)
                if obj.GetAssociativity(i).FirstObject is not None
            ]
            index = next((tags.index(t) for t in associated if t in tags), None)
            position = self._dn_sheet_xy(sheet, obj.AnnotationOrigin)
            if index is None or marks[index] is not None or not close2(position, centers[index]):
                raise NXToolError(
                    "NX_VERIFICATION_FAILED",
                    "A native center mark is not at its edge's center or not associated with it",
                    details={"center": position, "expected": centers},
                )
            marks[index] = {
                "annotation": self._reference(obj, "annotation", part, "Center mark"),
                "native_type": type(obj).__name__,
                "center": position,
                "edge": self._reference(targets[index], "edge", part, "Edge"),
                "associations": self._dn_associations(obj),
            }
        refs_out = [m["annotation"] for m in marks]
        return {
            "annotations": marks,
            "created": refs_out,
            "view": self._reference(target_view, "drawing_view", part, "View"),
            "sheet_units": self._sheet_units(sheet),
            "coordinate_frame": "drawing_sheet",
        }

    def _dn_arc_points(self, edge):
        import NXOpen.UF as U

        general = U.UFSession.GetUFSession().ModlGeneral
        return [
            [float(v) for v in general.AskCurveProps(edge.Tag, t)[0]] for t in (0.0, 1 / 3, 2 / 3)
        ]

    def _dn_arc_center(self, edge):
        import NXOpen.UF as U

        # Center of curvature: point + principal normal * radius of curvature.
        props = U.UFSession.GetUFSession().ModlGeneral.AskCurveProps(edge.Tag, 0.0)
        point, normal, radius = props[0], props[2], float(props[5])
        return [float(p) + float(n) * radius for p, n in zip(point, normal, strict=True)]

    # ---- centerline ------------------------------------------------------
    def _add_centerline(self, view, objects, kind="3d"):
        if kind not in CENTERLINE_KINDS:
            raise _invalid("kind must be 2d or 3d")
        refs = distinct_ids(objects, *((1, 1) if kind == "3d" else (2, 2)), "objects")
        target_view, sheet = self._dn_view(view)
        import NXOpen
        import NXOpen.Annotations as A

        part = self._work_part()
        collection = part.Annotations.Centerlines
        if kind == "3d":
            face = self._dn_geometry(refs[0], {"face"})
            targets = [face]
            line_point, line_direction = self._dn_axis(target_view, face)
        else:
            targets = [self._dn_geometry(r, {"edge"}) for r in refs]
            if any(e.SolidEdgeType != NXOpen.Edge.EdgeType.Linear for e in targets):
                raise NXToolError(
                    "NX_OBJECT_TYPE_MISMATCH", "A 2D centerline needs two linear edges"
                )
            (a0, a1), (b0, b1) = (self._dn_edge_ends(target_view, e) for e in targets)
            da, db = [a1[0] - a0[0], a1[1] - a0[1]], [b1[0] - b0[0], b1[1] - b0[1]]
            if not parallel2(da, db, 1e-4) or line_distance(b0, a0, da) < 1e-6:
                raise NXToolError(
                    "NX_INVALID_ARGUMENT",
                    "The two edges must appear parallel and distinct in the view",
                )
            line_point = [(a0[i] + a1[i] + b0[i] + b1[i]) / 4 for i in (0, 1)]
            line_direction = da
        builder = (
            collection.CreateCenterline3dBuilder(A.Centerline3d.Null)
            if kind == "3d"
            else collection.CreateCenterline2dBuilder(A.Centerline2d.Null)
        )
        try:
            if kind == "3d":
                on_face = self._dn_model_point(face)
                builder.Face.Add(face, target_view, self.nxopen.Point3d(*on_face))
            else:
                builder.Type = A.Centerline2dBuilder.Types.FromCurves
                for side, edge in zip((builder.Side1, builder.Side2), targets, strict=True):
                    on_edge = self._dn_model_point(edge)
                    side.SetValue(edge, target_view, self.nxopen.Point3d(*on_edge))
            obj = self._dn_commit(builder, "centerline")
        finally:
            builder.Destroy()
        self._update_model()
        position = self._dn_sheet_xy(sheet, obj.AnnotationOrigin)
        offset = line_distance(position, line_point, line_direction)
        if offset > 1e-3 * max(1.0, abs(position[0]), abs(position[1])):
            raise NXToolError(
                "NX_VERIFICATION_FAILED",
                "The native centerline is not on the expected axis/mid-line",
                details={"origin": position, "offset": offset},
            )
        ref = self._reference(obj, "annotation", part, "Centerline")
        return {
            "annotation": ref,
            "native_type": type(obj).__name__,
            "created": [ref],
            "kind": kind,
            "objects": [self._reference(t, self._dn_kind(t), part, "Geometry") for t in targets],
            "associations": self._dn_associations(obj),
            "view": self._reference(target_view, "drawing_view", part, "View"),
            "origin": position,
            "line_point": line_point,
            "line_direction": line_direction,
            "sheet_units": self._sheet_units(sheet),
            "coordinate_frame": "drawing_sheet",
        }

    # ---- surface finish --------------------------------------------------
    def _add_surface_finish(
        self, view, object, roughness, origin, symbol="material_removal_required"
    ):
        if symbol not in SYMBOLS:
            raise _invalid("symbol must be basic, material_removal_required or prohibited")
        value = positive(roughness, "roughness", MAX_RA_UM)
        point = sheet_point(origin)
        object_id(object, "object")
        target_view, sheet = self._dn_view(view)
        geometry = self._dn_geometry(object, {"edge", "face"})
        import NXOpen.Annotations as A

        part = self._work_part()
        text = ra_text(value)
        kinds = A.BaseSurfaceFinishSymbolBuilder.SymbolType
        collection = part.Annotations.DraftingSurfaceFinishSymbol
        builder = collection.CreateDraftingSurfaceFinishSymbolBuilder(
            A.DraftingSurfaceFinishSymbol.Null
        )
        try:
            builder.Finish = getattr(kinds, SYMBOLS[symbol])
            builder.A2 = text
            anchor = self._dn_leader(builder.Leader, geometry, target_view, sheet)
            self._dn_origin(builder.Origin, sheet, point)
            obj = self._dn_commit(builder, "surface finish symbol")
        finally:
            builder.Destroy()
        self._update_model()
        reader = collection.CreateDraftingSurfaceFinishSymbolBuilder(obj)
        try:
            stored = {
                "finish": enum_name(kinds, reader.Finish, SYMBOLS.values()),
                "a2": str(reader.A2),
            }
            self._dn_check_leader(reader, geometry, target_view)
        finally:
            reader.Destroy()
        if stored["finish"] != SYMBOLS[symbol] or stored["a2"] != text:
            raise NXToolError(
                "NX_VERIFICATION_FAILED",
                "Native surface finish symbol differs from the request",
                details={"actual": stored},
            )
        return self._dn_result(
            obj,
            sheet,
            "Surface finish",
            point,
            symbol=symbol,
            roughness_text=text,
            native=stored,
            leader=self._dn_leader_info(geometry, target_view, anchor),
        )

    # ---- weld symbol -----------------------------------------------------
    def _add_weld_symbol(self, view, object, weld, size, origin, side="arrow"):
        if weld not in WELDS:
            raise _invalid("weld must be fillet, square_butt or v_butt")
        if side not in SIDES:
            raise _invalid("side must be arrow, other or both")
        value = positive(size, "size")
        point = sheet_point(origin)
        object_id(object, "object")
        target_view, sheet = self._dn_view(view)
        geometry = self._dn_geometry(object, {"edge", "face"})
        import NXOpen.Annotations as A

        part = self._work_part()
        symbols = A.LineWeldDataBuilder.WeldSymbolType
        builder = part.Annotations.Welds.CreateLineWeldBuilder(A.LineWeld.Null)
        try:
            for name in SIDES[side]:
                data = builder.ArrowSideWeldData if name == "arrow" else builder.OtherSideWeldData
                data.WeldSymbol = getattr(symbols, WELDS[weld])
                data.WeldSize = number_text(value)
            anchor = self._dn_leader(builder.Leader, geometry, target_view, sheet)
            self._dn_origin(builder.Origin, sheet, point)
            obj = self._dn_commit(builder, "weld symbol")
        finally:
            builder.Destroy()
        self._update_model()
        reader = part.Annotations.Welds.CreateLineWeldBuilder(obj)
        try:
            stored = {
                name: {
                    "symbol": enum_name(symbols, data.WeldSymbol, [*WELDS.values(), "None"]),
                    "size": str(data.WeldSize),
                }
                for name, data in (
                    ("arrow", reader.ArrowSideWeldData),
                    ("other", reader.OtherSideWeldData),
                )
            }
            self._dn_check_leader(reader, geometry, target_view)
        finally:
            reader.Destroy()
        wanted = {
            name: {
                "symbol": WELDS[weld] if name in SIDES[side] else "None",
                "size": number_text(value) if name in SIDES[side] else "",
            }
            for name in ("arrow", "other")
        }
        if stored != wanted:
            raise NXToolError(
                "NX_VERIFICATION_FAILED",
                "Native weld symbol differs from the request",
                details={"actual": stored, "requested": wanted},
            )
        return self._dn_result(
            obj,
            sheet,
            "Weld symbol",
            point,
            weld=weld,
            side=side,
            size=value,
            native=stored,
            leader=self._dn_leader_info(geometry, target_view, anchor),
        )
