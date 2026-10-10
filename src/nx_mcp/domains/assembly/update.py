"""Propagate prototype changes into the assembly and its documentation (tool-surface 3.8).

Solves every unsuppressed assembly constraint (all must report Solved), updates the
model, refreshes managed explosion traces, runs every ``@refresher`` hook (parts
lists, bend tables, detail views) and updates the drawing views sheet by sheet
(NX 2406 updates a view's bounds only while its sheet is open). Retained
dimensions and annotations are reported: they need re-association.
"""

from __future__ import annotations

import contextlib
from typing import TypedDict

from nx_mcp.core.catalogue import hooks, tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.domains.assembly import common, constraints, exploded
from nx_mcp.domains.assembly.constraints import ConstraintData
from nx_mcp.kit import drawing, model, result


class UpdateData(TypedDict):
    constraints: list[ConstraintData]
    traces: list[result.Row]
    refreshed: list[result.Row]
    views: list[result.Row]
    retained: list[result.Row]
    documentation_complete: bool


def _retained(tx, part) -> list:
    rows = []
    for d in getattr(part, "Dimensions", None) or []:
        if getattr(d, "IsRetained", False):
            rows.append(tx.ref(d, "dimension"))
    for name in ("Notes", "Labels"):
        for a in getattr(part, name, None) or []:
            if getattr(a, "IsRetained", False):
                rows.append(tx.ref(a, "annotation"))
    return rows


@tool(
    effect="model",
    label="Update assembly",
    summary="Update assembly and documentation",
    keywords=("update assembly", "solve constraints", "refresh BOM", "update drawing views"),
    requires=("work_part", "no_active_sketch", "assembly"),
    cost="long",
    invalidates="deleted",
    example={},
)
def nx_update_assembly(tx: Tx) -> UpdateData:
    """Propagate prototype edits through the assembly and its documentation. Solves the
    constraints, refreshes traces, parts lists and views, lists retained dimensions."""
    part = tx.part
    explosions = list(getattr(part.ComponentAssembly, "Explosions", None) or [])
    if explosions or list(getattr(part, "DrawingSheets", None) or []):
        common.explosion_context(tx)  # traces and sheets are refreshed on the display part
    active = [c for c in constraints.all_constraints(part) if not c.Suppressed]
    if active:
        with constraints.network(tx) as (_positioner, net):
            constraints.solve(tx, net)
            failed = [c for c in active if not constraints.solved(tx, c)]
            if failed:
                fail(
                    "NX_CONSTRAINT_UNSATISFIED",
                    f"{len(failed)} assembly constraint(s) do not solve; nothing was updated",
                    hint="Fix or suppress them with nx_edit_assembly_constraint",
                    details={"constraints": [constraints.record(tx, c) for c in failed][:20]},
                )
    model.update(tx)
    traces = []
    for ex in explosions:
        tx.checkpoint()
        traces.extend(exploded.refresh_traces(tx, ex))
    refreshed: list[result.Row] = []
    for _kind, refresh in sorted(hooks("refresher").items()):
        tx.checkpoint()
        refreshed.extend(refresh(tx, None) or [])
    views: list[result.Row] = []
    for sheet in list(getattr(part, "DrawingSheets", None) or []):
        tx.checkpoint()
        owned = list(sheet.GetDraftingViews())
        if not owned:
            continue
        with drawing.on_sheet(tx, sheet, part):
            drawing.refresh(tx, owned)
            for view in owned:
                with contextlib.suppress(Exception):
                    view.UpdateAutomaticViewBound()
        views.extend(tx.ref(v, "drawing_view") for v in owned)
    retained = _retained(tx, part)
    return {
        "constraints": [constraints.record(tx, c) for c in active],
        "traces": [tx.ref(line, "traceline") for line in traces],
        "refreshed": refreshed,
        "views": views,
        "retained": retained,
        "documentation_complete": not retained,
    }
