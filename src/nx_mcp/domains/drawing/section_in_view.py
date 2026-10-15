"""Section in View: which bodies or components a section view cuts.

NX 2406 findings encoded here:

- ``DraftingViewCollection.CreateSectionInViewBuilder`` takes views (``Views``), objects
  (``EditObjects.Objects``) and ``EditType`` (MakeNonSectioned, MakeSectioned,
  RemoveViewSpecific); ``Commit`` returns None. A non-sectioned body is drawn whole (no
  cut faces, no crosshatch) after the view update.
- In a view that is not a section view NX commits without any visible change: such views
  are refused before NX.
- The cut solids of a section view are read back with UF ``Draw.AskSxsolidsOfSxview``
  (``DraftingBody`` objects, mapped to bodies through all their curves' parent edges and
  faces); a non-sectioned body leaves that list and its crosshatch is deleted. A
  component is checked through its bodies.
- NX accepts a body of a part outside the work assembly and changes nothing: objects must
  be the work part's or in its assembly tree, and drawn in the view.
"""

from __future__ import annotations

import contextlib
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, RefTo
from nx_mcp.domains.drawing.common import ViewArg, quiet_sheet_sketch, uf_types, update
from nx_mcp.kit import args, builders, drawing, licenses, parts, readback, result, select

MODES = {
    "non_sectioned": "MakeNonSectioned",
    "sectioned": "MakeSectioned",
    "default": "RemoveViewSpecific",
}


class SectioningData(TypedDict, total=False):
    view: result.Row
    mode: str
    objects: list[result.Row]
    cut: Annotated[list[result.Row], Doc("Bodies the view cuts after the change")]


def cut_bodies(tx, view):
    """Tags of the bodies a section view cuts, or None when NX cannot list them.

    UF ``Draw.AskSxsolidsOfSxview(view, leg)`` lists the view's section solids
    (``DraftingBody`` objects; both legs of a revolved section); each maps to its model
    body through the parents of its drafting curves (``Draw.AskDraftingCurveParents``:
    edges, or faces for cut outlines and silhouettes). Every curve is looked at: the first
    ones of a solid may have no model parent.
    """
    draw = tx.uf.Draw
    lookup = tx.nx.TaggedObjectManager.GetTaggedObject
    legs = uf_types(tx).Draw.SxlineLeg
    solids = set()
    try:
        for leg in (legs.SXLINE_LEG1, legs.SXLINE_LEG2):
            solids |= {int(s) for s in draw.AskSxsolidsOfSxview(view.Tag, leg)[1] or []}
    except Exception:  # an NX build that cannot answer for this view
        return None
    tags = set()
    for solid in sorted(solids):
        for drawn in getattr(lookup(solid), "DraftingCurves", None) or []:
            tx.checkpoint()
            with contextlib.suppress(Exception):  # a curve without model parents
                for tag in draw.AskDraftingCurveParents(drawn.Tag)[1] or []:
                    parent = lookup(tag)
                    if hasattr(parent, "GetBody"):
                        tags.add(int(parent.GetBody().Tag))
    return tags


def _is_section(view):
    return type(view).__name__ == "SectionView"


def _in_tree(tx, obj, field):
    """Refuse a body or component that is not the work part's or in its assembly tree."""
    if getattr(obj, "IsOccurrence", False):
        component = obj if _is_component(obj) else getattr(obj, "OwningComponent", None)
        tags = {int(c.Tag) for c, _ in parts.components(tx.part)}
        if component is not None and int(component.Tag) in tags:
            return obj
    elif getattr(obj, "OwningPart", None) == tx.part and not _is_component(obj):
        return obj
    owner = getattr(obj, "OwningPart", None)
    fail(
        "NX_WRONG_OWNER",
        "The object is not in the work part or its assembly"
        + (f" (it belongs to {owner.Leaf})" if hasattr(owner, "Leaf") else ""),
        field=field,
        hint="Pass a body of the work part, or a component or component body of its "
        "assembly as the view shows it (nx_list_components, nx_list_objects)",
    )


def _subtree(component):
    """The components below ``component``, depth first."""
    out = []
    for child in component.GetChildren() if hasattr(component, "GetChildren") else []:
        out.append(child)
        out.extend(_subtree(child))
    return out


def _owner_tags(body):
    owner = getattr(body, "OwningComponent", None)
    return {int(owner.Tag)} if owner is not None else set()


def _drawn(tx, view, targets, refs):
    """Refuse targets the view does not draw (when NX lists what the view draws)."""
    bodies = drawing.view_bodies(edge for edge, _ in drawing.drafted_edges(tx, view))
    if not bodies:
        return
    shown = {int(b.Tag) for b in bodies}
    owners = set().union(*(_owner_tags(b) for b in bodies))
    for i, obj in enumerate(targets):
        if _is_component(obj):
            seen = bool({int(obj.Tag), *(int(c.Tag) for c in _subtree(obj))} & owners)
        else:
            seen = int(obj.Tag) in shown
        if not seen:
            fail("NX_WRONG_OWNER", f"{refs[i]} is not drawn in this view",
                 field=f"/objects/{i}",
                 hint="Pass bodies or components the section view shows")  # fmt: skip


