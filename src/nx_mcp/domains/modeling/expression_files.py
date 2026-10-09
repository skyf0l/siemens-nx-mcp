"""Expression files (.exp): export the work part's expressions, import a file.

NX 2406 lessons kept here:

- ``ExpressionCollection.ExportToFile(mode, file, sort)`` refuses an existing file
  and writes UTF-8 with a BOM (``[MilliMeter]len=10`` lines, unitless without a
  tag). The export goes to a staging file, published without overwrite; a name
  filter keeps the matching expression lines.
- ``ImportFromFile(file, mode)`` returns ``(modified, messages)`` but raises
  1050053 "Expression import failed." for a syntax error or a new expression
  naming a missing one, sometimes after applying earlier lines; the kernel's undo
  mark rolls that back.
- ``Replace`` silently accepts a changed formula naming a missing expression
  (evaluating it raises 1050007) and a dependency cycle (the model update then
  opens a modal "Error During Update" dialog): both are found before the update.
  A ``[Unit]`` tag never changes an existing expression's units (the number is
  applied as is), so a different tag is refused up front.
- ``DontReplace`` raises 1050053 when the file names any existing expression:
  ``keep`` imports a copy without those lines. ``DeleteImports`` deletes the
  file's expressions that nothing references and changes no value.
- The import changes no geometry until the model update, which runs here; update
  errors roll the import back (``NX_UPDATE_FAILED``).
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, PathTo, Text
from nx_mcp.domains.modeling.tree import feature_messages, solve_driven_sketch, update
from nx_mcp.kit import expr, files, result

NamePattern = Annotated[
    str,
    Text(100, r"^[A-Za-z_*?][A-Za-z0-9_*?]{0,99}$"),
    Doc("Expression name or a * ? wildcard pattern (case-sensitive)"),
]
# One expression line: optional BOM, optional (Type), optional [Unit], then name=...
LINE = re.compile(
    rb"^(?:\xef\xbb\xbf)?\s*(?:\([^)]*\)\s*)?(?:\[([^\]]*)\]\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*="
)
MAX_FILE = 4 * 1024 * 1024
MAX_ROWS = 200
SORTS = {"name": "AlphaNum", "timestamp": "TimeStamp"}
MODES = {"replace": "Replace", "keep": "DontReplace", "delete": "DeleteImports"}


def file_entries(data: bytes) -> list[tuple[str, str | None]]:
    """``(name, unit or None)`` of each expression line of an .exp file, in file order."""
    out = []
    for match in map(LINE.match, data.splitlines()):
        if match:
            unit = match.group(1)
            out.append((match.group(2).decode("ascii"), unit.decode("ascii") if unit else None))
    return out


def file_names(data: bytes) -> list[str]:
    """Expression names defined by the lines of an .exp file, in file order."""
    return [name for name, _ in file_entries(data)]


def keep_lines(data: bytes, patterns, keep=None) -> tuple[bytes, list[str]]:
    """``data`` with only the expression lines whose name matches one of ``patterns``
    (or passes ``keep``); comments and other lines stay. Also returns the kept names."""
    out, kept = [], []
    for line in data.splitlines(keepends=True):
        match = LINE.match(line)
        if match is None:
            out.append(line)
            continue
        name = match.group(2).decode("ascii")
        if keep(name) if keep else any(fnmatch.fnmatchcase(name, p) for p in patterns):
            out.append(line)
            kept.append(name)
    return b"".join(out), kept


class ExportData(TypedDict):
    path: str
    size: int
    sha256: str
    count: int
    names: list[str]
    truncated: bool


@tool(
    effect="file",
    label="Export expressions",
    summary=lambda a, say: f"Export expressions to {say('path')}",
    keywords=("exp file", "expression file", "export parameters", "save expressions"),
    requires=("work_part",),
    cost="light",
    example={"path": "out/block.exp"},
)
def nx_export_expressions(
    tx: Tx,
    *,
    path: PathTo("new", ".exp"),  # type: ignore[valid-type]
    names: Annotated[list[NamePattern], Items(1, 100)] | None = None,
    sort: Literal["name", "timestamp"] = "name",
) -> ExportData:
    """Write the work part's expressions (all, or those matching names) to a new .exp file.
    The model is not changed; an existing file is never overwritten."""
    part = tx.part
    temp = tx.files.stage(path)
    collection = part.Expressions
    with native(default="NX_EXPORT_FAILED"):
        collection.ExportToFile(
            collection.ExportMode.WorkPart, str(temp), getattr(collection.SortType, SORTS[sort])
        )
    data = temp.read_bytes() if temp.is_file() else b""
    exported = file_names(data)
    if names:
        missing = [n for n in names if not any(fnmatch.fnmatchcase(e, n) for e in exported)]
        if missing:
            fail(
                "NX_NOT_FOUND",
                f"No expression of the work part matches {', '.join(missing[:10])}",
                field="/names",
                details={"missing": missing[:50]},
            )
        data, exported = keep_lines(data, names)
        temp.write_bytes(data)
    elif not exported and list(part.Expressions):
        fail("NX_EXPORT_FAILED", "NX wrote no expression lines", details={"size": len(data)})
    published = tx.files.publish()[0]
    meta = files.metadata(published, tx.workspace.root)
    return {
        **meta,  # type: ignore[typeddict-item]
        "count": len(exported),
        "names": exported[:MAX_ROWS],
        "truncated": len(exported) > MAX_ROWS,
    }


class ExpressionRow(TypedDict):
    expression: result.Row
    formula: str
    value: float | None
    units: str


class ChangedRow(ExpressionRow):
    previous: str


class ImportData(TypedDict):
    path: str
    mode: str
    file_expressions: int
    created: list[ExpressionRow]
    changed: list[ChangedRow]
    deleted: list[str]
    skipped: list[str]
    warnings: list[dict]
    truncated: bool


def _state(part) -> dict:
    return {e.Name: (e, str(e.RightHandSide)) for e in part.Expressions}


def _row(tx, e) -> dict:
    return {
        "expression": tx.ref(e, kind="expression"),
        "formula": str(e.RightHandSide),
        "value": expr.value(tx, e),
        "units": expr.units(e),
    }


def _unit_clashes(entries, before) -> list[dict]:
    """File lines that would give an existing expression other units (NX would keep the
    old units silently and apply the number)."""
    out = []
    for name, unit in entries:
        exp = before.get(name, (None,))[0]
        if exp is None or unit is None:
            continue
        own = getattr(exp, "Units", None)
        if own is None or unit not in (getattr(own, "Name", None), own.Abbreviation):
            out.append({"name": name, "file_units": unit, "units": expr.units(exp)})
    return out


def _cyclic(exp) -> bool:
    seen, stack = set(), [exp]
    while stack:
        for dep in expr.dependents(stack.pop()):
            if dep.Tag == exp.Tag:
                return True
            if dep.Tag not in seen:
                seen.add(dep.Tag)
                stack.append(dep)
    return False


def _problems(tx, expressions) -> list[dict]:
    """Imported expressions NX accepted but cannot evaluate. NX 2406 imports a formula
    naming a missing expression, and a dependency cycle, without a message; a cycle
    reaching the model update opens a modal "Error During Update" dialog."""
    out = []
    for e in expressions:
        tx.checkpoint()
        if _cyclic(e):
            out.append({"name": e.Name, "problem": "depends on itself"})
            continue
        try:
            expr.value(tx, e)
        except Exception as error:
            row = {"name": e.Name, "problem": str(error)}
            code = getattr(error, "ErrorCode", None)
            if code is not None:
                row["nx_code"] = int(code)
            out.append(row)
    return out


def _import(tx, source, mode, before) -> list[str]:
    """Run NX's import; returns the file names left alone (``keep``: existing ones)."""
    collection = tx.part.Expressions
    skipped: list[str] = []
    if mode == "keep":  # NX DontReplace fails on any existing name: drop those lines
        data, kept = keep_lines(source.read_bytes(), [], keep=lambda n: n not in before)
        skipped = [n for n in file_names(source.read_bytes()) if n in before]
        if not kept:
            return skipped
        folder = tx.files.folder
        folder.mkdir(parents=True, exist_ok=True)
        source = folder / f"keep_{source.name}"
        source.write_bytes(data)
    try:
        with native(default="NX_IMPORT_FAILED"):
            _modified, messages = collection.ImportFromFile(
                str(source), getattr(collection.ImportMode, MODES[mode])
            )
    except NXError as error:
        error.field = error.field or "/path"
        error.hint = error.hint or (
            "Check the file's syntax; every name a new formula uses must exist in the part "
            "or the file"
        )
        raise
    finally:
        if mode == "keep":
            source.unlink(missing_ok=True)
    messages = [str(m) for m in messages or ()]
    if messages:
        fail(
            "NX_IMPORT_FAILED",
            f"NX refused the expression file: {messages[0]}",
            field="/path",
            details={"messages": messages[:20]},
        )
    return skipped


