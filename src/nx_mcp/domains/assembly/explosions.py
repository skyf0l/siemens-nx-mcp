"""Named explosions: create, set exploded poses, read, show (tool-surface 3.8).

Exploded poses are absolute assembly poses; a placement gives one, or an offset from the
pose the component inherits (assembled, or carried by its exploded parent). Assembled
poses never change (read back).
See ``exploded.py`` for the NX 2406 post-transform and trace-refresh rules.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    ComponentRef,
    Doc,
    DrawingViewRef,
    ExplosionRef,
    Items,
    Mat3,
    Name,
    Offset,
    PageLimit,
    Point3,
    Pose,
    RefTo,
    forbid_extra,
)
from nx_mcp.domains.assembly import common, exploded, structure
from nx_mcp.kit import display, drawing, readback, result, select
from nx_mcp.kit import pose as kpose


class ExplodedRow(TypedDict):
    component: result.Row
    translation: Point3
    rotation: Mat3
    assembled_translation: Point3
    assembled_rotation: Mat3
    suppressed: bool


class _PlacementRequired(TypedDict):
    component: ComponentRef


class Placement(_PlacementRequired, total=False):
    offset: Annotated[tuple[float, float, float], Doc("[dx, dy, dz] from unexploded pose")]
    pose: Pose


forbid_extra(Placement)
# Components listed in a create result (nx_get_explosion pages them all).
LISTED = 50


class ExplosionData(TypedDict):
    explosion: result.Row
    component_count: int
    components: list[result.Row]
    views: list[result.Row]


def _targets(placements) -> list:
    """Per placement, an absolute ``(t, R)`` or :func:`exploded.shifted` of its offset."""
    out = []
    for i, p in enumerate(placements):
        field = f"/placements/{i}"
        if ("pose" in p) == ("offset" in p):
            fail("NX_INVALID_ARGUMENT",
                 "Give exactly one of offset (move from the unexploded pose) or pose (absolute)",
                 field=field, hint='e.g. {"component": "o:4", "offset": [0, 0, 40]}')  # fmt: skip
        if "offset" in p:
            out.append(exploded.shifted(p["offset"]))
        else:
            out.append(kpose.as_pose(p["pose"], field + "/pose"))
    return out


def _pick(tx, entries, seen, ref, field) -> int:
    """The tag of an unsuppressed component of the explosion, once per call."""
    c = select.resolve(tx, ref, "component", field=field)
    tag = int(c.Tag)
    if tag not in entries or entries[tag][3]:
        fail("NX_INVALID_ARGUMENT", "Select an unsuppressed component of this explosion",
             field=field, hint="nx_get_explosion lists its components")  # fmt: skip
    if tag in seen:
        fail("NX_INVALID_ARGUMENT", "A component may appear once per call", field=field)
    seen.add(tag)
    return tag


def _explode(tx, ex, tags, targets, resets):
    """Reset then place components, read every pose back and refresh traces and drawing
    views; returns ``(rows by tag, moved count, drawing views)``."""
    entries = exploded.tree(ex)
    before = {tag: exploded.entry_row(tx, e) for tag, e in entries.items()}
    exploded.reset(tx, ex, resets)
    wanted = exploded.apply(tx, ex, list(zip(tags, targets, strict=True)))
    after_entries = exploded.tree(ex)
    after = {tag: exploded.entry_row(tx, e) for tag, e in after_entries.items()}
    for i, tag in enumerate(tags):
        got = (after[tag]["translation"], after[tag]["rotation"])
        structure.expect_pose(got, wanted[tag], f"placements[{i}]")
    for i, tag in enumerate(resets):
        row = after[tag]
        parent = after_entries[tag][1].Parent
        if parent is None or int(parent.Tag) not in after:
            got = (row["translation"], row["rotation"])
            structure.expect_pose(got, (row["assembled_translation"], row["assembled_rotation"]),
                                 f"reset[{i}]")  # fmt: skip
    readback.expect(set(before) == set(after), "components", "The explosion changed membership")
    for tag, row in before.items():
        was = (row["assembled_translation"], row["assembled_rotation"])
        now = (after[tag]["assembled_translation"], after[tag]["assembled_rotation"])
        readback.expect(kpose.close(was, now, 1e-7), "assembled", "An assembled pose changed")
    exploded.refresh_traces(tx, ex)
    exploded.regenerate(tx)
    views = exploded.drawing_views(tx, tx.part, ex)
    drawing.refresh(tx, views)
    moved = sum(
        not kpose.close(
            (before[t]["translation"], before[t]["rotation"]),
            (after[t]["translation"], after[t]["rotation"]),
            1e-9,
        )
        for t in before
    )
    return after, moved, views


@tool(
    effect="model",
    label="Create explosion",
    summary="Create explosion {name}",
    keywords=("exploded view", "explosion", "explode assembly", "new explosion"),
    requires=("work_part", "no_active_sketch", "assembly"),
    cost="light",
    invalidates="deleted",
    refresh_annotations=False,
    example={"name": "EXPLODED"},
)
def nx_create_explosion(tx: Tx, *, name: Name) -> ExplosionData:
    """Create a named explosion of the work assembly, every component at its assembled pose.
    Then move them with nx_edit_explosion offsets; the assembly must be the display part."""
    if name != name.strip():
        fail("NX_INVALID_ARGUMENT", "The name has leading or trailing spaces", field="/name")
    with tx.checks():
        part = common.explosion_context(tx)
        collection = part.ComponentAssembly.Explosions
        for x in collection:
            if str(x.Name).casefold() == name.casefold():
                fail("NX_NAME_CONFLICT", f"An explosion {x.Name} already exists", field="/name",
                     hint="Edit it with nx_edit_explosion(explosion=details.existing.id) "
                     "or choose another name",
                     details={"existing": tx.ref(x, "explosion")})  # fmt: skip
    with native(default="NX_BUILD_FAILED"):
        created = collection.Create(name)
    entries = exploded.tree(created)
    for i, (child, component, _n, _h) in enumerate(entries.values()):
        structure.expect_pose(
            exploded.exploded_pose(child), structure.pose(component), f"pose[{i}]"
        )
    tx.warn("NX_NO_EFFECT", "Nothing is exploded yet: every component is at its "
            "assembled pose. Move them with nx_edit_explosion placements "
            '[{"component": "o:N", "offset": [dx, dy, dz]}].')  # fmt: skip
    return {
        "explosion": tx.ref(created, "explosion"),
        "component_count": len(entries),
        "components": [
            common.component_row(tx, c, names, with_pose=False)
            for _child, c, names, _h in list(entries.values())[:LISTED]
        ],
        "views": [],
    }


class EditExplosionData(TypedDict):
    explosion: result.Row
    components: list[ExplodedRow]
    moved_count: int
    updated_views: list[result.Row]


def _edit_summary(a, say):
    n = len(a.get("placements") or []) + len(a.get("reset_components") or [])
    return f"Explode {n} component{'s' if n != 1 else ''} in {say('explosion')}"


@tool(
    effect="model",
    label="Edit explosion",
    summary=_edit_summary,
    keywords=("explode component", "exploded position", "move in explosion", "unexplode"),
    requires=("work_part", "no_active_sketch", "assembly"),
    cost="heavy",
    invalidates="deleted",
    refresh_annotations=False,
    example={
        "explosion": "ex:3",
        "placements": [{"component": "o:4", "offset": [0, 0, 40]}],
    },
)
def nx_edit_explosion(
    tx: Tx,
    *,
    explosion: ExplosionRef,
    placements: Annotated[list[Placement], Items(1, 1000)] | None = None,
    reset_components: Annotated[list[ComponentRef], Items(1, 1000)] | None = None,
) -> EditExplosionData:
    """Move components in an explosion by an offset or to an absolute pose, or reset them.
    Assembled poses never change; traces and drawing views follow."""
    placements = placements or []
    resets = reset_components or []
    if not placements and not resets:
        fail("NX_INVALID_ARGUMENT", "Give placements or reset_components",
             hint='e.g. placements=[{"component": "o:4", "offset": [0, 0, 40]}]')  # fmt: skip
    targets = _targets(placements)
    with tx.checks():
        common.explosion_context(tx)
        ex = common.explosion(tx, explosion)
        entries = exploded.tree(ex)
        seen: set[int] = set()
        reset_tags = [_pick(tx, entries, seen, r, f"/reset_components/{i}")
                      for i, r in enumerate(resets)]  # fmt: skip
        tags = [_pick(tx, entries, seen, p["component"], f"/placements/{i}/component")
                for i, p in enumerate(placements)]  # fmt: skip
    after, moved, views = _explode(tx, ex, tags, targets, reset_tags)
    return {
        "explosion": tx.ref(ex, "explosion"),
        "components": [after[t] for t in tags + reset_tags],
        "moved_count": moved,
        "updated_views": [tx.ref(v, "drawing_view") for v in views],
    }


class GetExplosionData(TypedDict):
    explosion: result.Row
    components: list[ExplodedRow]
    views: list[result.Row]
    traces: list[result.Row]
    page: dict[str, Any]


@tool(
    effect="read",
    label="Get explosion",
    summary="Get explosion {explosion}",
    keywords=("explosion info", "exploded poses", "explosion components", "trace lines"),
    requires=("work_part", "assembly"),
    cost="light",
    example={"explosion": "ex:3"},
)
def nx_get_explosion(
    tx: Tx,
    *,
    explosion: common.ExplosionId,
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> GetExplosionData:
    """Read the exploded and assembled poses of an explosion's components, paged, nested ones
    included. Also lists the views showing it and its trace lines. Geometry reads stay at
    assembled positions."""
    ex = common.explosion(tx, explosion)
    selected, page = result.page(list(exploded.tree(ex).values()), offset, limit)
    part = tx.part
    return {
        "explosion": tx.ref(ex, "explosion"),
        "components": [exploded.entry_row(tx, e) for e in selected],
        "views": exploded.views(tx, part, ex),
        "traces": [tx.ref(line, "traceline") for line in exploded.traces(tx, part, ex)],
        "page": page,
    }


class DisplayData(TypedDict, total=False):
    explosion: result.Row
    view: result.Row
    view_kind: str


def _display_summary(a, say):
    what = say("explosion") if a.get("explosion") else "assembled state"
    where = (
        say("drawing_view")
        if a.get("drawing_view")
        else say("model_view")
        if a.get("model_view")
        else "work view"
    )
    return f"Show {what} in {where}"


@tool(
    effect="model",
    label="Show explosion",
    summary=_display_summary,
    keywords=("show exploded", "unexplode view", "assembled view", "explosion display"),
    requires=("work_part", "no_active_sketch", "assembly"),
    cost="heavy",
    invalidates="deleted",
    refresh_annotations=False,
    example={"explosion": "ex:3"},
)
def nx_set_explosion_display(
    tx: Tx,
    *,
    explosion: common.ExplosionId | None = None,
    drawing_view: DrawingViewRef | None = None,
    model_view: Annotated[str, RefTo("modeling_view", doc="Modeling view 'mv:N'")] | None = None,
) -> DisplayData:
    """Show an explosion in a view, or the assembled (unexploded) state when explosion is
    omitted. The view is the modeling work view unless drawing_view or model_view names one;
    orient it with nx_set_camera."""
    if drawing_view is not None and model_view is not None:
        fail("NX_INVALID_ARGUMENT", "Give drawing_view or model_view, not both",
             field="/model_view")  # fmt: skip
    with tx.checks():
        part = common.explosion_context(tx)
        ex = common.explosion(tx, explosion) if explosion is not None else None
        if drawing_view is not None:
            view = select.resolve(tx, drawing_view, "drawing_view", field="/drawing_view")
            kind = "drawing_view"
        elif model_view is not None:
            view = select.resolve(tx, model_view, "modeling_view", field="/model_view")
            kind = "modeling_view"
        else:
            view, kind = None, "modeling_view"
        if view is not None and not display.same(getattr(view, "OwningPart", None), part):
            field = "/drawing_view" if drawing_view is not None else "/model_view"
            fail("NX_WRONG_OWNER", "The view belongs to another part", field=field)
    if view is None:
        if drawing.current(part) is not None or drawing.work_view_is_drawing(part):
            drawing.close_sheets(tx, part)
        view = part.ModelingViews.WorkView
    if ex is not None:
        exploded.refresh_traces(tx, ex)
    api = exploded.uf(tx)
    with native(default="NX_BUILD_FAILED"):
        api.SetViewExplosion(view.Tag, ex.Tag if ex is not None else 0)
    exploded.regenerate(tx)
    if drawing_view is not None:
        drawing.refresh(tx, [view])
    elif model_view is None:
        with native():
            view.Fit()
    want = int(ex.Tag) if ex is not None else 0
    got = int(api.AskViewExplosion(view.Tag) or 0)
    readback.expect(got == want, "view", "The view did not take the explosion", actual=got)
    out: DisplayData = {"view": tx.ref(view, kind), "view_kind": kind}
    if ex is not None:
        out["explosion"] = tx.ref(ex, "explosion")
    return out
