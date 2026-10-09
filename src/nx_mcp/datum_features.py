"""Datum features, sketches on faces/datums and circular feature patterns.

Every handler reads back what NX built and raises NX_VERIFICATION_FAILED when
it differs from the request, so the operation's undo mark rolls it back.
"""

from __future__ import annotations

from nx_mcp.kit.geom import xyz as _xyz
from nx_mcp.legacy_kit import unit_normal
from nx_mcp.runtime import NXToolError

PLANAR_FACE = 22  # UF face type for planes


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

    def _df_plane_of(self, obj):
        """(origin, unit normal) of a planar face or datum plane."""
        if hasattr(obj, "Normal") and hasattr(obj, "Origin"):
            return _xyz(obj.Origin), unit_normal(_xyz(obj.Normal))
        if hasattr(obj, "Origin"):
            # Datum axes and coordinate systems share the "datum" kind but are not planes;
            # AskFaceData on them raises a raw NX error (820022) instead of a stable code.
            raise NXToolError(
                "NX_OBJECT_TYPE_MISMATCH", "Select a planar face or a datum plane, not an axis/CSYS"
            )
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

    # ---- datum plane ----------------------------------------------------

    # ---- datum axis -----------------------------------------------------

    # ---- datum csys -----------------------------------------------------

    # ---- point ----------------------------------------------------------

    # ---- sketch on face or datum plane ----------------------------------
    # ---- circular feature pattern ---------------------------------------
