"""Verified part saves and closes, shared by nx_save_part, nx_close_part and exports.

A save is audited: every loaded part's flags and file fingerprint are taken
before and after the native save. It counts as saved only when the target's
file exists, the part is no longer modified, NX reported no unsaved objects,
and no other loaded part or file changed (``NX_VERIFICATION_FAILED`` with
``details.check = "save"`` otherwise). Saving expires NX undo marks.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TypedDict

from nx_mcp.core.errors import classify, fail
from nx_mcp.kit import files, parts


class SaveAudit(TypedDict):
    saved_files: list[str]
    changed_files: list[str]
    other_modified_parts: int
    verified_parts: int


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


def verify(before: dict, after: dict, tag: int, native_errors: list, root) -> SaveAudit:
    """The audit of a save of part ``tag``; raises ``NX_VERIFICATION_FAILED``."""
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
        "other_modified_parts": sum(1 for t, row in before.items() if t != tag and row["modified"]),
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
    native_errors: list = []
    save_error = None
    called = False
    try:
        with parts.saving(tx, part):
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
                before, snapshot(tx), int(part.Tag), native_errors, tx.workspace.root
            )
        except Exception as error:
            save_error.details["audit"] = getattr(error, "details", {"error": str(error)})
        raise save_error
    return verify(before, snapshot(tx), int(part.Tag), native_errors, tx.workspace.root)


def close_part(tx, target, *, save: bool) -> dict:
    """Close ``target`` (saving it first when asked); returns closed rows and counts.

    NX may unload unused prototypes with it: every part that disappeared is
    reported and its references, undo history and ownership are forgotten.
    Closing re-enters the display part's remembered application: a displayed
    sheet-metal part is recorded in Sheet Metal first (``parts.keep_sheet_metal``),
    or NX would open its blocking Sheet Metal warning.
    """
    before = {}
    for p in tx.session.Parts:
        before[int(p.Tag)] = (tx.ref(p, "part"), tx.services.part_key(p))
    audit = verified_save(tx, target) if save else None
    if not parts.same_part(tx.session.Parts.Display, target):
        parts.keep_sheet_metal(tx)
    try:
        target.Close(
            tx.nx.BasePart.CloseWholeTree.FalseValue,
            tx.nx.BasePart.CloseModified.CloseModified,
            None,
        )
    except Exception as error:
        raise classify(error) from error
    finally:
        remaining = {int(p.Tag) for p in tx.session.Parts}
        gone = [key for tag, (_row, key) in before.items() if tag not in remaining]
        with contextlib.suppress(Exception):
            tx.services.forget_parts(gone)
    closed = [row for tag, (row, _key) in before.items() if tag not in remaining]
    out = {"closed": closed, "remaining_count": len(remaining), "saved": bool(save)}
    if audit is not None:
        out["audit"] = audit
    return out
