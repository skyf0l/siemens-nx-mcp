"""NX-side handlers: Native hole features on faces: simple, counterbore, countersink, threaded.

Conventions: datum_features.py is the reference implementation. Validate every
argument before touching NXOpen; builders create -> configure -> commit ->
Destroy() in finally; read back what NX built and raise NX_VERIFICATION_FAILED
on mismatch so the operation's undo mark rolls it back.
"""

from __future__ import annotations

import math

from nx_mcp.runtime import NXToolError

FORMS = ("simple", "counterbore", "countersink", "threaded")
MAX_POSITIONS = 100
PLANAR_FACE = 22  # UF face type for planes


def _number(value, name, positive=False, allow_none=False):
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise NXToolError("NX_INVALID_ARGUMENT", f"{name} must be a finite number")
    if positive and value <= 0:
        raise NXToolError("NX_INVALID_ARGUMENT", f"{name} must be positive")
    return float(value)


def _angle(value, name):
    value = _number(value, name)
    if not 0 < value < 180:
        raise NXToolError("NX_INVALID_ARGUMENT", f"{name} must be between 0 and 180 degrees")
    return value


def _sub(a, b):
    return [x - y for x, y in zip(a, b, strict=True)]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def _norm(a):
    return math.sqrt(_dot(a, a))


def tip_height(diameter, tip_angle):
    """Height of the drill-tip cone of a hole of this diameter."""
    return diameter / 2 / math.tan(math.radians(tip_angle) / 2)


def countersink_height(diameter, countersink_diameter, countersink_angle):
    return (countersink_diameter - diameter) / 2 / math.tan(math.radians(countersink_angle) / 2)


def hole_volume(spec):
    """Analytic volume of one blind hole (cylinders, countersink frustum, drill tip)."""
    r = spec["diameter"] / 2
    depth = spec["depth"]
    volume = math.pi * r * r * tip_height(spec["diameter"], spec["tip_angle"]) / 3
    if spec["form"] == "counterbore":
        big = spec["counterbore_diameter"] / 2
        h = spec["counterbore_depth"]
        return volume + math.pi * big * big * h + math.pi * r * r * (depth - h)
    if spec["form"] == "countersink":
        big = spec["countersink_diameter"] / 2
        h = countersink_height(
            spec["diameter"], spec["countersink_diameter"], spec["countersink_angle"]
        )
        frustum = math.pi * h / 3 * (big * big + big * r + r * r)
        return volume + frustum + math.pi * r * r * (depth - h)
    return volume + math.pi * r * r * depth


