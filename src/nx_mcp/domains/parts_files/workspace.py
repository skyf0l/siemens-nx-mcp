"""Workspace files (tool-surface 3.2): list, create folders, upload, download.

These tools run in the sidecar (``runs="sidecar"``): they never reach NX, so the
engineer's control hold never delays them. Paths are checked by gate 2 in the
proxy and again by the kernel (traversal, outside the workspace, ``.nx-mcp`` in
any case, drive- or root-relative paths). Internal ``.nx-mcp`` state is never
listed, read or written.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Offset, PageLimit, PathIn, PathTo, Range, Text
from nx_mcp.kit import files, result

CHUNK_BYTES = 262_144
MAX_UPLOAD = 256 * 1024 * 1024
INLINE_IMAGE_BYTES = 8 * 1024 * 1024
TEXT_BYTES = 65_536  # delivery text: a whole small file read as UTF-8
# Parts, images, documents, expression files (.exp) and every file nx_import_model reads
# (importing.SUFFIXES).
UPLOAD_SUFFIXES = (".prt", ".step", ".stp", ".png", ".json", ".zip", ".txt", ".pdf",
                   ".igs", ".iges", ".x_t", ".x_b", ".stl", ".dxf", ".dwg", ".catpart",
                   ".catproduct", ".exp")  # fmt: skip
RESERVED = ".nx-mcp"


class FileRow(TypedDict, total=False):
    path: str
    kind: str
    size: int
    sha256: str


class FilesData(TypedDict):
    path: str
    items: list[FileRow]
    page: dict


class DirectoryData(TypedDict):
    path: str
    created: bool


class UploadData(TypedDict):
    path: str
    received: int
    total_size: int
    committed: bool
    sha256: str


class ImageInfo(TypedDict):
    path: str
    sha256: str
    inline: bool
    mime_type: str
    width: int
    height: int


class DownloadData(TypedDict, total=False):
    path: str
    size: int
    sha256: str
    delivery: str
    image: ImageInfo
    offset: int
    bytes_returned: int
    next_offset: int | None
    eof: bool
    data_base64: str
    text: str


def _root(tx) -> Path:
    return Path(tx.workspace.root)


def _upload_bytes(content: str | None, data_base64: str | None, offset: int) -> bytes:
    """The bytes of this upload call: ``content`` (a whole file) or one base64 chunk."""
    if (content is None) == (data_base64 is None):
        both = content is not None
        fail(
            "NX_INVALID_ARGUMENT",
            "Pass content (text of the whole file) or data_base64 (a chunk of bytes)"
            + (", not both" if both else ""),
            field="/content" if both else "/data_base64",
        )
    if content is not None:
        if offset:
            fail("NX_INVALID_ARGUMENT", "content is the whole file: offset is 0", field="/offset")
        data = content.encode("utf-8")
        if len(data) > CHUNK_BYTES:
            fail(
                "NX_REQUEST_TOO_LARGE",
                "content holds at most 256 KiB of UTF-8; send larger files as data_base64 chunks",
                field="/content",
            )
        return data
    try:
        data = base64.b64decode(data_base64 or "", validate=True)
    except (binascii.Error, ValueError):
        fail("NX_INVALID_ARGUMENT", "data_base64 is not valid base64", field="/data_base64")
    if len(data) > CHUNK_BYTES:
        fail("NX_REQUEST_TOO_LARGE", "A chunk holds at most 256 KiB", field="/data_base64")
    return data


def _reserved(path: Path, root: Path) -> bool:
    try:
        parts = path.resolve().relative_to(root).parts
    except ValueError:
        return True  # outside the workspace (a junction): never shown
    return any(p.casefold() == RESERVED for p in parts)


@tool(
    effect="read",
    label="List workspace files",
    summary=lambda a, say: f"List files in {say('path') if a.get('path') else 'workspace'}",
    keywords=("workspace listing", "directory listing", "folder contents", "ls", "dir"),
    cost="light",
    runs="sidecar",
    example={"path": "projects/demo"},
)
def nx_list_files(
    tx: Tx,
    *,
    path: Annotated[str, PathTo("dir")] = "",
    prefix: Annotated[str, Text(128), Doc("Only names starting with this text")] | None = None,
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> FilesData:
    """List a workspace folder one page at a time: files with size and SHA-256, and
    subfolders. Internal .nx-mcp state is never listed."""
    root = _root(tx)
    folder = Path(tx.workspace.resolve(path or "."))
    if not folder.exists():
        fail(
            "NX_FILE_NOT_FOUND",
            f"No folder {files.relative(folder, root)} in the workspace",
            field="/path",
            hint="List its parent folder with nx_list_files, or create it with nx_create_directory",
        )
    if not folder.is_dir():
        fail(
            "NX_WRONG_KIND",
            "path names a file, not a folder",
            field="/path",
            hint="Read one file with nx_download_file; list its folder with nx_list_files",
        )
    needle = (prefix or "").casefold()
    entries = [
        e
        for e in sorted(folder.iterdir(), key=lambda e: e.name.casefold())
        if e.name.casefold().startswith(needle) and not _reserved(e, root)
    ]
    selected, page = result.page(entries, offset, limit)
    items: list[FileRow] = []
    for entry in selected:
        if entry.is_file():
            meta = files.metadata(entry, root)
            items.append(
                {
                    **meta,
                    "kind": "file",
                }
            )
        else:
            items.append({"path": files.relative(entry, root), "kind": "directory"})
    return {"path": files.relative(folder, root) or ".", "items": items, "page": page}


@tool(
    effect="file",
    label="Create workspace folder",
    summary="Create folder {path}",
    keywords=("mkdir", "new folder", "create directory", "project folder"),
    cost="light",
    runs="sidecar",
    example={"path": "projects/demo/parts"},
)
def nx_create_directory(tx: Tx, *, path: Annotated[str, PathTo("dir")]) -> DirectoryData:
    """Create a workspace folder and its missing parents. An existing folder succeeds with
    created false; an existing file is refused."""
    folder = Path(path)
    existed = folder.is_dir()
    tx.touched = True
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        fail(
            "NX_WRONG_KIND",
            "A file already exists at this path",
            field="/path",
            hint="Choose another folder path; nx_list_files shows what exists",
        )
    except OSError as error:
        fail("NX_TRANSFER_FAILED", f"Cannot create the folder: {error}", field="/path")
    return {"path": files.relative(folder, _root(tx)), "created": not existed}


Base64 = Annotated[
    str,
    Text(349_528, r"^[A-Za-z0-9+/]*={0,2}$"),
    Doc("Base64 bytes of this chunk, at most 256 KiB decoded"),
]
Sha256 = Annotated[str, Text(64, r"^[0-9a-f]{64}$"), Doc("Lowercase SHA-256 of the whole file")]


@tool(
    effect="file",
    label="Upload workspace file",
    summary="Upload {path}",
    keywords=("upload", "send file", "transfer file", "put file", "copy to workspace"),
    cost="light",
    runs="sidecar",
    example={"path": "inbox/notes.txt", "content": "Bracket rev B"},
)
def nx_upload_file(
    tx: Tx,
    *,
    path: Annotated[str, PathTo("new", *UPLOAD_SUFFIXES)],
    content: Annotated[str, Text(CHUNK_BYTES), Doc("Whole file as UTF-8 text")] | None = None,
    data_base64: Base64 | None = None,
    offset: Annotated[int, Range(ge=0, le=MAX_UPLOAD), Doc("Byte offset of this chunk")] = 0,
    total_size: Annotated[int, Range(ge=0, le=MAX_UPLOAD), Doc("Default: one chunk")] | None = None,
    sha256: Sha256 | None = None,
) -> UploadData:
    """Upload a new workspace file: text as content, or bytes as base64 chunks in order. A file
    of several chunks needs total_size and sha256. It appears when complete; never overwrites."""
    data = _upload_bytes(content, data_base64, offset)
    if total_size is None:
        if offset:
            fail("NX_INVALID_ARGUMENT", "Chunked uploads need total_size", field="/total_size")
        total_size = len(data)
    if offset > total_size:
        fail("NX_INVALID_ARGUMENT", "offset is beyond total_size", field="/offset")
    if offset + len(data) > total_size:
        fail("NX_INVALID_ARGUMENT", "The chunk ends beyond total_size", field="/data_base64")
    if sha256 is None:
        if offset or len(data) != total_size:
            fail(
                "NX_INVALID_ARGUMENT",
                "A file of several chunks needs the SHA-256 of the whole file",
                field="/sha256",
            )
        sha256 = hashlib.sha256(data).hexdigest()
    target = Path(path)
    staging = _root(tx) / RESERVED / "uploads"
    key = hashlib.sha256(f"{target}|{sha256}|{total_size}".encode()).hexdigest()
    temp = staging / key
    tx.touched = True
    staging.mkdir(parents=True, exist_ok=True)
    size = temp.stat().st_size if temp.exists() else 0
    if offset < size:
        with temp.open("rb") as stream:
            stream.seek(offset)
            if stream.read(len(data)) != data:
                fail(
                    "NX_TRANSFER_FAILED",
                    "This chunk differs from the bytes already received at that offset",
                    field="/data_base64",
                    details={"received": size},
                )
    elif offset == size:
        with temp.open("ab") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        size += len(data)
    else:
        fail(
            "NX_TRANSFER_FAILED",
            f"Chunks must be contiguous: the next chunk starts at offset {size}",
            field="/offset",
            details={"received": size},
        )
    committed = size == total_size
    if committed:
        if files.sha256(temp) != sha256:
            temp.unlink(missing_ok=True)
            fail(
                "NX_TRANSFER_FAILED",
                "The uploaded bytes do not match sha256; the upload was discarded",
                field="/sha256",
                hint="Send the SHA-256 of the whole file, or omit it for a one-chunk file; "
                "a file of several chunks also needs total_size",
            )
        files.publish_new(temp, target)
    return {
        "path": files.relative(target, _root(tx)),
        "received": size,
        "total_size": total_size,
        "committed": committed,
        "sha256": sha256,
    }


@tool(
    effect="read",
    label="Download workspace file",
    summary=lambda a, say: (
        {"image": "Show image", "chunk": "Download", "text": "Read"}.get(
            a.get("delivery"), "File info"
        )
        + f" {say('path')}"
    ),
    keywords=("download", "read file", "file checksum", "show png", "get file"),
    variants="delivery",
    cost="light",
    runs="sidecar",
    example={"path": "captures/top.png", "delivery": "image"},
)
def nx_download_file(
    tx: Tx,
    *,
    path: PathIn,
    delivery: Literal["metadata", "image", "text", "chunk"] = "metadata",
    offset: Annotated[int, Range(ge=0), Doc("chunk: first byte")] | None = None,
    length: Annotated[int, Range(ge=1, le=CHUNK_BYTES), Doc("chunk: bytes, at most 256 KiB")]
    | None = None,
) -> DownloadData:
    """Read a workspace file: metadata (size, SHA-256), image (inline PNG), text (UTF-8, up to
    64 KiB) or chunk (base64)."""
    if delivery != "chunk" and (offset is not None or length is not None):
        fail(
            "NX_INVALID_ARGUMENT",
            "offset and length apply only to delivery chunk",
            field="/offset" if offset is not None else "/length",
        )
    file = Path(path)
    meta = files.metadata(file, _root(tx))
    out: DownloadData = {
        **meta,
        "delivery": delivery,
    }
    if delivery == "image":
        if meta["size"] > INLINE_IMAGE_BYTES:
            fail(
                "NX_RESPONSE_TOO_LARGE",
                "Inline images are limited to 8 MiB; download it in chunks",
                field="/path",
            )
        size = files.png_size(file)
        if size is None:
            fail(
                "NX_UNSUPPORTED_FILE_TYPE",
                "delivery image shows PNG files only",
                field="/path",
                hint="Use delivery text for a text file, chunk for other bytes",
            )
        out["image"] = {
            "path": meta["path"],
            "sha256": meta["sha256"],
            "inline": True,
            "mime_type": "image/png",
            "width": size[0],
            "height": size[1],
        }
    elif delivery == "text":
        out["text"] = _text(file, meta["size"])
    elif delivery == "chunk":
        start = offset or 0
        with file.open("rb") as stream:
            stream.seek(start)
            data = stream.read(length or CHUNK_BYTES)
        end = start + len(data)
        out.update(
            {
                "offset": start,
                "bytes_returned": len(data),
                "next_offset": end if end < meta["size"] else None,
                "eof": end >= meta["size"],
                "data_base64": base64.b64encode(data).decode(),
            }
        )
    return out


def _text(file: Path, size: int) -> str:
    """The whole file as UTF-8 text (a BOM is dropped), else a refusal naming chunk."""
    if size > TEXT_BYTES:
        fail(
            "NX_RESPONSE_TOO_LARGE",
            f"delivery text reads files up to 64 KiB; this one has {size} bytes",
            field="/path",
            hint="Read it with delivery chunk",
        )
    try:
        return file.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        fail(
            "NX_UNSUPPORTED_FILE_TYPE",
            "The file is not UTF-8 text",
            field="/path",
            hint="Read its bytes with delivery chunk",
        )
