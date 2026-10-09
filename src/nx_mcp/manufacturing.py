"""Native thread and geometric manufacturing inspection operations."""

from __future__ import annotations

from nx_mcp.legacy_kit import finite
from nx_mcp.runtime import NXToolError

# Principal radii at or beyond this many part units mean a straight (flat) direction.
FLAT_RADIUS = 1e8


class ManufacturingMixin:
    def _face_samples(self, faces, samples_per_axis):
        import NXOpen.UF as U

        if type(samples_per_axis) is not int or not 1 <= samples_per_axis <= 20:
            raise NXToolError("NX_INVALID_ARGUMENT", "samples_per_axis must be 1..20")
        if len(faces) * samples_per_axis**2 > 10000:
            raise NXToolError(
                "NX_SAMPLE_LIMIT", "Select fewer faces or lower sample density (maximum 10000)"
            )
        uf = U.UFSession.GetUFSession().Modeling
        samples = []
        skipped = 0
        for face in faces:
            bounds = uf.AskFaceUvMinmax(face.Tag)
            for i in range(samples_per_axis):
                for j in range(samples_per_axis):
                    uv = [
                        bounds[0] + (bounds[1] - bounds[0]) * (i + 0.5) / samples_per_axis,
                        bounds[2] + (bounds[3] - bounds[2]) * (j + 0.5) / samples_per_axis,
                    ]
                    data = uf.AskFaceProps(face.Tag, uv)
                    if uf.AskPointContainment(data[0], face.Tag) == 2:
                        skipped += 1
                        continue
                    samples.append((face, uv, data))
        return samples, skipped

    def _face_analysis(self, faces, samples_per_axis=3, pull_direction=None, minimum_draft=1.0):
        import math

        from nx_mcp.legacy_kit import unit_normal

        objects = self._freeform_refs(faces, "face")
        direction = unit_normal(pull_direction) if pull_direction is not None else None
        minimum = finite(minimum_draft, "minimum_draft")
        if not 0 <= minimum < 90 or (direction is None and minimum_draft != 1.0):
            raise NXToolError(
                "NX_INVALID_ARGUMENT",
                "minimum_draft requires a pull direction and must be 0..<90 degrees",
            )
        samples, skipped = self._face_samples(objects, samples_per_axis)
        reports = []
        for face, uv, data in samples:
            point, _, _, _, _, normal, radii = data
            # NX reports a straight direction as a huge radius: about 1e30 on v2606,
            # 1e10 inch (3.937e8 mm) on v2406. Any radius past 1e8 part units is flat.
            curvature = [
                0.0 if abs(r) >= FLAT_RADIUS else 1 / r if abs(r) > 1e-12 else None for r in radii
            ]
            record = {
                "face": self._reference(face, "face", self._work_part(), "Face"),
                "uv": uv,
                "point": list(point),
                "normal": list(normal),
                "principal_radii": [None if abs(r) >= FLAT_RADIUS else r for r in radii],
                "principal_curvatures": curvature,
            }
            if direction is not None:
                cosine = sum(a * b for a, b in zip(normal, direction, strict=False))
                draft = math.degrees(math.asin(max(-1.0, min(1.0, cosine))))
                record.update(
                    {
                        "signed_draft_degrees": draft,
                        "classification": "negative"
                        if draft < -1e-7
                        else "below_minimum"
                        if draft < minimum
                        else "positive",
                    }
                )
            reports.append(record)
        return {
            "samples": reports,
            "sample_count": len(reports),
            "skipped_outside_trim": skipped,
            "units": self._units(),
            "curvature_units": "1/" + self._units(),
            "coordinate_frame": "work_part",
            "pull_direction": direction,
            "minimum_draft_degrees": minimum if direction is not None else None,
            "method": "native face derivatives at a trimmed UV grid; sampled values, not certified global extrema or mold-release feasibility",
        }

    def _wall_thickness(self, body, faces=None, samples_per_axis=3, tolerance=0.001):
        import math

        import NXOpen.UF as U

        target = self._engineering_owned(body, "body")
        if not target.IsSolidBody:
            raise NXToolError("NX_NOT_SOLID", "Wall thickness requires a solid body")
        tolerance = finite(tolerance, "tolerance", True)
        selected = (
            self._freeform_refs(faces, "face") if faces is not None else list(target.GetFaces())
        )
        if any(f.GetBody() != target for f in selected):
            raise NXToolError("NX_OBJECT_OWNER_MISMATCH", "Faces must belong to the selected body")
        samples, skipped = self._face_samples(selected, samples_per_axis)
        uf = U.UFSession.GetUFSession().Modeling
        face_map = {int(f.Tag): f for f in target.GetFaces()}
        identity = [1.0 if i % 5 == 0 else 0.0 for i in range(16)]
        reports = []
        for face, _uv, data in samples:
            point, normal = data[0], data[5]
            direction = [-v for v in normal]
            origin = [p + tolerance * d for p, d in zip(point, direction, strict=False)]
            if uf.AskPointContainment(origin, target.Tag) != 1:
                reports.append(
                    {
                        "source_face": self._reference(face, "face", self._work_part(), "Face"),
                        "point": list(point),
                        "status": "unresolved",
                        "reason": "Inward offset is not inside solid; decrease tolerance or sample away from boundaries",
                    }
                )
                continue
            _, hits = uf.TraceARay(1, [target.Tag], origin, direction, identity, 0)
            hit = next(
                (
                    h
                    for h in hits
                    if sum(
                        (a - b) * d for a, b, d in zip(h.HitPoint, origin, direction, strict=False)
                    )
                    > 0
                ),
                None,
            )
            record = {
                "source_face": self._reference(face, "face", self._work_part(), "Face"),
                "point": list(point),
                "status": "unresolved",
            }
            if hit is not None:
                distance = math.dist(point, hit.HitPoint)
                record.update(
                    {
                        "status": "measured",
                        "thickness": distance,
                        "opposite_point": list(hit.HitPoint),
                        "opposite_face": self._reference(
                            face_map[int(hit.HitFace)], "face", self._work_part(), "Face"
                        ),
                    }
                )
            reports.append(record)
        distances = [x["thickness"] for x in reports if x["status"] == "measured"]
        return {
            "body": self._reference(target, "body", self._work_part(), "Body"),
            "samples": reports,
            "sample_count": len(reports),
            "measured_count": len(distances),
            "skipped_outside_trim": skipped,
            "minimum_sampled_thickness": min(distances) if distances else None,
            "maximum_sampled_thickness": max(distances) if distances else None,
            "tolerance": tolerance,
            "units": self._units(),
            "coordinate_frame": "work_part",
            "method": "first exit along inward native face normal; sampled wall distance, not global minimum thickness or rolling-ball thickness",
        }
