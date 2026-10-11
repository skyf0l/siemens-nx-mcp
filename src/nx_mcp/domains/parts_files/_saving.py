"""Verified part saves and closes, shared by nx_save_part, nx_close_part and exports.

A save is audited: every loaded part's flags and file fingerprint are taken
before and after the native save. It counts as saved only when the target's
file exists, the part is no longer modified, NX reported no unsaved objects,
and no other loaded part or file changed (``NX_VERIFICATION_FAILED`` with
``details.check = "save"`` otherwise). ``other_modified_parts`` names the other
loaded parts that had unsaved changes before the save (left unsaved). Saving expires
NX undo marks.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TypedDict

from nx_mcp.core import marks as undo_marks
from nx_mcp.core import target as targets
from nx_mcp.core.errors import classify, fail
from nx_mcp.kit import files, parts, result


class _Audit(TypedDict):
    saved_files: list[str]
    changed_files: list[str]
    other_modified_parts: list[result.Row]
    verified_parts: int


class SaveAudit(_Audit, total=False):
    updated_views: list[result.Row]  # out-of-date drawing views updated before the save


def fingerprint(path) -> dict:
    if not path or not Path(path).is_file():
        return {"exists": False}
    file = Path(path)
    before = file.stat()
    digest = files.sha256(file)
    after = file.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OSError(f"File changed while verifying: {path}")
    return {"exists": True, "sha256": digest, "size": after.st_size, "mtime_ns": after.st_mtime_ns}


def snapshot(tx) -> dict:
    return {
        int(p.Tag): {
            "path": parts.file_of(p),
            "modified": bool(p.IsModified),
            "file": fingerprint(parts.file_of(p)),
        }
        for p in tx.session.Parts
    }


def verify(
    before: dict, after: dict, tag: int, native_errors: list, root, others: list | None = None
) -> SaveAudit:
    """The audit of a save of part ``tag``; raises ``NX_VERIFICATION_FAILED``.
    ``others``: rows of the other parts that were modified before the save."""
    target = after.get(tag)
    unexpected = [
        files.relative(row["path"], root)
        for other, row in before.items()
        if other != tag and after.get(other) != row
    ]
    new_parts = [files.relative(row["path"], root) for t, row in after.items() if t not in before]
    saved = bool(
        target
        and target["path"] == before[tag]["path"]
        and not target["modified"]
        and target["file"]["exists"]
        and not native_errors
    )
    audit: SaveAudit = {
        "saved_files": [files.relative(target["path"], root)] if saved and target else [],
        "changed_files": [
            files.relative(row["path"], root)
            for t, row in after.items()
            if t not in before or row["file"] != before[t]["file"]
        ],
        "other_modified_parts": list(others or []),
        "verified_parts": len(before),
    }
    if not saved or unexpected or new_parts:
        fail(
            "NX_VERIFICATION_FAILED",
            "The save did not leave exactly the part's own file saved; inspect details",
            details={
                **audit,
                "check": "save",
                "unexpected_changes": unexpected,
                "unexpected_new_parts": new_parts,
                "native_save_errors": native_errors,
            },
        )
    return audit


def _status_errors(status) -> list:
    out: list[dict] = []
    if not status:
        return out
    for i in range(getattr(status, "NumberUnsavedParts", 0) or 0):
        out.append({"path": status.GetPart(i).FullPath, "nx_code": status.GetStatus(i)})
    for i in range(getattr(status, "NumberUnsavedObjects", 0) or 0):
        out.append({"object_index": i, "nx_code": status.GetObjectStatus(i)})
    return out


def verified_save(tx, part) -> SaveAudit:
    """Save ``part`` in place (drawing previews kept) and verify it; returns the audit."""
    with tx.checks():  # nothing is saved when the loaded files cannot be fingerprinted
        try:
            before = snapshot(tx)
        except Exception as error:
            fail("NX_SAVE_FAILED", f"Could not fingerprint the loaded parts before saving: {error}")
    tag = int(part.Tag)
    others = [
        tx.ref(p, "part")
        for p in tx.session.Parts
        if int(p.Tag) != tag and before.get(int(p.Tag), {}).get("modified")
    ]
    native_errors: list = []
    updated: list = []
    save_error = None
    called = False
    try:
        with parts.saving(tx, part) as views:
            updated = [tx.ref(v, "drawing_view") for v in views]
            tx.non_undoable("native NX save")
            called = True
            status = part.Save(
                tx.nx.BasePart.SaveComponents.FalseValue,
                tx.nx.BasePart.CloseAfterSave.FalseValue,
            )
            try:
                native_errors.extend(_status_errors(status))
            finally:
                if status and hasattr(status, "Dispose"):
                    with contextlib.suppress(Exception):
                        status.Dispose()
    except Exception as error:
        if not called:
            raise
        save_error = classify(error, default="NX_SAVE_FAILED")
        if save_error.code == "NX_RESTORE_FAILED":
            raise save_error from error
        native_errors.append({"message": str(error), "nx_code": save_error.nx_code})
    if save_error is not None:
        # The native failure is the cause; the audit of what changed goes with it.
        try:
            save_error.details["audit"] = verify(
                before, snapshot(tx), tag, native_errors, tx.workspace.root, others
            )
        except Exception as error:
            save_error.details["audit"] = getattr(error, "details", {"error": str(error)})
        raise save_error
    audit = verify(before, snapshot(tx), tag, native_errors, tx.workspace.root, others)
    if updated:
        audit["updated_views"] = updated
        note_updated(tx, updated)
    return audit


def note_updated(tx, updated) -> None:
    """The note for drawing views ``parts.saving`` updated before a save."""
    tx.warn(
        "NX_NOTE",
        f"Updated {len(updated)} out-of-date drawing view(s) before saving, so the "
        "sheets do not reopen marked (Out of Date)",
    )


def replacement(tx, target):
    """The loaded part to display instead of ``target`` before it closes: another
    top-level part of the agent's (``Services.owns``), not one of ``target``'s
    components; parts without sheet metal first, then the one a call worked in last
    (``core/busy.py``). None when there is none."""
    services = tx.services
    nested = {int(p.Tag) for p in _component_parts(target)}
    entries = (
        services.part_activity().get("parts", {}) if hasattr(services, "part_activity") else {}
    )

    def recent(part) -> float:
        with contextlib.suppress(Exception):
            return float((entries.get(services.part_key(part)) or {}).get("at") or 0.0)
        return 0.0

    candidates = []
    for part in targets.roots(services):
        with contextlib.suppress(Exception):
            if parts.same_part(part, target) or int(part.Tag) in nested:
                continue
            if services.owns(part) and parts.file_of(part):
                candidates.append(part)
    candidates.sort(key=lambda p: (parts.has_sheet_metal(p), -recent(p)))
    return candidates[0] if candidates else None


def _component_parts(part) -> list:
    out = []
    for component, _names in parts.components(part):
        prototype = getattr(component, "Prototype", None)
        if prototype is not None and hasattr(prototype, "Tag"):
            out.append(prototype)
    return out


def _display_instead(tx, target):
    """Display another agent part in place of the displayed ``target`` (undo marks kept,
    ``parts.activate``); the part shown, or None when there is no other part. A
    sheet-metal part is shown in Sheet Metal (recorded there), so the close does not
    re-enter Modeling on it and open NX's Sheet Metal warning."""
    other = replacement(tx, target)
    if other is None:
        return None
    parts.activate(tx, other)
    session = tx.session
    if str(session.ApplicationName or "") != parts.SHEET_METAL_APP and parts.has_sheet_metal(other):
        session.ApplicationSwitchImmediate(parts.SHEET_METAL_APP)
    return other


