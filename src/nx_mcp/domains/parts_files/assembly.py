"""Copy a saved assembly with its prototypes (tool-surface 3.2): ZIP package or clone.

Both formats refuse unsaved or unloaded parts and files outside the workspace,
and never overwrite anything. ``zip`` writes every file with a SHA-256 manifest
(``nx-assembly-manifest.json``) and verifies the archive. ``clone`` uses the NX
clone (UF ``Clone``) with new file names ``<prefix><name>`` in a new folder;
NX 2406 lesson: the session load options may search folders without the
workspace, so components load *as saved* during the clone and the reopen (the
option is restored). The copy is reopened and its component references are
verified against the new files; ``nx-project-manifest.json`` records them.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import zipfile
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, PathTo, Text
from nx_mcp.domains.parts_files import _saving
from nx_mcp.kit import files, parts, readback, result

MANIFEST = "nx-assembly-manifest.json"
FORMAT_DOC = "Default: clone when prefix is given, else zip"
PREFIX_DOC = "Required for clone"
CLONE_MANIFEST = "nx-project-manifest.json"
CLONE_API = ("Initialise", "SetDefAction", "AddAssembly", "SetNaming", "PerformClone", "Terminate")


class FileEntry(TypedDict, total=False):
    path: str
    source: str
    size: int
    sha256: str


class AssemblyExportData(TypedDict, total=False):
    path: str
    format: str
    size: int
    sha256: str
    top_part: str
    files: list[FileEntry]
    file_count: int
    component_count: int
    part: result.Row


def _sources(tx, part) -> tuple[list[Path], int]:
    """Files of the work part and every component prototype, checked; occurrence count."""
    root = Path(tx.workspace.root)
    occurrences = parts.components(part)
    unloaded = ["/".join(names) for c, names in occurrences if not parts.prototype_file(c)]
    if unloaded:
        fail(
            "NX_NOT_LOADED",
            "Some component prototypes are not loaded",
            hint="Open the assembly with nx_open_part(load_components=true) and retry",
            details={"components": unloaded[:50]},
        )
    found: dict[str, Path] = {}
    unsaved: list = []
    for p in [part, *(c.Prototype for c, _ in occurrences)]:
        file = parts.in_workspace(tx, p, field=None)
        key = str(file).casefold()
        if key in found:
            continue
        found[key] = file
        if p.IsModified:
            unsaved.append((p, file))
        elif not file.is_file():
            fail("NX_FILE_NOT_FOUND", f"{files.relative(file, root)} is not on disk")
    if unsaved:
        names = [files.relative(f, root) for _, f in unsaved]
        fail(
            "NX_UNSAVED_PART",
            f"Save {', '.join(names[:5])}{' ...' if len(names) > 5 else ''} before exporting "
            "the assembly (the assembly and every component file are copied as saved)",
            hint="Save each with nx_save_part(part=<ref from details.parts>), then retry",
            details={"part": names[0], "parts": [tx.ref(p, "part") for p, _ in unsaved[:50]]},
        )
    return sorted(found.values()), len(occurrences)


def _format_of(a: dict) -> str:
    """The requested format, else zip for a .zip path and clone with a prefix."""
    if a.get("format"):
        return str(a["format"])
    if str(a.get("path", "")).lower().endswith(".zip"):
        return "zip"
    return "clone" if a.get("prefix") else ""


def _summary(a: dict, say) -> str:
    verb = {"zip": "Package assembly as", "clone": "Clone assembly to"}
    return f"{verb.get(_format_of(a), 'Export assembly to')} {say('path')}"


@tool(
    effect="file",
    label="Export assembly",
    summary=_summary,
    keywords=("pack and go", "package assembly", "zip project", "clone assembly", "copy project"),
    variants="format",
    requires=("work_part", "no_active_sketch"),
    cost="long",
    example={"path": "exports/asm1.zip"},
)
def nx_export_assembly(
    tx: Tx,
    *,
    path: Annotated[str, PathTo("new"), Doc("New .zip (zip) or new folder (clone)")],
    format: Annotated[Literal["zip", "clone"], Doc(FORMAT_DOC)] | None = None,
    prefix: Annotated[str, Text(32, r"^[A-Za-z0-9_-]{1,32}$"), Doc(PREFIX_DOC)] | None = None,
    activate: Annotated[bool, Doc("clone: leave the copy open as work part")] = False,
) -> AssemblyExportData:
    """Copy the saved work part and its component files to a new .zip or clone folder. zip
    adds a SHA-256 manifest; clone names files <prefix><name>. Unsaved or unloaded parts are refused."""
    target = Path(path)
    if format is None:
        if target.suffix.lower() == ".zip":
            format = "zip"
        elif prefix is not None:
            format = "clone"
        else:
            fail(
                "NX_INVALID_ARGUMENT",
                "Give a path ending in .zip (zip package), or a new folder path with prefix "
                "(clone)",
                field="/format",
                hint="e.g. {path: 'exports/asm.zip'} or {path: 'exports/asm_v2', prefix: 'v2_'}",
            )
    if format == "zip":
        if prefix is not None or activate:
            fail("NX_INVALID_ARGUMENT", "prefix and activate apply to clone only", field="/prefix",
                 hint="Omit prefix and activate for a zip package")  # fmt: skip
        if target.suffix.lower() != ".zip":
            fail("NX_UNSUPPORTED_FILE_TYPE", "A package path ends in .zip", field="/path")
    elif prefix is None:
        fail("NX_INVALID_ARGUMENT", "clone needs prefix: every copied file is named "
             "<prefix><name>", field="/prefix", hint="e.g. prefix 'v2_'")  # fmt: skip
    with tx.checks():
        part = tx.part
        sources, occurrences = _sources(tx, part)
    if format == "zip":
        return _package(tx, part, sources, occurrences, target)
    assert prefix is not None  # a clone without prefix failed above
    return _clone(tx, part, sources, target, prefix, activate)


def _package(tx, part, sources, occurrences, target) -> AssemblyExportData:
    root = Path(tx.workspace.root)
    manifest: list[FileEntry] = [{**files.metadata(f, root)} for f in sources]
    temp = files.temp_path(root, target.name, tx.operation_id)
    tx.touched = True
    try:
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
            for entry, file in zip(manifest, sources, strict=True):
                archive.write(file, entry["path"])
                tx.checkpoint()
            archive.writestr(
                MANIFEST,
                json.dumps(
                    {
                        "assembly": files.relative(parts.file_of(part), root),
                        "component_count": occurrences,
                        "files": manifest,
                    },
                    indent=2,
                ),
            )
        with zipfile.ZipFile(temp) as archive:  # read back what was written
            readback.expect(archive.testzip() is None, "zip_integrity")
            names = set(archive.namelist())
            readback.expect(names == {e["path"] for e in manifest} | {MANIFEST}, "zip_members")
        files.publish_new(temp, target)
    finally:
        with contextlib.suppress(OSError):
            shutil.rmtree(temp.parent)
    meta = files.metadata(target, root)
    return {
        "path": meta["path"],
        "format": "zip",
        "size": meta["size"],
        "sha256": meta["sha256"],
        "top_part": files.relative(parts.file_of(part), root),
        "files": manifest,
        "file_count": len(manifest),
        "component_count": occurrences,
    }


def _clone(tx, part, sources, destination: Path, prefix: str, activate: bool) -> AssemblyExportData:
    root = Path(tx.workspace.root)
    top = Path(parts.file_of(part)).resolve()
    base = Path(os.path.commonpath([str(p.parent) for p in sources]))
    mapping = {p: destination / p.relative_to(base).parent / (prefix + p.name) for p in sources}
    with tx.checks():
        for new in mapping.values():
            parts.check_new_name(tx, new, field="/prefix")
        clone = tx.uf.Clone
        missing = [name for name in CLONE_API if not hasattr(clone, name)]
        if missing:
            fail("NX_VERSION_UNSUPPORTED", f"This NX lacks the clone API: {missing}")
    hashes = {p: files.sha256(p) for p in sources}
    originals = {int(p.Tag) for p in tx.session.Parts}
    options = tx.session.Parts.LoadOptions
    load_method = getattr(options, "ComponentLoadMethod", None)
    as_saved = getattr(getattr(tx.nx, "LoadOptions", None), "LoadMethod", None)
    created = False
    try:
        destination.mkdir(parents=True, exist_ok=False)
        created = True
        for new in mapping.values():
            new.parent.mkdir(parents=True, exist_ok=True)
        if load_method is not None and as_saved is not None:
            options.ComponentLoadMethod = as_saved.AsSaved
        with native(default="NX_EXPORT_FAILED"):
            clone.Initialise(clone.OperationClass.CLONE_OPERATION)
            try:
                clone.SetDefAction(clone.Action.CLONE)
                loaded = clone.AddAssembly(str(top))
                status = loaded[0] if isinstance(loaded, tuple) else loaded
                if getattr(status, "Failed", False) is True:
                    fail(
                        "NX_NOT_LOADED",
                        "The NX clone could not load the assembly's parts",
                        details={"files": [str(f) for f in getattr(status, "FileNames", [])]},
                    )
                for source, new in mapping.items():
                    clone.SetNaming(str(source), clone.NamingTechnique.USER_NAME, str(new))
                clone.PerformClone(clone.InitNamingFailures())
            finally:
                clone.Terminate()
        for source, new in mapping.items():
            readback.expect(new.is_file(), "clone_file", path=files.relative(new, root))
            if files.sha256(source) != hashes[source]:
                fail("NX_STALE_SOURCE", f"{source.name} changed during the clone")
        with native():
            copy, status = tx.session.Parts.OpenBase(str(mapping[top]))
        if status is not None and hasattr(status, "Dispose"):
            with contextlib.suppress(Exception):
                status.Dispose()
        resolved = {Path(parts.prototype_file(c)).resolve() for c, _ in parts.components(copy)}
        expected = {p.resolve() for p in mapping.values()} - {mapping[top].resolve()}
        readback.expect(
            resolved == expected,
            "clone_references",
            "The copied assembly does not use the copied component files",
            resolved=sorted(files.relative(p, root) for p in resolved),
        )
        manifest: list[FileEntry] = [
            {
                "source": files.relative(s, root),
                "path": files.relative(t, root),
                "sha256": files.sha256(t),
            }
            for s, t in sorted(mapping.items())
        ]
        files.write_new(
            destination / CLONE_MANIFEST,
            json.dumps(
                {
                    "source_root": files.relative(base, root),
                    "top_part": files.relative(mapping[top], root),
                    "files": manifest,
                    "dependency_count": len(expected),
                    "references_verified": True,
                },
                indent=2,
            ).encode(),
        )
        out: AssemblyExportData = {
            "path": files.relative(destination, root),
            "format": "clone",
            "top_part": files.relative(mapping[top], root),
            "files": manifest,
            "file_count": len(manifest),
            "component_count": len(parts.components(copy)),
        }
        if activate:
            parts.activate(tx, copy)
            out["part"] = tx.ref(copy, "part")
        else:
            _close_new(tx, originals, destination)
            parts.activate(tx, part)
        return out
    except BaseException:
        problems = []
        try:
            _close_new(tx, originals, destination)
            parts.activate(tx, part)
        except Exception as error:
            problems.append(str(error))
        if created:
            try:
                shutil.rmtree(destination)
            except OSError as error:
                problems.append(f"remove {destination.name}: {error}")
        if problems:
            tx.non_undoable("; ".join(problems))
        raise
    finally:
        if load_method is not None and as_saved is not None:
            with contextlib.suppress(Exception):
                options.ComponentLoadMethod = load_method


def _close_new(tx, originals: set, destination: Path) -> None:
    """Close the parts the clone loaded from ``destination``, assembly first (NX may
    unload its prototypes with it, so the session is listed again after each close)."""
    folder = destination.resolve()
    for _ in range(1000):
        pending = [
            p
            for p in tx.session.Parts
            if int(p.Tag) not in originals
            and parts.file_of(p)
            and Path(parts.file_of(p)).resolve().is_relative_to(folder)
        ]
        if not pending:
            return
        _saving.close_part(tx, pending[0], save=False)
