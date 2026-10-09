"""Native drawing/annotation editing; NXOpen calls remain on the NX thread."""

from nx_mcp.authoring import page
from nx_mcp.kit.geom import xyz
from nx_mcp.release_engineering import drawing_work_view


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
