"""Explicit reference-set membership and datum display controls for NX 2606."""


class ReferenceGeometryMixin:
    def _datum_objects(self):
        part = self._work_part()
        values = list(part.Datums) + list(part.CoordinateSystems)
        return list({int(x.Tag): x for x in values}.values())

    def _list_datums(self):
        values = self._datum_objects()
        return {
            "datums": [
                {
                    "object": self._reference(x, "datum", self._work_part(), "Datum"),
                    "native_type": type(x).__name__,
                    "blanked": bool(x.IsBlanked),
                }
                for x in values
            ],
            "count": len(values),
            "scope": "work_part",
        }
