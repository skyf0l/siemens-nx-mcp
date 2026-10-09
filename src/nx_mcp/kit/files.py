"""Workspace files: checksums, metadata and writes that never overwrite.

    meta = files.metadata(path, root)        # {path, size, sha256}, path workspace-relative
    files.write_new(target, data)             # exclusive create, NX_ALREADY_EXISTS if present
    files.publish_new(temp, target)           # hard-link a finished temp file into place

A new artifact appears whole or not at all: it is written to a temporary file
first and published with ``os.link``, which fails instead of replacing a file
another writer created in the meantime (``os.replace`` would overwrite it).
Paths are already resolved inside the workspace (gate 2); nothing here checks
the workspace boundary again.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import struct
from pathlib import Path

from nx_mcp.core.errors import fail

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


def metadata(path, root) -> dict:
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


def _exists(target, field):
    fail(
        "NX_ALREADY_EXISTS",
        f"{Path(target).name} already exists; files are never overwritten",
        field=field,
        details={"path": str(target)},
    )


def publish_new(temp, target, *, field: str | None = "/path") -> Path:
    """Move a finished ``temp`` file to ``target`` without ever overwriting it.

    The parent folder is created. A file that appeared at ``target`` meanwhile
    raises ``NX_ALREADY_EXISTS`` and ``temp`` is removed either way.
    """
    temp, target = Path(temp), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(temp, target)
    except FileExistsError:
        _exists(target, field)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()
    return target


def write_new(target, data: bytes, *, field: str | None = "/path") -> Path:
    """Create ``target`` with ``data`` exclusively; a partial file is removed on error."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        stream = target.open("xb")
    except FileExistsError:
        _exists(target, field)
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
