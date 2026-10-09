"""Drawing inspection and precise native documentation edits."""

from typing import Annotated

from nx_mcp.schema_types import Field

Point3 = Annotated[list[float], Field(min_length=3, max_length=3)]
Point2 = Annotated[list[float], Field(min_length=2, max_length=2)]

READ_ONLY = {"nx_list_drawings", "nx_list_annotations"}
NON_MODEL: set[str] = set()


def nx_list_drawings():
    """List work-part drawing sheets and views with typed IDs, dimensions, scale and active-sheet state. No activation or mutation."""


def nx_list_annotations(offset: int = 0, limit: int = 100):
    """Enumerate native notes, PMI, BOMs, balloons, explosion traces, center marks, centerlines, surface finish and weld symbols in the work part. Returns subtype, typed references, position/text where available, and managed-refresh metadata. Paged, maximum 200 items per call."""


READ_ONLY.add("nx_list_dimensions")


def nx_list_dimensions():
    """List actual computed native dimension values, native retention and measurement_valid flags, typed IDs and annotation origins in the work part. Includes drafting and PMI dimensions, identified by native subtype. Values/origins use native work-part units; inspect view association separately. Read-only, no regeneration."""