@tool(
    effect="model",
    label="Set view sectioning",
    summary=lambda a, say: (
        f"Make {len(a.get('objects') or [])} objects {a.get('mode')} in " + say("view")
    ),
    keywords=(
        "section in view",
        "non-sectioned",
        "do not section",
        "unsectioned component",
        "section components",
        "shaft not sectioned",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    cost="light",
    batchable=True,
    example={"view": "v:9", "objects": ["b:4"], "mode": "non_sectioned"},
)
def nx_set_view_sectioning(
    tx: Tx,
    *,
    view: ViewArg,
    objects: Annotated[list[Annotated[str, RefTo("body", "component")]], Items(1, 50)],
    mode: Annotated[
        Literal["non_sectioned", "sectioned", "default"],
        Doc("non_sectioned: drawn whole; sectioned: cut; default: the view's own rule"),
    ],
) -> SectioningData:
    """Choose which bodies or components a section view cuts, without a new view. Shafts,
    bolts and pins are usually drawn non-sectioned. The view must be a section view
    (nx_create_drawing_view type section, half_section, stepped_section or revolved_section)
    that draws the objects; cut lists the bodies NX cuts afterwards."""
    args.distinct("/objects", objects, "objects")
    with tx.checks():
        shown, _sheet = drawing.view(tx, view)
        if not _is_section(shown):
            fail(
                "NX_WRONG_KIND",
                f"{shown.Name} is not a section view: NX sections only section views",
                field="/view",
                hint="Create one with nx_create_drawing_view type section",
            )
        targets = [
            _in_tree(
                tx,
                select.resolve(tx, r, "body", "component", field=f"/objects/{i}"),
                f"/objects/{i}",
            )  # fmt: skip
            for i, r in enumerate(objects)
        ]
        _drawn(tx, shown, targets, objects)
    licenses.require(tx, ("drafting",), "Section in View needs a drafting license")
    native = MODES[mode]
    with drawing.on_sheet(tx, _sheet) as _sh:
        quiet_sheet_sketch(tx)
        with builders.build(tx, tx.part.DraftingViews.CreateSectionInViewBuilder) as b:
            b.Views.Add(shown)
            for obj in targets:
                b.EditObjects.Objects.Add(obj)
            b.EditType = getattr(tx.nx.Drawings.SectionInViewBuilder.EditSxtype, native)
            if b.has("Validate") and not b.Validate():
                fail("NX_BUILD_FAILED", "NX rejected the Section in View inputs")
            b.commit()
        drawing.refresh(tx, [shown])
        update(tx)
        cut = cut_bodies(tx, shown)
    out: SectioningData = {
        "view": tx.ref(shown, "drawing_view"),
        "mode": mode,
        "objects": [tx.ref(o, "component" if _is_component(o) else "body") for o in targets],
    }
    if cut is not None:
        lookup = tx.nx.TaggedObjectManager.GetTaggedObject
        out["cut"] = [tx.ref(lookup(t), "body") for t in sorted(cut)]
        if mode != "default":
            _check_cut(tx, targets, cut, mode)
    return out


def _target_cut(tx, obj, cut):
    """Whether NX cuts ``obj``: a body is in ``cut``; a component has a body in it."""
    if not _is_component(obj):
        return int(obj.Tag) in cut
    lookup = tx.nx.TaggedObjectManager.GetTaggedObject
    family = {int(obj.Tag), *(int(c.Tag) for c in _subtree(obj))}
    return any(_owner_tags(lookup(tag)) & family for tag in cut)


def _check_cut(tx, targets, cut, mode):
    """Every target is cut (sectioned) or not (non_sectioned) as asked. NX listing no cut
    body at all after a request to cut is not conclusive: a warning, not a rollback."""
    wanted = mode == "sectioned"
    wrong = [o for o in targets if _target_cut(tx, o, cut) != wanted]
    if not wrong:
        return
    rows = [tx.ref(o, "component" if _is_component(o) else "body") for o in wrong]
    if wanted and not cut:
        ids = [r["id"] for r in rows]
        tx.warn("NX_UPDATE_WARNING", f"NX lists no cut body for this view, so {ids} could not "
                "be read back as sectioned: check the view with nx_capture_view")  # fmt: skip
        return
    readback.expect(
        False,
        "sectioned",
        f"NX does not show every object {mode.replace('_', '-')}",
        objects=rows,
    )


def _is_component(obj):
    return type(obj).__name__ == "Component"
