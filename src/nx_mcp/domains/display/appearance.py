"""Visibility, appearance and highlights of displayed objects (tool-surface 3.12).

Each change keeps the objects' blank state, colour and transparency first
(``tx.display.keep``), so a failure restores them and ``restore_id`` undoes a success
(``nx_restore_display``, newest first). Colour and transparency are part attributes:
they persist when the part is saved (decision 9). Highlights are the agent's own and
are cleared when the engineer takes control back.

A body is one display record however many faces it has (``kit.display.body_appearance``):
its colour, the transparency most faces share and only the faces that differ, which the
result lists as ``faces_differ`` (and the first few in ``faces``). Faces are read and
changed one by one only when they are given. Colours: a palette index, a basic name, a
colour-table label, ``#RRGGBB`` or ``[r, g, b]``; the last two take the nearest entry of
the part's colour table and the result's ``color_match`` names it.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Range, RefTo, Text
from nx_mcp.core.validate import match_option
from nx_mcp.domains.display.common import (
    TARGET_KINDS,
    DisplayRow,
    Targets,
    display_rows,
    faces_of,
    kind_of,
    targets,
)
from nx_mcp.kit import bodies, display, parts, readback
from nx_mcp.kit.result import Row

ColorName = Literal[
    "red", "green", "blue", "yellow", "cyan", "magenta", "orange", "white", "black", "gray"
]
ColorIndex = Annotated[int, Range(ge=1, le=216), Doc("Colour-table index")]
ColorRgb = Annotated[
    tuple[float, float, float], Doc("[r,g,b] 0-255, or 0-1 if all <=1: nearest table colour")
]
ColorText = Annotated[str, Text(48), Doc("'#RRGGBB' or a colour-table name ('Light Gray')")]
COLOR_ALIASES = {"grey": "gray", "silver": "gray", "purple": "magenta", "aqua": "cyan"}
Percent = Annotated[int, Range(ge=0, le=100)] | Annotated[str, Text(8), Doc("'40%'")]
# Kinds that can be blanked: faces and edges follow their body.
VISIBLE_KINDS = ("body", "curve", "point", "sketch", "component", "feature", "datum")


class VisibilityData(TypedDict):
    mode: str
    objects: list[DisplayRow]
    count: int
    blanked: int


class ColorMatch(TypedDict, total=False):
    index: int
    label: str
    hex: str
    requested: str


class AppearanceData(TypedDict, total=False):
    objects: list[DisplayRow]
    count: int
    marks_part_modified: bool
    color_match: ColorMatch


class AppearanceInfo(TypedDict, total=False):
    objects: list[DisplayRow]
    count: int
    faces: int


class HighlightData(TypedDict):
    highlighted: list[Row]
    count: int


def _visibility_summary(a, say):
    what = "all datums" if a.get("datums") else say("objects") if a.get("objects") else "all"
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
    objects: Annotated[list[Annotated[str, RefTo(*VISIBLE_KINDS)]], Items(1, 1000)] | None = None,
    datums: bool = False,
) -> VisibilityData:
    """Show, hide or isolate bodies, components, curves and datums (not faces). datums=true:
    all datums; mode=show alone shows every hidden body and component. Isolate hides all
    other bodies and components. Undo: nx_restore_display(restore_id)."""
    if datums and objects is not None:
        fail("NX_INVALID_ARGUMENT", "Give objects or datums=true, not both", field="/objects")
    show_all = not datums and objects is None
    if show_all and mode != "show":
        fail(
            "NX_INVALID_ARGUMENT",
            f"mode={mode} needs objects (or datums=true)",
            field="/objects",
            hint="List the bodies/components to change; only mode=show works without objects",
        )
    if datums and mode == "isolate":
        fail("NX_INVALID_ARGUMENT", "Datums can be shown or hidden, not isolated", field="/mode")
    with tx.checks():
        part = display.shown_part(tx, work=True)
        if datums:
            values = display.datums(part)
            if not values:
                fail("NX_NOT_FOUND", "The part has no datums", field="/datums")
        elif show_all:
            everything = bodies.scope(tx, "assembly") + [c for c, _ in parts.components(part)]
            values = [o for o in {int(o.Tag): o for o in everything}.values() if o.IsBlanked]
        else:
            values = targets(tx, objects, expand=mode == "isolate")
            kinds = {kind_of(tx, v) for v in values}
            if kinds & {"face", "edge"}:
                fail(
                    "NX_WRONG_KIND",
                    "Visibility applies to bodies, components, curves and datums, not faces or "
                    "edges",
                    field="/objects",
                    hint="Hide the owning body, or use nx_set_appearance transparency=100",
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
    rows = display_rows(tx, pairs)
    hidden = sum(1 for r in rows if r["blanked"])
    return {"mode": mode, "objects": rows, "count": len(pairs), "blanked": hidden}


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
    color: ColorIndex | ColorName | ColorRgb | ColorText | None = None,
    transparency: Percent | None = None,
) -> AppearanceData:
    """Set the colour and/or transparency (0 opaque, 100 clear) of objects. color: index
    1-216, a basic name, a colour-table name, '#RRGGBB' or [r,g,b] (nearest table colour,
    see color_match). A body is coloured whole (all its faces); components and features
    colour their bodies. Persists when saved; undo: nx_restore_display(restore_id)."""
    if color is None and transparency is None:
        fail("NX_INVALID_ARGUMENT", "Give color or transparency", field="/color")
    spec = _color(color)
    transparency = _percent(transparency)
    with tx.checks():
        part = display.shown_part(tx, work=True)
        values = targets(tx, objects, expand=True)
        if transparency is not None and any(kind_of(tx, v) not in ("body", "face") for v in values):
            fail("NX_WRONG_KIND", "Transparency applies to bodies and faces", field="/objects")
        index, match = display.color_from(tx, part, spec) if spec else (None, None)
        tx.display.keep(values, appearance=True)  # reads only: a refusal is not_started
    display.apply_appearance(tx, values, index, transparency)
    pairs = tx.services.display_records(values, True)
    for obj, record in pairs:
        _expect(obj, record, index, transparency)
    out: AppearanceData = {
        "objects": display_rows(tx, pairs),
        "count": len(pairs),
        "marks_part_modified": True,
    }
    if match is not None and len(match) > 1:
        out["color_match"] = match  # type: ignore[typeddict-item]
    return out


def _expect(obj, record, index, transparency) -> None:
    """Read-back of one record, a body's differing faces included."""
    rows = [record, *(record.get("faces") or [])]
    for row in rows:
        if index is not None:
            readback.expect(row.get("color_index") == index, "color", tag=int(obj.Tag),
                            color=row.get("color_index"))  # fmt: skip
        if transparency is not None and row.get("transparency") is not None:
            readback.expect(row["transparency"] == transparency, "transparency",
                            tag=int(obj.Tag), transparency=row["transparency"])  # fmt: skip