@tool(
    effect="model",
    label="Import expressions",
    summary=lambda a, say: f"Import expressions from {say('path')}",
    keywords=("exp file", "expression file", "import parameters", "load expressions"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    variants="mode",
    example={"path": "out/block.exp", "mode": "replace"},
)
def nx_import_expressions(
    tx: Tx,
    *,
    path: PathTo("read", ".exp"),  # type: ignore[valid-type]
    mode: Annotated[
        Literal["replace", "keep", "delete"],
        Doc("replace: file formulas win; keep: existing stay; delete: remove unused listed"),
    ] = "replace",
) -> ImportData:
    """Import a workspace .exp file into the work part and update the model. Reports created,
    changed and deleted expressions; any error (missing name, cycle) rolls it back."""
    source = Path(path)
    with tx.checks():
        if source.stat().st_size > MAX_FILE:
            fail("NX_INVALID_ARGUMENT", "Expression files are limited to 4 MB", field="/path")
        entries = file_entries(source.read_bytes())
        if not entries:
            fail("NX_INVALID_ARGUMENT", "The file defines no expression", field="/path")
        before = _state(tx.part)
        clashes = _unit_clashes(entries, before) if mode == "replace" else []
        if clashes:
            fail(
                "NX_INVALID_ARGUMENT",
                f"{clashes[0]['name']} exists in {clashes[0]['units']}; its units cannot change",
                field="/path",
                details={"expressions": clashes[:20]},
            )
    part = tx.part
    skipped = _import(tx, source, mode, before)
    after = _state(part)
    created = [e for name, (e, _) in after.items() if name not in before]
    changed = [
        (e, before[name][1]) for name, (e, rhs) in after.items()
        if name in before and before[name][1] != rhs
    ]  # fmt: skip
    problems = _problems(tx, created + [e for e, _ in changed])
    if problems:
        fail(
            "NX_IMPORT_FAILED",
            f"{problems[0]['name']}: {problems[0]['problem']}",
            field="/path",
            hint="Every name a formula uses must exist, without cycles; fix the file",
            details={"expressions": problems[:20]},
        )
    try:
        update(tx)
    except NXError as error:
        errors = [
            {"feature": f.JournalIdentifier, "message": m}
            for f in part.Features
            for m in feature_messages(f)
        ]
        error.details = {**(error.details or {}), "errors": errors[:20]}
        raise
    if changed:
        solve_driven_sketch(tx, [e for e, _ in changed])
    deleted = sorted(set(before) - set(after))
    if mode == "delete":
        skipped = [name for name, _ in entries if name in after]
    warnings = [
        {"feature": f.JournalIdentifier, "message": m}
        for f in part.Features
        for m in feature_messages(f, "Warning")
    ]
    return {
        "path": files.relative(source, tx.workspace.root),
        "mode": mode,
        "file_expressions": len(entries),
        "created": [_row(tx, e) for e in created[:MAX_ROWS]],  # type: ignore[misc]
        "changed": [{**_row(tx, e), "previous": old} for e, old in changed[:MAX_ROWS]],  # type: ignore[misc,typeddict-item]
        "deleted": deleted[:MAX_ROWS],
        "skipped": skipped[:MAX_ROWS],
        "warnings": warnings[:50],
        "truncated": max(map(len, (created, changed, deleted, skipped))) > MAX_ROWS,
    }
