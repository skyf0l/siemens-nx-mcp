"""Datum features, sketches on faces/datums and circular feature patterns.

Every handler reads back what NX built and raises NX_VERIFICATION_FAILED when
it differs from the request, so the operation's undo mark rolls it back.
"""

from __future__ import annotations

import math

from nx_mcp.runtime import NXToolError
from nx_mcp.visual_tools import unit_normal

PLANAR_FACE = 22  # UF face type for planes


def _vec(value, name):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise NXToolError("NX_INVALID_ARGUMENT", f"{name} must contain three numbers")
    result = [float(v) for v in value]
    if not all(math.isfinite(v) for v in result):
        raise NXToolError("NX_INVALID_ARGUMENT", f"{name} must be finite")
    return result


def _sub(a, b):
    return [x - y for x, y in zip(a, b, strict=True)]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _norm(a):
    return math.sqrt(_dot(a, a))


def _tolerance(*points):
    scale = max([1.0] + [abs(c) for p in points for c in p])
    return 1e-6 * scale


def plane_through(points):
    """Unit normal of the plane through three points; rejects collinear points."""
    if not isinstance(points, (list, tuple)) or len(points) != 3:
        raise NXToolError("NX_INVALID_ARGUMENT", "three_points needs exactly three points")
    a, b, c = (_vec(p, f"points[{i}]") for i, p in enumerate(points))
    normal = _cross(_sub(b, a), _sub(c, a))
    if _norm(normal) <= _tolerance(a, b, c) * max(_norm(_sub(b, a)), _norm(_sub(c, a)), 1.0):
        raise NXToolError("NX_INVALID_ARGUMENT", "The three points are collinear")
    return [v / _norm(normal) for v in normal]


def parallel(a, b, tol=1e-6):
    """True when unit vectors a and b are parallel or antiparallel."""
    return _norm(_cross(a, b)) <= tol


def same_direction(a, b, tol=1e-6):
    """True when unit vectors a and b are parallel and point the same way."""
    return parallel(a, b, tol) and _dot(a, b) > 0


def on_plane(point, origin, normal, tol):
    return abs(_dot(_sub(point, origin), normal)) <= tol


def orthonormal_frame(x_axis, y_axis):
    x, y = _vec(x_axis, "x_axis"), _vec(y_axis, "y_axis")
    if abs(_norm(x) - 1) > 1e-8 or abs(_norm(y) - 1) > 1e-8 or abs(_dot(x, y)) > 1e-8:
        raise NXToolError("NX_INVALID_ARGUMENT", "x_axis and y_axis must be orthonormal")
    return x, y, _cross(x, y)


