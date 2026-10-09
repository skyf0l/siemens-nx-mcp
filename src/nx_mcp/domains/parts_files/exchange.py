"""Neutral-format exchange (tool-surface 3.2, 4): export and import models.

Export never saves the engineer's part (decision 10): every translator writes the
display part from memory, so unsaved changes are included and the part's file and
modified flag stay as they were; both are verified. STEP and IGES (``StepCreator``,
``IgesCreator`` with ``ExportFrom.DisplayPart`` and ``FileSaveFlag`` false) and
Parasolid (``ParasolidExporter``, ``DisplayedPart``) read ``InputFile`` only for an
unmodified part; STL (``StlCreator``) tessellates the loaded bodies. JT
(``PvtransManager.CreateJtCreator``, monolithic) is refused for a modified part: NX 2406
writes edited bodies as last saved (a moved body stays where it was saved), new bodies
as they are. The translator writes into
``.nx-mcp/staging``; the file is read back (``kit.neutral``: schema, units, solids or
triangles, extent against the bodies) and published without overwriting anything.
STEP AP242 needs its own NX license; without it NX 2406 raises "Interrupt" (code 2,
mapped to ``NX_LICENSE_UNAVAILABLE``) for a modified part, and its external job for an
unmodified part writes no file (``NX_EXPORT_FAILED`` with a license hint).

Import uses the NX ``Step214Importer`` into the work part (NX 2406 needs
``OutputFile`` set even then); ``new_part`` first creates and activates the new
part. Translator files stay in ``imports/<operation_id>/`` (prototypes of an
unflattened assembly are loaded from there). Settings files come from
``UGII_BASE_DIR``.
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
from nx_mcp.core.errors import classify, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Angle, ByVariant, Doc, Items, Json, Length, PathTo, RefTo
from nx_mcp.domains.parts_files import _saving
from nx_mcp.domains.parts_files.parts import new_part
from nx_mcp.kit import args, files, measure, neutral, parts, readback, result
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
}
ALL_SUFFIXES = tuple(dict.fromkeys(s for group in SUFFIXES.values() for s in group))
# NX 2406 raises "Interrupt" (code 2) when the STEP AP242 translator license is missing
# (syslog: "STEP AP242 License not found").
AP242_LICENSE_CODE = 2
# An unmodified part is translated by the external step242 job, which writes nothing then.
AP242_HINT = "STEP AP242 needs its own NX translator license; use format step (AP214)"
JT_RELATIVE_TOL = 0.005  # JT tessellation tolerance relative to the model size


class NoOptions(TypedDict, total=False):
    """This format has no options."""


class StlOptions(TypedDict, total=False):
    binary: Annotated[bool, Doc("false writes ASCII STL (default true)")]
    chordal_tolerance: Annotated[Length, Doc("Max chord deviation (NX default 0.058)")]  # type: ignore[valid-type]
    angular_tolerance: Annotated[Angle, Doc("Max angle between facets (NX default 18)")]  # type: ignore[valid-type]


OPTION_FORMS = {
    "step": NoOptions,
    "step_ap242": NoOptions,
    "stl": StlOptions,
    "iges": NoOptions,
    "parasolid": NoOptions,
    "jt": NoOptions,
}
Options = Annotated[
    dict,
    Json(max_keys=3),
    ByVariant("format", OPTION_FORMS),
    Doc("Per-format options (only stl has any): nx_get_tool_schemas(tool#format)"),
]


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


class ImportData(TypedDict):
    part: result.Row
    target: str
    bodies: list[result.Row]
    body_count: int
    components_created: int
    flatten: bool
    staging: str


def _settings(*names: str, code: str) -> Path:
    base = os.environ.get("UGII_BASE_DIR")
    if not base:
        fail(code, "UGII_BASE_DIR is not set; the NX translator settings cannot be found")
    path = Path(base).joinpath(*names)
    if not path.is_file():
        fail(code, f"NX translator settings file is missing: {path}")
    return path


def _tail(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")[-LOG_TAIL:]
    except OSError:
        return None


@tool(
    effect="file",
    label="Export model",
    summary=lambda a, say: f"Export {str(a.get('format', '')).upper()} {say('path')}",
    keywords=("export step", "ap242", "stl", "iges", "parasolid x_t", "jt", "neutral format"),
    variants="format",
    requires=("work_part", "no_active_sketch"),
    cost="long",
    example={"path": "exports/base.step", "format": "step"},
)
def nx_export_model(
    tx: Tx,
    *,
    path: PathTo("new", *ALL_SUFFIXES),  # type: ignore[valid-type]
    format: Literal["step", "step_ap242", "stl", "iges", "parasolid", "jt"],
    bodies: Annotated[list[RefTo("body")], Items(1, 200), Doc("Only these bodies")]  # type: ignore[valid-type]
    | None = None,
    options: Options = {},  # noqa: B006 - never mutated; None would be Optional on Python 3.10
) -> ExportData:
    """Export the work part, or some of its bodies, to a new neutral CAD file. STEP/AP242
    .step, STL .stl, IGES .igs, Parasolid .x_t/.x_b, JT .jt (saved part only). Unsaved
    changes are exported; the part is never saved. Work part = display part."""
    target = Path(path)
    if target.suffix.lower() not in SUFFIXES[format]:
        fail(
            "NX_UNSUPPORTED_FILE_TYPE",
            f"A {format} export is written to {' or '.join(SUFFIXES[format])}, "
            f"not {target.suffix or 'a path without suffix'}",
            field="/path",
        )
    opts = dict(options or {})
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
        settings = _export_settings(format)
    modified = bool(part.IsModified)
    file_before = _saving.fingerprint(parts.file_of(part))
    root = Path(tx.workspace.root)
    temp = files.temp_path(root, target.name, tx.operation_id)
    tx.touched = True
    try:
        builder = None
        codes = {AP242_LICENSE_CODE: "NX_LICENSE_UNAVAILABLE"} if format == "step_ap242" else None
        try:
            with native(codes, default="NX_EXPORT_FAILED"):
                create, configure = WRITERS[format]
                builder = create(tx)  # destroyed in finally even if configuring fails
                configure(tx, builder, format, part, selected, settings, temp, opts)
                builder.Commit()
        finally:
            if builder is not None:
                with contextlib.suppress(Exception):
                    builder.Destroy()
        log = temp.with_suffix(".log")
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
        files.publish_new(written, target)
    finally:
        with contextlib.suppress(OSError):
            shutil.rmtree(temp.parent)
    meta = files.metadata(target, root)
    return {
        "path": meta["path"],
        "size": meta["size"],
        "sha256": meta["sha256"],
        "format": format,
        **report,  # type: ignore[typeddict-item]
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
    return None


def _select(tx, block, selected) -> None:
    """STEP, IGES and Parasolid: an ``ObjectSelector`` limited to the selected bodies."""
    if selected:
        block.SelectionScope = tx.nx.ObjectSelector.Scope.SelectedObjects
        readback.expect(block.SelectionComp.Add(selected), "selection")


def _step_writer(tx, builder, format, part, selected, settings, temp, opts):
    builder.SettingsFile = str(settings)
    if format == "step_ap242":
        builder.ExportAs = tx.nx.StepCreator.ExportAsOption.Ap242
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
}
READERS = {
    "step": neutral.step_facts,
    "step_ap242": neutral.step_facts,
    "iges": neutral.iges_facts,
    "parasolid": neutral.parasolid_facts,
    "stl": neutral.stl_facts,
    "jt": neutral.jt_facts,
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


def _check_step(source: Path) -> str:
    text = source.read_text(errors="replace")
    framing = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL).strip("﻿ \t\r\n")
    if not re.match(r"ISO-10303-21\s*;", framing, re.IGNORECASE) or not re.search(
        r"END-ISO-10303-21\s*;\s*$", framing, re.IGNORECASE
    ):
        fail(
            "NX_INVALID_ARGUMENT",
            "The STEP file lacks its ISO-10303-21 opening or closing marker (truncated?)",
            field="/path",
            hint="Upload the complete STEP file; no translator was started",
        )
    return text


def _product_names(text: str) -> set[str]:
    names = set()
    pattern = r"PRODUCT\s*\(\s*'((?:[^']|'')*)'\s*,\s*'((?:[^']|'')*)'"
    for pair in re.findall(pattern, text, re.IGNORECASE):
        names.update(Path(v.replace("''", "'")).stem.casefold() for v in pair)
    return names


def _diagnostics(folder: Path, settings: Path, root) -> dict:
    out = []
    for f in sorted(folder.iterdir()) if folder.is_dir() else []:
        if f.is_file() and f.name != "input.step":
            row = {"path": files.relative(f, root), "size": f.stat().st_size}
            if f.suffix.lower() in (".log", ".err", ".txt"):
                row["text"] = _tail(f)
            out.append(row)
    return {"translator": "Step214Importer", "settings": str(settings), "files": out}


@tool(
    effect="model",
    label="Import model",
    summary=lambda a, say: f"Import {str(a.get('format', '')).upper()} {say('path')}",
    keywords=("import step", "read step", "open step", "neutral import", "translate in"),
    variants="format",
    requires=("no_active_sketch",),
    cost="long",
    invalidates="deleted",
    new_part="new_part_path",
    example={"path": "inbox/bracket.step", "format": "step"},
)
def nx_import_model(
    tx: Tx,
    *,
    path: PathTo("read", ".step", ".stp"),  # type: ignore[valid-type]
    format: Literal["step"],
    target: Literal["work_part", "new_part"] = "work_part",
    new_part_path: PathTo("new", ".prt") | None = None,  # type: ignore[valid-type]
    flatten: Annotated[bool, Doc("false keeps the assembly structure as components")] = True,
) -> ImportData:
    """Import a STEP file into the work part, or into a new part (new_part_path) that becomes
    the work part. flatten false keeps assembly structure as components."""
    if (target == "new_part") != (new_part_path is not None):
        fail(
            "NX_INVALID_ARGUMENT",
            "Give new_part_path exactly when target is new_part",
            field="/new_part_path",
        )
    source = Path(path)
    text = _check_step(source)
    settings = _settings("STEP214UG", "step214ug.def", code="NX_IMPORT_FAILED")
    root = Path(tx.workspace.root)
    with tx.checks():
        if not flatten and "NEXT_ASSEMBLY_USAGE_OCCURRENCE" in text.upper():
            names = _product_names(text)
            loaded = [
                files.relative(parts.file_of(p), root)
                for p in tx.session.Parts
                if Path(parts.file_of(p)).stem.casefold() in names
            ]
            if loaded:
                fail(
                    "NX_NAME_CONFLICT",
                    "Parts with the STEP product names are already loaded",
                    field="/path",
                    hint="Close those parts, or import with flatten true",
                    details={"loaded_parts": loaded},
                )
        if new_part_path is not None:
            parts.check_new_name(tx, new_part_path, field="/new_part_path")
        elif tx.session.Parts.Work is None:
            fail("NX_NO_WORK_PART", "No work part is open", hint="Use target new_part")
    loaded_before = {int(p.Tag) for p in tx.session.Parts}
    previous = (tx.session.Parts.Work, tx.session.Parts.Display)
    folder = root / "imports" / (tx.operation_id or "import")
    try:
        if new_part_path is not None:
            new_part(tx, Path(new_part_path), "mm")
        part = tx.part
        bodies_before = {int(b.Tag) for b in part.Bodies}
        components_before = {int(c.Tag) for c, _ in parts.components(part)}
        folder.mkdir(parents=True, exist_ok=False)
        staged = folder / "input.step"
        shutil.copyfile(source, staged)
        builder = None
        try:
            with native(default="NX_IMPORT_FAILED"):
                builder = tx.session.DexManager.CreateStep214Importer()
                builder.SettingsFile = str(settings)
                builder.InputFile = str(staged)
                # NX 2406 validates OutputFile even when importing into the work part.
                builder.OutputFile = str(folder / "output.prt")
                builder.ImportTo = tx.nx.Step214Importer.ImportToOption.WorkPart
                builder.FileOpenFlag = False
                builder.FlattenAssembly = flatten
                builder.SimplifyGeometry = False
                builder.ObjectTypes.Solids = True
                builder.ObjectTypes.Surfaces = True
                builder.ObjectTypes.Curves = True
                builder.ProcessHoldFlag = True
                builder.Commit()
        except Exception as error:
            failure = classify(error, default="NX_IMPORT_FAILED")
            failure.details.update(_diagnostics(folder, settings, root))
            raise failure from error
        finally:
            if builder is not None:
                with contextlib.suppress(Exception):
                    builder.Destroy()
        bodies = [b for b in part.Bodies if int(b.Tag) not in bodies_before]
        added = [c for c, _ in parts.components(part) if int(c.Tag) not in components_before]
        if not bodies and not added:
            fail(
                "NX_INCOMPLETE_RESULT",
                "The translator imported no bodies or components",
                details=_diagnostics(folder, settings, root),
            )
    except BaseException:
        _undo_import(tx, loaded_before if new_part_path else None, previous, folder)
        raise
    return {
        "part": tx.ref(part, "part"),
        "target": target,
        "bodies": [tx.ref(b, "body") for b in bodies[:200]],
        "body_count": len(bodies),
        "components_created": len(added),
        "flatten": flatten,
        "staging": files.relative(folder, root),
    }


def _undo_import(tx, loaded_before: set | None, previous: tuple, folder: Path) -> None:
    """After a failed import: close a new part (and what it loaded), reactivate the
    previous parts and remove the translator files. Into the work part, the kernel's
    undo removes the bodies and components; prototypes it loaded stay loaded."""
    problems = []
    if loaded_before is not None:
        for p in reversed(list(tx.session.Parts)):
            if int(p.Tag) not in loaded_before:
                try:
                    _saving.close_part(tx, p, save=False)
                except Exception as error:
                    problems.append(f"close {parts.file_of(p)}: {error}")
        work, display = previous
        with contextlib.suppress(Exception):
            if display is not None:
                parts.activate(tx, display, work=False)
            if work is not None:
                parts.activate(tx, work, display=False)
    in_use = [
        parts.file_of(p)
        for p in tx.session.Parts
        if parts.file_of(p) and Path(parts.file_of(p)).resolve().is_relative_to(folder.resolve())
    ]
    if in_use:
        problems.append(f"translator parts in {folder.name} are still loaded")
    elif folder.exists():
        try:
            shutil.rmtree(folder)
        except OSError as error:
            problems.append(f"remove {folder.name}: {error}")
    if problems:
        tx.non_undoable("; ".join(problems))
