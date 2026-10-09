"""Public signatures and contracts for authoring/review tools."""

from __future__ import annotations

from typing import Any, Literal

READ_ONLY = {
    "nx_list_expressions",
    "nx_model_health",
}
NON_MODEL = {
    "nx_preview_change",
}


def nx_list_expressions(name_contains: str | None = None, offset: int = 0, limit: int = 50):
    """Paginate work-part expressions, formulas, numeric values in expression units, editability and stored immediate dependencies. Names are case-sensitive. Non-numeric values are null. Conditional dependencies may be incomplete."""


def nx_model_health(scope: Literal["part", "assembly"] = "part", offset: int = 0, limit: int = 50):
    """Inspect native feature errors/warnings, suppression, unloaded prototype availability and UF body consistency. Assembly scope checks unique loaded unsuppressed prototypes. Does not force rebuild or certify design intent. Paginate issues; counts cover the whole query."""


def nx_preview_change(operations: list[dict[str, Any]], capture: bool = True):
    """Temporarily apply 1–25 edits under a native checkpoint, return before/after bounds/volume/parameters/health and optional screenshot, then roll back before returning. Each entry has method and params. No legacy tool is previewable any more (component placement moved to catalogue tools). Returned geometry IDs are stale after rollback. Preview is session-scoped and expires on intervening mutations or manual handoff."""


def nx_finish_preview(preview_id: str, action: Literal["accept", "discard"]):
    """Accept by atomically reapplying stored preview edits only if its part and mutation epoch are unchanged, or discard the stored plan. Preview already restored original geometry. Accepted edits remain unsaved and undoable. Stable operation_id prevents double application."""


def nx_delete_explosion(explosion: str):
    """Delete a native explosion only when no model/drawing views reference it. Detach dependent views with nx_set_explosion_display (no explosion) first. Native deletion is transactional; returned reference becomes stale. Actual assembly components are retained."""
