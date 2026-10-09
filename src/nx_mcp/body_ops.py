"""NX-side handlers: Body operations: asymmetric chamfer, variable-radius blend, mirror about a datum or face, split/trim/scale body.

Conventions: datum_features.py is the reference implementation. Validate every
argument before touching NXOpen; builders create -> configure -> commit ->
Destroy() in finally; read back what NX built and raise NX_VERIFICATION_FAILED
on mismatch so the operation's undo mark rolls it back.
"""

from __future__ import annotations

import math

from nx_mcp.runtime import NXToolError

PLANAR_FACE = 22  # UF face type for planes


def _bad(message):
    return NXToolError("NX_INVALID_ARGUMENT", message)


def _number(value, name, positive=False):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or (positive and value <= 0)
    ):
        raise _bad(name + " must be finite" + (" and positive" if positive else ""))
    return float(value)


def _vec(value, name):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise _bad(f"{name} must contain three numbers")
    return [_number(v, name) for v in value]


def _ids(values, name):
    if not isinstance(values, (list, tuple)) or not values:
        raise _bad(f"{name} must be a non-empty list of object IDs")
    if not all(isinstance(v, str) and v for v in values):
        raise _bad(f"{name} must contain object ID strings")
    if len(set(values)) != len(values):
        raise _bad(f"{name} contains duplicates")
    return list(values)


def _id(value, name):
    if not isinstance(value, str) or not value:
        raise _bad(f"{name} must be an object ID")
    return value


def chamfer_offsets(method, offset1, offset2=None, angle=None):
    """Validate a chamfer request; returns (offset1, offset2, angle) as floats/None.

    offset2 is the second-face offset NX will produce (for offset_angle it is
    offset1 * tan(angle), used for volume expectations)."""
    o1 = _number(offset1, "offset1", True)
    if method == "symmetric":
        if offset2 is not None or angle is not None:
            raise _bad("symmetric takes only offset1 (no offset2 or angle)")
        return o1, o1, None
    if method == "two_offsets":
        if angle is not None:
            raise _bad("two_offsets takes offset1 and offset2, not angle")
        if offset2 is None:
            raise _bad("two_offsets needs offset2")
        return o1, _number(offset2, "offset2", True), None
    if method == "offset_angle":
        if offset2 is not None:
            raise _bad("offset_angle takes offset1 and angle, not offset2")
        if angle is None:
            raise _bad("offset_angle needs angle")
        a = _number(angle, "angle")
        if not 0.0 < a < 90.0:
            raise _bad("angle must be between 0 and 90 degrees (exclusive)")
        return o1, o1 * math.tan(math.radians(a)), a
    raise _bad("method must be symmetric, two_offsets or offset_angle")


def blend_points(points):
    """Validate variable-radius points; returns [(parameter, radius)] sorted by parameter."""
    if not isinstance(points, (list, tuple)) or not 2 <= len(points) <= 20:
        raise _bad("points must contain 2-20 {parameter, radius} items")
    result = []
    for i, item in enumerate(points):
        if not isinstance(item, dict) or set(item) != {"parameter", "radius"}:
            raise _bad(f"points[{i}] must be exactly {{parameter, radius}}")
        p = _number(item["parameter"], f"points[{i}].parameter")
        if not 0.0 <= p <= 1.0:
            raise _bad(f"points[{i}].parameter must be within 0..1")
        result.append((p, _number(item["radius"], f"points[{i}].radius", True)))
    result.sort()
    if any(b[0] - a[0] < 1e-6 for a, b in zip(result, result[1:], strict=False)):
        raise _bad("point parameters must be distinct")
    return result


def scale_factor(factor):
    f = _number(factor, "factor", True)
    if abs(f - 1.0) < 1e-12:
        raise _bad("factor 1 would not change the body")
    return f


def _xyz(p):
    return [float(p.X), float(p.Y), float(p.Z)]


