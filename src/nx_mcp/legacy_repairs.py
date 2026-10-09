"""Typed-reference replacements for legacy NX 2606 entry points."""

from __future__ import annotations

import math

from nx_mcp.runtime import NXToolError


class LegacyRepairsMixin:
    def _delete_feature(self, name):
        feature = self._resolve(name, {"feature"})
        ref = self._reference(feature, "feature", self._work_part(), "Feature")
        manager = self.session.UpdateManager
        self._require_api(manager, "AddToDeleteList", "DoUpdate")
        manager.AddToDeleteList(feature)
        self._update_model()
        return {"deleted": [ref]}

    def _angle_direction(self, obj):
        if isinstance(obj, self.nxopen.Line):
            a, b = obj.StartPoint, obj.EndPoint
            return [b.X - a.X, b.Y - a.Y, b.Z - a.Z], "line_start_to_end"
        if isinstance(obj, self.nxopen.Edge):
            if obj.SolidEdgeType != self.nxopen.Edge.EdgeType.Linear:
                raise NXToolError("NX_INVALID_ARGUMENT", "Angle requires a straight edge")
            a, b = obj.GetVertices()
            return [b.X - a.X, b.Y - a.Y, b.Z - a.Z], "edge_vertex_order"
        if isinstance(obj, self.nxopen.Face):
            uf = self.nxopen.UF.UFSession.GetUFSession()
            code, _, direction, _, _, _, sign = uf.Modeling.AskFaceData(obj.Tag)
            if code != 22:
                raise NXToolError("NX_INVALID_ARGUMENT", "Angle requires a planar face")
            return [float(x) * sign for x in direction], "outward_face_normal"
        raise NXToolError("NX_OBJECT_TYPE_MISMATCH", "Use lines, straight edges or planar faces")

    def _measure_angle(self, obj1, obj2):
        objects = [self._resolve(r, {"curve", "edge", "face"}) for r in (obj1, obj2)]
        vectors, conventions = zip(*(self._angle_direction(o) for o in objects), strict=True)
        lengths = [math.sqrt(sum(x * x for x in v)) for v in vectors]
        if any(n <= 1e-12 for n in lengths):
            raise NXToolError("NX_INVALID_ARGUMENT", "Cannot measure a degenerate direction")
        dot = sum(a * b for a, b in zip(*vectors, strict=True)) / math.prod(lengths)
        angle = math.degrees(math.acos(max(-1.0, min(1.0, dot))))
        return {
            "angle_deg": angle,
            "units": "deg",
            "coordinate_frame": "work_part",
            "range": [0, 180],
            "direction_conventions": list(conventions),
            "resolved_references": [
                self._reference(
                    o,
                    "face"
                    if isinstance(o, self.nxopen.Face)
                    else "edge"
                    if isinstance(o, self.nxopen.Edge)
                    else "curve",
                    self._work_part(),
                    "Angle target",
                )
                for o in objects
            ],
        }
