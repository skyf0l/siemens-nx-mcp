"""Named explosions: create, set exploded poses, read, show (tool-surface 3.8).

Exploded poses are absolute assembly poses; assembled poses never change (read back).
See ``exploded.py`` for the NX 2406 post-transform and trace-refresh rules.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Items, Mat3, Name, Offset, PageLimit, Point3, Pose, RefTo
from nx_mcp.domains.assembly import common, exploded
from nx_mcp.domains.assembly.common import ComponentRef
from nx_mcp.kit import assembly, drawing, readback, result, select
from nx_mcp.kit import pose as kpose

ExplosionRef = RefTo("explosion")


class ExplodedRow(TypedDict):
    component: result.Row
    translation: Point3
    rotation: Mat3
    assembled_translation: Point3
    assembled_rotation: Mat3
    suppressed: bool


class ExplosionData(TypedDict, total=False):
    explosion: result.Row
    component_count: int
    views: list[result.Row]


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
    Move them with nx_edit_explosion; the assembly must be the display part."""
    if name != name.strip():
        fail("NX_INVALID_ARGUMENT", "The name has leading or trailing spaces", field="/name")
    with tx.checks():
        part = common.explosion_context(tx)
        collection = part.ComponentAssembly.Explosions
        if any(str(x.Name).casefold() == name.casefold() for x in collection):
            fail("NX_NAME_CONFLICT", f"An explosion {name} already exists", field="/name")
    with native(default="NX_BUILD_FAILED"):
        created = collection.Create(name)
    entries = exploded.tree(created)
    for i, (child, component, _n, _h) in enumerate(entries.values()):
        assembly.expect_pose(exploded.exploded_pose(child), assembly.pose(component), f"pose[{i}]")
    return {
        "explosion": tx.ref(created, "explosion"),
        "component_count": len(entries),
        "views": [],
    }


class Placement(TypedDict):
    component: ComponentRef  # type: ignore[valid-type]
    pose: Pose


Placement.__pydantic_config__ = {"extra": "forbid"}  # type: ignore[attr-defined]


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
        "placements": [{"component": "o:4", "pose": {"translation": [0, 0, 40]}}],
    },
)
def nx_edit_explosion(
    tx: Tx,
    *,
    explosion: ExplosionRef,  # type: ignore[valid-type]
    placements: Annotated[list[Placement], Items(1, 1000)] | None = None,
    reset_components: Annotated[list[ComponentRef], Items(1, 1000)] | None = None,  # type: ignore[valid-type]
) -> EditExplosionData:
    """Set absolute exploded poses of components (nested ones too) or reset them to the
    assembled pose. Assembled poses never change; traces and drawing views follow."""
    placements = placements or []
    resets = reset_components or []
    if not placements and not resets:
        fail("NX_INVALID_ARGUMENT", "Give placements or reset_components")
    targets = [kpose.as_pose(p["pose"], f"/placements/{i}/pose") for i, p in enumerate(placements)]
    with tx.checks():
        part = common.explosion_context(tx)
        ex = common.explosion(tx, explosion)
        entries = exploded.tree(ex)
        seen: set = set()

        def pick(ref, field):
            c = select.resolve(tx, ref, "component", field=field)
            tag = int(c.Tag)
            if tag not in entries or entries[tag][3]:
                fail("NX_INVALID_ARGUMENT", "Select an unsuppressed component of this explosion",
                     field=field)  # fmt: skip
            if tag in seen:
                fail("NX_INVALID_ARGUMENT", "A component may appear once per call", field=field)
            seen.add(tag)
            return tag

        reset_tags = [pick(r, f"/reset_components/{i}") for i, r in enumerate(resets)]
        pending = [
            (pick(p["component"], f"/placements/{i}/component"), target)
            for i, (p, target) in enumerate(zip(placements, targets, strict=True))
        ]
    before = {tag: exploded.entry_row(tx, e) for tag, e in entries.items()}
    exploded.reset(tx, ex, reset_tags)
    exploded.apply(tx, ex, pending)
    after_entries = exploded.tree(ex)
    after = {tag: exploded.entry_row(tx, e) for tag, e in after_entries.items()}
    for i, (tag, target) in enumerate(pending):
        got = (after[tag]["translation"], after[tag]["rotation"])
        assembly.expect_pose(got, target, f"placements[{i}]")
    for i, tag in enumerate(reset_tags):
        row = after[tag]
        parent = after_entries[tag][1].Parent
        if parent is None or int(parent.Tag) not in after:
            got = (row["translation"], row["rotation"])
            assembly.expect_pose(got, (row["assembled_translation"], row["assembled_rotation"]),
                                 f"reset[{i}]")  # fmt: skip
    readback.expect(set(before) == set(after), "components", "The explosion changed membership")
    for tag, row in before.items():
        was = (row["assembled_translation"], row["assembled_rotation"])
        now = (after[tag]["assembled_translation"], after[tag]["assembled_rotation"])
        readback.expect(kpose.close(was, now, 1e-7), "assembled", "An assembled pose changed")
    exploded.refresh_traces(tx, ex)
    exploded.regenerate(tx)
    views = exploded.drawing_views(tx, part, ex)
    drawing.refresh(tx, views)
    moved = sum(
        not kpose.close(
            (before[t]["translation"], before[t]["rotation"]),
            (after[t]["translation"], after[t]["rotation"]),
            1e-9,
        )
        for t in before
    )
    order = [tag for tag, _ in pending] + reset_tags
    return {
        "explosion": tx.ref(ex, "explosion"),
        "components": [after[t] for t in order],
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
    explosion: ExplosionRef,  # type: ignore[valid-type]
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> GetExplosionData:
    """Read the exploded and assembled poses of an explosion's components, paged, nested ones
    included. Also lists the views showing it and its trace lines."""
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
    explosion: ExplosionRef | None = None,  # type: ignore[valid-type]
    drawing_view: RefTo("drawing_view") | None = None,  # type: ignore[valid-type]
    model_view: RefTo("modeling_view") | None = None,  # type: ignore[valid-type]
) -> DisplayData:
    """Show an explosion, or the assembled state when omitted, in a view. The view is the
    modeling work view unless drawing_view or model_view names one."""
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
        if view is not None and not assembly.same(getattr(view, "OwningPart", None), part):
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
    out = {"view": tx.ref(view, kind), "view_kind": kind}
    if ex is not None:
        out["explosion"] = tx.ref(ex, "explosion")
    return out
