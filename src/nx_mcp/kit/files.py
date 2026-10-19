"""Workspace files: checksums, metadata and writes that never overwrite.

    meta = files.metadata(path, root)        # {path, size, sha256}, path workspace-relative
    files.write_new(target, data)             # exclusive create, NX_ALREADY_EXISTS if present
    files.publish_new(temp, target)           # hard-link a finished temp file into place
    files.note_written(tx, target)            # a tool created it: nx_delete_file may delete it

A new artifact appears whole or not at all: it is written to a temporary file
first and published with ``os.link`` (``core.tx.publish_new``), which fails instead
of replacing a file another writer created in the meantime (``os.replace`` would
overwrite it).
Paths are already resolved inside the workspace (gate 2); nothing here checks
the workspace boundary again.
"""

from __future__ import annotations

import contextlib
import hashlib
import struct
from pathlib import Path
from typing import TypedDict

from nx_mcp.core import tx as tx_module
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import publish_new as publish_new  # one never-overwrite move

CHUNK = 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def sha256(path) -> str:
    """Hex SHA-256 of a file, read in 1 MiB chunks."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relative(path, root) -> str:
    """``path`` relative to ``root`` with forward slashes (absolute when outside)."""
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError):
        return Path(path).as_posix()


class FileMeta(TypedDict):
    """What :func:`metadata` returns; result rows spread it with ``**meta``."""

    path: str  # workspace-relative
    size: int
    sha256: str


def metadata(path, root) -> FileMeta:
    """``{path, size, sha256}`` of a file; ``path`` is workspace-relative."""
    file = Path(path)
    return {"path": relative(file, root), "size": file.stat().st_size, "sha256": sha256(file)}


def png_size(path) -> tuple[int, int] | None:
    """``(width, height)`` from a PNG header, or None when the file is not a PNG."""
    with Path(path).open("rb") as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != PNG_SIGNATURE or header[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", header[16:24])
    return int(width), int(height)


def _exists(target, field, root=None):
    fail(
        "NX_ALREADY_EXISTS",
        f"{Path(target).name} already exists; files are never overwritten",
        field=field,
        details={"path": relative(target, root) if root is not None else str(target)},
    )


def write_new(target, data: bytes, *, field: str | None = "/path", root=None) -> Path:
    """Create ``target`` with ``data`` exclusively; a partial file is removed on error.
    With ``root`` (the workspace) an error names the path relative to it."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        stream = target.open("xb")
    except FileExistsError:
        _exists(target, field, root)
    try:
        with stream:
            stream.write(data)
    except BaseException:
        with contextlib.suppress(OSError):
            target.unlink()
        raise
    return target


def temp_path(root, name: str, operation_id: str | None = None) -> Path:
    """A fresh path under ``<root>/.nx-mcp/staging/<operation>/`` for ``name``."""
    import uuid

    folder = Path(root) / ".nx-mcp" / "staging" / (operation_id or uuid.uuid4().hex)
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{uuid.uuid4().hex[:8]}_{Path(name).name}"


# --- files NX MCP tools created (nx_delete_file deletes only these) -------------------
#
# ``<workspace>/.nx-mcp/created-files.json``: ``{"files": {key: row}}`` with the key the
# workspace-relative path in lower case and the row ``{path, tool, operation_id, caller,
# at}``. Both processes write it (sidecar uploads, host exports) under a lock file; a
# file the engineer or another program made is never listed, so it is never deleted.

CREATED_FILES = "created-files.json"
CREATED_MAX = 5000  # rows kept (the oldest go first)
LOCK_SECONDS = 10.0  # a lock file this old was left by a crash: taken over


def _created_file(root) -> Path:
    return Path(root) / ".nx-mcp" / CREATED_FILES


def _created_key(path, root) -> str:
    return relative(path, root).casefold()


@contextlib.contextmanager
def _created_lock(root):
    """Exclusive access to the created-files list across processes (at most ~2 s wait;
    an older lock than ``LOCK_SECONDS`` is a crashed writer's and is taken over)."""
    import os
    import time

    lock = _created_file(root).with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    for _ in range(40):
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - lock.stat().st_mtime > LOCK_SECONDS:
                    lock.unlink()
                    continue
            time.sleep(0.05)
    try:
        yield
    finally:
        if handle is not None:
            os.close(handle)
            with contextlib.suppress(OSError):
                lock.unlink()


def _created_rows(root) -> dict:
    import json

    try:
        data = json.loads(_created_file(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = data.get("files") if isinstance(data, dict) else None
    return rows if isinstance(rows, dict) else {}


def _write_created(root, rows: dict) -> None:
    import json
    import os

    if len(rows) > CREATED_MAX:
        newest = sorted(rows.items(), key=lambda kv: float(kv[1].get("at") or 0.0))
        rows = dict(newest[-CREATED_MAX:])
    target = _created_file(root)
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps({"files": rows}), encoding="utf-8")
    os.replace(temp, target)


def note_created(root, path, *, tool: str | None = None, operation_id=None, caller=None) -> None:
    """Record that a tool call created the workspace file ``path`` (its later deletion with
    nx_delete_file is then allowed). ``caller``: the call's caller key, when known. Never
    raises: the record is a convenience, never a reason to fail the call that wrote it."""
    import time

    with contextlib.suppress(Exception):
        ident = caller.get("id") if isinstance(caller, dict) else caller
        row = {
            "path": relative(path, root),
            "tool": tool,
            "operation_id": operation_id,
            "caller": ident if isinstance(ident, str) else None,
            "at": time.time(),
        }
        with _created_lock(root):
            rows = _created_rows(root)
            rows[_created_key(path, root)] = row
            _write_created(root, rows)


def created_row(root, path) -> dict | None:
    """The record of a workspace file a tool created (:func:`note_created`), else None."""
    row = _created_rows(root).get(_created_key(path, root))
    return row if isinstance(row, dict) else None


def forget_created(root, path) -> None:
    """Drop the record of ``path`` (deleted). Never raises."""
    with contextlib.suppress(Exception), _created_lock(root):
        rows = _created_rows(root)
        if rows.pop(_created_key(path, root), None) is not None:
            _write_created(root, rows)


def note_written(tx, path) -> None:
    """:func:`note_created` for a file the call ``tx`` wrote (its tool, operation and
    caller)."""
    note_created(
        tx.workspace.root,
        path,
        tool=getattr(getattr(tx, "spec", None), "name", None),
        operation_id=getattr(tx, "operation_id", None),
        caller=getattr(tx, "caller", None),
    )


# Every file a call publishes through ``tx.files`` is recorded as written by it (core
# imports no kit module, so ``Files.publish`` runs the hooks registered here): any domain's
# outputs can then be deleted with nx_delete_file.
if note_written not in tx_module.WRITTEN_HOOKS:
    tx_module.WRITTEN_HOOKS.append(note_written)
