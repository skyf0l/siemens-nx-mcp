"""Part lifecycle (tool-surface 3.2): create, open, activate, list, save, close.

Ownership (gate 6): a part the agent creates or opens (not already loaded) is
the agent's; saving or closing any other part needs the engineer's grant
(``tx.require_owned``). Parts outside the workspace are refused first, since
no grant can help there. NX names parts by file basename, so a new file name a
loaded part already uses is refused before NX acts (``NX_NAME_CONFLICT``).

Closing an assembly also closes the component parts it leaves unused that the agent
created or opened (``unload_components``, default true): NX 2406 unloads prototypes
the assembly loaded itself, but keeps parts loaded on their own (a cover and a bolt the
agent created before assembling them stayed loaded after the kit closed, round 2). A
component part is kept (``kept_components`` with the reason) when the agent did not
create or open it, another loaded assembly uses it, it was the work or display part
before the call, or it has unsaved changes (never saved or discarded implicitly).
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path
from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import REF_PATTERN, Doc, Offset, PageLimit, PathTo, RefTo, Text, Units
from nx_mcp.domains.parts_files import _saving
from nx_mcp.kit import files, parts, readback, result
from nx_mcp.kit import units as kit_units

TEMPLATES = {"mm": "model-plain-1-mm-template.prt", "in": "model-plain-1-inch-template.prt"}
# A Doc placed before PathTo/RefTo wins over their generic "Workspace path"/"Ref: part".
PartPath = Annotated[str, Doc("New workspace .prt path"), PathTo("new", ".prt")]
PartArg = Annotated[str, RefTo("part", doc="Ref 'p:N' from nx_list_parts")]
# A ref, or a loaded part's file (matched against the loaded parts only; nothing is read).
LoadedPart = Annotated[
    str, Text(260), Doc("Ref 'p:N' from nx_list_parts, or a loaded part's workspace path or name")
]
# Save-as target. "replace" lets the part's own file through gate 2 (an in-place save);
# any other existing file is refused in the handler: save-as never overwrites.
SaveAsPath = Annotated[
    str, Doc("New .prt to save as; omit to save in place"), PathTo("replace", ".prt")
]
REF_HINT = "Use the part's current ref from nx_list_parts; a reopened part gets a new ref"


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
    other_modified_parts: list[result.Row]  # loaded parts left with unsaved changes
    verified_parts: int
    updated_views: list[result.Row]  # out-of-date drawing views updated before the save


class KeptPart(TypedDict):
    part: result.Row
    path: str
    reason: str


class CloseData(TypedDict, total=False):
    closed: list[result.Row]
    closed_count: int
    remaining_count: int
    saved: bool
    unloaded_components: list[result.Row]
    kept_components: list[KeptPart]


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


def _report_units(tx) -> None:
    """Make the envelope's units block describe the work part this call left (gate 9
    read the units of the work part before the call)."""
    work = tx.session.Parts.Work
    tx.units["length"] = kit_units.part_units(tx, work) if work is not None else None


def _loaded_rows(tx, limit: int = 10) -> list[dict]:
    rows = []
    for p in list(tx.session.Parts)[:limit]:
        with contextlib.suppress(Exception):
            rows.append({**tx.ref(p, "part"), "path": _rel(tx, parts.file_of(p))})
    return rows


def resolve_part(tx, ref: str, field: str = "/part"):
    """The loaded part ``ref`` names. A stale or unknown part ref fails with a hint to
    nx_list_parts and ``details.loaded_parts`` (the current refs, at most 10)."""
    try:
        return tx.resolve(ref, "part")
    except NXError as error:
        if error.category in ("stale_reference", "not_found"):
            error.hint = REF_HINT
            error.field = error.field or field
            with contextlib.suppress(Exception):
                error.details.setdefault("loaded_parts", _loaded_rows(tx))
        raise


def loaded_part(tx, text: str, field: str = "/part"):
    """The loaded part a ref, workspace path or file name names. A path is matched against
    the loaded parts' files; a bare file name (``plate`` or ``plate.prt``) against their
    file names, which NX keeps unique."""
    if re.match(REF_PATTERN, text):
        return resolve_part(tx, text, field)
    name = text if Path(text).suffix else text + ".prt"
    try:
        target = Path(tx.workspace.resolve(name))
    except Exception as error:
        reserved = any(p.casefold() == ".nx-mcp" for p in Path(name).parts)
        fail(
            "NX_PATH_RESERVED" if reserved else "NX_PATH_OUTSIDE_WORKSPACE",
            f"{field}: {error}",
            field=field,
            details={"path": text},
        )
    part = parts.loaded(tx, target)
    if part is None and len(Path(name).parts) == 1:
        key = name.casefold()
        named = [p for p in tx.session.Parts if Path(parts.file_of(p)).name.casefold() == key]
        part = named[0] if len(named) == 1 else None
    if part is None:
        fail(
            "NX_NOT_FOUND",
            f"{field}: no loaded part has the file {text!r}",
            field=field,
            hint="nx_list_parts lists the loaded parts with their refs and paths",
            details={"path": text, "loaded_parts": _loaded_rows(tx)},
        )
    return part


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


def new_part(tx, target: Path, units: str, *, display: bool = True):
    """Create and load a new part at ``target`` (NX writes it on first save); with
    ``display`` make it the work and display part, else leave both as they were.

    ``FileNew`` never displays the part itself (``MakeDisplayedPart`` false): a
    displaying ``FileNew`` drops every NX undo mark, ``parts.activate`` keeps them. The
    caller has checked the name (``parts.check_new_name``). A failed read-back closes
    the new part again, so nothing is left loaded.
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
            builder.MakeDisplayedPart = False
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
        if display:
            parts.activate(tx, part)
        actual = kit_units.part_units(tx, part)
        readback.expect(actual == units, "units", actual=actual)
        _check_active(tx, part, work=display, display=display)
    except Exception:
        if part is not None:
            _discard(tx, part)
        raise
    return part


