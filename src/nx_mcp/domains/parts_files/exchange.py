"""Neutral-format exchange (tool-surface 3.2, 4): export models.

Export never saves the engineer's part (decision 10): every translator writes the
display part from memory, so unsaved changes are included and the part's file and
modified flag stay as they were; both are verified. STEP and IGES (``StepCreator``,
``IgesCreator`` with ``ExportFrom.DisplayPart`` and ``FileSaveFlag`` false) and
Parasolid (``ParasolidExporter``, ``DisplayedPart``) read ``InputFile`` only for an
unmodified part; STL (``StlCreator``) tessellates the loaded bodies. JT
(``PvtransManager.CreateJtCreator``, monolithic) is refused for a modified part: NX 2406
writes edited bodies as last saved (a moved body stays where it was saved), new bodies
as they are. DXF/DWG (``DexManager.CreateDxfdwgCreator``, model data exported 3D, display
part, ``FileSaveFlag`` false) writes faceted faces (3DFACE in a block placed by one
INSERT, NX 2406) and needs the ``ug_to_dxf`` license (asked before NX). The translator
writes into
``.nx-mcp/staging``; the file is read back (``neutral``: schema, units, solids or
triangles, extent against the bodies) and published without overwriting anything.
STEP AP242 needs its own NX license; without it NX 2406 raises "Interrupt" (code 2) for
a modified part and writes "STEP AP242 License not found" to the syslog. Code 2 maps to
``NX_LICENSE_UNAVAILABLE`` only when the syslog lines of this call or the translator log
say so (any other interrupt is ``NX_EXPORT_FAILED``, so it is not remembered as a missing
license). Its external job for an unmodified part writes no file (``NX_EXPORT_FAILED``
with a license hint).

Import (``nx_import_model``) lives in ``importing.py``.
"""

from __future__ import annotations

import contextlib
import glob
import os
import re
import shutil
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Angle, BodyRef, Doc, Items, Length, PathTo, Range
from nx_mcp.domains.parts_files import _saving, neutral
from nx_mcp.kit import args, files, licenses, measure, parts, readback, result
from nx_mcp.kit import units as kit_units

LOG_TAIL = 4000
# Export formats and their suffixes; the Parasolid suffix picks text (.x_t) or binary.
SUFFIXES = {
    "step": (".step", ".stp"),
    "step_ap242": (".step", ".stp"),
    "stl": (".stl",),
    "iges": (".igs", ".iges"),
    "parasolid": (".x_t", ".x_b"),
    "jt": (".jt",),
    "dxf": (".dxf",),
    "dwg": (".dwg",),
}
ALL_SUFFIXES = tuple(dict.fromkeys(s for group in SUFFIXES.values() for s in group))
# NX 2406 raises "Interrupt" (code 2) when the STEP AP242 translator license is missing
# (syslog: "STEP AP242 License not found"); code 2 alone is any interrupt.
AP242_INTERRUPT_CODE = 2
LICENSE_MISSING = re.compile(r"licen[cs]e\s+(?:is\s+)?(?:not\s+found|not\s+available|unavailable)",
                             re.IGNORECASE)  # fmt: skip
SYSLOG_TAIL = 65536
# An unmodified part is translated by the external step242 job, which writes nothing then.
AP242_HINT = "STEP AP242 needs its own NX translator license; use format step (AP214)"
JT_RELATIVE_TOL = 0.005  # JT tessellation tolerance relative to the model size
DXF_RELATIVE_TOL = 0.03  # DXF facet sag relative to the model size (coarse NX facets)
DXF_LICENSE = "ug_to_dxf"  # the NX-to-DXF/DWG translator feature


class ExportOptions(TypedDict, total=False):
    """STL only; the other formats take no options."""

    binary: Annotated[bool, Doc("stl: false writes ASCII (default true)")]
    chordal_tolerance: Annotated[
        Length, Range(gt=0), Doc("stl: max chord deviation, > 0 (NX default 0.058)")
    ]
    angular_tolerance: Annotated[
        Angle, Range(gt=0, le=90), Doc("stl: max angle between facets, 0-90 (NX default 18)")
    ]


