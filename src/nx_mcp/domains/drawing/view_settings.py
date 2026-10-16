"""Per-view display settings nx_edit_drawing_view does not set: threads, virtual
intersections, silhouettes, UV grids, edges hidden by edges, tangent (smooth) edges.

NX 2406 findings encoded here:

- ``DraftingViewCollection.CreateCustomViewSettingsBuilder`` holds the view-creation
  wizard's defaults for new views; it has no views and changes no existing view. The
  same settings of an existing view are the View Style groups of
  ``SettingsManager.CreateDrawingEditViewSettingsBuilder`` (used here, one builder per
  view, inherited from that view so its other settings stay).
- ``ViewStyleThreads.StandardData`` is an int: 0 none, 1 simplified (full arc),
  4 simplified (3/4 arc), 2 schematic, 3 detailed.
- Secondary geometry in views (``CreateSecondaryGeometryInViewsBuilder``, work part
  only) commits, but no read-back is reliable: a new builder's
  ``Components.InitializeListFromObject(view)`` lists the same, other component for
  both geometry types. It is not offered.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Range
from nx_mcp.domains.drawing.common import ViewArg, update, view_of
from nx_mcp.kit import drawing, licenses, readback, result

THREADS = {"none": 0, "simplified": 1, "simplified_three_quarter": 4, "schematic": 2,
           "detailed": 3}  # fmt: skip
SETTINGS = {
    "threads": ("ViewStyleThreads", "StandardData"),
    "thread_min_pitch": ("ViewStyleThreads", "MinimumPitchFieldData"),
    "thread_true_hidden": ("ViewStyleThreads", "TrueHiddenLine"),
    "hidden_lines": ("ViewStyleHiddenLines", "HiddenLine"),
    "edges_hidden_by_edges": ("ViewStyleHiddenLines", "EdgesHiddenByEdges"),
    "smooth_edges": ("ViewStyleSmoothEdges", "SmoothEdge"),
    "virtual_intersections": ("ViewStyleVirtualIntersections", "VirtualIntersections"),
    "adjacent_blends": ("ViewStyleVirtualIntersections", "AdjacentBlends"),
    "silhouettes": ("ViewStyleGeneral", "Silhouettes"),
    "uv_grid": ("ViewStyleGeneral", "UVGrid"),
}


HIDDEN_LINES_DOC = "Hidden-line removal; false draws hidden edges solid"
HIDDEN_LINES_OFF = (
    "Hidden-line removal is off: occluded edges are drawn solid like visible ones. To hide "
    "dashed hidden lines instead, keep hidden_lines and set nx_edit_drawing_view style "
    "hidden_font invisible"
)


class ViewSettings(TypedDict, total=False):
    threads: Annotated[
        Literal["none", "simplified", "simplified_three_quarter", "schematic", "detailed"],
        Doc("Thread display standard (simplified: full arc)"),
    ]
    thread_min_pitch: Annotated[
        float, Range(ge=0), Doc("Threads of a smaller pitch are not drawn; part units")
    ]
    thread_true_hidden: Annotated[bool, Doc("Hidden threads drawn with true hidden lines")]
    hidden_lines: Annotated[bool, Doc(HIDDEN_LINES_DOC)]
    edges_hidden_by_edges: bool
    smooth_edges: Annotated[bool, Doc("Tangent (smooth) edges between blended faces")]
    virtual_intersections: Annotated[bool, Doc("Virtual sharp edges of blended faces")]
    adjacent_blends: Annotated[bool, Doc("Virtual intersections of adjacent blends")]
    silhouettes: bool
    uv_grid: bool


class ViewRow(result.Row, total=False):
    settings: ViewSettings


class ViewSettingsData(TypedDict):
    views: list[ViewRow]


def _plain(key, value):
    if key == "threads":
        return next((k for k, v in THREADS.items() if v == int(value)), int(value))
    if key == "thread_min_pitch":
        return float(value)
    return bool(value)


def read_settings(tx, view, updates=None):
    """The view's settings; ``updates`` (plain values) are committed first."""
    b = tx.part.SettingsManager.CreateDrawingEditViewSettingsBuilder([view])
    try:
        b.InheritSettingsFromSelectedObjects(view)
        if updates:
            for key, value in updates.items():
                group, prop = SETTINGS[key]
                native = THREADS[value] if key == "threads" else value
                setattr(getattr(b.ViewStyle, group), prop, native)
            b.Commit()
        return {
            key: _plain(key, getattr(getattr(b.ViewStyle, group), prop))
            for key, (group, prop) in SETTINGS.items()
        }
    finally:
        b.Destroy()


