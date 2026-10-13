"""Export drawing sheets to PDF, CGM, DXF or DWG files in the workspace.

NX 2406 findings encoded here:

- A sheet's plotted presentation is current only after its views are updated and
  the sheet was displayed: views are updated and each exported sheet is opened
  before plotting, then the previous sheet or the modeling view is restored.
- Views NX flagged out of date (``DraftingView.IsOutOfDate``, the "(Out of Date)" sheet
  banner, e.g. a part saved with stale views and reopened) are reported in
  ``updated_views``; after the update none may remain flagged.
- DXF/DWG export (``DxfdwgCreator``, "export from displayed part") includes unsaved
  changes and never saves the part; it runs an external translator (tens of
  seconds) and writes one sheet per file.
- Assemblies must be fully loaded: partially loaded components plot empty.
- CGM (``PlotManager.CreateCgmBuilder``, ``Multiple = Single``) writes every selected
  sheet into one binary CGM file named by ``SetFilenames`` ("NX8.5 Metafile"), one
  picture per sheet named after it; the element stream is parsed from BEGIN to END
  METAFILE and the picture names are returned.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Aliases, Doc, Items, PathTo
from nx_mcp.domains.drawing.common import SheetArg, sheet_of
from nx_mcp.kit import builders, drawing, files, parts, result

HEADERS = {
    "pdf": (b"%PDF-",),
    "dxf": (b"  0\r\nSECTION", b"0\nSECTION", b"AutoCAD Binary DXF"),
    "dwg": (b"AC10",),
}


class _CgmData(TypedDict, total=False):
    pictures: Annotated[list[str], Doc("CGM only: the file's pictures, one per sheet")]


class ExportData(_CgmData):
    path: str
    format: str
    size: int
    sha256: str
    sheets: list[result.Row]
    updated_views: Annotated[list[result.Row], Doc("Views out of date before the export")]


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


def _cgm_text(raw):
    """A CGM string parameter without its ISO 2022 character-set escapes."""
    return re.sub(r"[ -/]*[0-~]", "", raw.decode("latin-1"))


def cgm_pictures(data):
    """``(metafile name, [picture names])`` of a binary CGM that opens with BEGIN METAFILE
    and closes with END METAFILE, else None. Every picture is one exported sheet."""
    elements, at = [], 0
    while at + 2 <= len(data):
        word = int.from_bytes(data[at : at + 2], "big")
        cls, element, length, at = word >> 12, (word >> 5) & 0x7F, word & 0x1F, at + 2
        body = b""
        more = length == 31
        if not more:
            body, at = data[at : at + length], at + length + (length & 1)
        while more:  # long form: partitions of up to 32767 bytes, bit 15 = more follow
            if at + 2 > len(data):
                return None
            head = int.from_bytes(data[at : at + 2], "big")
            size, more = head & 0x7FFF, bool(head & 0x8000)
            body += data[at + 2 : at + 2 + size]
            at += 2 + size + (size & 1)
        if at > len(data):
            return None
        elements.append((cls, element, body))
    if not elements or elements[0][:2] != (0, 1) or elements[-1][:2] != (0, 2):
        return None

    def name(body):
        return _cgm_text(body[1 : 1 + body[0]]) if body else ""

    return name(elements[0][2]), [name(b) for c, e, b in elements if (c, e) == (0, 3)]


def _valid(data, kind):
    if kind == "cgm":
        return cgm_pictures(data) is not None
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
    path: Annotated[str, PathTo("new", ".pdf", ".cgm", ".dxf", ".dwg")],
    format: Annotated[Literal["pdf", "cgm", "dxf", "dwg"], Aliases(acad="dwg", autocad="dwg")]  # noqa: A002
    | None = None,
    sheets: Annotated[list[SheetArg], Items(1, 50)] | None = None,
) -> ExportData:
    """Export drawing sheets of the work part to a new PDF or CGM file (all or the given
    sheets) or a DXF/DWG file (one sheet). format follows the path's extension. Unsaved
    changes are included; the part is not saved."""
    suffix = str(path).rsplit(".", 1)[-1].lower()
    kind = format or suffix
    if suffix != kind:
        fail("NX_INVALID_ARGUMENT", f"The path ends in .{suffix} but format is {kind}",
             field="/path", hint=f"Use a .{kind} path, or omit format to follow the path")  # fmt: skip
    with tx.checks():
        part = tx.part
        _check_loaded(part)
        selected = [sheet_of(tx, s, f"/sheets/{i}") for i, s in enumerate(sheets or [])]
        if not selected:
            selected = list(part.DrawingSheets)
        if not selected:
            fail("NX_WRONG_APPLICATION", "The work part has no drawing sheet",
                 hint="Create one with nx_create_drawing_sheet")  # fmt: skip
        if kind in ("dxf", "dwg"):
            if len(selected) != 1:
                which = "" if sheets else " (sheets omitted: every sheet of the work part)"
                fail(
                    "NX_INVALID_ARGUMENT",
                    f"A {kind.upper()} file holds one sheet, not {len(selected)}{which}",
                    field="/sheets",
                    hint="Pass sheets: [one sheet ref or name] and export each sheet to its "
                    f"own .{kind} file, or export all sheets to one .pdf or .cgm",
                    details={"sheets": [tx.ref(s, "drawing_sheet") for s in part.DrawingSheets]},
                )  # fmt: skip
            if tx.session.Parts.Display != part:
                fail("NX_WRONG_PART_CONTEXT", "Make the work part the display part",
                     hint="Activate it with nx_activate_part")  # fmt: skip
    temp = tx.files.stage(path)
    with parts.saving(tx, part, force_display=True, update_views=False):
        views = [v for s in selected for v in s.GetDraftingViews()]
        stale = drawing.update_out_of_date(tx, views)
        if views:
            part.DraftingViews.UpdateViews(views)
        for sheet in selected:  # refresh each sheet's presentation before plotting
            drawing.open_sheet(tx, sheet, part)
            tx.checkpoint()
        if kind == "pdf":
            _pdf(tx, part, selected, temp)
        elif kind == "cgm":
            _cgm(tx, part, selected, temp)
        else:
            _dxf(tx, part, selected[0], temp, kind)
    data = temp.read_bytes() if temp.is_file() else b""
    if not data or not _valid(data, kind):
        fail("NX_EXPORT_FAILED", f"NX did not produce a {kind.upper()} file",
             details={"size": len(data)})  # fmt: skip
    published = tx.files.publish()[0]
    meta = files.metadata(published, tx.workspace.root)
    out: ExportData = {
        "path": meta["path"],
        "format": kind,
        "size": meta["size"],
        "sha256": meta["sha256"],
        "sheets": [tx.ref(s, "drawing_sheet") for s in selected],
        "updated_views": [tx.ref(v, "drawing_view") for v in stale],
    }
    metafile = cgm_pictures(data) if kind == "cgm" else None
    if metafile is not None:
        out["pictures"] = metafile[1]
    return out


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


def _cgm(tx, part, sheets, temp):
    with builders.build(tx, part.PlotManager.CreateCgmBuilder, codes=None) as b:
        b.Action = b.ActionOption.FileBrowser
        b.Multiple = b.MultipleOption.Single
        b.Size = b.SizeOption.FullScale
        b.Units = b.UnitsOption.Metric
        b.OutputText = b.OutputTextOption.Text
        b.Colors = b.Color.AsDisplayed
        b.Widths = b.Width.StandardWidths
        b.VdcCoordinates = b.Vdc.Real
        b.RasterImages = True
        b.ShadedGeometry = False
        b.SourceBuilder.SetSheets(list(sheets))
        b.SetFilenames([str(temp)])
        b.commit()