def hole_spec(
    form,
    diameter,
    depth=None,
    through_all=False,
    tip_angle=118.0,
    counterbore_diameter=None,
    counterbore_depth=None,
    countersink_diameter=None,
    countersink_angle=90.0,
    thread_standard=None,
    thread_size=None,
    thread_depth=None,
):
    """Validate the hole shape arguments (no NX needed) and return a normalized spec."""
    if form not in FORMS:
        raise NXToolError("NX_INVALID_ARGUMENT", "form must be one of " + ", ".join(FORMS))
    if not isinstance(through_all, bool):
        raise NXToolError("NX_INVALID_ARGUMENT", "through_all must be true or false")
    spec = {
        "form": form,
        "diameter": _number(diameter, "diameter", positive=True),
        "through_all": through_all,
        "tip_angle": _angle(tip_angle, "tip_angle"),
        "depth": _number(depth, "depth", positive=True, allow_none=True),
    }
    if through_all == (spec["depth"] is not None):
        raise NXToolError("NX_INVALID_ARGUMENT", "Give either depth or through_all=true")
    unused = {
        "simple": ("counterbore", "countersink", "thread"),
        "counterbore": ("countersink", "thread"),
        "countersink": ("counterbore", "thread"),
        "threaded": ("counterbore", "countersink"),
    }[form]
    given = {
        "counterbore": counterbore_diameter is not None or counterbore_depth is not None,
        "countersink": countersink_diameter is not None,
        "thread": any(v is not None for v in (thread_standard, thread_size, thread_depth)),
    }
    for group in unused:
        if given[group]:
            raise NXToolError(
                "NX_INVALID_ARGUMENT", f"{group} arguments do not apply to a {form} hole"
            )
    if form == "counterbore":
        if counterbore_diameter is None or counterbore_depth is None:
            raise NXToolError(
                "NX_INVALID_ARGUMENT",
                "counterbore needs counterbore_diameter and counterbore_depth",
            )
        spec["counterbore_diameter"] = _number(counterbore_diameter, "counterbore_diameter", True)
        spec["counterbore_depth"] = _number(counterbore_depth, "counterbore_depth", True)
        if spec["counterbore_diameter"] <= spec["diameter"]:
            raise NXToolError(
                "NX_INVALID_ARGUMENT", "counterbore_diameter must be larger than diameter"
            )
        if spec["depth"] is not None and spec["counterbore_depth"] >= spec["depth"]:
            raise NXToolError("NX_INVALID_ARGUMENT", "counterbore_depth must be less than depth")
    elif form == "countersink":
        if countersink_diameter is None:
            raise NXToolError("NX_INVALID_ARGUMENT", "countersink needs countersink_diameter")
        spec["countersink_diameter"] = _number(countersink_diameter, "countersink_diameter", True)
        spec["countersink_angle"] = _angle(countersink_angle, "countersink_angle")
        if spec["countersink_diameter"] <= spec["diameter"]:
            raise NXToolError(
                "NX_INVALID_ARGUMENT", "countersink_diameter must be larger than diameter"
            )
        h = countersink_height(
            spec["diameter"], spec["countersink_diameter"], spec["countersink_angle"]
        )
        if spec["depth"] is not None and h >= spec["depth"]:
            raise NXToolError(
                "NX_INVALID_ARGUMENT",
                f"The countersink cone ({h:.6g} deep) must be shallower than depth",
            )
    elif form == "threaded":
        for name, value in (("thread_standard", thread_standard), ("thread_size", thread_size)):
            if not isinstance(value, str) or not value.strip():
                raise NXToolError("NX_INVALID_ARGUMENT", f"threaded holes need {name}")
        spec["thread_standard"], spec["thread_size"] = thread_standard, thread_size
        spec["thread_depth"] = _number(thread_depth, "thread_depth", True, allow_none=True)
        if (
            spec["thread_depth"] is not None
            and spec["depth"] is not None
            and spec["thread_depth"] > spec["depth"] * (1 + 1e-9)
        ):
            raise NXToolError("NX_INVALID_ARGUMENT", "thread_depth may not exceed depth")
    return spec


def hole_positions(positions):
    """1-100 distinct finite [x, y, z] points."""
    if not isinstance(positions, (list, tuple)) or not 1 <= len(positions) <= MAX_POSITIONS:
        raise NXToolError("NX_INVALID_ARGUMENT", f"positions must hold 1-{MAX_POSITIONS} points")
    points = []
    for i, p in enumerate(positions):
        if not isinstance(p, (list, tuple)) or len(p) != 3:
            raise NXToolError("NX_INVALID_ARGUMENT", f"positions[{i}] must be [x, y, z]")
        points.append([_number(v, f"positions[{i}]") for v in p])
    scale = max([1.0] + [abs(c) for p in points for c in p])
    for i, a in enumerate(points):
        for j in range(i):
            if _norm(_sub(a, points[j])) <= 1e-6 * scale:
                raise NXToolError(
                    "NX_INVALID_ARGUMENT", f"positions[{i}] duplicates positions[{j}]"
                )
    return points


def plane_tolerance(points, origin):
    return 1e-4 * max([1.0, *(abs(c) for p in [*points, origin] for c in p)])


def off_plane(points, origin, normal, tol):
    """Indices and signed distances of points farther than tol from the plane."""
    result = []
    for i, p in enumerate(points):
        distance = _dot(_sub(p, origin), normal)
        if abs(distance) > tol:
            result.append((i, distance))
    return result


