"""Drawing sheets: create from a standard size, a custom size or a template; activate."""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Name, PathTo, Range, RefTo, Text, Units
from nx_mcp.kit import drawing, readback, result, select
from nx_mcp.kit import units as kit_units

SIZES = {
    "a0": (1189.0, 841.0),
    "a1": (841.0, 594.0),
    "a2": (594.0, 420.0),
    "a3": (420.0, 297.0),
    "a4": (297.0, 210.0),
}
PROJECTIONS = {"first_angle": "First", "third_angle": "Third"}
SIZE_TOL_MM = 0.5


TemplateName = Annotated[str, Text(64, r"^[\w-]+$"), Doc("Installed template, e.g. 'A3'")]


class SheetData(TypedDict):
    sheet: result.Row
    size: str | None
    dimensions: list[float]
    units: str
    scale: float
    projection: str
    template: str | None
    title_block_added: bool


def _installed(name):
    """The installed NX sheet template ``name`` (A0-A4 or a full template name)."""
    text = name.strip()
    if text.lower() in SIZES:
        text = f"Drawing-{text.upper()}-Size2D-template"
    base = os.environ.get("UGII_BASE_DIR", "")
    candidate = Path(base) / "DRAFTING" / "templates" / (text + ".prt") if base else None
    if candidate is None or not candidate.is_file():
        fail(
            "NX_NOT_FOUND",
            f"No installed NX drawing template {name!r} in UGII_BASE_DIR/DRAFTING/templates",
            hint="Use 'A3' or a name such as 'Drawing-A3-Size2D-template'",
            field="/template",
        )
    return candidate


def _standard(dims_mm):
    return next(
        (
            k
            for k, (w, h) in SIZES.items()
            if abs(dims_mm[0] - w) <= SIZE_TOL_MM and abs(dims_mm[1] - h) <= SIZE_TOL_MM
        ),
        None,
    )


def _projection(tx, sheet):
    third = tx.nx.Drawings.DrawingSheet.ProjectionAngleType.ThirdAngle
    return "third_angle" if sheet.ProjectionAngle == third else "first_angle"


def _summary(args, say):
    what = say("size") if isinstance(args.get("size"), str) else "custom size"
    if args.get("template") or args.get("template_path"):
        what = "from template"
    return f"Drawing sheet {args['name']} {what}" if args.get("name") else f"Drawing sheet {what}"