@tool(
    target="creates",
    effect="file",
    label="Create part",
    summary=lambda a, say: (
        f"Create part {say('path')} in {a.get('units') or 'mm'}"
        + ("" if a.get("display", True) else ", not displayed")
    ),
    keywords=(
        "new part",
        "file new",
        "blank part",
        "create prt",
        "start model",
        "new assembly",
        "assembly part",
        "create assembly",
    ),
    cost="heavy",
    example={"path": "projects/demo/base.prt", "units": "mm"},
)
def nx_create_part(
    tx: Tx,
    *,
    path: PartPath,
    units: Units = "mm",
    display: Annotated[bool, Doc("false: only load it; work and display parts stay")] = True,
) -> PartData:
    """Create a new workspace part, load it and make it the work and display part (display
    false: only load it). Missing folders are created; the file is written by the first
    save."""
    target = Path(path)
    with tx.checks():
        parts.check_new_name(tx, target)
    part = new_part(tx, target, units, display=display)
    _report_units(tx)
    return _state(tx, part)


@tool(
    target="creates",
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
    path: Annotated[str, Doc("Workspace .prt path"), PathTo("read", ".prt")],
    work: bool = True,
    display: bool = True,
    load_components: Annotated[bool, Doc("Fully load every component prototype")] = False,
) -> OpenData:
    """Open a workspace .prt, or reuse it if already loaded, and make it the work and display
    part as asked. A reopened part gets a new ref."""
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
    _report_units(tx)
    out: OpenData = {**_state(tx, part), "already_loaded": already}
    if loaded_count is not None:
        out["components_loaded"] = loaded_count
    return out


@tool(
    target="arg:part",
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
    part: PartArg,
    work: bool = True,
    display: bool = True,
) -> PartData:
    """Make a loaded part the work and/or display part; nothing is closed. A new display
    part also becomes the work part."""
    if not (work or display):
        fail("NX_INVALID_ARGUMENT", "Set work or display (or both)", field="/work")
    with tx.checks():
        target = resolve_part(tx, part)
        if work and not display:
            parts.check_work_only(tx, target)
    parts.activate(tx, target, work=work, display=display)
    _check_active(tx, target, work=work, display=display)
    _report_units(tx)
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
    """List the loaded parts: file, units, work/display state, modified flag and owned (the
    agent may save or change it)."""
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


