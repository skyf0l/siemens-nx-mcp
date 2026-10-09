"""Explicit reference-set membership and datum display controls for NX 2606."""


class ReferenceGeometryMixin:
    def _datum_objects(self):
        part = self._work_part()
        values = list(part.Datums) + list(part.CoordinateSystems)
        return list({int(x.Tag): x for x in values}.values())