@tool(
    effect="model",
    label="Create drawing sheet",
    summary=_summary,
    keywords=("drawing", "new sheet", "drafting", "title block", "border", "paper size"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    invalidates="deleted",
    example={"name": "SHT1", "size": "a3", "units": "mm", "projection": "first_angle"},
)
def nx_create_drawing_sheet(
    tx: Tx,
    *,
    name: Name | None = None,
    size: Literal["a0", "a1", "a2", "a3", "a4"]
    | Annotated[tuple[float, float], Doc("[width, height]")]
    | None = None,
    scale: Annotated[float, Range(gt=0, le=1000), Doc("Sheet scale, e.g. 2 = 2:1")] = 1.0,
    units: Units = "mm",
    projection: Literal["first_angle", "third_angle"] = "third_angle",
    template: TemplateName | None = None,
    template_path: PathTo("read", ".prt") | None = None,  # type: ignore[valid-type]
) -> SheetData:
    """Create a drawing sheet from a standard or custom size or a sheet template, and open
    it. Default A3; the installed A0-A4 templates are metric."""
    custom = list(size) if isinstance(size, (list, tuple)) else None
    if custom is not None and not (custom[0] > 0 and custom[1] > 0):
        fail("NX_INVALID_ARGUMENT", "width and height must be positive", field="/size")
    if template is not None and template_path is not None:
        fail("NX_INVALID_ARGUMENT", "Give template or template_path", field="/template_path")
    if (template or template_path) and custom is not None:
        fail("NX_INVALID_ARGUMENT", "A template sets its own size", field="/size")
    installed = template
    if installed and installed.lower() in SIZES and units != "mm":
        fail(
            "NX_INVALID_ARGUMENT",
            "The installed A0-A4 template sheets are metric: use units mm",
            field="/units",
        )
    with tx.checks():
        part = tx.part
        existing = list(part.DrawingSheets)
        if name is None:
            taken = {s.Name.casefold() for s in existing}
            name = next(f"SHT{i}" for i in range(1, 1000) if f"sht{i}" not in taken)
        elif any(s.Name.casefold() == name.casefold() for s in existing):
            fail("NX_ALREADY_EXISTS", f"A drawing sheet named {name!r} exists", field="/name")
        path = _installed(installed) if installed else template_path
        before = {int(s.Tag) for s in existing}
        title_blocks = len(list(part.DraftingManager.TitleBlocks))
    b = part.DraftingDrawingSheets.CreateDraftingDrawingSheetBuilder(None)
    try:
        b.Name = name
        b.Units = b.SheetUnits.Metric if units == "mm" else b.SheetUnits.English
        if path is None:
            b.Option = b.SheetOption.CustomSize
            width, height = (
                custom
                if custom
                else [v / (25.4 if units == "in" else 1.0) for v in SIZES[size or "a3"]]
            )
            b.Length, b.Height = float(width), float(height)
        else:
            b.Option = b.SheetOption.UseTemplate
            if units == "mm":
                b.MetricSheetTemplateLocation = str(path)
            else:
                b.EnglishSheetTemplateLocation = str(path)
            b.AutoStartViewCreation = False
        b.ScaleNumerator = float(scale)
        b.ScaleDenominator = 1.0
        b.ProjectionAngle = getattr(b.SheetProjectionAngle, PROJECTIONS[projection])
        try:
            b.Commit()
        except Exception as error:
            if path is not None and "units are different" in str(error):
                raise NXError(
                    "NX_UNSUPPORTED_OPTION",
                    "The template's units differ from the work part's: use a metric template "
                    "(A0-A4) in a mm part and an inch template (e.g. Drawing-B-Size2D-template) "
                    "in an inch part",
                    field="/template",
                    nx_code=getattr(error, "ErrorCode", None),
                ) from None
            raise
    finally:
        b.Destroy()
    created = [s for s in part.DrawingSheets if int(s.Tag) not in before]
    readback.expect(len(created) == 1, "sheet_count", created=len(created))
    sheet = created[0]
    if sheet.Name != name:
        sheet.SetName(name)  # template sheets keep the template's own sheet name
    template_projection = _projection(tx, sheet) if path is not None else None
    template_scale = sheet.GetScale()
    # Templates carry their own scale and projection: apply the request explicitly.
    angle = getattr(
        tx.nx.Drawings.DrawingSheet.ProjectionAngleType, PROJECTIONS[projection] + "Angle"
    )
    sheet.SetParameters(sheet.Height, sheet.Length, float(scale), 1.0, sheet.Units, angle)
    drawing.open_sheet(tx, sheet, part)
    actual_units = kit_units.sheet_units(tx, sheet)
    dims = drawing.size(sheet)
    dims_mm = [v * (25.4 if actual_units == "in" else 1.0) for v in dims]
    numerator, denominator = sheet.GetScale()
    actual = {
        "units": actual_units,
        "scale": numerator / denominator,
        "projection": _projection(tx, sheet),
        "name": sheet.Name,
        "size": _standard(dims_mm),
    }
    wanted_size = size if isinstance(size, str) else ("a3" if path is None and not custom else None)
    readback.expect(
        actual["units"] == units
        and math.isclose(actual["scale"], scale, rel_tol=1e-10)
        and actual["projection"] == projection
        and actual["name"] == name
        and (wanted_size is None or actual["size"] == wanted_size),
        "sheet",
        "NX drawing sheet differs from the request",
        **actual,
    )
    if custom:
        readback.expect_close(dims, custom, "dimensions", 1e-6)
    if path is not None and template_projection != projection:
        tx.warn(
            "NX_UPDATE_WARNING",
            f"The template was drawn for {template_projection.replace('_', ' ')} projection; "
            "its printed projection symbol is border graphics and was not changed",
        )
    if path is not None and not math.isclose(
        template_scale[0] / template_scale[1], scale, rel_tol=1e-10
    ):
        tx.warn(
            "NX_UPDATE_WARNING",
            "The sheet scale differs from the template's; title-block scale text is unchanged",
        )
    return {
        "sheet": tx.ref(sheet, "drawing_sheet"),
        "size": actual["size"],
        "dimensions": dims,
        "units": actual_units,
        "scale": actual["scale"],
        "projection": actual["projection"],
        "template": str(path) if path is not None else None,
        "title_block_added": len(list(part.DraftingManager.TitleBlocks)) > title_blocks,
    }


class SheetRow(result.Row, total=False):
    active: bool
    size: list[float]
    units: str
    scale: float
    views: list[result.Row]


class ActivateData(TypedDict):
    sheet: result.Row | None
    modeling: bool
    sheets: list[SheetRow]


def sheet_rows(tx, part):
    current = drawing.current(part)
    out = []
    for sheet in part.DrawingSheets:
        numerator, denominator = sheet.GetScale()
        out.append(
            {
                **tx.ref(sheet, "drawing_sheet"),
                "active": current is not None and int(current.Tag) == int(sheet.Tag),
                "size": drawing.size(sheet),
                "units": kit_units.sheet_units(tx, sheet),
                "scale": numerator / denominator,
                "views": [tx.ref(v, "drawing_view") for v in sheet.GetDraftingViews()],
            }
        )
    return out


@tool(
    effect="session",
    label="Activate drawing sheet",
    summary=lambda a, say: f"Open sheet {say('sheet')}" if a.get("sheet") else "Return to modeling",
    keywords=("open drawing", "switch sheet", "show drawing", "modeling view", "drafting"),
    requires=("work_part", "no_active_sketch"),
    cost="light",
    example={"sheet": "sh:3"},
)
def nx_activate_drawing_sheet(
    tx: Tx,
    *,
    sheet: RefTo("drawing_sheet") | None = None,  # type: ignore[valid-type]
) -> ActivateData:
    """Open a drawing sheet of the work part, or return to the modeling view when sheet is
    omitted. Lists the part's sheets with their views."""
    with tx.checks():
        target = (
            select.resolve(tx, sheet, "drawing_sheet", field="/sheet", work_part=True)
            if sheet
            else None
        )
        part = tx.part
        if tx.session.Parts.Display != part:
            fail(
                "NX_WRONG_PART_CONTEXT",
                "Drawing sheets show in the display part: make the work part the display part",
                hint="Activate it with nx_activate_part",
            )
    if target is None:
        drawing.close_sheets(tx, part)
    else:
        drawing.open_sheet(tx, target, part)
    current = drawing.current(part)
    return {
        "sheet": tx.ref(current, "drawing_sheet") if current is not None else None,
        "modeling": current is None and not drawing.work_view_is_drawing(part),
        "sheets": sheet_rows(tx, part),
    }