@tool(
    effect="model",
    label="Set view settings",
    summary=lambda a, say: f"Set display settings of {len(a.get('views') or [])} view(s)",
    keywords=(
        "thread display drawing",
        "virtual intersections",
        "tangent edges",
        "silhouettes",
        "edges hidden by edges",
        "custom view settings",
    ),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    invalidates="deleted",
    example={"views": ["v:7"], "settings": {"threads": "schematic", "smooth_edges": False}},
)
def nx_set_view_settings(
    tx: Tx,
    *,
    views: Annotated[list[ViewArg], Items(1, 20)],
    settings: ViewSettings,
) -> ViewSettingsData:
    """Set edge and thread display settings of one or more drawing views of the work part.
    Covers thread standard and minimum pitch, hidden lines, edges hidden by edges, tangent
    (smooth) edges, virtual intersections, silhouettes and UV grids. Settings left out are
    kept; every view's settings are read back. Fonts, scale, shading: nx_edit_drawing_view."""
    changes = {k: v for k, v in settings.items() if v is not None}
    if not changes:
        fail("NX_INVALID_ARGUMENT", "Give at least one setting", field="/settings",
             hint=f"Settings: {', '.join(SETTINGS)}")  # fmt: skip
    with tx.checks():
        found = [view_of(tx, v, f"/views/{i}") for i, v in enumerate(views)]
        targets = list({int(v.Tag): (v, sheet) for v, sheet in found}.values())
    chosen = {int(v.Tag) for v, _ in targets}
    sheets = list({int(s.Tag): s for _, s in targets}.values())
    others = [v for s in sheets for v in s.GetDraftingViews() if int(v.Tag) not in chosen]
    before = {int(v.Tag): read_settings(tx, v) for v in others}
    for view, sheet in targets:
        tx.checkpoint()
        with licenses.attempt(tx, ("drafting",)), drawing.on_sheet(tx, sheet):
            read_settings(tx, view, changes)
    update(tx)
    tx.part.DraftingViews.UpdateViews([v for v, _ in targets])
    rows = []
    for view, _ in targets:
        actual = read_settings(tx, view)
        wrong = {k: actual[k] for k, v in changes.items() if not _same(actual[k], v)}
        readback.expect(not wrong, "settings", "NX kept other view settings", **wrong)
        rows.append({**tx.modified(view, "drawing_view"), "settings": actual})
    moved = [tx.ref(v, "drawing_view")["id"] for v in others
             if read_settings(tx, v) != before[int(v.Tag)]]  # fmt: skip
    if changes.get("hidden_lines") is False:
        tx.warn("NX_UPDATE_WARNING", HIDDEN_LINES_OFF)
    if moved:
        tx.warn("NX_UPDATE_WARNING", f"NX also changed the settings of views {moved}; set "
                "them back with nx_set_view_settings")  # fmt: skip
    return {"views": rows}  # type: ignore[typeddict-item]  # rows: a ref plus settings


def _same(actual, wanted):
    if isinstance(wanted, float) and not isinstance(wanted, bool):
        return abs(float(actual) - wanted) <= 1e-9 * max(1.0, abs(wanted))
    return actual == wanted