def _missing_parents(path: Path) -> list[Path]:
    """The folders above ``path`` that do not exist yet, deepest first."""
    out = []
    folder = path.parent
    while not folder.exists() and folder != folder.parent:
        out.append(folder)
        folder = folder.parent
    return out


def _save_as(tx, target, destination: Path) -> list:
    """Native Save As of ``target`` to ``destination``; returns the rows of the
    out-of-date drawing views updated first. A refusal that left the part and the disk as
    they were is not partial (folders this call made are removed again)."""
    made = _missing_parents(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    before = parts.file_of(target)
    with parts.saving(tx, target) as views:
        updated = [tx.ref(v, "drawing_view") for v in views]
        try:
            with native(default="NX_SAVE_FAILED"):
                status = target.SaveAs(str(destination))
        except NXError as error:
            try:
                unchanged = parts.same_file(parts.file_of(target), before) and not (
                    destination.exists()
                )
            except Exception:
                unchanged = False
            if not unchanged:
                tx.non_undoable("native NX save as")
                raise
            for folder in made:
                with contextlib.suppress(OSError):
                    folder.rmdir()
            error.hint = (
                "Nothing was saved. Choose a new .prt name no loaded part uses, or omit path "
                "to save the part in place"
            )
            raise
        tx.non_undoable("native NX save as")
        if status is not None and hasattr(status, "Dispose"):
            with contextlib.suppress(Exception):
                status.Dispose()
    return updated


@tool(
    target="arg:part",
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
    part: PartArg | None = None,
    path: SaveAsPath | None = None,
) -> SaveData:
    """Save a loaded part (default the work part) in place, or as a new .prt (path).
    Save-as moves the loaded part and its ref to the new file; the old file stays. Never
    overwrites; expires NX undo marks; updates out-of-date drawing views first."""
    with tx.checks():
        target = resolve_part(tx, part) if part else tx.part
        field = "/part" if part else None
        if path is not None and parts.same_file(parts.file_of(target), path):
            path = None  # the part's own file: save it in place
        parts.in_workspace(tx, target, field=field, allow_unsaved=path is not None)
        tx.require_owned(target, field=field)
        if path is not None:
            if Path(path).exists():
                fail(
                    "NX_ALREADY_EXISTS",
                    "/path: a file already exists there; save-as never overwrites",
                    field="/path",
                    hint="Choose a new .prt path, or omit path to save the part in place",
                    details={"path": str(path)},
                )
            parts.check_new_name(tx, path, ignore=target)
    tx.warn("NX_NOTE", "Saving expires NX undo marks and checkpoints")
    if path is None:
        audit = _saving.verified_save(tx, target)
        return {"part": _row(tx, target), "path": _rel(tx, parts.file_of(target)), **audit}
    destination = Path(path)
    updated = _save_as(tx, target, destination)
    readback.expect(
        parts.same_file(parts.file_of(target), destination),
        "save_as",
        actual=parts.file_of(target),
    )
    readback.expect(destination.is_file(), "save_as_file", path=_rel(tx, destination))
    readback.expect(not target.IsModified, "save_as_modified")
    out: SaveData = {
        "part": _row(tx, target),
        "path": _rel(tx, destination),
        "saved_as": True,
        "saved_files": [_rel(tx, destination)],
    }
    if updated:
        out["updated_views"] = updated
        _saving.note_updated(tx, updated)
    return out


def _close_summary(a, say) -> str:
    what = str(a.get("part") or "")
    what = say("part") if re.match(REF_PATTERN, what) else Path(what).name or what
    return f"Close {what}" + (", saving it" if a.get("save") else " without saving")


@tool(
    target="arg:part",
    effect="session",
    label="Close part",
    summary=_close_summary,
    keywords=("close", "unload part", "close file", "discard part", "close without saving"),
    cost="heavy",
    example={"part": "p:1", "save": False},
)
def nx_close_part(
    tx: Tx,
    *,
    part: LoadedPart,
    save: Annotated[bool, Doc("true saves first, false discards changes; required if modified")]
    | None = None,
    unload_components: Annotated[
        bool, Doc("Also close the unused, unmodified component parts the agent loaded")
    ] = True,
) -> CloseData:
    """Close one loaded part by ref, workspace path or file name (never implicitly the work
    part). With unsaved changes, save (true or false) is required. Refused while a loaded
    assembly uses it; its unused component parts the agent loaded close too."""
    with tx.checks():
        target = loaded_part(tx, part)
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
        if save is None and bool(target.IsModified):
            fail(
                "NX_UNSAVED_PART",
                "The part has unsaved changes: give save=true to save them or save=false "
                "to discard them",
                field="/save",
                hint="Call nx_close_part again with save=true or save=false",
                details={
                    "part": _row(tx, target),
                    "path": _rel(tx, parts.file_of(target)),
                    "modified": True,
                },
            )
    save = bool(save)
    session_parts = tx.session.Parts
    active = {int(p.Tag) for p in (session_parts.Work, session_parts.Display) if p is not None}
    prototypes = _prototypes(target) if unload_components else []
    closed = _saving.close_part(tx, target, save=save)
    rows = list(closed["closed"])
    out: CloseData = {"saved": bool(save)}
    if prototypes:
        unloaded, kept = _unload_unused(tx, prototypes, active)
        rows += unloaded
        if unloaded:
            out["unloaded_components"] = unloaded
        if kept:
            out["kept_components"] = kept
    _report_units(tx)
    out["closed"] = rows
    out["closed_count"] = len(rows)
    out["remaining_count"] = len(list(tx.session.Parts))
    return out


def _prototypes(part) -> list:
    """The loaded component parts of ``part`` (every level), once each."""
    found: dict[int, object] = {}
    for component, _path in parts.components(part):
        prototype = getattr(component, "Prototype", None)
        if prototype is not None and hasattr(prototype, "Tag"):
            found.setdefault(int(prototype.Tag), prototype)
    return list(found.values())


def _created(tx, part) -> bool:
    """Created or opened by the agent (a part only granted by the engineer is not)."""
    created = getattr(tx.services, "created", None)
    return bool(created(part) if created is not None else tx.services.owns(part))


def _unload_unused(tx, prototypes, active) -> tuple[list, list]:
    """Close the component parts a closed assembly left unused that the agent loaded;
    ``(closed rows, kept rows with the reason)``. Sub-assemblies go before their parts."""
    loaded = {int(p.Tag) for p in tx.session.Parts}
    pending = [p for p in prototypes if int(p.Tag) in loaded]
    unloaded: list = []
    reasons: dict[int, str] = {}
    progress = True
    while pending and progress:
        progress = False
        for part in list(pending):
            tag = int(part.Tag)
            if tag not in {int(p.Tag) for p in tx.session.Parts}:
                pending.remove(part)  # NX unloaded it with an earlier close
                continue
            reason = None
            if not _created(tx, part):
                reason = "not created or opened by the agent"
            elif tag in active:
                reason = "work or display part before the close"
            elif parts.dependents(tx, part):
                reasons[tag] = "used by another loaded assembly"
                continue  # a parent may close later in this loop
            elif bool(getattr(part, "IsModified", False)):
                reason = "unsaved changes: close it with save=true or save=false"
            if reason is None:
                known = {int(p.Tag) for p in prototypes}
                for nested in _prototypes(part):  # a sub-assembly's parts come next
                    if int(nested.Tag) not in known:
                        prototypes.append(nested)
                        pending.append(nested)
                try:
                    unloaded.extend(_saving.close_part(tx, part, save=False)["closed"])
                    progress = True
                except Exception as error:
                    reason = f"close failed: {error}"
            if reason is not None:
                reasons[tag] = reason
            pending.remove(part)
    still = {int(p.Tag) for p in tx.session.Parts}
    kept = [
        {"part": _row(tx, p), "path": _rel(tx, parts.file_of(p)), "reason": reasons[int(p.Tag)]}
        for p in prototypes
        if int(p.Tag) in still and int(p.Tag) in reasons
    ]
    return unloaded, kept