def _path(services, part) -> str:
    try:
        return str(services.part_path(part))
    except Exception:
        return parts.file_of(part)


def close_part(tx, target, *, save: bool, retain: bool = True, barrier: bool = True) -> dict:
    """Close ``target`` (saving it first when asked); returns closed rows and counts.

    With ``retain`` (default) NX keeps every undo mark (``CloseRetainUndoMarks``): NX
    allows it for a part that is not displayed, so a displayed target is first replaced
    on the display by another agent part (:func:`replacement`). Without one, or without
    ``retain`` (a cleanup inside a call that fails), the plain ``Close`` drops every NX
    undo mark. ``undo_marks`` in the result says which (``kept``/``dropped``). With
    ``barrier`` a kept-marks close ends every part's agent undo there
    (``core/marks.close_barrier``): undoing across it would load the part again.

    NX may unload unused prototypes with it: every part that disappeared is
    reported and its references, undo history and ownership are forgotten.
    Closing re-enters the display part's remembered application: a displayed
    sheet-metal part is recorded in Sheet Metal first (``parts.keep_sheet_metal``),
    or NX would open its blocking Sheet Metal warning.
    """
    services = tx.services
    before = {}
    for p in tx.session.Parts:
        before[int(p.Tag)] = (tx.ref(p, "part"), services.part_key(p), _path(services, p))
    target_key = services.part_key(target)  # NX's wrapper reports tag 0 once closed
    audit = verified_save(tx, target) if save else None
    keep_marks = getattr(target, "CloseRetainUndoMarks", None) if retain else None
    shown = None
    if parts.same_part(tx.session.Parts.Display, target):
        if keep_marks is not None:
            shown = _display_instead(tx, target)
            if shown is None:
                keep_marks = None  # the last part on display: NX's plain close
    else:
        parts.keep_sheet_metal(tx)
    whole = tx.nx.BasePart.CloseWholeTree.FalseValue
    modified = tx.nx.BasePart.CloseModified.CloseModified
    try:
        if keep_marks is not None:
            keep_marks(whole, modified, None)
        else:
            target.Close(whole, modified, None)
    except Exception as error:
        raise classify(error) from error
    finally:
        remaining = {int(p.Tag) for p in tx.session.Parts}
        gone = [key for tag, (_row, key, _path) in before.items() if tag not in remaining]
        with contextlib.suppress(Exception):
            services.forget_parts(gone)
    closed = [row for tag, (row, _key, _path) in before.items() if tag not in remaining]
    if keep_marks is not None and barrier:
        paths = [path for tag, (_row, _key, path) in before.items() if tag not in remaining]
        method = getattr(getattr(tx, "spec", None), "name", None) or "nx_close_part"
        undo_marks.close_barrier(
            services, paths, target_key, getattr(tx, "operation_id", None), method
        )
    out = {
        "closed": closed,
        "remaining_count": len(remaining),
        "saved": bool(save),
        "undo_marks": "kept" if keep_marks is not None else "dropped",
    }
    if shown is not None:
        out["displayed"] = tx.ref(shown, "part")
    if audit is not None:
        out["audit"] = audit
    return out
