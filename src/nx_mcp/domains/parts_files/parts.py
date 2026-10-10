"""Part lifecycle (tool-surface 3.2): create, open, activate, list, save, close.

Ownership (gate 6): a part the agent creates or opens (not already loaded) is
the agent's; saving or closing any other part needs the engineer's grant
(``tx.require_owned``). Parts outside the workspace are refused first, since
no grant can help there. NX names parts by file basename, so a new file name a
loaded part already uses is refused before NX acts (``NX_NAME_CONFLICT``).
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Offset, PageLimit, PartRef, PathTo, Text, Units
from nx_mcp.domains.parts_files import _saving
from nx_mcp.kit import files, parts, readback, result
from nx_mcp.kit import units as kit_units

TEMPLATES = {"mm": "model-plain-1-mm-template.prt", "in": "model-plain-1-inch-template.prt"}
PartPath = Annotated[str, PathTo("new", ".prt")]


class PartData(TypedDict):
    part: result.Row
    path: str
    units: str
    work: bool
    display: bool


class OpenData(PartData, total=False):
    already_loaded: bool
    components_loaded: int


class PartRow(TypedDict):
    part: result.Row
    path: str
    units: str
    work: bool
    display: bool
    modified: bool
    owned: bool


class PartsData(TypedDict):
    items: list[PartRow]
    page: dict


class SaveData(TypedDict, total=False):
    part: result.Row
    path: str
    saved_as: bool
    saved_files: list[str]
    changed_files: list[str]
    other_modified_parts: int
    verified_parts: int


class CloseData(TypedDict):
    closed: list[result.Row]
    closed_count: int
    remaining_count: int
    saved: bool


def _row(tx, part) -> result.Row:
    return tx.ref(part, "part")


def _rel(tx, path) -> str:
    return files.relative(path, tx.workspace.root)


def _state(tx, part) -> PartData:
    session_parts = tx.session.Parts
    return {
        "part": _row(tx, part),
        "path": _rel(tx, parts.file_of(part)),
        "units": kit_units.part_units(tx, part),
        "work": parts.same_part(session_parts.Work, part),
        "display": parts.same_part(session_parts.Display, part),
    }


def _discard(tx, part) -> None:
    """Close a part this call created after a later step failed (cleanup)."""
    try:
        _saving.close_part(tx, part, save=False)
    except Exception as error:
        tx.non_undoable(f"the new part could not be closed again: {error}")


def _check_active(tx, part, *, work: bool, display: bool) -> None:
    session_parts = tx.session.Parts
    if work:
        readback.expect(parts.same_part(session_parts.Work, part), "work_part")
    if display:
        readback.expect(parts.same_part(session_parts.Display, part), "display_part")


def new_part(tx, target: Path, units: str):
    """Create, load and activate a new part at ``target`` (NX writes it on first save).

    The caller has checked the name (``parts.check_new_name``). A failed read-back
    closes the new part again, so nothing is left loaded.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    before = {int(p.Tag) for p in tx.session.Parts}
    builder = None
    created = None
    try:
        with native(default="NX_SAVE_FAILED"):
            builder = tx.session.Parts.FileNew()
            builder.TemplateFileName = TEMPLATES[units]
            builder.Units = (
                tx.nx.Part.Units.Millimeters if units == "mm" else tx.nx.Part.Units.Inches
            )
            builder.NewFileName = str(target)
            builder.DisplayPartOption = tx.nx.DisplayPartOption.AllowAdditional
            created = builder.Commit()
    finally:
        if builder is not None:
            with contextlib.suppress(Exception):
                builder.Destroy()
    part = created if created is not None and hasattr(created, "FullPath") else None
    part = part or next((p for p in tx.session.Parts if int(p.Tag) not in before), None)
    try:
        readback.expect(part is not None, "created", path=str(target))
        readback.expect(
            parts.same_file(parts.file_of(part), target), "path", actual=parts.file_of(part)
        )
        # NX 2406: FileNew without a display part leaves the display part empty.
        parts.activate(tx, part)
        actual = kit_units.part_units(tx, part)
        readback.expect(actual == units, "units", actual=actual)
        _check_active(tx, part, work=True, display=True)
    except Exception:
        if part is not None:
            _discard(tx, part)
        raise
    return part