@tool(
    effect="read",
    label="Get appearance",
    summary="Read the appearance of {objects}",
    keywords=("display info", "object color", "is hidden", "blanked state", "transparency value"),
    requires=("work_part",),
    cost="light",
    example={"objects": ["b:41"]},
)
def nx_get_appearance(
    tx: Tx,
    *,
    objects: Targets,
    count_only: bool = False,
) -> AppearanceInfo:
    """Read blank state, colour and transparency of objects, one row per object. A body
    row gives faces_differ (faces coloured unlike the body) and the first few; pass faces
    to read each. count_only: just the number of objects and their faces."""
    values = targets(tx, objects, expand=True)
    if count_only:
        faces = len({int(f.Tag) for v in values for f in faces_of(v)})
        return {"count": len(values), "faces": faces}
    pairs = tx.services.display_records(values, True)
    return {"objects": display_rows(tx, pairs), "count": len(pairs)}


HEX = re.compile(r"#?([0-9a-fA-F]{6})")


def _color(value):
    """The parsed colour, checked before NX is asked: ``("index", n)``, ``("name",
    canonical)``, ``("rgb", [r, g, b] 0..1)`` or ``("label", text)``; None stays None."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return ("rgb", _rgb(value))
    if not isinstance(value, str):
        return ("index", value)
    text = value.strip()
    if text.isdigit():
        if not 1 <= int(text) <= display.PALETTE_SIZE:
            _bad(f"A colour index is 1 to {display.PALETTE_SIZE}, not {text}")
        return ("index", int(text))
    hexed = HEX.fullmatch(text)
    if hexed and (text.startswith("#") or any(c.isdigit() for c in text)):
        digits = hexed.group(1)
        return ("rgb", [int(digits[i : i + 2], 16) / 255.0 for i in (0, 2, 4)])
    if text.startswith("#"):
        _bad(f"{text!r} is not a #RRGGBB colour")
    name = match_option(text, tuple(display.COLOR_NAMES), tuple(COLOR_ALIASES.items()))
    if name is not None:
        return ("name", name)
    if not text:
        _bad("color is empty")
    return ("label", text)


def _rgb(value) -> list[float]:
    """[r, g, b] as 0..1: 0-255 when any component is above 1."""
    if len(value) != 3 or not all(isinstance(v, (int, float)) for v in value):
        _bad("An RGB colour is [r, g, b], three numbers")
    values = [float(v) for v in value]
    if any(v > 1.0 for v in values):
        values = [v / 255.0 for v in values]
    if not all(0.0 <= v <= 1.0 for v in values):
        _bad(f"RGB components are 0-255 (or 0-1), not {list(value)}")
    return values


def _bad(message: str):
    fail("NX_INVALID_ARGUMENT", message, field="/color", hint=display.COLOR_FORMS)


def _percent(value):
    """Transparency 0..100 from an integer or a percent string ('40%', '40 %', '40')."""
    if not isinstance(value, str):
        return value
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*%?\s*", value)
    number = float(match.group(1)) if match else -1.0
    if not 0 <= number <= 100:
        fail(
            "NX_INVALID_ARGUMENT",
            f"Transparency {value!r} is not a percentage 0-100",
            field="/transparency",
            hint="Send an integer 0 (opaque) to 100 (clear), e.g. 40 or '40%'",
        )
    return round(number)


@tool(
    effect="view",
    label="Set highlight",
    summary=lambda a, say: (
        f"Highlight {say('objects')}" if a.get("objects") else "Clear highlights"
    ),
    keywords=("highlight", "select", "clear highlights", "emphasize", "point out"),
    requires=("work_part",),
    cost="light",
    example={"objects": ["fc:88"]},
)
def nx_set_highlight(
    tx: Tx,
    *,
    objects: Annotated[list[Annotated[str, RefTo(*TARGET_KINDS)]], Items(0, 1000)],
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
