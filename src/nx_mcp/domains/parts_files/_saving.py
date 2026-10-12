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


def _tab_back(tx, target):
    """After the active tab's part closed, NX has no display part: activate the most
    recently active remaining tab (``parts.tabs``; undo marks kept, no Sheet Metal
    dialog, ``parts.activate``). The part shown, or None when no tab is left. Another
    part never takes the closed part's place in a tab."""
    session_parts = tx.session.Parts
    if session_parts.Display is not None:
        return None
    for part in parts.tabs(session_parts):
        with contextlib.suppress(Exception):
            if parts.same_part(part, target) or not parts.file_of(part):
                continue
            parts.activate(tx, part)
            return part
    return None


def _path(services, part) -> str:
    try:
        return str(services.part_path(part))
    except Exception:
        return parts.file_of(part)


def close_part(tx, target, *, save: bool, retain: bool = True, barrier: bool = True) -> dict:
    """Close ``target`` (saving it first when asked); returns closed rows and counts.

    With ``retain`` (default) NX keeps every undo mark (``CloseRetainUndoMarks``, also
    for a displayed part); without it (a cleanup inside a call that fails) the plain
    ``Close`` drops every NX undo mark. ``undo_marks`` in the result says which
    (``kept``/``dropped``). The part's NX tab closes with it; when it was the active tab,
    the most recently active remaining tab comes back (``displayed``, :func:`_tab_back`).
    With ``barrier`` a kept-marks close ends every part's agent undo there
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
    active = parts.same_part(tx.session.Parts.Display, target)
    if not active:
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
    if active:
        shown = _tab_back(tx, target)
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
