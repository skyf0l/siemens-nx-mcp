"""Propagate prototype changes into the assembly and its documentation (tool-surface 3.8).

Solves every unsuppressed assembly constraint (all must report Solved), updates the
model, refreshes managed explosion traces, runs every ``@refresher`` hook (parts
lists, bend tables, detail views) and updates the drawing views sheet by sheet
(NX 2406 updates a view's bounds only while its sheet is open). Retained
dimensions and annotations are reported: they need re-association.
"""

from __future__ import annotations

import contextlib
from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import hooks, tool
from nx_mcp.core.errors import counted, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc
from nx_mcp.domains.assembly import common, constraints, exploded
from nx_mcp.domains.assembly.common import ComponentRow
from nx_mcp.domains.assembly.constraints import ConstraintData
from nx_mcp.kit import deform, drawing, model, result


class ConstraintCounts(TypedDict):
    total: int
    solved: int
    suppressed: int


class UpdateData(TypedDict):
    constraint_counts: ConstraintCounts
    constraints: list[ConstraintData]  # not solved (suppressed), or all with all_constraints
    moved: list[ComponentRow]  # direct components whose pose changed
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
    switch="display",  # traces, explosions and sheets refresh on the displayed assembly
    effect="model",
    label="Update assembly",
    summary="Update assembly and documentation",
    keywords=("update assembly", "solve constraints", "refresh BOM", "update drawing views"),
    requires=("work_part", "no_active_sketch", "assembly"),
    cost="long",
    invalidates="deleted",
    example={},
)
def nx_update_assembly(
    tx: Tx,
    *,
    all_constraints: Annotated[bool, Doc("List every constraint, solved ones too")] = False,
) -> UpdateData:
    """Propagate prototype edits through the assembly and its documentation. Solves the
    unsuppressed constraints (counted; unsolved ones listed), reports moved components,
    refreshes traces, parts lists and views, lists retained dimensions."""
    part = tx.part
    before = constraints.poses(part)
    explosions = list(getattr(part.ComponentAssembly, "Explosions", None) or [])
    if explosions or list(getattr(part, "DrawingSheets", None) or []):
        common.explosion_context(tx)  # traces and sheets are refreshed on the display part
    every = constraints.all_constraints(part)
    active = [c for c in every if not c.Suppressed]
    if active:
        with constraints.network(tx) as (_positioner, net):
            constraints.solve(tx, net)
            failed = [c for c in active if not constraints.solved(tx, c)]
            if failed:
                fail(
                    "NX_CONSTRAINT_UNSATISFIED",
                    f"{counted(len(failed), 'assembly constraint')} not solved; nothing was updated",
                    hint="Fix or suppress them with nx_edit_assembly_constraint",
                    details={"constraints": [constraints.record(tx, c) for c in failed][:20]},
                )
    model.update(tx)
    for message in deform.range_messages(tx, part):  # a formula may drive one past its range
        tx.warn("NX_UPDATE_WARNING", message)
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
    rows = [constraints.record(tx, c) for c in every]
    solved = sum(r.get("status") == "Solved" for r in rows)
    listed = rows if all_constraints else [r for r in rows if r.get("status") != "Solved"]
    return {
        "constraint_counts": {
            "total": len(rows),
            "solved": solved,
            "suppressed": sum(bool(r.get("suppressed")) for r in rows),
        },
        "constraints": listed,
        "moved": constraints.moved(tx, before),
        "traces": [tx.ref(line, "traceline") for line in traces],
        "refreshed": refreshed,
        "views": views,
        "retained": retained,
        "documentation_complete": not retained,
    }
