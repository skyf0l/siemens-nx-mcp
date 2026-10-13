"""Section in View: which bodies or components a section view cuts.

NX 2406 findings encoded here:

- ``DraftingViewCollection.CreateSectionInViewBuilder`` takes views (``Views``), objects
  (``EditObjects.Objects``) and ``EditType`` (MakeNonSectioned, MakeSectioned,
  RemoveViewSpecific); ``Commit`` returns None. A non-sectioned body is drawn whole (no
  cut faces, no crosshatch) after the view update.
- In a view that is not a section view NX commits without any visible change: such views
  are refused before NX.
- The cut solids of a section view are read back with UF ``Draw.AskSxsolidsOfSxview``
  (``DraftingBody`` objects, mapped to bodies through their curves' parent edges); a
  non-sectioned body leaves that list and its crosshatch is deleted.
"""

from __future__ import annotations

import contextlib
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, DrawingViewRef, Items, RefTo
from nx_mcp.domains.drawing.common import quiet_sheet_sketch, uf_types, update
from nx_mcp.kit import args, builders, drawing, facts, licenses, readback, result, select

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
    body through the parent edges of its drafting curves (``Draw.AskDraftingCurveParents``).
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
            with contextlib.suppress(Exception):  # a curve without model parents
                parents = draw.AskDraftingCurveParents(drawn.Tag)[1] or []
                bodies = [lookup(t).GetBody() for t in parents if facts.is_edge(lookup(t))]
                if bodies:
                    tags.add(int(bodies[0].Tag))
                    break
    return tags


def _is_section(view):
    return type(view).__name__ == "SectionView"


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
    view: DrawingViewRef,
    objects: Annotated[list[Annotated[str, RefTo("body", "component")]], Items(1, 50)],
    mode: Annotated[
        Literal["non_sectioned", "sectioned", "default"],
        Doc("non_sectioned: drawn whole; sectioned: cut; default: the view's own rule"),
    ],
) -> SectioningData:
    """Choose which bodies or components a section view cuts, without a new view. Shafts,
    bolts and pins are usually drawn non-sectioned. The view must be a section view
    (nx_create_drawing_view type section, half_section, stepped_section or revolved_section);
    cut lists the bodies NX cuts afterwards."""
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
            select.resolve(tx, r, "body", "component", field=f"/objects/{i}")
            for i, r in enumerate(objects)
        ]
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
        bodies = [o for o in targets if not _is_component(o)]
        tags = {int(o.Tag) for o in bodies}
        if mode != "default":
            wanted = mode == "sectioned"
            readback.expect(
                all((int(o.Tag) in cut) == wanted for o in bodies),
                "sectioned",
                f"NX does not show every body {mode.replace('_', '-')}",
                cut=sorted(cut & tags),
            )
        lookup = tx.nx.TaggedObjectManager.GetTaggedObject
        out["cut"] = [tx.ref(lookup(t), "body") for t in sorted(cut)]
    return out


def _is_component(obj):
    return type(obj).__name__ == "Component"
