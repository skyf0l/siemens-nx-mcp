"""Display snapshots and presentation files: restore a ``restore_id``, save and restore a
presentation (camera, sections, appearance) as workspace JSON (tool-surface 3.12).

Snapshots (``kit.display.snapshots``) are restored newest first per part, so a show
restored before the hide it followed cannot hide the object again (NX 2406 lesson).
After a restore every recorded object is read back and must equal the saved blank
state, colour and transparency. Presentation objects are located by kind, journal
identifier and component path, so they survive a save and reopen of the part.

``restore_id`` ``last`` (or ``latest``, or no argument at all) restores the newest display
change of the shown part. Applying a presentation is itself restorable: its result has a
``restore_id`` that puts the camera and the recorded appearance back (not the section).
Restoring a ``restore_id`` returns none (no redo), so ``last`` walks back one change at a
time.

A presentation stores its part's path relative to the workspace (absolute only for a part
outside it) and applies to that part, or to a part of the same file name (a copy or a
moved workspace: objects are found by journal identifier). ``skip_missing`` applies what
is still there and names the objects it skipped.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, PathTo, Text
from nx_mcp.domains.display.common import Camera, DisplayRow, camera_row, display_rows
from nx_mcp.domains.display.sections import guarded
from nx_mcp.kit import args, bodies, display, files, geom, parts, readback, select

FORMAT, VERSION = "nx-mcp-presentation", 2
MAX_FILE = 8 * 1024 * 1024
LATEST = ("last", "latest")
RestoreId = Annotated[str, Text(64, r"^(display_[0-9a-f]{32}|last|latest)$")]


class RestoreData(TypedDict, total=False):
    restored: str
    objects: list[DisplayRow]
    count: int
    camera: Camera
    sections: int
    identical: bool


class PresentationData(TypedDict):
    path: str
    size: int
    sha256: str
    objects: int
    sections: int


# ---- objects by locator -----------------------------------------------------------------------


def _jid(obj) -> str:
    return str(getattr(obj, "JournalIdentifier", "") or "")


def _index(part) -> dict:
    """``{(kind, journal id, component path): obj}`` of everything a presentation records."""
    out: dict = {}

    def add(kind, obj, path):
        out.setdefault((kind, _jid(obj), path), obj)

    for body in part.Bodies:
        add("body", body, None)
        for face in body.GetFaces():
            add("face", face, None)
    for component, names in parts.components(part):
        path = "/".join(names)
        add("component", component, path)
        for body in bodies.occurrences(component):
            add("body", body, path)
            for face in body.GetFaces():
                add("face", face, path)
    return out


def _check(state: dict, saved: dict, check: str, tag: int) -> None:
    for key, value in display.state_of(saved).items():
        if key == "transparency" and (value is None or state.get(key) is None):
            continue
        readback.expect(state.get(key) == value, check, tag=tag, key=key, actual=state.get(key),
                        wanted=value)  # fmt: skip


def _expect_records(tx, pairs, appearance: bool) -> None:
    """Every object shows the saved blank state, colour and transparency again."""
    now = {int(o.Tag): r for o, r in tx.services.display_records([o for o, _ in pairs], appearance)}
    for obj, saved in pairs:
        state = now.get(int(obj.Tag))
        if state is not None:
            _check(state, saved, "restored", int(obj.Tag))


def _apply(tx, pairs) -> None:
    for obj, record in pairs:
        if "color_index" in record:
            display.apply_appearance(tx, [obj], record["color_index"], record.get("transparency"))
        (obj.Blank if record["blanked"] else obj.Unblank)()


# ---- restore -------------------------------------------------------------------------------------


@tool(
    effect="view",
    label="Undo display change or load presentation",
    summary=lambda a, say: (
        f"Restore presentation {say('presentation')}"
        if a.get("presentation")
        else "Restore the display"
    ),  # fmt: skip
    keywords=("undo display", "restore view", "presentation", "reset appearance", "unhide"),
    requires=("work_part",),
    cost="light",
    restorable=True,
    example={"restore_id": "last"},
)
def nx_restore_display(
    tx: Tx,
    *,
    restore_id: RestoreId | None = None,
    presentation: Annotated[str, PathTo("read", ".json")] | None = None,
    skip_missing: Annotated[bool, Doc("Presentation: apply what still exists")] = False,
) -> RestoreData:
    """Undo a display change by restore_id ('last' or none: the newest), or apply an
    nx_save_presentation file. Newest first; a presentation fits its part or one of that
    file name. nx_set_camera, nx_set_visibility, nx_set_appearance and a presentation
    return restore_ids; sections do not (nx_delete_objects)."""
    if restore_id is not None and presentation is not None:
        fail("NX_INVALID_ARGUMENT", "Give restore_id or presentation, not both",
             field="/presentation")  # fmt: skip
    if skip_missing and presentation is None:
        fail("NX_INVALID_ARGUMENT", "skip_missing goes with presentation",
             field="/skip_missing")  # fmt: skip
    if presentation is not None:
        return _restore_presentation(tx, Path(presentation), skip_missing)
    with tx.checks():
        part = display.shown_part(tx, work=True)
        key = tx.services.part_key(part)
        if restore_id is None or restore_id in LATEST:
            restore_id = display.snapshots.latest(key)
            if restore_id is None:
                fail(
                    "NX_NOT_FOUND",
                    "No display change left to restore in the work part",
                    field="/restore_id",
                    hint="Camera, visibility and appearance changes return a restore_id; "
                    "to apply a saved view give presentation=<file.json>",
                )
        snapshot = display.snapshots.get(restore_id, key)
        pairs = [
            (select.resolve(tx, display.record_id(r), field="/restore_id"), r)
            for r in snapshot["records"]
        ]
        camera = snapshot.get("camera")
        view = display.model_view(tx, part) if camera is not None else None
    appearance = any("color_index" in r for _, r in pairs)
    tx.display.keep([o for o, _ in pairs], appearance=appearance)
    if camera is not None:
        tx.display.keep_camera()
    tx.services.restore_display(camera, pairs, None)
    _expect_records(tx, pairs, appearance)
    out: RestoreData = {"restored": restore_id, "count": len(pairs), "identical": True}
    if camera is not None:
        got = display.camera(tx, view)
        display.expect_camera(got, camera.get("rotation"), camera.get("origin"),
                              camera.get("scale"))  # fmt: skip
        out["camera"] = camera_row(got)
        tx.frame = "display_part_absolute"
    out["objects"] = display_rows(
        tx, tx.services.display_records([o for o, _ in pairs], appearance)
    )
    display.snapshots.drop(restore_id)
    # No new restore_id for a restore_id restore: ``last`` then walks back one change at
    # a time instead of toggling between two states.
    tx.display.forget()
    return out


def _save_row(saved: list, where: dict, obj, record: dict) -> None:
    key = where.get(int(obj.Tag))
    if key is None:
        return
    kind, jid, component = key
    row = display.state_of(record)
    row["locator"] = {"kind": kind, "journal_id": jid, "component": component}
    saved.append(row)


def _read(path: Path) -> dict:
    if path.stat().st_size > MAX_FILE:
        fail("NX_REQUEST_TOO_LARGE", "The presentation exceeds 8 MiB", field="/presentation")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError):
        data = None
    if (
        not isinstance(data, dict)
        or data.get("format") != FORMAT
        or data.get("version") != VERSION
        or not isinstance(data.get("display"), list)
        or not isinstance(data.get("camera"), dict)
    ):
        fail(
            "NX_UNSUPPORTED_FILE_TYPE",
            "Not a presentation saved by nx_save_presentation",
            field="/presentation",
        )
    if len(data["display"]) > display.MAX_RECORDS:
        fail("NX_TOO_MANY_OBJECTS", "The presentation lists too many objects",
             field="/presentation")  # fmt: skip
    return data


def _same_part(tx, saved, part) -> bool:
    """Whether a presentation saved for ``saved`` (workspace-relative, or absolute) applies
    to ``part``: the same file, or a file of the same name (a copy, a moved workspace)."""
    text = str(saved or "")
    if not text:
        return False
    own = parts.file_of(part)
    full = Path(text) if Path(text).is_absolute() else Path(tx.workspace.root) / text
    if parts.same_file(str(full), own):
        return True
    return Path(text.replace("\\", "/")).name.casefold() == Path(str(own)).name.casefold()


def _restore_presentation(tx, path: Path, skip_missing: bool = False) -> RestoreData:
    data = _read(path)
    with tx.checks():
        part = display.shown_part(tx, work=True)
        view = display.model_view(tx, part)
        if not _same_part(tx, data.get("part_path"), part):
            fail(
                "NX_WRONG_PART_CONTEXT",
                "The presentation belongs to another part",
                field="/presentation",
                hint="Apply it with that part displayed (nx_activate_part), or a part of the "
                "same file name",
                details={"part_path": data.get("part_path")},
            )
        index = _index(part)
        pairs, missing = [], []
        for row in data["display"]:
            loc = row.get("locator") or {}
            obj = index.get((loc.get("kind"), loc.get("journal_id"), loc.get("component")))
            if obj is None:
                missing.append(loc)
            else:
                pairs.append((obj, row))
        if missing and not skip_missing:
            fail(
                "NX_STALE_REFERENCE",
                f"{len(missing)} saved objects no longer exist in the part",
                field="/presentation",
                hint="Add skip_missing=true to apply the rest, or save the presentation again",
                details={"missing": missing[:20]},
            )
        cam = data["camera"]
        rotation = geom.rotation(cam.get("rotation"), "/presentation")
        scale = args.number(cam.get("scale"), "/presentation", positive=True)
        origin = args.vec3(cam.get("origin"), "/presentation")
    tx.display.keep([o for o, _ in pairs], appearance=True)
    tx.display.keep_camera()
    with guarded(tx, part, view):
        _apply(tx, pairs)
        section = data.get("section")
        if section:
            current = view.ActiveDynamicSection
            made = display.write_section(
                tx, part, view, section=current, origin=section["origin"],
                normal=section["normal"], cap=bool(section.get("cap", True)),
                name=None if current is not None else "Presentation section",
            )  # fmt: skip
            display.set_sectioning(view, made, True)
        if not data.get("sectioning"):
            view.DisplaySectioningToggle = False
        style = cam.get("style")
        if style in display.STYLES:
            display.set_style(tx, view, style)
        got = display.set_camera(tx, view, rotation, origin, scale)
        _expect_records(tx, pairs, True)
    tx.frame = "display_part_absolute"
    if missing:
        tx.warn("NX_NOTE", f"{len(missing)} saved objects no longer exist and were skipped: "
                + ", ".join(str(m.get("journal_id")) for m in missing[:10]))  # fmt: skip
    return {
        "restored": files.relative(path, tx.workspace.root),
        "count": len(pairs),
        "camera": camera_row(got),
        "sections": len(display.sections(part)),
        "identical": True,
    }


# ---- save ------------------------------------------------------------------------------------------


@tool(
    effect="file",
    label="Save view and appearance to JSON",
    summary=lambda a, say: f"Save the view and appearance to {say('path')}",
    keywords=("save view", "save display", "presentation file", "view state", "bookmark view"),
    requires=("work_part",),
    cost="light",
    example={"path": "views/review.json"},
)
def nx_save_presentation(
    tx: Tx,
    *,
    path: Annotated[str, PathTo("replace", ".json")],
    overwrite: bool = False,
) -> PresentationData:
    """Save the camera, active section and every body and component's appearance to a .json
    file. overwrite=true replaces one; nx_restore_display(presentation=...) applies it
    again. Lights and decals are not saved."""
    if Path(path).exists() and not overwrite:
        fail(
            "NX_ALREADY_EXISTS",
            "/path: a file already exists there; set overwrite=true to replace it",
            field="/path",
            details={"path": files.relative(path, tx.workspace.root)},
        )
    if overwrite and Path(path).exists():
        try:
            old = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            old = None
        if not isinstance(old, dict) or old.get("format") != FORMAT:
            fail("NX_UNSUPPORTED_FILE_TYPE", "overwrite replaces presentation files only; the "
                 "file there is not one", field="/path")  # fmt: skip
    part = display.shown_part(tx, work=True)
    view = display.model_view(tx, part)
    index = _index(part)
    where = {int(obj.Tag): key for key, obj in index.items()}
    values = bodies.scope(tx, "assembly") + [c for c, _ in parts.components(part)]
    saved: list = []
    for obj, record in tx.services.display_records(values, True):
        _save_row(saved, where, obj, record)
        for item in record.get("faces") or ():  # faces unlike their body, after it
            try:
                face = select.resolve(tx, display.record_id(item), "face")
            except NXError:
                continue
            _save_row(saved, where, face, {"blanked": bool(face.IsBlanked), **item})
    active = view.ActiveDynamicSection
    section = None
    if active is not None:
        state = display.section_state(tx, part, view, active)
        section = {k: state[k] for k in ("origin", "normal", "cap")}
    cam = display.camera(tx, view)
    data = {
        "format": FORMAT,
        "version": VERSION,
        "part_path": files.relative(parts.file_of(part), tx.workspace.root),
        "camera": {k: cam[k] for k in ("rotation", "origin", "scale", "style")},
        "display": saved,
        "section": section,
        "sectioning": bool(view.DisplaySectioningToggle),
    }
    temp = tx.files.stage(path, replace=overwrite)
    temp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    published = tx.files.publish()[0]
    meta = files.metadata(published, tx.workspace.root)
    return {
        **meta,
        "objects": len(saved),
        "sections": 1 if section else 0,
    }
