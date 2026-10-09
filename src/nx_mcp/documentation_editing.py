"""Native drawing/annotation editing; NXOpen calls remain on the NX thread."""

from nx_mcp.authoring import page
from nx_mcp.kit.geom import xyz
from nx_mcp.legacy_kit import finite
from nx_mcp.release_engineering import drawing_work_view
from nx_mcp.runtime import NXToolError


class DocumentationEditingMixin:
    def _list_drawings(self):
        part = self._work_part()
        current = part.DrawingSheets.CurrentDrawingSheet
        return {
            "sheets": [
                {
                    "object": self._reference(s, "drawing_sheet", part, "Sheet"),
                    "name": s.Name,
                    "active": s == current,
                    "width": s.Length,
                    "height": s.Height,
                    "units": self._sheet_units(s),
                    "scale": list(s.GetScale()),
                    "views": [
                        {"object": self._reference(v, "drawing_view", part, "View"), "name": v.Name}
                        for v in s.GetDraftingViews()
                    ],
                }
                for s in part.DrawingSheets
            ],
            "units": "per_sheet",
            "coordinate_frame": "drawing_sheet",
            # An expanded member view shows the drawing without a current sheet.
            "modeling_active": current is None and not drawing_work_view(part),
        }

    def _list_annotations(self, offset=0, limit=100):
        part = self._work_part()
        values = [(x, "annotation") for x in self._documentation_annotations(part)] + [
            (x, "traceline") for x in part.Tracelines
        ]
        selected_page = page(values, offset, limit)
        items = []
        for value, kind in selected_page["items"]:
            item = {
                "object": self._reference(value, kind, part, "Annotation"),
                "native_type": type(value).__name__,
            }
            if hasattr(value, "HasUserAttribute") and value.HasUserAttribute(
                "NX_MCP_MEASURED_PMI_V1", self.nxopen.NXObject.AttributeType.String, -1
            ):
                item["managed_refresh"] = (
                    value.GetStringAttribute("NX_MCP_MEASURED_PMI_V1") != "disabled"
                )
            if hasattr(value, "IsRetained"):
                item["retained"] = bool(value.IsRetained)
            if hasattr(value, "AnnotationOrigin"):
                item["position"] = xyz(value.AnnotationOrigin)
            if hasattr(value, "GetText"):
                item["text"] = list(value.GetText())
            if hasattr(value, "HasUserAttribute") and value.HasUserAttribute(
                "NX_MCP_DRAWING_TABLE_V1", self.nxopen.NXObject.AttributeType.String, -1
            ):
                item["table_kind"] = value.GetStringAttribute("NX_MCP_DRAWING_TABLE_V1")
            if type(value).__name__ == "BendTable":
                item["rows"] = self._table_cells(value)
            if kind == "traceline":
                item.update(
                    start=xyz(value.StartPoint.Coordinates),
                    end=xyz(value.EndPoint.Coordinates),
                    start_offset=value.StartOffset,
                    end_offset=value.EndOffset,
                )
            items.append(item)
        return {
            "items": items,
            "total": len(values),
            "offset": offset,
            "next_offset": offset + limit if offset + limit < len(values) else None,
            "units": self._units(),
            "position_frame": "native annotation frame: sheet for drafting, work part for PMI; assembly for traces",
        }

    def _edit_explosion_trace(
        self, traceline, start_percent=None, end_percent=None, start_offset=None, end_offset=None
    ):
        import json

        line = self._resolve(traceline, {"traceline"})
        ex = line.AskExplosion()
        if all(x is None for x in [start_percent, end_percent, start_offset, end_offset]):
            raise NXToolError("NX_INVALID_ARGUMENT", "Supply an edit")
        if not line.HasUserAttribute(
            "NX_MCP_TRACE_V1", self.nxopen.NXObject.AttributeType.String, -1
        ):
            raise NXToolError("NX_UNSUPPORTED_EDIT", "Select a managed MCP trace")
        records = json.loads(line.GetStringAttribute("NX_MCP_TRACE_V1"))
        for record, value in zip(records, [start_percent, end_percent], strict=True):
            if value is not None:
                value = finite(value, "percent")
                if not 0 <= value <= 100:
                    raise NXToolError("NX_INVALID_ARGUMENT", "Percentage must be 0..100")
                record["percent"] = value
        offsets = [
            finite(x, "offset") if x is not None else None for x in [start_offset, end_offset]
        ]
        line.SetAttribute("NX_MCP_TRACE_V1", json.dumps(records))
        for attr, value in zip(["StartOffset", "EndOffset"], offsets, strict=True):
            if value is not None:
                setattr(line, attr, value)
        self._refresh_explosion_traces(ex)
        self._regenerate_explosion_display()
        return {
            "traceline": self._reference(line, "traceline", self._work_part(), "Trace"),
            "start": xyz(line.StartPoint.Coordinates),
            "end": xyz(line.EndPoint.Coordinates),
            "start_offset": line.StartOffset,
            "end_offset": line.EndOffset,
            "units": self._units(),
        }