@tool(
    effect="file",
    label="Create part",
    summary=lambda a, say: f"Create part {say('path')} in {a.get('units') or 'mm'}",
    keywords=("new part", "file new", "blank part", "create prt", "start model"),
    cost="heavy",
    example={"path": "projects/demo/base.prt", "units": "mm"},
)
def nx_create_part(
    tx: Tx,
    *,
    path: PartPath,
    units: Units = "mm",
) -> PartData:
    """Create a new workspace part, load it and make it the work and display part. Missing
    folders are created; the file is written by the first save."""
    target = Path(path)
    with tx.checks():
        parts.check_new_name(tx, target)
    return _state(tx, new_part(tx, target, units))


@tool(
    effect="session",
    label="Open part",
    summary="Open part {path}",
    keywords=("open file", "load part", "reuse part", "open assembly", "load components"),
    cost="heavy",
    example={"path": "projects/demo/base.prt"},
)
def nx_open_part(
    tx: Tx,
    *,
    path: Annotated[str, PathTo("read", ".prt")],
    work: bool = True,
    display: bool = True,
    load_components: Annotated[bool, Doc("Fully load every component prototype")] = False,
) -> OpenData:
    """Open a workspace part, or reuse it when it is already loaded, and make it the work and
    display part as asked. load_components fully loads every unsuppressed component."""
    source = Path(path)
    with tx.checks():
        part = parts.loaded(tx, source)
        if part is None:
            parts.check_new_name(tx, source)
        if work and not display:
            parts.check_work_only(tx, part)
    already = part is not None
    if part is None:
        with native():
            part, status = tx.session.Parts.OpenBase(str(source))
        if status is not None and hasattr(status, "Dispose"):
            with contextlib.suppress(Exception):
                status.Dispose()
    parts.activate(tx, part, work=work, display=display)
    loaded_count = parts.load_components(tx, part) if load_components else None
    _check_active(tx, part, work=work, display=display)
    out: OpenData = {**_state(tx, part), "already_loaded": already}
    if loaded_count is not None:
        out["components_loaded"] = loaded_count
    return out


@tool(
    effect="session",
    label="Activate part",
    summary="Activate part {part}",
    keywords=("switch part", "work part", "display part", "make work", "change part"),
    cost="light",
    example={"part": "p:1"},
)
def nx_activate_part(
    tx: Tx,
    *,
    part: PartRef,
    work: bool = True,
    display: bool = True,
) -> PartData:
    """Make a loaded part the work and/or display part; nothing is closed. A new display
    part also becomes the work part."""
    if not (work or display):
        fail("NX_INVALID_ARGUMENT", "Set work or display (or both)", field="/work")
    with tx.checks():
        target = tx.resolve(part, "part")
        if work and not display:
            parts.check_work_only(tx, target)
    parts.activate(tx, target, work=work, display=display)
    _check_active(tx, target, work=work, display=display)
    return _state(tx, target)