# Option keys each format accepts (only STL has any).
FORMAT_OPTIONS = {"stl": frozenset(ExportOptions.__annotations__)}
ExportFormat = Literal["step", "step_ap242", "stl", "iges", "parasolid", "jt", "dxf", "dwg"]
# The format a path suffix names when ``format`` is omitted (never step_ap242: it needs
# its own license and is chosen explicitly).
INFERRED: dict[str, ExportFormat] = {
    ".step": "step", ".stp": "step", ".stl": "stl", ".igs": "iges", ".iges": "iges",
    ".x_t": "parasolid", ".x_b": "parasolid", ".jt": "jt", ".dxf": "dxf", ".dwg": "dwg",
}  # fmt: skip
FORMAT_DOC = (
    "Default: from the path suffix (.step/.stp: AP214); AP242 needs the STEP AP242 translator "
    "license"
)


class Bounds(TypedDict):
    min: list[float]
    max: list[float]


class ExportData(TypedDict):
    path: str
    size: int
    sha256: str
    format: str
    schema: str | None
    encoding: str
    units: str
    file_units: str | None
    bodies: int
    solids: int | None
    surfaces: int | None
    shapes: int | None
    triangles: int | None
    bounds: Bounds | None
    tolerances: dict | None
    part: result.Row
    part_saved: bool
    part_modified: bool


def _settings(*names: str, code: str) -> Path:
    base = os.environ.get("UGII_BASE_DIR")
    if not base:
        fail(code, "UGII_BASE_DIR is not set; the NX translator settings cannot be found")
    path = Path(base).joinpath(*names)
    if not path.is_file():
        fail(code, f"NX translator settings file is missing: {path}")
    return path


def _syslog(tx) -> tuple[Path | None, int]:
    """NX's syslog and its current size (the lines this call writes start there)."""
    try:
        path = Path(str(tx.session.LogFile.FileName))
        return path, path.stat().st_size
    except Exception:
        return None, 0


def _license_proof(syslog: tuple[Path | None, int], log: Path) -> str | None:
    """The syslog (since ``syslog``'s offset) or translator-log line that says a
    license is missing, or None."""
    texts = []
    path, start = syslog
    if path is not None:
        try:
            with path.open("rb") as stream:
                stream.seek(max(start, path.stat().st_size - SYSLOG_TAIL))
                texts.append(stream.read().decode(errors="replace"))
        except OSError:
            pass
    texts.append(_tail(log) or "")
    for text in texts:
        for line in text.splitlines():
            if LICENSE_MISSING.search(line):
                return line.strip()[:300]
    return None


