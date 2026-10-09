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


def nx_edit_explosion_trace(
    traceline: str,
    start_percent: float | None = None,
    end_percent: float | None = None,
    start_offset: float | None = None,
    end_offset: float | None = None,
):
    """Edit a managed native explosion trace's anchored edge arc-length percentages (0..100) and native endpoint offsets in assembly units. Persistent edge/component anchors and the named explosion are retained. Recomputes endpoints and returns readback. Use nx_edit_annotation(delete=true) to remove a trace."""


READ_ONLY.update({"nx_geometry_anchor", "nx_resolve_geometry_anchor"})


def nx_geometry_anchor(object: str):
    """Capture a persistent native handle for an owned body/face/edge of the work part (the part needs a file path; save it before closing or the anchor cannot outlive the session). Anchor survives model edits and save/reopen while the native entity survives. Store the returned anchor rather than session-scoped object IDs. Occurrence geometry must be anchored in its prototype work part."""


def nx_resolve_geometry_anchor(anchor: dict):
    """Resolve an exact native geometry anchor into a current typed ID. Verifies owner path and object kind. Deleted or replaced topology explicitly returns NX_STALE_REFERENCE; never silently substitutes the nearest face."""


def nx_update_assembly_documentation():
    """Explicitly propagate loaded prototype changes in the active assembly. Solve existing mates, refresh managed explosion traces, native BOMs, measured annotations and drafting views. Returns constraint states, evaluated BOM rows, view bounds, retained annotations/dimensions, documentation_complete and assembly health. Retained values require explicit reassociation or recreation. Requires work/display match and no active sketch. Unsatisfied mates or stale managed anchors roll back this update; earlier prototype edits remain separate operations."""


READ_ONLY.add("nx_list_dimensions")


def nx_list_dimensions():
    """List actual computed native dimension values, native retention and measurement_valid flags, typed IDs and annotation origins in the work part. Includes drafting and PMI dimensions, identified by native subtype. Values/origins use native work-part units; inspect view association separately. Read-only, no regeneration."""