@tool(
    effect="read",
    label="List loaded parts",
    summary=lambda a, say: (
        "List "
        + {True: "modified ", False: "saved "}.get(a.get("modified"), "")
        + ("active " if a.get("active_only") else "")
        + "parts in NX"
    ),
    keywords=("open parts", "loaded parts", "session parts", "modified parts", "part list"),
    cost="light",
    example={"modified": True},
)
def nx_list_parts(
    tx: Tx,
    *,
    path_prefix: Annotated[str, Text(260), Doc("Workspace-relative or absolute path start")]
    | None = None,
    modified: Annotated[bool, Doc("Only modified (true) or unmodified (false) parts")]
    | None = None,
    active_only: Annotated[bool, Doc("Only the work and display parts")] = False,
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> PartsData:
    """List the loaded parts with file, units, work/display state and modified flag. owned
    says whether the agent may save or change the part."""
    session_parts = tx.session.Parts
    work, display = session_parts.Work, session_parts.Display
    needle = (path_prefix or "").replace("\\", "/").casefold()
    found = []
    for p in session_parts:
        full = parts.file_of(p)
        rel = _rel(tx, full)
        if needle and not any(
            text.replace("\\", "/").casefold().startswith(needle) for text in (rel, full)
        ):
            continue
        if modified is not None and bool(p.IsModified) != modified:
            continue
        active = parts.same_part(p, work) or parts.same_part(p, display)
        if active_only and not active:
            continue
        found.append(p)
    selected, page = result.page(found, offset, limit)
    items: list[PartRow] = [
        {
            "part": _row(tx, p),
            "path": _rel(tx, parts.file_of(p)),
            "units": kit_units.part_units(tx, p),
            "work": parts.same_part(p, work),
            "display": parts.same_part(p, display),
            "modified": bool(p.IsModified),
            "owned": bool(tx.services.owns(p)),
        }
        for p in selected
    ]
    return {"items": items, "page": page}


def _save_summary(args, say) -> str:
    what = say("part") if args.get("part") else "work part"
    return f"Save {what} as {say('path')}" if args.get("path") else f"Save {what}"


@tool(
    effect="file",
    label="Save part",
    summary=_save_summary,
    keywords=("save", "save as", "write part", "store part", "save copy"),
    cost="heavy",
    example={"path": "projects/demo/base_v2.prt"},
)
def nx_save_part(
    tx: Tx,
    *,
    part: PartRef | None = None,
    path: Annotated[str, PathTo("new", ".prt")] | None = None,
) -> SaveData:
    """Save a loaded part (default the work part) in place, or as a new workspace .prt
    (path). The save is verified against every loaded file; nothing is overwritten. Saving
    expires NX undo marks."""
    with tx.checks():
        target = tx.resolve(part, "part") if part else tx.part
        field = "/part" if part else None
        parts.in_workspace(tx, target, field=field, allow_unsaved=path is not None)
        tx.require_owned(target, field=field)
        if path is not None:
            parts.check_new_name(tx, path, ignore=target)
    tx.warn("NX_UPDATE_WARNING", "Saving expires NX undo marks and checkpoints")
    if path is None:
        audit = _saving.verified_save(tx, target)
        return {"part": _row(tx, target), "path": _rel(tx, parts.file_of(target)), **audit}
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tx.non_undoable("native NX save as")
    with parts.saving(tx, target):
        with native(default="NX_SAVE_FAILED"):
            status = target.SaveAs(str(destination))
        if status is not None and hasattr(status, "Dispose"):
            with contextlib.suppress(Exception):
                status.Dispose()
    readback.expect(
        parts.same_file(parts.file_of(target), destination),
        "save_as",
        actual=parts.file_of(target),
    )
    readback.expect(destination.is_file(), "save_as_file", path=_rel(tx, destination))
    readback.expect(not target.IsModified, "save_as_modified")
    return {
        "part": _row(tx, target),
        "path": _rel(tx, destination),
        "saved_as": True,
        "saved_files": [_rel(tx, destination)],
    }


@tool(
    effect="session",
    label="Close part",
    summary=lambda a, say: (
        f"Close {say('part')}" + ("" if a.get("save", True) else " without saving")
    ),
    keywords=("close", "unload part", "close file", "discard part", "close without saving"),
    cost="heavy",
    example={"part": "p:1", "save": False},
)
def nx_close_part(
    tx: Tx,
    *,
    part: Annotated[PartRef, Doc("The part to close: its Ref from nx_list_parts or nx_open_part")],
    save: bool = True,
) -> CloseData:
    """Close one loaded part, named by its Ref (never implicitly the work part), saving it
    first unless save is false. Refused while a loaded assembly uses it; NX may also
    unload prototypes nobody uses."""
    with tx.checks():
        target = tx.resolve(part, "part")
        field = "/part"
        parts.in_workspace(tx, target, field=field)
        tx.require_owned(target, field=field)
        parents = parts.dependents(tx, target)
        if parents:
            fail(
                "NX_PART_IN_USE",
                "A loaded assembly uses this part; close the assembly first",
                field=field,
                hint="Close the assemblies in details.parent_assemblies with nx_close_part",
                details={"parent_assemblies": [_rel(tx, p) for p in parents]},
            )
    closed = _saving.close_part(tx, target, save=save)
    return {
        "closed": closed["closed"],
        "closed_count": len(closed["closed"]),
        "remaining_count": closed["remaining_count"],
        "saved": bool(save),
    }
