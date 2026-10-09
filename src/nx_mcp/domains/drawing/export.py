"""Export drawing sheets to PDF, DXF or DWG files in the workspace.

NX 2406 findings encoded here:

- A sheet's plotted presentation is current only after its views are updated and
  the sheet was displayed: views are updated and each exported sheet is opened
  before plotting, then the previous sheet or the modeling view is restored.
- DXF/DWG export (``DxfdwgCreator``, "export from displayed part") includes unsaved
  changes and never saves the part; it runs an external translator (tens of
  seconds) and writes one sheet per file.
- Assemblies must be fully loaded: partially loaded components plot empty.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Items, PathTo, RefTo
from nx_mcp.kit import builders, drawing, files, parts, result, select

HEADERS = {
    "pdf": (b"%PDF-",),
    "dxf": (b"  0\r\nSECTION", b"0\nSECTION", b"AutoCAD Binary DXF"),
    "dwg": (b"AC10",),
}


class ExportData(TypedDict):
    path: str
    format: str
    size: int
    sha256: str
    sheets: list[result.Row]


def _check_loaded(part):
    missing = [
        "/".join(names)
        for component, names in parts.components(part)
        if not component.IsSuppressed
        and not (
            parts.prototype_file(component) and getattr(component.Prototype, "IsFullyLoaded", True)
        )
    ]
    if missing:
        fail(
            "NX_NOT_LOADED",
            "Load the assembly components before exporting drawings",
            hint="Open the assembly with nx_open_part(load_components=true)",
            details={"components": missing[:50]},
        )


def _valid(data, kind):
    if kind == "dxf":
        head = data[:64].lstrip(b" \r\n")
        return any(
            data.startswith(h) or head.startswith(h.lstrip(b" \r\n")) for h in HEADERS["dxf"]
        )
    return any(data.startswith(h) for h in HEADERS[kind])


@tool(
    effect="file",
    label="Export drawing",
    summary=lambda a, say: f"Export drawing to {say('path')}",
    keywords=("pdf", "dxf", "dwg", "plot", "print drawing"),
    requires=("work_part", "no_active_sketch"),
    cost="long",
    example={"path": "out/drawing.pdf", "format": "pdf"},
)
def nx_export_drawing(
    tx: Tx,
    *,
    path: PathTo("new", ".pdf", ".dxf", ".dwg"),  # type: ignore[valid-type]
    format: Literal["pdf", "dxf", "dwg"] = "pdf",  # noqa: A002 - surface name
    sheets: Annotated[list[RefTo("drawing_sheet")], Items(1, 50)] | None = None,  # type: ignore[valid-type]
) -> ExportData:
    """Export drawing sheets of the work part to a new PDF (all or the given sheets) or a
    DXF/DWG file (one sheet). Unsaved changes are included; the part is not saved."""
    if not str(path).lower().endswith("." + format):
        fail("NX_INVALID_ARGUMENT", f"Use a .{format} path for format {format}", field="/path")
    with tx.checks():
        part = tx.part
        _check_loaded(part)
        selected = [
            select.resolve(tx, s, "drawing_sheet", field=f"/sheets/{i}", work_part=True)
            for i, s in enumerate(sheets or [])
        ]
        if not selected:
            selected = list(part.DrawingSheets)
        if not selected:
            fail("NX_WRONG_APPLICATION", "The work part has no drawing sheet",
                 hint="Create one with nx_create_drawing_sheet")  # fmt: skip
        if format != "pdf":
            if len(selected) != 1:
                fail("NX_INVALID_ARGUMENT", f"A {format.upper()} file holds one sheet",
                     field="/sheets")  # fmt: skip
            if tx.session.Parts.Display != part:
                fail("NX_WRONG_PART_CONTEXT", "Make the work part the display part",
                     hint="Activate it with nx_activate_part")  # fmt: skip
    temp = tx.files.stage(path)
    with parts.saving(tx, part, force_display=True):
        views = [v for s in selected for v in s.GetDraftingViews()]
        if views:
            part.DraftingViews.UpdateViews(views)
        for sheet in selected:  # refresh each sheet's presentation before plotting
            drawing.open_sheet(tx, sheet, part)
            tx.checkpoint()
        if format == "pdf":
            _pdf(tx, part, selected, temp)
        else:
            _dxf(tx, part, selected[0], temp, format)
    data = temp.read_bytes() if temp.is_file() else b""
    if not data or not _valid(data, format):
        fail("NX_EXPORT_FAILED", f"NX did not produce a {format.upper()} file",
             details={"size": len(data)})  # fmt: skip
    published = tx.files.publish()[0]
    meta = files.metadata(published, tx.workspace.root)
    return {
        "path": meta["path"],
        "format": format,
        "size": meta["size"],
        "sha256": meta["sha256"],
        "sheets": [tx.ref(s, "drawing_sheet") for s in selected],
    }


def _pdf(tx, part, sheets, temp):
    with builders.build(tx, part.PlotManager.CreatePrintPdfbuilder, codes=None) as b:
        b.Filename = str(temp)
        b.Action = b.ActionOption.Native
        b.Size = b.SizeOption.FullScale
        b.Units = b.UnitsOption.Metric
        b.OutputText = b.OutputTextOption.Text
        b.RasterImages = True
        b.ShadedGeometry = False
        b.ImageResolution = b.ImageResolutionOption.High
        b.Colors = b.Color.AsDisplayed
        b.SourceBuilder.SetSheets(list(sheets))
        b.commit()


def _dxf(tx, part, sheet, temp, kind):
    creator = tx.session.DexManager.CreateDxfdwgCreator
    with builders.build(tx, creator) as c:
        c.ExportData = c.ExportDataOption.Drawing
        c.ExportFrom = c.ExportFromOption.DisplayPart
        c.OutputFileType = (
            c.OutputFileTypeOption.Dxf if kind == "dxf" else c.OutputFileTypeOption.Dwg
        )
        c.InputFile = str(part.FullPath)
        c.OutputFile = str(temp)
        c.FileSaveFlag = False
        c.ProcessHoldFlag = True
        c.DrawingList = str(sheet.Name)
        c.commit()
