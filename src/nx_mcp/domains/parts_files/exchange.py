"""Neutral-format exchange (tool-surface 3.2, 4): export and import models.

Export never saves the engineer's part (decision 10): STEP is written from the
display part in memory (``StepCreator`` ``ExportFrom.DisplayPart``), so unsaved
changes are included and the part's file and modified flag stay as they were;
both are verified. The translator writes into ``.nx-mcp/staging`` and the result
is published without overwriting anything.

Import uses the NX ``Step214Importer`` into the work part (NX 2406 needs
``OutputFile`` set even then); ``new_part`` first creates and activates the new
part. Translator files stay in ``imports/<operation_id>/`` (prototypes of an
unflattened assembly are loaded from there). Settings files come from
``UGII_BASE_DIR``.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import classify, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, PathTo, RefTo
from nx_mcp.domains.parts_files import _saving
from nx_mcp.domains.parts_files.parts import new_part
from nx_mcp.kit import args, files, parts, readback, result
from nx_mcp.kit import units as kit_units

SOLID_RECORDS = ("MANIFOLD_SOLID_BREP", "BREP_WITH_VOIDS", "FACETED_BREP")
LOG_TAIL = 4000


class ExportData(TypedDict):
    path: str
    size: int
    sha256: str
    format: str
    schema: str
    units: str
    solids: int
    bodies: int
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


def _solid_count(path: Path) -> int:
    text = path.read_text(errors="replace")
    return sum(len(re.findall(r"\b" + name + r"\s*\(", text)) for name in SOLID_RECORDS)


@tool(
    effect="file",
    label="Export model",
    summary=lambda a, say: f"Export {str(a.get('format', '')).upper()} {say('path')}",
    keywords=("export step", "step file", "ap214", "neutral format", "translate out"),
    variants="format",
    requires=("work_part", "no_active_sketch"),
    cost="long",
    example={"path": "exports/base.step", "format": "step"},
)
def nx_export_model(
    tx: Tx,
    *,
    path: PathTo("new", ".step", ".stp"),  # type: ignore[valid-type]
    format: Literal["step"],
    bodies: Annotated[list[RefTo("body")], Items(1, 200), Doc("Only these bodies")]  # type: ignore[valid-type]
    | None = None,
) -> ExportData:
    """Export the work part, or some of its bodies, to a new STEP (AP214) file. Unsaved
    changes are exported as they are; the part is never saved. The work part must also be
    the display part."""
    target = Path(path)
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
        settings = _settings("STEP214UG", "ugstep214.def", code="NX_EXPORT_FAILED")
    modified = bool(part.IsModified)
    file_before = _saving.fingerprint(parts.file_of(part))
    root = Path(tx.workspace.root)
    temp = files.temp_path(root, target.name, tx.operation_id)
    tx.touched = True
    try:
        builder = None
        try:
            with native(default="NX_EXPORT_FAILED"):
                builder = tx.session.DexManager.CreateStepCreator()
                builder.SettingsFile = str(settings)
                builder.ExportFrom = tx.nx.StepCreator.ExportFromOption.DisplayPart
                # NX 2406: an unmodified display part is read from InputFile ("No parts in
                # current input file" without it); a modified one is exported from memory.
                builder.InputFile = parts.file_of(part)
                if selected:
                    block = builder.ExportSelectionBlock
                    block.SelectionScope = tx.nx.ObjectSelector.Scope.SelectedObjects
                    readback.expect(block.SelectionComp.Add(selected), "selection")
                builder.LayerMask = "1-256"
                builder.ObjectTypes.Solids = True
                builder.ObjectTypes.Surfaces = True
                builder.ObjectTypes.Curves = False
                builder.OutputFile = str(temp)
                builder.FileSaveFlag = False  # never save the part before exporting
                builder.ProcessHoldFlag = True
                builder.Commit()
        finally:
            if builder is not None:
                with contextlib.suppress(Exception):
                    builder.Destroy()
        log = temp.with_suffix(".log")
        if not temp.is_file() or temp.stat().st_size == 0:
            fail(
                "NX_EXPORT_FAILED",
                "The STEP translator wrote no file",
                details={"translator_log": _tail(log)},
            )
        solids = _solid_count(temp)
        wanted = sum(1 for b in selected if getattr(b, "IsSolidBody", True))
        if solids == 0 or solids < wanted:
            fail(
                "NX_INCOMPLETE_RESULT",
                f"The STEP file holds {solids} solids; {max(wanted, 1)} expected",
                details={"translator_log": _tail(log)},
            )
        # Decision 10: the engineer's part is neither saved nor changed by the export.
        readback.expect(bool(part.IsModified) == modified, "part_modified", before=modified)
        readback.expect(
            _saving.fingerprint(parts.file_of(part)) == file_before, "part_file_unchanged"
        )
        files.publish_new(temp, target)
    finally:
        with contextlib.suppress(OSError):
            shutil.rmtree(temp.parent)
    meta = files.metadata(target, root)
    return {
        "path": meta["path"],
        "size": meta["size"],
        "sha256": meta["sha256"],
        "format": format,
        "schema": "AP214",
        "units": kit_units.part_units(tx, part),
        "solids": solids,
        "bodies": len(selected) if selected else len(list(part.Bodies)),
        "part": tx.ref(part, "part"),
        "part_saved": False,
        "part_modified": bool(part.IsModified),
    }


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
