"""Sheet borders and zones: the frame, the zone markings and labels, and the margins.

NX 2406 findings encoded here (``DraftingManager.BordersAndZonesObjects``, reached as
``part.Drafting``):

- A sheet has at most one ``BordersAndZones`` (``DrawingSheet.BordersAndZones``); a new
  builder on a sheet that has one replaces it, so the existing one is edited instead.
  The collection itself enumerates nothing.
- The builder's own margin/zone properties are deprecated since NX 10: the settings live
  in ``BorderAndZoneStyle``: ``SheetMarginSettingsStyle`` (trimmed margins = the outer
  frame), ``SheetBorderSettingsStyle`` (``CreateBorders``; ``BorderLineWidth`` = the
  band between the outer and the inner frame) and ``SheetZoneSettingsStyle`` (zone sizes,
  origin corner, markings, labels).
- NX draws the frames as lines and the zone labels as notes in the sheet's own view
  (``View.AskVisibleObjects``); they are read back from there. NX rounds the number of
  zones to an even count (ISO 5457), so the zones it draws are about the requested size.
- ``UF ModlGeneral.AskBoundingBox`` on a ``BordersAndZones`` crashes NX: never asked.
"""

from __future__ import annotations

import contextlib
import string
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, SheetLength
from nx_mcp.domains.drawing.common import SheetArg, open_or_only_sheet, sheet_of, update
from nx_mcp.kit import builders, drawing, licenses, readback, result

ORIGINS = {
    "bottom_right": "BottomRight",
    "bottom_left": "BottomLeft",
    "top_left": "TopLeft",
    "top_right": "TopRight",
}
MARGINS = ("LeftTrimmedMargin", "BottomTrimmedMargin", "RightTrimmedMargin", "TopTrimmedMargin")
MAX_MM = 200.0


class BordersData(TypedDict, total=False):
    sheet: result.Row
    borders: bool
    zones: bool
    margins: Annotated[list[float], Doc("[left, bottom, right, top] in sheet units")]
    border_width: float
    outer: Annotated[list[float], Doc("Outer frame [x0, y0, x1, y1] on the sheet")]
    inner: Annotated[list[float], Doc("Inner frame [x0, y0, x1, y1]: the drawing area")]
    zone_size: Annotated[list[float], Doc("Requested [horizontal, vertical] zone size")]
    zone_origin: str
    columns: Annotated[list[str], Doc("Zone labels along the sheet, as NX drew them")]
    rows: Annotated[list[str], Doc("Zone labels up the sheet, as NX drew them")]
    replaced: Annotated[bool, Doc("True when the sheet had borders and zones before")]


def _existing(sheet):
    with contextlib.suppress(Exception):  # NX raises when the sheet has none
        found = sheet.BordersAndZones
        if found is not None and int(found.Tag):
            return found
    return None


def _read(tx, b, sh):
    style = b.BorderAndZoneStyle
    border = style.SheetBorderSettingsStyle
    zone = style.SheetZoneSettingsStyle
    margin = style.SheetMarginSettingsStyle
    origin = result.enum_name(zone.Origin, tx.nx.Drawings.ZoneOrigin, list(ORIGINS.values()))
    return {
        "borders": bool(border.CreateBorders),
        "zones": bool(zone.CreateZones),
        "margins": [float(getattr(margin, name)) / sh.scale for name in MARGINS],
        "border_width": float(border.BorderLineWidth) / sh.scale,
        "zone_size": [float(zone.HorizontalSize) / sh.scale, float(zone.VerticalSize) / sh.scale],
        "zone_origin": next((k for k, v in ORIGINS.items() if v == origin), origin),
    }


def _shown(tx, sh):
    """``(lines, labels)`` NX shows in the sheet's own view: line ends on the sheet and
    ``{tag: text}`` of the one-line notes."""
    lines, labels = [], {}
    try:
        shown = list(sh.sheet.View.AskVisibleObjects())
    except Exception:  # a sheet whose own view NX cannot list
        return lines, labels
    for obj in shown:
        kind = type(obj).__name__
        if kind == "Line":
            lines.append((sh.to_sheet(obj.StartPoint), sh.to_sheet(obj.EndPoint)))
        elif kind == "Note" and hasattr(obj, "GetText"):
            text = [str(t) for t in obj.GetText()]
            if len(text) == 1:
                labels[int(obj.Tag)] = text[0].strip()
    return lines, labels


def _has_frame(lines, box, tol):
    """True when four lines draw the rectangle ``box``."""
    x0, y0, x1, y1 = box
    sides = [((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))]

    def same(p, q):
        return abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol

    return all(
        any((same(a, s) and same(b, e)) or (same(a, e) and same(b, s)) for a, b in lines)
        for s, e in sides
    )


def _labels(before, after):
    """Zone labels among the notes NX added (``{tag: text}`` not in ``before``): digits
    (columns) and letters (rows), each sorted and distinct. NX draws new label notes each
    time the borders are set."""
    new = [text for tag, text in after.items() if tag not in before]
    columns = sorted({t for t in new if t.isdigit()}, key=int)
    rows = sorted({t for t in new if len(t) == 1 and t in string.ascii_uppercase})
    return columns, rows


def _check_values(zone_size, margins, border_width, length, height):
    """Sizes that fit the sheet (sheet units), before NX."""
    limit = min(length, height)
    for field, values in (("zone_size", zone_size), ("margins", margins)):
        for i, value in enumerate(values or []):
            if not 0 < float(value) <= limit / 2:
                fail("NX_INVALID_ARGUMENT", f"{field} values must be > 0 and at most half the "
                     f"sheet's smaller side ({limit / 2:g})", field=f"/{field}/{i}")  # fmt: skip
    if margins is not None and (
        float(margins[0]) + float(margins[2]) >= length
        or float(margins[1]) + float(margins[3]) >= height
    ):
        fail("NX_INVALID_ARGUMENT", "The margins leave no drawing area", field="/margins")
    if border_width is not None and not 0 < float(border_width) <= limit / 4:
        fail("NX_INVALID_ARGUMENT", f"border_width must be > 0 and at most {limit / 4:g}",
             field="/border_width")  # fmt: skip