def _unit(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def plane_frame(normal):
    """Orthonormal (x, y, z) rows with z = normal (unit)."""
    z = _unit(normal)
    helper = [1.0, 0.0, 0.0] if abs(z[0]) < 0.9 else [0.0, 1.0, 0.0]
    x = _unit(_cross(helper, z))
    return x, _cross(z, x), z


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def _sub(a, b):
    return [x - y for x, y in zip(a, b, strict=True)]


def plane_box_range(box_min, box_max, origin, normal):
    """(min, max) signed distance of an axis-aligned box's 8 corners to a plane."""
    n = _unit(normal)
    d = [
        _dot(_sub([x, y, z], origin), n)
        for x in (box_min[0], box_max[0])
        for y in (box_min[1], box_max[1])
        for z in (box_min[2], box_max[2])
    ]
    return min(d), max(d)


def plane_misses_box(box_min, box_max, origin, normal):
    """True when the plane cannot cut the box (all corners on one side or on it)."""
    lo, hi = plane_box_range(box_min, box_max, origin, normal)
    tol = 1e-6 * max(1.0, *(abs(v) for v in (*box_min, *box_max)))
    return hi <= tol or lo >= -tol


def _close(a, b, rel=1e-6):
    return abs(a - b) <= rel * max(1.0, abs(a), abs(b))


class BodyOpsMixin:
    # ---- helpers --------------------------------------------------------
    def _bo_verify(self, ok, message, **actual):
        if not ok:
            raise NXToolError("NX_VERIFICATION_FAILED", message, details={"actual": actual})

    def _bo_solid(self, ref, name="body"):
        body = self._engineering_owned(_id(ref, name), "body")
        if not body.IsSolidBody:
            raise NXToolError("NX_NOT_SOLID", f"{name} must be a solid body")
        return body

    def _bo_volume(self, body):
        return self._bo_mass(body)[0]

    def _bo_mass(self, body):
        """(volume, centroid) of a solid body."""
        part = self._work_part()
        units = [
            part.UnitCollection.FindObject(n)
            for n in ["SquareMilliMeter", "CubicMilliMeter", "Kilogram", "MilliMeter", "Newton"]
        ]
        props = part.MeasureManager.NewMassProperties(units, 0.999, [body])
        try:
            props.InformationUnit = self.nxopen.MeasureBodies.AnalysisUnit.KilogramMillimeter
            return float(props.Volume), _xyz(props.Centroid)
        finally:
            props.Dispose()

    def _bo_box(self, body):
        import NXOpen.UF

        box = list(NXOpen.UF.UFSession.GetUFSession().ModlGeneral.AskBoundingBox(body.Tag))
        return [float(v) for v in box[:3]], [float(v) for v in box[3:6]]

    def _bo_plane(self, ref, name):
        """(object, origin, outward unit normal, is_datum) of a datum plane or planar face."""
        obj = self._resolve(_id(ref, name), {"face", "datum"})
        if getattr(obj, "IsOccurrence", False) or obj.OwningPart != self._work_part():
            raise NXToolError("NX_OBJECT_OWNER_MISMATCH", f"{name} must be owned by the work part")
        if hasattr(obj, "Normal") and hasattr(obj, "Origin"):
            return obj, _xyz(obj.Origin), _unit(_xyz(obj.Normal)), True
        if not hasattr(obj, "GetBody"):
            raise NXToolError("NX_OBJECT_TYPE_MISMATCH", f"{name} must be a datum plane or face")
        import NXOpen.UF

        data = NXOpen.UF.UFSession.GetUFSession().Modeling.AskFaceData(obj.Tag)
        if data[0] != PLANAR_FACE:
            raise NXToolError("NX_NON_PLANAR", f"{name} must be a planar face or a datum plane")
        # AskFaceData's direction is already the outward face normal.
        return obj, [float(v) for v in data[1]], _unit(list(data[2])), False

    def _bo_fixed_datum(self, origin, normal):
        x, y, z = plane_frame(normal)
        matrix = self.nxopen.Matrix3x3()
        for row, prefix in zip((x, y, z), "XYZ", strict=True):
            for value, suffix in zip(row, "xyz", strict=True):
                setattr(matrix, prefix + suffix, value)
        return self._work_part().Datums.CreateFixedDatumPlane(self.nxopen.Point3d(*origin), matrix)

    def _bo_tool_rule(self, tool, is_datum):
        factory = self._work_part().ScRuleFactory
        if is_datum:
            return factory.CreateRuleFaceDatum([tool])
        return factory.CreateRuleFaceDumb([tool])

    def _bo_result(self, feature, bodies, **extra):
        part = self._work_part()
        refs = [self._reference(b, "body", part, "Body") for b in bodies]
        ref = self._reference(feature, "feature", part, "Feature")
        out = {
            "feature": ref,
            "feature_type": str(getattr(feature, "FeatureType", "")),
            "bodies": refs,
            "body_count": len(refs),
            "body": refs[0] if refs else None,
            "created": [ref],
        }
        out.update(extra, units=self._units(), coordinate_frame="work_part")
        return out

    # ---- chamfer --------------------------------------------------------
    def _chamfer_advanced(self, edges, method, offset1, offset2=None, angle=None, reverse=False):
        refs = _ids(edges, "edges")
        o1, o2, a = chamfer_offsets(method, offset1, offset2, angle)
        if not isinstance(reverse, bool):
            raise _bad("reverse must be a boolean")
        if reverse and method == "symmetric":
            raise _bad("reverse applies only to two_offsets and offset_angle")
        selected = [self._engineering_owned(r, "edge") for r in refs]
        if len({int(e.GetBody().Tag) for e in selected}) != 1:
            raise _bad("Select edges of one owned body")
        target = selected[0].GetBody()
        before = self._bo_volume(target)
        options = self.nxopen.Features.ChamferBuilder.ChamferOption
        b = self._work_part().Features.CreateChamferBuilder(None)
        try:
            b.Tolerance = 0.001 if self._units() == "mm" else 0.001 / 25.4
            b.SmartCollector = self._engineering_collector(selected, "Edge")
            if method == "symmetric":
                b.Option = options.SymmetricOffsets
            elif method == "two_offsets":
                b.Option = options.TwoOffsets
                b.SecondOffsetExp.RightHandSide = repr(o2)
            else:
                b.Option = options.OffsetAndAngle
                b.AngleExp.RightHandSide = repr(a)
            b.FirstOffsetExp.RightHandSide = repr(o1)
            b.ReverseOffsets = bool(reverse)
            feature = b.CommitFeature()
        finally:
            b.Destroy()
        after = self._bo_volume(target)
        self._bo_verify(after < before, "Chamfer removed no material", volume=after)
        return self._bo_result(
            feature,
            [target],
            method=method,
            offset1=o1,
            offset2=o2 if method != "offset_angle" else None,
            angle_deg=a,
            second_offset_effective=o2,
            reverse=reverse,
            volume_before=before,
            volume_after=after,
            volume_removed=before - after,
            modified=[self._reference(target, "body", self._work_part(), "Body")],
        )

    # ---- variable blend -------------------------------------------------
    def _variable_blend(self, edge, points):
        pts = blend_points(points)
        target_edge = self._engineering_owned(_id(edge, "edge"), "edge")
        body = target_edge.GetBody()
        before = self._bo_volume(body)
        part = self._work_part()
        update = self.nxopen.SmartObject.UpdateOption.WithinModeling
        b = part.Features.CreateEdgeBlendBuilder(None)
        placed = []
        try:
            b.Tolerance = 0.001 if self._units() == "mm" else 0.001 / 25.4
            collector = self._engineering_collector([target_edge], "Edge")
            b.AddChainset(collector, repr(pts[0][1]))
            for parameter, radius in pts:
                scalar = part.Scalars.CreateScalar(
                    parameter * 100.0, self.nxopen.Scalar.DimensionalityType.NotSet, update
                )
                point = part.Points.CreatePoint(
                    target_edge,
                    scalar,
                    self.nxopen.PointCollection.PointOnCurveLocationOption.PercentArcLength,
                    self.nxopen.Point.Null,
                    update,
                )
                b.AddVariableRadiusDataNew(
                    target_edge, repr(parameter * 100.0), repr(radius), point, False, False
                )
                placed.append((parameter, radius, point))
            feature = b.CommitFeature()
        finally:
            b.Destroy()
        after = self._bo_volume(body)
        self._bo_verify(after != before, "Variable blend did not change the body", volume=after)
        return self._bo_result(
            feature,
            [body],
            points=[
                {"parameter": p, "radius": r, "position": _xyz(pt.Coordinates)}
                for p, r, pt in placed
            ],
            volume_before=before,
            volume_after=after,
            modified=[self._reference(body, "body", part, "Body")],
        )

    # ---- mirror ---------------------------------------------------------
    def _mirror_body_about(self, body, plane, keep_original=True):
        _id(body, "body")
        _id(plane, "plane")
        if not isinstance(keep_original, bool):
            raise _bad("keep_original must be a boolean")
        target = self._bo_solid(body)
        obj, origin, normal, is_datum = self._bo_plane(plane, "plane")
        volume = self._bo_volume(target)
        datum = obj if is_datum else self._bo_fixed_datum(origin, normal)
        part = self._work_part()
        b = part.Features.CreateMirrorBodyBuilder(None)
        try:
            b.MirrorBodyCollector.ReplaceRules(
                self._engineering_collector([target], "Body").GetRules(), False
            )
            b.Plane.Value = datum
            feature = b.CommitFeature()
        finally:
            b.Destroy()
        bodies = list(feature.GetBodies())
        if not keep_original:
            # Mirror Body has no delete-source option and stays associative to
            # the source, so the source is hidden rather than removed.
            self.session.DisplayManager.BlankObjects([target])
        self._bo_verify(bool(bodies), "Mirror created no body")
        volumes = [self._bo_volume(x) for x in bodies]
        self._bo_verify(
            all(_close(v, volume) for v in volumes),
            "Mirrored body volume differs from the source",
            source=volume,
            mirrored=volumes,
        )
        return self._bo_result(
            feature,
            bodies,
            plane_origin=origin,
            plane_normal=normal,
            plane_kind="datum_plane" if is_datum else "planar_face",
            keep_original=keep_original,
            source_hidden=not keep_original,
            source_volume=volume,
        )

    # ---- split / trim ---------------------------------------------------
    def _bo_set_tool(self, builder, tool, is_datum, origin, normal):
        import NXOpen.GeometricUtilities

        boolean_tool = builder.BooleanTool
        types = NXOpen.GeometricUtilities.BooleanToolBuilder.BooleanToolType
        if is_datum:
            boolean_tool.ToolOption = types.FaceOrPlane
            boolean_tool.FacePlaneTool.ToolFaces.FaceCollector.ReplaceRules(
                [self._bo_tool_rule(tool, is_datum)], False
            )
        else:
            # NX does not extend a selected face, so cut with an associative
            # plane coincident with the face (infinite, follows the face).
            boolean_tool.ToolOption = types.NewPlane
            plane = self._work_part().Planes.CreatePlane(
                self.nxopen.Point3d(*origin),
                self.nxopen.Vector3d(*normal),
                self.nxopen.SmartObject.UpdateOption.WithinModeling,
            )
            plane.SetMethod(self.nxopen.PlaneTypes.MethodType.Coincident)
            plane.SetGeometry([tool])
            plane.Evaluate()
            boolean_tool.FacePlaneTool.ToolPlane = plane
        return boolean_tool

    def _bo_cut_args(self, body, tool, verb):
        _id(body, "body")
        _id(tool, "tool")
        if body == tool:
            raise _bad("body and tool must differ")
        target = self._bo_solid(body)
        obj, origin, normal, is_datum = self._bo_plane(tool, "tool")
        if not is_datum and int(obj.GetBody().Tag) == int(target.Tag):
            raise _bad(f"tool face must not belong to the body being {verb}")
        # Reject a plane that cannot cut the body before NX fails on it: a failed
        # native commit leaves its error message on the next feature created.
        box_min, box_max = self._bo_box(target)
        if plane_misses_box(box_min, box_max, origin, normal):
            lo, hi = plane_box_range(box_min, box_max, origin, normal)
            raise NXToolError(
                "NX_INVALID_ARGUMENT",
                f"The tool plane does not cut the body: the whole body lies on one side of it "
                f"(signed distances {lo:g}..{hi:g} along the plane normal)",
                details={"plane_origin": origin, "plane_normal": normal},
            )
        return target, obj, origin, normal, is_datum

    def _split_body(self, body, tool):
        target, obj, origin, normal, is_datum = self._bo_cut_args(body, tool, "split")
        volume = self._bo_volume(target)
        part = self._work_part()
        b = part.Features.CreateSplitBodyBuilder(None)
        try:
            b.TargetBodyCollector = self._engineering_collector([target], "Body")
            self._bo_set_tool(b, obj, is_datum, origin, normal)
            feature = b.CommitFeature()
        finally:
            b.Destroy()
        bodies = list(feature.GetBodies())
        volumes = [self._bo_volume(x) for x in bodies]
        self._bo_verify(
            len(bodies) >= 2 and _close(sum(volumes), volume, 1e-5),
            "The tool does not split the body into pieces of the original volume",
            body_count=len(bodies),
            volumes=volumes,
            original=volume,
        )
        return self._bo_result(
            feature,
            bodies,
            volumes=volumes,
            original_volume=volume,
            plane_origin=origin,
            plane_normal=normal,
        )

    def _trim_body(self, body, tool, reverse=False):
        if not isinstance(reverse, bool):
            raise _bad("reverse must be a boolean")
        target, obj, origin, normal, is_datum = self._bo_cut_args(body, tool, "trimmed")
        volume = self._bo_volume(target)
        part = self._work_part()
        b = part.Features.CreateTrimBody2Builder(None)
        try:
            b.TargetBodyCollector = self._engineering_collector([target], "Body")
            boolean_tool = self._bo_set_tool(b, obj, is_datum, origin, normal)
            boolean_tool.ReverseDirection = bool(reverse)
            feature = b.CommitFeature()
        finally:
            b.Destroy()
        bodies = list(feature.GetBodies()) or [target]
        masses = [self._bo_mass(x) for x in bodies]
        after = sum(m[0] for m in masses)
        self._bo_verify(
            0 < after < volume * (1 - 1e-9),
            "The tool does not trim the body",
            volume=after,
            original=volume,
        )
        sides = [_dot(_sub(c, origin), normal) for _, c in masses]
        self._bo_verify(
            all((side > 0) == reverse for side in sides),
            "NX kept the other side of the tool than requested",
            centroid_offsets_along_normal=sides,
        )
        return self._bo_result(
            feature,
            bodies,
            original_volume=volume,
            volume_after=after,
            plane_origin=origin,
            plane_normal=normal,
            kept_side="along_normal" if reverse else "against_normal",
            modified=[self._reference(target, "body", part, "Body")],
        )

    # ---- scale ----------------------------------------------------------
    def _scale_body(self, bodies, factor, origin=None):
        refs = _ids(bodies, "bodies")
        f = scale_factor(factor)
        o = _vec(origin if origin is not None else [0.0, 0.0, 0.0], "origin")
        targets = [self._bo_solid(r, "bodies") for r in refs]
        before = [self._bo_volume(t) for t in targets]
        part = self._work_part()
        b = part.Features.CreateScaleBuilder(None)
        try:
            b.Type = self.nxopen.Features.ScaleBuilder.Types.Uniform
            b.BodyToScale.Add(targets)
            b.Point = part.Points.CreatePoint(self.nxopen.Point3d(*o))
            b.UniformFactor.RightHandSide = repr(f)
            feature = b.CommitFeature()
        finally:
            b.Destroy()
        after = [self._bo_volume(t) for t in targets]
        self._bo_verify(
            all(_close(a, v * f**3, 1e-5) for a, v in zip(after, before, strict=True)),
            "Scaled volume is not factor^3 times the original",
            before=before,
            after=after,
        )
        return self._bo_result(
            feature,
            targets,
            factor=f,
            origin=o,
            volumes_before=before,
            volumes_after=after,
            modified=[self._reference(t, "body", part, "Body") for t in targets],
        )