class DatumFeaturesMixin:
    # ---- helpers --------------------------------------------------------
    def _df_point(self, value):
        return self._work_part().Points.CreatePoint(self.nxopen.Point3d(*value))

    def _df_direction(self, vector, origin=(0.0, 0.0, 0.0)):
        return self._work_part().Directions.CreateDirection(
            self.nxopen.Point3d(*map(float, origin)),
            self.nxopen.Vector3d(*vector),
            self.nxopen.SmartObject.UpdateOption.WithinModeling,
        )

    def _df_commit(self, builder):
        commit = getattr(builder, "CommitFeature", None) or builder.Commit
        return commit()

    def _df_name(self, feature, name):
        if name:
            feature.SetName(str(name))

    def _df_plane_of(self, obj):
        """(origin, unit normal) of a planar face or datum plane."""
        if hasattr(obj, "Normal") and hasattr(obj, "Origin"):
            return _xyz(obj.Origin), unit_normal(_xyz(obj.Normal))
        import NXOpen.UF

        data = NXOpen.UF.UFSession.GetUFSession().Modeling.AskFaceData(obj.Tag)
        if data[0] != PLANAR_FACE:
            raise NXToolError("NX_NON_PLANAR", "Select a planar face or a datum plane")
        return [float(v) for v in data[1]], unit_normal(list(data[2]))

    def _df_planar_target(self, ref):
        obj = self._resolve(ref, {"face", "datum"})
        if getattr(obj, "IsOccurrence", False) or obj.OwningPart != self._work_part():
            raise NXToolError(
                "NX_OBJECT_OWNER_MISMATCH", "Select a face or datum plane owned by the work part"
            )
        return obj, *self._df_plane_of(obj)

    def _df_verify(self, ok, message, **actual):
        if not ok:
            raise NXToolError("NX_VERIFICATION_FAILED", message, details={"actual": actual})

    def _df_result(self, feature, kind, obj, label, **extra):
        part = self._work_part()
        result = {"feature": self._reference(feature, "feature", part, "Feature")}
        if obj is not None:
            result[kind] = self._reference(obj, "datum", part, label)
        result.update(extra, units=self._units(), coordinate_frame="work_part")
        return result

    # ---- datum plane ----------------------------------------------------
    def _datum_plane(
        self,
        method,
        reference=None,
        distance=None,
        origin=None,
        normal=None,
        points=None,
        flip=False,
        name=None,
    ):
        part = self._work_part()
        if method == "offset":
            if reference is None or distance is None or not math.isfinite(float(distance)):
                raise NXToolError(
                    "NX_INVALID_ARGUMENT", "offset needs a planar reference and a finite distance"
                )
            target, ref_origin, ref_normal = self._df_planar_target(reference)
            # NX offsets along the reference normal; a negative value (or flip) goes the
            # other way. Plane.SetFlip only reverses the result normal, so it is not used.
            signed = -float(distance) if flip else float(distance)
            geometry, plane_method = [target], "Distance"
        elif method == "point_normal":
            if origin is None or normal is None:
                raise NXToolError("NX_INVALID_ARGUMENT", "point_normal needs origin and normal")
            o, n = _vec(origin, "origin"), unit_normal(normal)
            geometry, plane_method = None, "PointDir"
        elif method == "three_points":
            n = plane_through(points)
            pts = [_vec(p, "point") for p in points]
            geometry, plane_method = None, "ThreePoints"
        else:
            raise NXToolError(
                "NX_INVALID_ARGUMENT", "method must be offset, point_normal or three_points"
            )
        # Arguments are valid; only now create NX smart objects and builders.
        if method == "point_normal":
            geometry = [self._df_point(o), self._df_direction(n, o)]
        elif method == "three_points":
            geometry = [self._df_point(p) for p in pts]
        builder = part.Features.CreateDatumPlaneBuilder(self.nxopen.Features.Feature.Null)
        try:
            plane = builder.GetPlane()
            plane.SetUpdateOption(self.nxopen.SmartObject.UpdateOption.WithinModeling)
            plane.SetMethod(getattr(self.nxopen.PlaneTypes.MethodType, plane_method))
            plane.SetGeometry(geometry)
            if method == "offset":
                plane.Expression.RightHandSide = repr(signed)
            plane.Evaluate()
            feature = self._df_commit(builder)
        finally:
            builder.Destroy()
        datum = feature.DatumPlane
        self._df_name(feature, name)
        actual_origin, actual_normal = _xyz(datum.Origin), unit_normal(_xyz(datum.Normal))
        if method == "offset":
            gap = _dot(_sub(actual_origin, ref_origin), ref_normal)
            ok = same_direction(actual_normal, ref_normal) and abs(gap - signed) <= _tolerance(
                actual_origin, ref_origin, [signed] * 3
            )
        elif method == "point_normal":
            ok = same_direction(actual_normal, n) and on_plane(
                o, actual_origin, actual_normal, _tolerance(o, actual_origin)
            )
        else:
            ok = same_direction(actual_normal, n) and all(
                on_plane(p, actual_origin, actual_normal, _tolerance(p, actual_origin)) for p in pts
            )
        self._df_verify(
            ok,
            "NX datum plane differs from the request",
            origin=actual_origin,
            normal=actual_normal,
        )
        return self._df_result(
            feature,
            "datum_plane",
            datum,
            "Datum plane",
            origin=actual_origin,
            normal=actual_normal,
            method=method,
        )

    # ---- datum axis -----------------------------------------------------
    def _datum_axis(self, method, origin=None, direction=None, start=None, end=None, name=None):
        part = self._work_part()
        if method == "point_direction":
            if origin is None or direction is None:
                raise NXToolError(
                    "NX_INVALID_ARGUMENT", "point_direction needs origin and direction"
                )
            o, d = _vec(origin, "origin"), unit_normal(direction, "direction")
        elif method == "two_points":
            if start is None or end is None:
                raise NXToolError("NX_INVALID_ARGUMENT", "two_points needs start and end")
            o, e = _vec(start, "start"), _vec(end, "end")
            if _norm(_sub(e, o)) <= _tolerance(o, e):
                raise NXToolError("NX_INVALID_ARGUMENT", "start and end must differ")
            d = unit_normal(_sub(e, o), "end - start")
        else:
            raise NXToolError("NX_INVALID_ARGUMENT", "method must be point_direction or two_points")
        types = self.nxopen.Features.DatumAxisBuilder.Types
        builder = part.Features.CreateDatumAxisBuilder(self.nxopen.Features.Feature.Null)
        try:
            if method == "point_direction":
                builder.Type = types.PointAndDir
                builder.Point = self._df_point(o)
                builder.Vector = self._df_direction(d, o)
            else:
                builder.Type = types.TwoPoints
                builder.Point1 = self._df_point(o)
                builder.Point2 = self._df_point(e)
            feature = self._df_commit(builder)
        finally:
            builder.Destroy()
        axis = feature.DatumAxis
        self._df_name(feature, name)
        actual_origin, actual_direction = _xyz(axis.Origin), unit_normal(_xyz(axis.Direction))
        on_line = _norm(_cross(_sub(actual_origin, o), d)) <= _tolerance(actual_origin, o)
        self._df_verify(
            same_direction(actual_direction, d) and on_line,
            "NX datum axis differs from the request",
            origin=actual_origin,
            direction=actual_direction,
        )
        return self._df_result(
            feature,
            "datum_axis",
            axis,
            "Datum axis",
            origin=actual_origin,
            direction=actual_direction,
            method=method,
        )

    # ---- datum csys -----------------------------------------------------
    def _datum_csys(self, origin, x_axis, y_axis, name=None):
        o = _vec(origin, "origin")
        x, y, z = orthonormal_frame(x_axis, y_axis)
        part = self._work_part()
        update = self.nxopen.SmartObject.UpdateOption.WithinModeling
        xform = part.Xforms.CreateXform(
            self.nxopen.Point3d(*o),
            self.nxopen.Vector3d(*x),
            self.nxopen.Vector3d(*y),
            update,
            1.0,
        )
        csys = part.CoordinateSystems.CreateCoordinateSystem(xform, update)
        builder = part.Features.CreateDatumCsysBuilder(self.nxopen.Features.Feature.Null)
        try:
            builder.Csys = csys
            feature = self._df_commit(builder)
        finally:
            builder.Destroy()
        self._df_name(feature, name)
        frames = [
            e for e in feature.GetEntities() if hasattr(e, "Orientation") and hasattr(e, "Origin")
        ]
        self._df_verify(bool(frames), "NX datum CSYS has no coordinate system")
        m = frames[0].Orientation.Element
        actual = {
            "origin": _xyz(frames[0].Origin),
            "x_axis": [m.Xx, m.Xy, m.Xz],
            "y_axis": [m.Yx, m.Yy, m.Yz],
            "z_axis": [m.Zx, m.Zy, m.Zz],
        }
        tol = _tolerance(o, actual["origin"])
        ok = _norm(_sub(actual["origin"], o)) <= tol and all(
            _norm(_sub(actual[k], v)) <= 1e-6
            for k, v in (("x_axis", x), ("y_axis", y), ("z_axis", z))
        )
        self._df_verify(ok, "NX datum CSYS differs from the request", **actual)
        return self._df_result(feature, "csys", frames[0], "Datum CSYS", **actual)

    # ---- point ----------------------------------------------------------
    def _datum_point(self, position, name=None):
        p = _vec(position, "position")
        part = self._work_part()
        builder = part.BaseFeatures.CreatePointFeatureBuilder(self.nxopen.Features.Feature.Null)
        try:
            builder.Point = self._df_point(p)
            feature = self._df_commit(builder)
        finally:
            builder.Destroy()
        self._df_name(feature, name)
        points = [e for e in feature.GetEntities() if hasattr(e, "Coordinates")]
        self._df_verify(bool(points), "NX point feature has no point")
        actual = _xyz(points[0].Coordinates)
        self._df_verify(
            _norm(_sub(actual, p)) <= _tolerance(p, actual),
            "NX point differs from the request",
            position=actual,
        )
        return self._df_result(feature, "point", None, "Point", position=actual)

    # ---- sketch on face or datum plane ----------------------------------
    def _create_sketch_on(self, target, name=None, x_direction=None):
        if self.session.ActiveSketch is not None:
            raise NXToolError("NX_SKETCH_ACTIVE", "Finish the active sketch first")
        obj, origin, normal = self._df_planar_target(target)
        part = self._work_part()
        x = None
        if x_direction is not None:
            x = unit_normal(x_direction, "x_direction")
            if abs(_dot(x, normal)) > 1e-6:
                raise NXToolError("NX_INVALID_ARGUMENT", "x_direction must lie in the plane")
        nxopen = self.nxopen
        builder = part.Sketches.CreateSketchInPlaceBuilder2(nxopen.Sketch.Null)
        try:
            if x is None:
                # Selecting the face/plane directly keeps the sketch associative; NX picks
                # the horizontal axis and projects the absolute origin onto the plane.
                builder.PlaneOption = nxopen.Sketch.PlaneOption.Inferred
                builder.PlaneOrFace.Value = obj
            else:
                # NX 2406 ignores AxisReference in Inferred mode. A coincident smart plane
                # with an explicit axis and origin stays associative to the target.
                update = nxopen.SmartObject.UpdateOption.WithinModeling
                plane = part.Planes.CreatePlane(
                    nxopen.Point3d(*origin), nxopen.Vector3d(*normal), update
                )
                plane.SetMethod(nxopen.PlaneTypes.MethodType.Coincident)
                plane.SetGeometry([obj])
                plane.Evaluate()
                builder.PlaneOption = nxopen.Sketch.PlaneOption.NewPlane
                builder.PlaneReference = plane
                builder.AxisReference = self._df_direction(x, origin)
                builder.OriginOption = nxopen.OriginMethod.SpecifyPoint
                builder.SketchOrigin = self._df_point(origin)
            sketch = builder.Commit()
        finally:
            builder.Destroy()
        if name:
            sketch.SetName(str(name))
        frame = self._sketch_frame(sketch)
        tol = _tolerance(origin, frame["origin"])
        ok = parallel(unit_normal(frame["normal"]), normal) and on_plane(
            frame["origin"], origin, normal, tol
        )
        if x is not None:
            ok = ok and _norm(_sub(unit_normal(frame["x_axis"]), x)) <= 1e-6
        self._df_verify(ok, "NX sketch plane differs from the target plane", **frame)
        sketch.Activate(self.nxopen.Sketch.ViewReorient.FalseValue)
        return {
            "object": self._reference(sketch, "sketch", part, "Sketch"),
            "frame": frame,
            "target": self._reference(
                obj, "face" if not hasattr(obj, "Normal") else "datum", part, "Target"
            ),
            "associative": True,
            "message": "Created sketch on the target plane",
        }

    # ---- circular feature pattern ---------------------------------------
    def _circular_pattern(
        self, features, count, angle, axis=None, axis_origin=None, axis_direction=None
    ):
        if type(count) is not int or not 2 <= count <= 360:
            raise NXToolError("NX_INVALID_ARGUMENT", "count must be 2-360 total instances")
        if not isinstance(angle, (int, float)) or not math.isfinite(angle) or angle <= 0:
            raise NXToolError("NX_INVALID_ARGUMENT", "angle must be a positive pitch in degrees")
        if (count - 1) * float(angle) > 360.0 - 1e-9:  # The last instance would land on the seed.
            raise NXToolError(
                "NX_INVALID_ARGUMENT", "Instances would overlap: (count - 1) * angle must be < 360"
            )
        if (axis is None) == (axis_origin is None and axis_direction is None):
            raise NXToolError(
                "NX_INVALID_ARGUMENT", "Give either a datum axis ID or axis_origin + axis_direction"
            )
        datum = None
        if axis is not None:
            datum = self._resolve(axis, {"datum"})
            if not hasattr(datum, "Direction"):
                raise NXToolError("NX_OBJECT_TYPE_MISMATCH", "axis must be a datum axis")
            o, d = _xyz(datum.Origin), unit_normal(_xyz(datum.Direction))
        else:
            if axis_origin is None or axis_direction is None:
                raise NXToolError(
                    "NX_INVALID_ARGUMENT", "axis_origin and axis_direction are both required"
                )
            o, d = _vec(axis_origin, "axis_origin"), unit_normal(axis_direction, "axis_direction")
        seeds = [self._resolve(f, {"feature"}) for f in features or []]
        if not seeds:
            raise NXToolError("NX_INVALID_ARGUMENT", "At least one feature is required")
        import NXOpen.GeometricUtilities

        part = self._work_part()
        builder = part.Features.CreatePatternFeatureBuilder(self.nxopen.Features.Feature.Null)
        try:
            builder.FeatureList.Add(seeds)
            builder.PatternMethod = (
                self.nxopen.Features.PatternFeatureBuilder.PatternMethodOptions.Simple
            )
            builder.PatternService.PatternType = (
                NXOpen.GeometricUtilities.PatternDefinition.PatternEnum.Circular
            )
            definition = builder.PatternService.CircularDefinition
            update = self.nxopen.SmartObject.UpdateOption.WithinModeling
            if datum is not None:
                # Direction derived from the datum axis keeps the pattern associative to it.
                direction = part.Directions.CreateDirection(
                    datum, self.nxopen.Sense.Forward, update
                )
                definition.RotationAxis = part.Axes.CreateAxis(self._df_point(o), direction, update)
            else:
                definition.RotationAxis = part.Axes.CreateAxis(
                    self._df_point(o), self._df_direction(d, o), update
                )
            spacing = definition.AngularSpacing
            spacing.SpaceType = NXOpen.GeometricUtilities.PatternSpacing.SpacingType.Offset
            spacing.NCopies.RightHandSide = str(count)
            spacing.PitchAngle.RightHandSide = repr(float(angle))
            definition.RadialSpacing.NCopies.RightHandSide = "1"
            feature = builder.CommitFeature()
        finally:
            builder.Destroy()
        return {
            "feature": self._reference(feature, "feature", part, "Pattern"),
            "feature_type": feature.FeatureType,
            "count": count,
            "angle_deg": float(angle),
            "axis_origin": o,
            "axis_direction": d,
            "units": self._units(),
            "coordinate_frame": "work_part",
        }


def _xyz(p):
    return [float(p.X), float(p.Y), float(p.Z)]