def _tail(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")[-LOG_TAIL:]
    except OSError:
        return None


def _format_of(a: dict) -> str:
    """The requested format, or the one the path suffix names (panel summary)."""
    return str(a.get("format") or INFERRED.get(Path(str(a.get("path", ""))).suffix.lower(), ""))


@tool(
    target="assembly",
    switch="display",
    effect="file",
    label="Export model",
    summary=lambda a, say: f"Export {_format_of(a).upper()} {say('path')}",
    keywords=(
        "export step",
        "ap242",
        "stl",
        "iges",
        "parasolid x_t",
        "jt",
        "3d dxf dwg",
        "neutral format",
    ),
    variants="format",
    requires=("work_part", "no_active_sketch"),
    cost="long",
    example={"path": "exports/base.step"},
)
def nx_export_model(
    tx: Tx,
    *,
    path: Annotated[str, PathTo("new", *ALL_SUFFIXES)],
    format: Annotated[ExportFormat, Doc(FORMAT_DOC)] | None = None,
    bodies: Annotated[list[BodyRef], Items(1, 200), Doc("Only these bodies")] | None = None,
    options: ExportOptions | None = None,
) -> ExportData:
    """Export the work part, or some of its bodies, to a new neutral CAD file. STEP .step/.stp
    (AP214), STL .stl, IGES .igs, Parasolid .x_t/.x_b, JT .jt (saved part only), DXF .dxf/DWG
    .dwg (3D model data; drawing sheets: nx_export_drawing); the suffix picks the format.
    Unsaved changes are exported; the part is never saved. A part named by
    part is displayed for the export, then the tab shown before comes back."""
    target = Path(path)
    suffix = target.suffix.lower()
    if format is None:
        if suffix not in INFERRED:  # gate 2 already admits only the export suffixes
            fail("NX_UNSUPPORTED_FILE_TYPE", f"Name the format: expected {'/'.join(INFERRED)}",
                 field="/path")  # fmt: skip
        format = INFERRED[suffix]
    if suffix not in SUFFIXES[format]:
        fail(
            "NX_UNSUPPORTED_FILE_TYPE",
            f"A {format} export is written to {' or '.join(SUFFIXES[format])}, "
            f"not {target.suffix or 'a path without suffix'}",
            field="/path",
            hint="Give the path the format's suffix, or omit format to use the one the "
            "suffix names",
        )
    opts = dict(options or {})
    allowed = FORMAT_OPTIONS.get(format, frozenset())
    foreign = sorted(set(opts) - allowed)
    if foreign:
        fail(
            "NX_INVALID_ARGUMENT",
            f"A {format} export takes no option {foreign[0]!r}; binary, chordal_tolerance and "
            "angular_tolerance apply to stl only",
            field=f"/options/{foreign[0]}",
            hint="Omit options for this format",
        )
    if bodies:
        args.distinct("/bodies", bodies, "bodies")
    with tx.checks():
        part = tx.part
        if not parts.same_part(tx.session.Parts.Display, part):
            fail(
                "NX_WRONG_PART_CONTEXT",
                "The export writes the display part; the work part is not displayed",
                hint="Display the part with nx_activate_part(display=true) and retry",
            )
        selected = [tx.resolve(b, "body", work_part=True) for b in bodies or []]
        exported = selected or list(part.Bodies)
        if format in ("stl", "jt") and not exported:
            fail("NX_INVALID_ARGUMENT", "The work part has no bodies to export", field="/bodies")
        if format == "jt" and part.IsModified:
            # NX 2406 JT writes edited bodies as last saved (a moved body stays put).
            fail(
                "NX_UNSAVED_PART",
                "JT export writes the saved geometry of edited bodies; the part has unsaved "
                "changes",
                hint="Save the part (nx_save_part) first, or export step, parasolid, iges or "
                "stl, which write the unsaved state",
            )
        if format in ("dxf", "dwg"):
            licenses.require(tx, (DXF_LICENSE,), "DXF/DWG export needs the NX DXF/DWG "
                             "translator license (ug_to_dxf)", by_argument=True)  # fmt: skip
        settings = _export_settings(format)
    modified = bool(part.IsModified)
    file_before = _saving.fingerprint(parts.file_of(part))
    root = Path(tx.workspace.root)
    temp = tx.files.stage(target)  # published through tx.files: a late failure is partial
    log = temp.with_suffix(".log")
    syslog = _syslog(tx) if format == "step_ap242" else (None, 0)
    try:
        builder = None
        try:
            with native(default="NX_EXPORT_FAILED"):
                create, configure = WRITERS[format]
                builder = create(tx)  # destroyed in finally even if configuring fails
                configure(tx, builder, format, part, selected, settings, temp, opts)
                builder.Commit()
        except NXError as error:
            proof = _license_proof(syslog, log) if error.nx_code == AP242_INTERRUPT_CODE else None
            if format != "step_ap242" or proof is None:
                raise
            fail("NX_LICENSE_UNAVAILABLE", f"NX refused the STEP AP242 export: {proof}",
                 nx_code=AP242_INTERRUPT_CODE, hint=AP242_HINT)  # fmt: skip
        finally:
            if builder is not None:
                with contextlib.suppress(Exception):
                    builder.Destroy()
        written = _written(temp)
        if written is None:
            fail(
                "NX_EXPORT_FAILED",
                f"The {format} translator wrote no file",
                hint=AP242_HINT if format == "step_ap242" else None,
                details={"translator_log": _tail(log)},
            )
        try:
            facts = READERS[format](written.read_bytes())
        except ValueError as error:
            readback.expect(False, "file_content", f"The exported file is unreadable: {error}")
        report = _verify(tx, format, facts, exported, selected, target, opts, log)
        # Decision 10: the engineer's part is neither saved nor changed by the export.
        readback.expect(bool(part.IsModified) == modified, "part_modified", before=modified)
        readback.expect(
            _saving.fingerprint(parts.file_of(part)) == file_before, "part_file_unchanged"
        )
        if written != temp:
            written.replace(temp)  # e.g. an ASCII STL written as <name>.stl.txt
        tx.files.publish()
    finally:
        with contextlib.suppress(OSError):
            shutil.rmtree(temp.parent)
    meta = files.metadata(target, root)
    return {
        **meta,
        "format": format,
        **report,  # type: ignore[typeddict-item]  # _verify's format-specific fields
        "bodies": len(exported),
        "part": tx.ref(part, "part"),
        "part_saved": False,
        "part_modified": bool(part.IsModified),
    }


def _export_settings(format: str) -> Path | None:
    if format in ("step", "step_ap242"):
        return _settings("STEP214UG", "ugstep214.def", code="NX_EXPORT_FAILED")
    if format == "iges":
        return _settings("IGES", "igesexport.def", code="NX_EXPORT_FAILED")
    if format in ("dxf", "dwg"):
        return _settings("DXFDWG", "dxfdwg.def", code="NX_EXPORT_FAILED")
    return None


def _select(tx, block, selected) -> None:
    """STEP, IGES and Parasolid: an ``ObjectSelector`` limited to the selected bodies."""
    if selected:
        block.SelectionScope = tx.nx.ObjectSelector.Scope.SelectedObjects
        readback.expect(block.SelectionComp.Add(selected), "selection")


def _step_writer(tx, builder, format, part, selected, settings, temp, opts):
    builder.SettingsFile = str(settings)
    # Always set the schema: a new StepCreator may start from the session's last choice.
    option = tx.nx.StepCreator.ExportAsOption
    builder.ExportAs = option.Ap242 if format == "step_ap242" else option.Ap214
    builder.ExportFrom = tx.nx.StepCreator.ExportFromOption.DisplayPart
    # NX 2406: an unmodified display part is read from InputFile ("No parts in current
    # input file" without it); a modified one is exported from memory.
    builder.InputFile = parts.file_of(part)
    _select(tx, builder.ExportSelectionBlock, selected)
    builder.LayerMask = "1-256"
    builder.ObjectTypes.Solids = True
    builder.ObjectTypes.Surfaces = True
    builder.ObjectTypes.Curves = False
    builder.OutputFile = str(temp)
    builder.FileSaveFlag = False  # never save the part before exporting
    builder.ProcessHoldFlag = True


def _iges_writer(tx, builder, format, part, selected, settings, temp, opts):
    builder.SettingsFile = str(settings)
    builder.ExportFrom = tx.nx.IgesCreator.ExportFromOption.DisplayPart
    builder.InputFile = parts.file_of(part)  # as STEP: read only when unmodified
    _select(tx, builder.ExportSelectionBlock, selected)
    builder.LayerMask = "1-256"
    builder.ObjectTypes.Solids = True
    builder.ObjectTypes.Surfaces = True
    builder.ObjectTypes.Curves = False
    builder.OutputFile = str(temp)
    builder.FileSaveFlag = False
    builder.ProcessHoldFlag = True


def _parasolid_writer(tx, builder, format, part, selected, settings, temp, opts):
    # No save flag exists: the displayed part is written from memory.
    builder.ExportFrom = tx.nx.ParasolidExporter.ExportFromOption.DisplayedPart
    builder.InputFile = parts.file_of(part)
    _select(tx, builder.ExportSelectionBlock, selected)
    builder.ObjectTypes.Solids = True
    builder.ObjectTypes.Surfaces = True
    builder.OutputFile = str(temp)
    builder.ProcessHoldFlag = True


def _stl_writer(tx, builder, format, part, selected, settings, temp, opts):
    # STL tessellates the loaded bodies; its ExportSelectionBlock is a plain object list.
    kinds = tx.nx.STLCreator.OutputTypeEnum
    builder.OutputType = kinds.Binary if opts.get("binary", True) else kinds.Text
    if opts.get("chordal_tolerance") is not None:
        builder.ChordalTol = float(opts["chordal_tolerance"])
    if opts.get("angular_tolerance") is not None:
        builder.AngularTol = float(opts["angular_tolerance"])
    readback.expect(builder.ExportSelectionBlock.Add(selected or list(part.Bodies)), "selection")
    builder.OutputFile = str(temp)
    opts["_tolerances"] = {
        "chordal": float(builder.ChordalTol),
        "angular": float(builder.AngularTol),
    }


def _jt_writer(tx, builder, format, part, selected, settings, temp, opts):
    # One monolithic file (NX's default writes an assembly file plus one file per part).
    builder.JtfileStructure = tx.nx.JtCreatorFileStructure.Monolithic
    if selected:
        readback.expect(builder.SelectBody.Add(selected), "selection")
    builder.OutputJtFile = str(temp)


def _dxf_writer(tx, builder, format, part, selected, settings, temp, opts):
    # 3D model data (not drawings): solids and surfaces of the display part, from memory.
    kinds = tx.nx.DxfdwgCreator
    builder.SettingsFile = str(settings)
    builder.ExportData = kinds.ExportDataOption.Modeling
    builder.ExportAs = kinds.ExportAsOption.ThreeD
    builder.ExportFrom = kinds.ExportFromOption.DisplayPart
    builder.OutputFileType = (
        kinds.OutputFileTypeOption.Dwg if format == "dwg" else kinds.OutputFileTypeOption.Dxf
    )
    builder.InputFile = parts.file_of(part)
    _select(tx, builder.ExportSelectionBlock, selected)
    builder.LayerMask = "1-256"
    builder.ObjectTypes.Solids = True
    builder.ObjectTypes.Surfaces = True
    builder.ObjectTypes.Curves = False
    builder.OutputFile = str(temp)
    builder.FileSaveFlag = False
    builder.ProcessHoldFlag = True


def _dex(name):
    return lambda tx: getattr(tx.session.DexManager, name)()


# format -> (create the NX translator builder, configure it)
WRITERS = {
    "step": (_dex("CreateStepCreator"), _step_writer),
    "step_ap242": (_dex("CreateStepCreator"), _step_writer),
    "iges": (_dex("CreateIgesCreator"), _iges_writer),
    "parasolid": (_dex("CreateParasolidExporter"), _parasolid_writer),
    "stl": (_dex("CreateStlCreator"), _stl_writer),
    "jt": (lambda tx: tx.session.PvtransManager.CreateJtCreator(), _jt_writer),
    "dxf": (_dex("CreateDxfdwgCreator"), _dxf_writer),
    "dwg": (_dex("CreateDxfdwgCreator"), _dxf_writer),
}
READERS = {
    "step": neutral.step_facts,
    "step_ap242": neutral.step_facts,
    "iges": neutral.iges_facts,
    "parasolid": neutral.parasolid_facts,
    "stl": neutral.stl_facts,
    "jt": neutral.jt_facts,
    "dxf": neutral.dxf_model_facts,
    "dwg": neutral.dwg_facts,
}


def _written(temp: Path) -> Path | None:
    """The file the translator wrote: ``temp``, or ``temp`` plus a suffix NX adds (an
    ASCII STL is written as ``<name>.stl.txt``)."""
    candidates = [temp, *sorted(temp.parent.glob(glob.escape(temp.name) + ".*"))]
    for candidate in candidates:
        if candidate.is_file() and candidate.suffix != ".log" and candidate.stat().st_size:
            return candidate
    return None


def _verify(tx, format, facts, exported, selected, target, opts, log) -> dict:
    """Check the file's own facts: schema, encoding, units, content and extent."""
    units = kit_units.part_units(tx)
    file_units = facts.get("units")
    report = {
        "schema": None, "encoding": "text", "units": units, "file_units": file_units,
        "solids": None, "surfaces": None, "shapes": None, "triangles": None,
        "bounds": None, "tolerances": None,
    }  # fmt: skip
    if format in ("step", "step_ap242"):
        schema = "AP242" if format == "step_ap242" else "AP214"
        readback.expect(facts["schema"] == schema, "schema", schema=facts["schema"])
        solids = facts["solids"]
        wanted = sum(1 for b in selected if getattr(b, "IsSolidBody", True))
        if solids == 0 or solids < wanted:
            fail(
                "NX_INCOMPLETE_RESULT",
                f"The STEP file holds {solids} solids; {max(wanted, 1)} expected",
                details={"translator_log": _tail(log)},
            )
        report.update(schema=schema, solids=solids)
    elif format == "iges":
        if not facts["surfaces"]:
            fail(
                "NX_INCOMPLETE_RESULT",
                "The IGES file holds no surfaces",
                details={"translator_log": _tail(log)},
            )
        report.update(schema=facts["version"], surfaces=facts["surfaces"])
    elif format == "parasolid":
        encoding = "binary" if target.suffix.lower() == ".x_b" else "text"
        readback.expect(facts["format"] == encoding, "encoding", encoding=facts["format"])
        report.update(schema=facts["schema"], encoding=encoding)
    elif format == "stl":
        if not facts["triangles"]:
            fail("NX_INCOMPLETE_RESULT", "The STL file holds no triangles")
        readback.expect(facts["binary"] == opts.get("binary", True), "encoding")
        report.update(
            encoding="binary" if facts["binary"] else "text",
            triangles=facts["triangles"],
            tolerances=opts["_tolerances"],
        )
    elif format == "dxf":
        if not (facts["solids"] or facts["faces"] or facts["meshes"]):
            fail(
                "NX_INCOMPLETE_RESULT",
                "The DXF file holds no solids, faces or meshes",
                details={"entities": facts["entities"], "translator_log": _tail(log)},
            )
        report.update(
            schema=facts["version"],
            solids=facts["solids"],
            surfaces=facts["faces"] + facts["meshes"],
            shapes=sum(facts["entities"].values()),
        )
    elif format == "dwg":
        report.update(schema=facts["version"], encoding="binary")
    else:  # jt
        if not facts["shapes"]:
            fail("NX_INCOMPLETE_RESULT", "The JT file holds no shapes")
        report.update(
            schema=facts["version"],
            encoding="binary",
            shapes=facts["shapes"],
            triangles=facts["triangles"],
        )
    if format != "parasolid" and file_units is not None:
        readback.expect(file_units == units, "file_units", file_units=file_units, part=units)
    if facts.get("min") is not None:
        report["bounds"] = _check_extent(tx, format, facts, exported, opts)
    return report


def _check_extent(tx, format, facts, exported, opts) -> dict:
    """The file's extent (STL in part units) against the bodies' box. Tessellations
    (STL, JT) lie within the chordal tolerance of the exact box; IGES control points
    enclose it. With a rotated WCS only the conservative box is known: it encloses the
    tessellation."""
    exact = measure.wcs_is_absolute(tx)
    low, high = measure.bounds(tx, exported, exact=exact)
    size = max(h - lo for lo, h in zip(low, high, strict=True))
    file_low = [float(v) for v in facts["min"]]
    file_high = [float(v) for v in facts["max"]]
    tol = kit_units.part_tolerance(tx) + 1e-6 * size
    if format == "stl":
        tol += opts["_tolerances"]["chordal"]
    elif format == "jt":
        tol += JT_RELATIVE_TOL * size
    elif format == "dxf":  # NX 2406 facets: a 20 mm cylinder's box is 0.22 mm short
        tol += DXF_RELATIVE_TOL * size
    if format == "iges" and not exact:
        ok = True  # control points and a conservative box do not bound each other
    elif format == "iges" or not exact:
        outer, inner = ((file_low, file_high), (low, high))
        if not exact:
            outer, inner = inner, outer
        ok = all(o <= i + tol for o, i in zip(outer[0], inner[0], strict=True)) and all(
            o >= i - tol for o, i in zip(outer[1], inner[1], strict=True)
        )
    else:
        pairs = zip(file_low + file_high, low + high, strict=True)
        ok = all(abs(a - b) <= tol for a, b in pairs)
    readback.expect(
        ok,
        "extent",
        f"The {format} file's extent does not match the exported bodies",
        file_min=file_low,
        file_max=file_high,
        model_min=low,
        model_max=high,
        tolerance=tol,
    )
    return {"min": file_low, "max": file_high}