def match_thread_row(rows, standard, size, diameter, units):
    """The one thread-table row for standard/size whose tap drill equals diameter."""
    if not any(r.get("Standard") == standard for r in rows):
        raise NXToolError(
            "NX_NOT_FOUND",
            f"Thread standard {standard!r} is not in the installed NX thread table",
            details={"hint": "list standards with nx_thread_catalog"},
        )
    candidates = [r for r in rows if r.get("Standard") == standard and r.get("Size") == size]
    if not candidates:
        raise NXToolError(
            "NX_NOT_FOUND",
            f"Thread size {size!r} is not in standard {standard!r}",
            details={"hint": "list sizes with nx_thread_catalog(standard=...)"},
        )
    unit = candidates[0].get("Unit")
    if (unit == "Metric") != (units == "mm"):
        raise NXToolError(
            "NX_UNSUPPORTED_ARGUMENT",
            f"Thread standard {standard!r} is {unit}; the work part is in {units}",
        )
    drills = sorted({float(r["TapDrillDia"]) for r in candidates if r.get("TapDrillDia")})
    tol = 1e-6 * max(1.0, diameter)
    matches = [
        r
        for r in candidates
        if r.get("TapDrillDia")
        and abs(float(r["TapDrillDia"]) - diameter) <= tol
        and r.get("RadialEngage") != "Custom"
    ]
    if not matches:
        raise NXToolError(
            "NX_INVALID_ARGUMENT",
            f"diameter must be a catalog tap-drill diameter of {size}: {drills}",
            details={"tap_drill_diameters": drills},
        )
    return matches[0]


def matches_nominal(removed, nominal_total):
    """Whether the measured removal equals the analytic total (None for through holes)."""
    if nominal_total is None:
        return None
    return abs(removed - nominal_total) <= 1e-6 * max(1.0, abs(nominal_total))


def _xyz(p):
    return [float(p.X), float(p.Y), float(p.Z)]


def _close(a, b):
    return abs(float(a) - float(b)) <= 1e-6 * max(1.0, abs(float(b)))