@tool(
    effect="model",
    label="Set sheet borders and zones",
    summary=lambda a, say: (
        "Set borders and zones" + (f" on {say('sheet')}" if a.get("sheet") else "")
    ),
    keywords=("border", "zones", "zone markings", "frame", "margins", "sheet border", "ISO 5457"),
    requires=("work_part", "no_active_sketch"),
    cost="light",
    batchable=True,
    invalidates="deleted",
    example={"sheet": "SHT1", "zone_size": [50, 50], "margins": [20, 10, 10, 10]},
)
def nx_set_sheet_borders(
    tx: Tx,
    *,
    sheet: SheetArg | None = None,
    borders: Annotated[bool, Doc("Draw the outer and inner frame")] = True,
    zones: Annotated[bool, Doc("Draw zone markings and labels (1, 2, ... and A, B, ...)")] = True,
    zone_size: Annotated[
        list[SheetLength],
        Items(2, 2),
        Doc("[horizontal, vertical] zone size; default NX's (50 mm)"),
    ]
    | None = None,
    zone_origin: Literal["bottom_right", "bottom_left", "top_left", "top_right"] | None = None,
    margins: Annotated[
        list[SheetLength], Items(4, 4), Doc("[left, bottom, right, top] sheet edge to outer frame")
    ]
    | None = None,
    border_width: Annotated[SheetLength, Doc("Gap between outer and inner frame")] | None = None,
) -> BordersData:
    """Add or replace a sheet's border frame, zone markings and margins. Omitted values keep
    NX's defaults (new) or the current ones (replaced). Read back: the outer and inner frame
    and the zone labels NX drew; NX rounds the zone count to an even number, so zones are
    about zone_size. zone_origin: the corner where labels 1 and A start. Title blocks:
    nx_create_drawing_table."""
    with tx.checks():
        target = sheet_of(tx, sheet, "/sheet") if sheet is not None else open_or_only_sheet(tx)
        length, height = drawing.size(target)
        _check_values(zone_size, margins, border_width, length, height)
    licenses.require(tx, ("drafting",), "Borders and zones need a drafting license")
    D = tx.nx.Drawings
    collection = tx.part.Drafting.BordersAndZonesObjects
    with drawing.on_sheet(tx, target) as sh:
        existing = _existing(target)
        _lines, labels_before = _shown(tx, sh)
        with builders.build(tx, collection.CreateBordersAndZonesBuilder,
                            existing if existing is not None else D.BordersAndZones.Null) as b:  # fmt: skip
            style = b.BorderAndZoneStyle
            border = style.SheetBorderSettingsStyle
            zone = style.SheetZoneSettingsStyle
            margin = style.SheetMarginSettingsStyle
            border.CreateBorders = bool(borders)
            zone.CreateZones = bool(zones)
            zone.CreateZoneMarkings = bool(zones)
            zone.CreateZoneLabels = bool(zones)
            if zone_size is not None:
                zone.HorizontalSize = float(zone_size[0]) * sh.scale
                zone.VerticalSize = float(zone_size[1]) * sh.scale
            if zone_origin is not None:
                zone.Origin = getattr(D.ZoneOrigin, ORIGINS[zone_origin])
            if margins is not None:
                for name, value in zip(MARGINS, margins, strict=True):
                    setattr(margin, name, float(value) * sh.scale)
            if border_width is not None:
                border.BorderLineWidth = float(border_width) * sh.scale
            made = b.commit()
        if made is None:
            fail("NX_BUILD_FAILED", "NX returned no borders and zones")
        update(tx)
        with builders.build(tx, collection.CreateBordersAndZonesBuilder, made) as reader:
            got = _read(tx, reader, sh)
        wanted: dict[str, object] = {"borders": bool(borders), "zones": bool(zones)}
        if zone_origin is not None:
            wanted["zone_origin"] = zone_origin
        readback.expect(all(got[k] == v for k, v in wanted.items()), "settings",
                        "NX stored other border settings", actual=got)  # fmt: skip
        sizes: tuple[tuple[str, list | None], ...] = (
            ("zone_size", zone_size),
            ("margins", margins),
        )
        for key, given in sizes:
            if given is not None:
                readback.expect_close(got[key], [float(v) for v in given], key, 1e-6)
        if border_width is not None:
            readback.expect_close(got["border_width"], float(border_width), "border_width", 1e-6)
        lines, labels_after = _shown(tx, sh)
        left, bottom, right, top = got["margins"]
        outer = [left, bottom, length - right, height - top]
        w = got["border_width"]
        inner = [outer[0] + w, outer[1] + w, outer[2] - w, outer[3] - w]
        tol = 1e-3 * max(1.0, length, height)
        if got["borders"]:
            readback.expect(_has_frame(lines, outer, tol) and _has_frame(lines, inner, tol),
                            "frame", "NX drew no frame at the margins", outer=outer,
                            inner=inner)  # fmt: skip
        columns, rows = _labels(labels_before, labels_after) if got["zones"] else ([], [])
        if got["zones"]:
            readback.expect(bool(columns) and bool(rows), "zones", "NX drew no zone labels")
        out: BordersData = {
            "sheet": tx.ref(target, "drawing_sheet"),
            **got,
            "outer": outer,
            "inner": inner,
            "columns": columns,
            "rows": rows,
            "replaced": existing is not None,
        }
        return out
