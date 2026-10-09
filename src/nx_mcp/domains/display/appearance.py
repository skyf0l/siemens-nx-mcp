"""Visibility, appearance and highlights of displayed objects (tool-surface 3.12).

Each change keeps the objects' blank state, colour and transparency first
(``tx.display.keep``), so a failure restores them and ``restore_id`` undoes a success
(``nx_restore_display``, newest first). Colour and transparency are part attributes:
they persist when the part is saved (decision 9). Highlights are the agent's own and
are cleared when the engineer takes control back.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Items, Range, RefTo
from nx_mcp.domains.display.common import (
    TARGET_KINDS,
    DisplayRow,
    Targets,
    display_rows,
    kind_of,
    targets,
)
from nx_mcp.kit import bodies, display, parts, readback
from nx_mcp.kit.result import Row

ColorName = Literal[
    "red", "green", "blue", "yellow", "cyan", "magenta", "orange", "white", "black", "gray"
]
ColorIndex = Annotated[int, Range(ge=1, le=216)]


class VisibilityData(TypedDict):
    mode: str
    objects: list[DisplayRow]
    count: int


class AppearanceData(TypedDict):
    objects: list[DisplayRow]
    count: int
    marks_part_modified: bool


class AppearanceInfo(TypedDict, total=False):
    objects: list[DisplayRow]
    count: int


class HighlightData(TypedDict):
    highlighted: list[Row]
    count: int


def _visibility_summary(a, say):
    what = "all datums" if a.get("datums") else say("objects")
    return f"{str(a.get('mode', 'show')).capitalize()} {what}"


@tool(
    effect="view",
    label="Set visibility",
    summary=_visibility_summary,
    keywords=("hide", "show", "isolate", "blank", "unblank", "datum visibility"),
    requires=("work_part",),
    cost="light",
    restorable=True,
    example={"objects": ["b:41"], "mode": "hide"},
)
def nx_set_visibility(
    tx: Tx,
    *,
    mode: Literal["show", "hide", "isolate"],
    objects: Targets | None = None,
    datums: bool = False,
) -> VisibilityData:
    """Show, hide or isolate bodies, components, curves and datums, or show/hide all datums
    of the part with datums=true. Isolate hides every other body and component; returns
    restore_id."""
    if datums == (objects is not None):
        fail("NX_INVALID_ARGUMENT", "Give objects or datums=true", field="/objects")
    if datums and mode == "isolate":
        fail("NX_INVALID_ARGUMENT", "Datums can be shown or hidden, not isolated", field="/mode")
    with tx.checks():
        part = display.shown_part(tx, work=True)
        if datums:
            values = display.datums(part)
            if not values:
                fail("NX_NOT_FOUND", "The part has no datums", field="/datums")
        else:
            values = targets(tx, objects, expand=mode == "isolate")
            kinds = {kind_of(tx, v) for v in values}
            if kinds & {"face", "edge"}:
                fail(
                    "NX_WRONG_KIND",
                    "Visibility applies to bodies, components, curves and datums, not faces or "
                    "edges",
                    field="/objects",
                )
            if mode == "isolate" and kinds - {"body"}:
                fail("NX_WRONG_KIND", "Isolate bodies, features or components", field="/objects")
    if mode == "isolate":
        keep = {int(v.Tag) for v in values}
        for v in values:  # an occurrence body keeps its component and that one's parents
            component = getattr(v, "OwningComponent", None) if getattr(v, "IsOccurrence", 0) else v
            while component is not None and hasattr(component, "FindOccurrence"):
                keep.add(int(component.Tag))
                component = getattr(component, "Parent", None)
        everything = bodies.scope(tx, "assembly") + [c for c, _ in parts.components(part)]
        changed = list({int(o.Tag): o for o in everything + values}.values())
        tx.display.keep(changed)
        for obj in changed:
            (obj.Unblank if int(obj.Tag) in keep else obj.Blank)()
        for obj in changed:
            readback.expect(
                bool(obj.IsBlanked) == (int(obj.Tag) not in keep), "blanked", tag=int(obj.Tag)
            )
        values = changed
    else:
        tx.display.keep(values)
        for obj in values:
            (obj.Unblank if mode == "show" else obj.Blank)()
        for obj in values:
            readback.expect(bool(obj.IsBlanked) == (mode == "hide"), "blanked", tag=int(obj.Tag))
    pairs = tx.services.display_records(values, False)
    return {"mode": mode, "objects": display_rows(tx, pairs), "count": len(pairs)}


def _appearance_summary(a, say):
    parts_ = []
    if a.get("color") is not None:
        parts_.append(f"colour {a['color']}")
    if a.get("transparency") is not None:
        parts_.append(f"{a['transparency']}% transparent")
    return f"Make {say('objects')} " + " and ".join(parts_ or ["unchanged"])


@tool(
    effect="view",
    label="Set appearance",
    summary=_appearance_summary,
    keywords=("color", "colour", "transparency", "translucency", "edit object display"),
    requires=("work_part",),
    cost="light",
    restorable=True,
    example={"objects": ["b:41"], "color": "red", "transparency": 40},
)
def nx_set_appearance(
    tx: Tx,
    *,
    objects: Targets,
    color: ColorIndex | ColorName | None = None,
    transparency: Annotated[int, Range(ge=0, le=100)] | None = None,
) -> AppearanceData:
    """Set the colour (index 1-216 or name) and/or transparency (0 opaque, 100 clear) of
    objects. Components and features colour their bodies. Persists when the part is saved;
    returns restore_id."""
    if color is None and transparency is None:
        fail("NX_INVALID_ARGUMENT", "Give color or transparency", field="/color")
    with tx.checks():
        display.shown_part(tx, work=True)
        values = targets(tx, objects, expand=True)
        if transparency is not None and any(kind_of(tx, v) not in ("body", "face") for v in values):
            fail("NX_WRONG_KIND", "Transparency applies to bodies and faces", field="/objects")
        index = display.color_index(tx, color) if isinstance(color, str) else color
    tx.display.keep(values, appearance=True)
    display.apply_appearance(tx, values, index, transparency)
    pairs = tx.services.display_records(values, True)
    for obj, record in pairs:
        if index is not None:
            readback.expect(record.get("color_index") == index, "color", tag=int(obj.Tag),
                            color=record.get("color_index"))  # fmt: skip
        if transparency is not None and record.get("transparency") is not None:
            readback.expect(
                record["transparency"] == transparency,
                "transparency",
                tag=int(obj.Tag),
                transparency=record["transparency"],
            )
    return {"objects": display_rows(tx, pairs), "count": len(pairs), "marks_part_modified": True}


@tool(
    effect="read",
    label="Get appearance",
    summary="Read the appearance of {objects}",
    keywords=("display info", "object color", "is hidden", "blanked state", "transparency value"),
    requires=("work_part",),
    cost="light",
    example={"objects": ["b:41"]},
)
def nx_get_appearance(tx: Tx, *, objects: Targets, count_only: bool = False) -> AppearanceInfo:
    """Read blank state, colour and transparency of objects; bodies list their faces too.
    count_only returns just how many objects and faces a display change would touch."""
    values = targets(tx, objects, expand=True)
    if count_only:
        count = len({int(f.Tag) for v in values for f in [v, *_faces(v)]})
        return {"count": count}
    pairs = tx.services.display_records(values, True)
    return {"objects": display_rows(tx, pairs), "count": len(pairs)}


def _faces(obj):
    return list(obj.GetFaces()) if hasattr(obj, "IsSolidBody") else []


@tool(
    effect="view",
    label="Set highlight",
    summary=lambda a, say: (
        f"Highlight {say('objects')}" if a.get("objects") else "Clear highlights"
    ),
    keywords=("highlight", "select", "clear highlights", "emphasize", "point out"),
    requires=("work_part",),
    cost="instant",
    example={"objects": ["fc:88"]},
)
def nx_set_highlight(
    tx: Tx,
    *,
    objects: Annotated[list[RefTo(*TARGET_KINDS)], Items(0, 1000)],  # type: ignore[valid-type]
) -> HighlightData:
    """Replace the agent's highlights with these objects (NX selection highlight, no colour
    change); an empty list clears them."""
    with tx.checks():
        display.shown_part(tx, work=True)
        values = targets(tx, objects) if objects else []
    tx.display.keep_highlights()
    tx.services.restore_display(None, [], values)  # makes the agent's highlights exactly these
    now = {int(o.Tag) for o in tx.services.highlights()}
    readback.expect(now == {int(v.Tag) for v in values}, "highlighted", count=len(now))
    return {"highlighted": [tx.ref(v, kind_of(tx, v)) for v in values], "count": len(values)}
