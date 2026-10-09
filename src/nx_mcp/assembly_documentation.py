"""Native parts lists, associative callouts and explosion documentation."""

from __future__ import annotations


class AssemblyDocumentationMixin:
    @staticmethod
    def _documentation_annotations(part):
        values = [*getattr(part, "Notes", []), *getattr(part, "Labels", [])]
        annotations = getattr(part, "Annotations", None)
        if annotations is not None:
            for name in [
                "Datums",
                "Fcfs",
                "IdSymbols",
                "PartsLists",
                "BendTables",
                "TableSections",
                "Tables",
                "Centerlines",
                "DraftingSurfaceFinishSymbol",
                "Welds",
            ]:
                try:
                    values.extend(getattr(annotations, name, None) or [])
                except Exception:  # A collection this NX build cannot enumerate.
                    continue
        values.extend(getattr(getattr(part, "DraftingManager", None), "TitleBlocks", []))
        return list({int(obj.Tag): obj for obj in values}.values())