class HolesMixin:
    # ---- placement ------------------------------------------------------
    def _holes_placement(self, face, target_body):
        """(face, body, plane origin, outward unit normal, UF modeling) of a planar face."""
        import NXOpen.UF

        from nx_mcp.visual_tools import unit_normal

        placement = self._engineering_owned(face, "face")
        modeling = NXOpen.UF.UFSession.GetUFSession().Modeling
        data = modeling.AskFaceData(placement.Tag)
        if data[0] != PLANAR_FACE:
            raise NXToolError("NX_NON_PLANAR", "The placement face must be planar")
        u0, u1, v0, v1 = modeling.AskFaceUvMinmax(placement.Tag)
        props = modeling.AskFaceProps(placement.Tag, [(u0 + u1) / 2, (v0 + v1) / 2])
        outward = unit_normal(list(props[5]))
        body = placement.GetBody()
        if target_body is not None:
            target = self._engineering_owned(target_body, "body")
            if int(target.Tag) != int(body.Tag):
                raise NXToolError(
                    "NX_INVALID_ARGUMENT", "target_body must be the body that owns the face"
                )
        if not body.IsSolidBody:
            raise NXToolError("NX_OBJECT_TYPE_MISMATCH", "The face must belong to a solid body")
        return placement, body, [float(v) for v in data[1]], outward, modeling

    def _holes_on_face(self, modeling, placement, points, origin, normal):
        """Project points within tolerance onto the plane; require them inside the face."""
        tol = plane_tolerance(points, origin)
        far = off_plane(points, origin, normal, tol)
        if far:
            index, distance = far[0]
            raise NXToolError(
                "NX_INVALID_ARGUMENT",
                f"positions[{index}] is {distance:.6g} off the placement face plane",
                details={
                    "off_plane": [{"index": i, "distance": d} for i, d in far],
                    "tolerance": tol,
                    "plane_origin": origin,
                    "plane_normal": normal,
                },
            )
        projected = [
            [c - _dot(_sub(p, origin), normal) * n for c, n in zip(p, normal, strict=True)]
            for p in points
        ]
        outside = []
        for i, p in enumerate(projected):
            state = modeling.AskPointContainment([float(c) for c in p], placement.Tag)
            if state != 1:  # 1 inside, 2 outside, 3 on the boundary
                outside.append({"index": i, "position": p, "on_boundary": state == 3})
        if outside:
            first = outside[0]["index"]
            raise NXToolError(
                "NX_INVALID_ARGUMENT",
                f"positions[{first}] is not inside the placement face",
                details={"outside": outside},
            )
        return projected

    # ---- builder configuration -----------------------------------------
    def _holes_configure(self, b, spec, row):
        import NXOpen.Features as F

        B = F.HolePackageBuilder
        expr = []
        if spec["form"] == "threaded":
            b.Type = B.Types.ThreadedHole
            b.ThreadStandard = row["Standard"]
            b.ThreadSize = row["Size"]
            b.RadialEngageOption = row["RadialEngage"]
            # Setting the size does not refresh the drill from the table; set it.
            expr.append((b.TapDrillDiameter, float(row["TapDrillDia"])))
            b.ThreadedStartChamferEnabled = False
            b.ThreadedEndChamferEnabled = False
            b.ThreadedReliefEnabled = False
            b.RelateHoleDepthToThreadDepth = False
            b.ThreadRotation = B.ThreadRotationOptions.Right
            expr.append((b.ThreadedTipAngle, spec["tip_angle"]))
            if spec["through_all"] and spec["thread_depth"] is None:
                b.ThreadLengthOption = B.ThreadLengthOptions.Full
            else:
                b.ThreadLengthOption = B.ThreadLengthOptions.Custom
                expr.append((b.ThreadDepth, spec["thread_depth"] or spec["depth"]))
            if not spec["through_all"]:
                expr.append((b.ThreadedHoleDepth, spec["depth"]))
        else:
            b.Type = B.Types.GeneralHole
            expr.append((b.GeneralTipAngle, spec["tip_angle"]))
            if spec["form"] == "simple":
                b.GeneralHoleForm = B.HoleForms.Simple
                expr.append((b.GeneralSimpleHoleDiameter, spec["diameter"]))
                depth = b.GeneralSimpleHoleDepth
            elif spec["form"] == "counterbore":
                b.GeneralHoleForm = B.HoleForms.Counterbored
                expr.append((b.GeneralCounterboreDiameter, spec["counterbore_diameter"]))
                expr.append((b.GeneralCounterboreDepth, spec["counterbore_depth"]))
                expr.append((b.GeneralCounterboreHoleDiameter, spec["diameter"]))
                depth = b.GeneralCounterboreHoleDepth
            else:
                b.GeneralHoleForm = B.HoleForms.Countersink
                expr.append((b.GeneralCountersinkDiameter, spec["countersink_diameter"]))
                expr.append((b.GeneralCountersinkAngle, spec["countersink_angle"]))
                expr.append((b.GeneralCountersinkHoleDiameter, spec["diameter"]))
                depth = b.GeneralCountersinkHoleDepth
            if not spec["through_all"]:
                expr.append((depth, spec["depth"]))
        b.HoleDepthLimitOption = (
            B.HoleDepthLimitOptions.ThroughBody
            if spec["through_all"]
            else B.HoleDepthLimitOptions.Value
        )
        b.DepthOption = B.HoleDepthOptions.ToCylinderBottom
        for expression, value in expr:
            expression.RightHandSide = repr(float(value))

    def _holes_read_back(self, feature, form):
        """Parameters NX stored in the committed hole feature (edit builder, not committed)."""
        import NXOpen.Features as F

        B = F.HolePackageBuilder
        b = self._work_part().Features.CreateHolePackageBuilder(feature)
        try:
            through = b.HoleDepthLimitOption == B.HoleDepthLimitOptions.ThroughBody
            if form == "threaded":
                full = b.ThreadLengthOption == B.ThreadLengthOptions.Full
                actual = {
                    "form": "threaded" if b.Type == B.Types.ThreadedHole else str(b.Type),
                    "diameter": b.TapDrillDiameter.Value,
                    "depth": None if through else b.ThreadedHoleDepth.Value,
                    "tip_angle": b.ThreadedTipAngle.Value,
                    "thread_standard": b.ThreadStandard,
                    "thread_size": b.ThreadSize,
                    "radial_engage": b.RadialEngageOption,
                    "thread_depth": None if full else b.ThreadDepth.Value,
                    "thread_length": "full" if full else "value",
                }
            else:
                forms = (
                    (B.HoleForms.Simple, "simple"),
                    (B.HoleForms.Counterbored, "counterbore"),
                    (B.HoleForms.Countersink, "countersink"),
                )
                name = next((n for v, n in forms if b.GeneralHoleForm == v), "other")
                if b.Type != B.Types.GeneralHole:
                    name = str(b.Type)
                diameter, depth = {
                    "counterbore": (
                        b.GeneralCounterboreHoleDiameter,
                        b.GeneralCounterboreHoleDepth,
                    ),
                    "countersink": (
                        b.GeneralCountersinkHoleDiameter,
                        b.GeneralCountersinkHoleDepth,
                    ),
                }.get(name, (b.GeneralSimpleHoleDiameter, b.GeneralSimpleHoleDepth))
                actual = {
                    "form": name,
                    "diameter": diameter.Value,
                    "depth": None if through else depth.Value,
                    "tip_angle": b.GeneralTipAngle.Value,
                }
                if name == "counterbore":
                    actual["counterbore_diameter"] = b.GeneralCounterboreDiameter.Value
                    actual["counterbore_depth"] = b.GeneralCounterboreDepth.Value
                if name == "countersink":
                    actual["countersink_diameter"] = b.GeneralCountersinkDiameter.Value
                    actual["countersink_angle"] = b.GeneralCountersinkAngle.Value
            actual["through_all"] = through
            actual["depth_measured_to"] = (
                "cylinder_bottom"
                if b.DepthOption == B.HoleDepthOptions.ToCylinderBottom
                else "cone_tip"
            )
            return actual
        finally:
            b.Destroy()

    def _holes_volume(self, body):
        import NXOpen.UF

        modeling = NXOpen.UF.UFSession.GetUFSession().Modeling
        mm = self._units() == "mm"
        mass = modeling.AskMassProps3d(
            [body.Tag], 1, 1, 4 if mm else 1, 0.0, 1, [0.999] + [0.0] * 10
        )
        return float(mass[0][1]) * (1e9 if mm else 1.0)

    # ---- tool -----------------------------------------------------------
    def _hole_feature(
        self,
        face,
        positions,
        form,
        diameter,
        depth=None,
        through_all=False,
        tip_angle=118.0,
        counterbore_diameter=None,
        counterbore_depth=None,
        countersink_diameter=None,
        countersink_angle=90.0,
        thread_standard=None,
        thread_size=None,
        thread_depth=None,
        target_body=None,
    ):
        spec = hole_spec(
            form,
            diameter,
            depth,
            through_all,
            tip_angle,
            counterbore_diameter,
            counterbore_depth,
            countersink_diameter,
            countersink_angle,
            thread_standard,
            thread_size,
            thread_depth,
        )
        points = hole_positions(positions)
        placement, body, origin, outward, modeling = self._holes_placement(face, target_body)
        centres = self._holes_on_face(modeling, placement, points, origin, outward)
        row = None
        if form == "threaded":
            row = match_thread_row(
                self._thread_rows(), thread_standard, thread_size, spec["diameter"], self._units()
            )
        drill = [-v for v in outward]
        # Arguments are valid; only now create NX smart objects and the builder.
        import NXOpen.Features as F
        import NXOpen.GeometricUtilities as G

        nx = self.nxopen
        part = self._work_part()
        before = self._holes_volume(body)
        b = part.Features.CreateHolePackageBuilder(F.HolePackage.Null)
        try:
            b.Tolerance = 0.01 if self._units() == "mm" else 0.0005
            self._holes_configure(b, spec, row)
            if row is not None and not _close(b.TapDrillDiameter.Value, spec["diameter"]):
                raise NXToolError(
                    "NX_VERIFICATION_FAILED",
                    "NX chose a different tap-drill diameter for this thread",
                    details={"actual": b.TapDrillDiameter.Value, "requested": spec["diameter"]},
                )
            b.BooleanOperation.Type = G.BooleanOperation.BooleanType.Subtract
            b.BooleanOperation.SetTargetBodies([body])
            smart = [part.Points.CreatePoint(nx.Point3d(*p)) for p in centres]
            section = b.HolePosition
            section.SetAllowedEntityTypes(nx.Section.AllowTypes.OnlyPoints)
            rule = part.ScRuleFactory.CreateRuleCurveDumbFromPoints(smart)
            section.AddToSection(
                [rule],
                smart[0],
                nx.NXObject.Null,
                nx.NXObject.Null,
                nx.Point3d(*centres[0]),
                nx.Section.Mode.Create,
                False,
            )
            if not b.Validate():
                raise NXToolError("NX_INVALID_GEOMETRY", "NX rejected the hole inputs")
            feature = b.CommitFeature()
        finally:
            b.Destroy()
        removed = before - self._holes_volume(body)
        actual = self._holes_read_back(feature, form)
        origins = [_xyz(o) for o in feature.GetOrigins()]
        directions = [_xyz(d) for d in feature.GetDirections()]
        self._holes_verify(spec, row, actual, centres, origins, directions, drill, removed, origin)
        result = self._engineering_result(feature, [body])
        nominal = None if spec["through_all"] else hole_volume(spec)
        result.update(
            feature_type=feature.FeatureType,
            native_feature="NXOpen.Features.HolePackage (Hole command)",
            form=form,
            parameters=actual,
            hole_count=len(origins),
            positions=origins,
            direction=drill,
            placement_face=self._reference(placement, "face", part, "Face"),
            removed_volume=removed,
            nominal_volume_per_hole=nominal,
            nominal_volume_total=None if nominal is None else nominal * len(origins),
            removed_matches_nominal=matches_nominal(
                removed, None if nominal is None else nominal * len(origins)
            ),
            volume_note=(
                "Nominal volumes assume each hole lies fully inside the material "
                "and overlaps no other hole; through holes have no nominal volume. "
                "removed_matches_nominal=false means a hole breaks out of the body "
                "or overlaps another cut."
            ),
        )
        if row is not None:
            result["thread"] = {
                "standard": actual["thread_standard"],
                "size": actual["thread_size"],
                "radial_engage": actual["radial_engage"],
                "tap_drill_diameter": actual["diameter"],
                "thread_depth": actual["thread_depth"],
                "thread_length": actual["thread_length"],
                "major_diameter": float(row["MajorDiameter"]),
                "minor_diameter": float(row["MinorDiameter"]),
                "pitch": float(row["Pitch"]),
                "angle": float(row["Angle"]),
                "callout": row.get("Callout"),
                "handedness": "right",
                "representation": "native hole thread data (symbolic, no helical geometry)",
            }
        return result

    def _holes_verify(self, spec, row, actual, centres, origins, directions, drill, removed, at):
        tol = plane_tolerance(centres, at)
        unmatched = [p for p in centres if not any(_norm(_sub(p, o)) <= tol for o in origins)]
        wrong_way = [d for d in directions if _dot(d, drill) < 1 - 1e-6]
        keys = (
            "diameter",
            "depth",
            "tip_angle",
            "counterbore_diameter",
            "counterbore_depth",
            "countersink_diameter",
            "countersink_angle",
            "thread_depth",
        )
        mismatch = {
            k: {"actual": actual.get(k), "requested": spec[k]}
            for k in keys
            if spec.get(k) is not None
            and actual.get(k) is not None
            and not _close(actual[k], spec[k])
        }
        if spec["through_all"] != actual["through_all"]:
            mismatch["through_all"] = {
                "actual": actual["through_all"],
                "requested": spec["through_all"],
            }
        if row is not None and (
            actual["thread_standard"] != row["Standard"] or actual["thread_size"] != row["Size"]
        ):
            mismatch["thread"] = {
                "actual": [actual["thread_standard"], actual["thread_size"]],
                "requested": [row["Standard"], row["Size"]],
            }
        if (
            len(origins) != len(centres)
            or unmatched
            or wrong_way
            or mismatch
            or actual["form"] != spec["form"]
            or removed <= 0
        ):
            raise NXToolError(
                "NX_VERIFICATION_FAILED",
                "NX hole feature differs from the request",
                details={
                    "actual": actual,
                    "mismatch": mismatch,
                    "origins": origins,
                    "directions": directions,
                    "unmatched_positions": unmatched,
                    "removed_volume": removed,
                },
            )
