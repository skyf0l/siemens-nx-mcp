"""Native bend tables and their transactional refresh.

Managed sheet-metal callouts refresh through the sheet-metal domain's ``@refresher``.
"""

from nx_mcp.runtime import NXToolError


class AnnotationUpdatesMixin:
    def _table_cells(self, obj):
        import NXOpen.UF as U

        tab = U.UFSession.GetUFSession().Tabnot
        columns = [tab.AskNthColumn(obj.Tag, i) for i in range(tab.AskNmColumns(obj.Tag))]
        return [
            [
                tab.AskEvaluatedCellText(tab.AskCellAtRowCol(tab.AskNthRow(obj.Tag, i), c))
                for c in columns
            ]
            for i in range(tab.AskNmRows(obj.Tag))
        ]

    def _refresh_annotations(self):
        part = self._work_part()
        self._refresh_detail_boundaries()
        updated = []
        tables = getattr(getattr(part, "Annotations", None), "BendTables", None)
        for table in list(tables or []):
            builder = tables.CreateBendTableBuilder(table)
            try:
                if not builder.Style.BendTable.AutomaticUpdate:
                    continue
                before = self._table_cells(table)
                if not builder.Validate():
                    raise NXToolError(
                        "NX_ANNOTATION_INVALID", "An automatic bend table no longer validates"
                    )
                obj = builder.Commit()
                if obj is None:
                    raise NXToolError("NX_VERIFICATION_FAILED", "NX returned no updated bend table")
                if self._table_cells(obj) != before:
                    updated.append(self._reference(obj, "annotation", part, "Bend table"))
            finally:
                builder.Destroy()
        return {
            "updated": updated,
            "updated_count": len(updated),
            "units": self._units(),
            "semantics": "Automatic bend tables are rebuilt through their native builder in the same transaction.",
        }
