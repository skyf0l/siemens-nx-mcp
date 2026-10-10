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
  file's expressions that nothing references and changes no value; it raises
  1050053 for a name the part lacks, so ``delete`` imports a copy without those.
- 1050053 names no line: on that failure the formulas are scanned for names that
  neither the part nor the file defines (``unknown_names``) to say which line.
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
from nx_mcp.domains.modeling.tree import (
    driven_features,
    feature_messages,
    solve_driven_sketch,
    update,
)
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


class FileRow(TypedDict):
    name: str
    formula: str
    units: str


def _formula_text(rest: str) -> str:
    """A line's formula without a trailing ``//`` comment (outside double quotes)."""
    quoted = False
    for i, ch in enumerate(rest):
        if ch == '"':
            quoted = not quoted
        elif not quoted and rest.startswith("//", i):
            return rest[:i].strip()
    return rest.strip()


def file_rows(data: bytes) -> list[FileRow]:
    """``{name, formula, units}`` of each expression line, in file order (units as
    ``mm``/``in``/``deg``/``rad`` where known, else NX's tag, ``unitless`` without one)."""
    short = {v: k for k, v in expr.UNIT_NAMES.items() if v}
    out: list[FileRow] = []
    for line in data.splitlines():
        match = LINE.match(line)
        if match:
            unit = match.group(1).decode("ascii", "replace") if match.group(1) else None
            out.append({
                "name": match.group(2).decode("ascii"),
                "formula": _formula_text(line[match.end():].decode("utf-8", "replace")),
                "units": short.get(unit, unit) if unit else "unitless",
            })  # fmt: skip
    return out


# A name a formula uses: not inside a longer token, not a function call, not interpart.
_USED = re.compile(r'(?<![\w.:"])([A-Za-z_][A-Za-z0-9_]*)\b(?!\s*\()')
_WORDS = {"if", "else", "then", "true", "false", "and", "or", "not"}


def unknown_names(rows, known) -> list[dict]:
    """``[{name, uses}]``: file lines whose formulas use names absent from ``known``."""
    out = []
    for row in rows:
        text = re.sub(r'"[^"]*"', '""', row["formula"])
        uses = sorted({
            n for n in _USED.findall(text) if n not in known and n.casefold() not in _WORDS
        })  # fmt: skip
        if uses:
            out.append({"name": row["name"], "uses": uses})
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
    expressions: list[FileRow]
    truncated: bool
    overwritten: bool


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
    path: Annotated[str, PathTo("replace", ".exp")],
    names: Annotated[list[NamePattern], Items(1, 100)] | None = None,
    sort: Literal["name", "timestamp"] = "name",
    overwrite: bool = False,
) -> ExportData:
    """Write the work part's expressions (all, or those matching names) to an .exp file. Returns
    each name, formula and units; the model is not changed. overwrite replaces a file."""
    target = Path(path)
    replacing = target.exists()
    if replacing and not overwrite:
        fail(
            "NX_ALREADY_EXISTS",
            "/path: a file already exists there; set overwrite=true to replace it",
            field="/path",
            details={"path": path},
        )
    part = tx.part
    temp = tx.files.stage(target, replace=overwrite)
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
                hint="Names are case-sensitive; * and ? are wildcards (see details.available)",
                details={"missing": missing[:50], "available": exported[:MAX_ROWS]},
            )
        data, exported = keep_lines(data, names)
        temp.write_bytes(data)
    elif not exported and list(part.Expressions):
        fail("NX_EXPORT_FAILED", "NX wrote no expression lines", details={"size": len(data)})
    published = tx.files.publish()[0]
    meta = files.metadata(published, tx.workspace.root)
    rows = file_rows(data)
    return {
        **meta,
        "count": len(rows),
        "expressions": rows[:MAX_ROWS],
        "truncated": len(rows) > MAX_ROWS,
        "overwritten": replacing,
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
    features: list[result.Row]
    truncated: bool


def _state(part) -> dict:
    return {e.Name: (e, str(e.RightHandSide)) for e in part.Expressions}


def _row(tx, e) -> ExpressionRow:
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
    """Run NX's import; returns the file names left alone (``keep``: existing ones,
    ``delete``: ones the part lacks)."""
    collection = tx.part.Expressions
    data = source.read_bytes()
    skipped: list[str] = []
    copy = None
    if mode in ("keep", "delete"):  # DontReplace fails on an existing name, delete on a new
        wanted = (lambda n: n not in before) if mode == "keep" else (lambda n: n in before)
        data, kept = keep_lines(data, [], keep=wanted)
        skipped = [n for n in file_names(source.read_bytes()) if not wanted(n)]
        if not kept:
            return skipped
        if skipped:
            folder = tx.files.folder
            folder.mkdir(parents=True, exist_ok=True)
            copy = source = folder / f"{mode}_{source.name}"
            source.write_bytes(data)
    try:
        with native(default="NX_IMPORT_FAILED"):
            _modified, messages = collection.ImportFromFile(
                str(source), getattr(collection.ImportMode, MODES[mode])
            )
    except NXError as error:
        _explain(error, data, before)
        raise
    finally:
        if copy is not None:
            copy.unlink(missing_ok=True)
    messages = [str(m) for m in messages or ()]
    if messages:
        fail(
            "NX_IMPORT_FAILED",
            f"NX refused the expression file: {messages[0]}",
            field="/path",
            details={"messages": messages[:20]},
        )
    return skipped


def _explain(error: NXError, data: bytes, before) -> None:
    """NX's 1050053 names no line: point at formulas using names nothing defines."""
    error.field = error.field or "/path"
    rows = file_rows(data)
    unknown = unknown_names(rows, set(before) | {r["name"] for r in rows})
    if unknown:
        first = unknown[0]
        error.message = (
            f"{error.message.rstrip('.')}: {first['name']} uses {', '.join(first['uses'])}, "
            "which neither the part nor the file defines"
        )
        error.details = {**(error.details or {}), "unknown_names": unknown[:20]}
    error.hint = error.hint or (
        "Check the file's syntax (name=formula per line, units as a [MilliMeter] prefix); "
        "every name a new formula uses must exist in the part or the file"
    )


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
    path: Annotated[str, PathTo("read", ".exp")],
    mode: Annotated[
        Literal["replace", "keep", "delete"],
        Doc("replace: file formulas win; keep: existing stay; delete: remove listed unused"),
    ] = "replace",
) -> ImportData:
    """Import a workspace .exp file into the work part and update the model. Reports created,
    changed, deleted and skipped expressions and the features they drive; an error (missing
    name, cycle) rolls it back."""
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
        skipped = [name for name, _ in entries if name not in deleted]
    warnings = [
        {"feature": f.JournalIdentifier, "message": m}
        for f in part.Features
        for m in feature_messages(f, "Warning")
    ]
    return {
        "path": files.relative(source, tx.workspace.root),
        "mode": mode,
        "file_expressions": len(entries),
        "created": [_row(tx, e) for e in created[:MAX_ROWS]],
        "changed": [{**_row(tx, e), "previous": old} for e, old in changed[:MAX_ROWS]],
        "deleted": deleted[:MAX_ROWS],
        "skipped": skipped[:MAX_ROWS],
        "warnings": warnings[:50],
        "features": driven_features(tx, [e for e, _ in changed]) if changed else [],
        "truncated": max(map(len, (created, changed, deleted, skipped))) > MAX_ROWS,
    }
