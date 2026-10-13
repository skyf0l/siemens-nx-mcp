"""Part families (native NX): a family table on a template part and its member parts.

NX 2406 findings encoded here (``Part.NewPartFamilyTemplateManager``):

- The manager acts on the work part only ("Object must belong to the work part").
- Family columns are chosen before ``CreatePartFamily``, which commits them: columns
  added afterwards stay uncommitted (``SavePartFamily`` drops them and
  ``AddInstanceDefinition`` fails "Uncommited changes exist").
- A native (non-importable) family starts with the ``Part_Name`` column; a member's
  name is its file name in ``SaveDirectory`` (NX's working directory unless set, so it
  is always set). ``SaveFamilyAndCreateMembers`` writes each member file (0 = created)
  without loading it and without saving the template; members are current only once
  the template, which stores the table, is saved.
- Writing the members of a template that keeps drawing CGM preview data shows NX's
  modal "Save CGM" dialog once per member (their sheets cannot be displayed): the
  template's ``SaveOptions.DrawingCgmData`` is off while NX writes them
  (``kit.parts.without_cgm``), so member files carry no CGM preview. The template
  itself is then saved with its option restored and its preview kept.
- An NX error inside the manager's calls is not rolled back by NX and can leave the
  session unstable: every argument is checked first (expression names against
  ``GetSelectableAttributes``, member names, files) so NX is never asked anything it
  would refuse.
- ``NXOpen.PartFamily`` may not be imported in the host: its enums are read from the
  manager's own attribute objects.
"""

from __future__ import annotations

import contextlib
import math
import re
from pathlib import Path
from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.core.tx import Tx, check_owned
from nx_mcp.core.types import Doc, Items, PathTo, Text
from nx_mcp.domains.parts_files import _saving
from nx_mcp.kit import args, files, parts, readback, result
from nx_mcp.kit import expr as kit_expr

MEMBER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
FORMULA_MAX = 200
NAME_COLUMN = "Part_Name"
EXPRESSION_TYPE = "Expression"
MEMBER_HINT = "Use letters, digits, _ and - (the member's file name without .prt)"
TEMPLATE_NOTE = (
    "The template part holds the new, unsaved family table; close it without saving "
    "(nx_close_part save=false) to discard it, or call again after fixing the cause"
)
SAVED_NOTE = (
    "The template part was saved with the family table and the member files were written; "
    "check them with nx_get_part_family"
)

Value = float | Annotated[str, Text(FORMULA_MAX)]


class Member(TypedDict):
    name: Annotated[str, Text(64), Doc("New member part name = its file name without .prt")]
    values: Annotated[
        list[Value],
        Items(1, 30),
        Doc(
            "One per expressions entry, same order: a number in that expression's units or a "
            "formula"
        ),  # fmt: skip
    ]


class MemberRow(TypedDict):
    name: str
    path: str
    values: Annotated[dict, Doc("Expression values read back from the member file")]
    up_to_date: Annotated[bool | None, Doc("null: NX cannot tell (template not loaded)")]


class FamilyData(TypedDict):
    template: result.Row
    template_saved: bool
    directory: str
    expressions: list[str]
    members: list[MemberRow]


class FamilyRow(TypedDict):
    name: str
    values: dict
    path: str | None
    exists: bool | None


class FamilyInfo(TypedDict):
    role: Annotated[str, Doc("template, member or none")]
    part: result.Row
    template: str | None
    directory: str | None
    columns: list[str]
    members: list[FamilyRow]
    up_to_date: bool | None


# --- argument checks (no NX) -------------------------------------------------------


def _check_arguments(expressions, members) -> list[tuple[str, list[str]]]:
    """``[(member name, [formula text per expression])]``; refuses bad input."""
    args.distinct("/expressions", expressions, "expressions")
    for i, name in enumerate(expressions):
        if not kit_expr.NAME.fullmatch(name):
            fail("NX_INVALID_ARGUMENT", f"{name!r} is not an expression name",
                 field=f"/expressions/{i}")  # fmt: skip
    seen: dict[str, int] = {}
    out = []
    for i, member in enumerate(members):
        name = member["name"]
        if not MEMBER_NAME.fullmatch(name):
            fail("NX_INVALID_ARGUMENT", f"{name!r} is not a valid member name",
                 field=f"/members/{i}/name", hint=MEMBER_HINT)  # fmt: skip
        if name.casefold() in seen:
            fail("NX_INVALID_ARGUMENT", f"Member {name} is listed twice (names ignore case)",
                 field=f"/members/{i}/name")  # fmt: skip
        seen[name.casefold()] = i
        values = member["values"]
        if len(values) != len(expressions):
            fail(
                "NX_INVALID_ARGUMENT",
                f"Member {name} has {len(values)} values for {len(expressions)} expressions",
                field=f"/members/{i}/values",
                hint="Give one value per expressions entry, in the same order",
            )
        texts = []
        for k, value in enumerate(values):
            texts.append(_formula(value, f"/members/{i}/values/{k}"))
        out.append((name, texts))
    return out


def _formula(value, field: str) -> str:
    if isinstance(value, bool):
        fail("NX_INVALID_ARGUMENT", "A value is a number or a formula", field=field)
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            fail("NX_INVALID_ARGUMENT", "A value must be finite", field=field)
        return repr(float(value)) if not float(value).is_integer() else str(int(value))
    text = str(value).strip()
    if not text or any(c in text for c in "\r\n;"):
        fail("NX_INVALID_ARGUMENT", "A formula is one non-empty line without ';'", field=field)
    return text


def _member_paths(tx, directory: Path, names) -> list[Path]:
    paths = []
    for i, name in enumerate(names):
        path = directory / f"{name}.prt"
        field = f"/members/{i}/name"
        if path.exists():
            fail(
                "NX_ALREADY_EXISTS",
                f"{path.name} already exists in the member folder; files are never overwritten",
                field=field,
                hint="Choose another member name or folder",
                details={"path": files.relative(path, tx.workspace.root)},
            )
        parts.check_new_name(tx, path, field=field)
        paths.append(path)
    return paths


# --- NX ----------------------------------------------------------------------------


def _uf_flag(tx, name: str, part) -> bool | None:
    """``UF_PART_is_family_template`` / ``..._instance``; None when NX cannot tell."""
    try:
        answer = getattr(tx.uf.Part, name)(part.Tag)
    except Exception:
        return None
    return bool(answer[0] if isinstance(answer, tuple) else answer)


def _attr_type(manager):
    """``PartFamily.FamilyAttribute.AttrType`` from the manager's default column."""
    chosen = list(manager.GetChosenAttributes())
    if not chosen:
        fail("NX_ERROR", "NX offered no default part family column")
    return type(chosen[0]).AttrType


def _column(chosen, name):
    return next((a for a in chosen if str(a.Name) == name), None)


def _close_quiet(tx, part) -> None:
    with contextlib.suppress(Exception):
        part.Close(
            tx.nx.BasePart.CloseWholeTree.FalseValue,
            tx.nx.BasePart.CloseModified.CloseModified,
            None,
        )


def _read_member(tx, path: Path, expressions) -> tuple[dict, bool | None]:
    """Open a member file without displaying it, read its expressions, close it."""
    with native(default="NX_FILE_OPEN_FAILED"):
        member, status = tx.session.Parts.OpenBase(str(path))
    with contextlib.suppress(Exception):
        status.Dispose()
    try:
        found = {str(e.Name): e for e in member.Expressions}
        values = {}
        for name in expressions:
            exp = found.get(name)
            values[name] = float(exp.Value) if exp is not None else None
        return values, _current(tx, member)
    finally:
        _close_quiet(tx, member)


def _current(tx, member) -> bool | None:
    """``UF_PART_is_family_inst_current`` (NX 2406 ``Instance.IsOutOfDate`` says true even
    right after ``SaveFamilyAndUpdateMembers``); None when NX cannot tell (template not
    loaded: "Family is not loaded")."""
    try:
        answer = tx.uf.Part.IsFamilyInstCurrent(member.Tag)
    except Exception:
        return None
    return bool(answer[0] if isinstance(answer, tuple) else answer)


def _plain_value(text: str) -> float | None:
    """The value a plain number formula sets; None for other formulas (not checked)."""
    try:
        return float(text)
    except ValueError:
        return None


@tool(
    switch="work",
    effect="file",
    label="Create part family",
    summary=lambda a, say: (
        f"Create a part family of {len(a.get('members') or [])} members from "
        + ", ".join(a.get("expressions") or [])
    ),
    keywords=("part family", "family table", "family members", "configurations", "variants"),
    requires=("work_part", "no_active_sketch"),
    cost="long",
    example={
        "expressions": ["length", "width"],
        "members": [{"name": "plate_100", "values": [100, 50]}],
        "directory": "parts/plates",
    },
)
def nx_create_part_family(
    tx: Tx,
    *,
    expressions: Annotated[
        list[Annotated[str, Text(100)]],
        Items(1, 30),
        Doc("Template expressions that become family columns"),
    ],
    members: Annotated[list[Member], Items(1, 100)],
    directory: Annotated[
        str, PathTo("dir"), Doc("Folder for member files (default: the template's)")
    ]
    | None = None,
) -> FamilyData:
    """Create a native part family on the saved work part and write its member parts. Each
    member is a new .prt (name.prt in directory) holding the template with the given
    expression values; existing files are never overwritten. The template part is saved
    afterwards (it stores the family table). Refused when the part already has a family or
    is a member; list it with nx_get_part_family."""
    rows = _check_arguments(expressions, members)
    with tx.checks():
        part = tx.part
        check_owned(tx.services, part)
        template_file = parts.in_workspace(tx, part)
        folder = Path(directory) if directory else Path(template_file).parent
        if folder.exists() and not folder.is_dir():
            fail("NX_WRONG_KIND", "The member folder is a file", field="/directory")
        paths = _member_paths(tx, folder, [name for name, _ in rows])
        if _uf_flag(tx, "IsFamilyTemplate", part):
            fail(
                "NX_ALREADY_EXISTS",
                "The work part already has a part family",
                hint="List it with nx_get_part_family; NX edits family tables interactively",
            )
        if _uf_flag(tx, "IsFamilyInstance", part):
            fail("NX_INVALID_ARGUMENT", "The work part is a part family member, not a template",
                 hint="Open the family's template part instead")  # fmt: skip
        known = {str(e.Name): e for e in part.Expressions}
        for i, name in enumerate(expressions):
            if name not in known:
                fail(
                    "NX_NOT_FOUND",
                    f"The work part has no expression {name}",
                    field=f"/expressions/{i}",
                    hint="List them with nx_list_objects kind=expression",
                )
    folder.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    manager = None
    family_made = False
    try:
        with native(default="NX_ERROR"):
            manager = part.NewPartFamilyTemplateManager()
            kinds = _attr_type(manager)
            selectable = {str(n) for n in manager.GetSelectableAttributes(kinds.Expression)}
            missing = [n for n in expressions if n not in selectable]
            if missing:
                fail(
                    "NX_INVALID_ARGUMENT",
                    f"NX cannot use expression {missing[0]} as a family column",
                    field=f"/expressions/{expressions.index(missing[0])}",
                    hint="Choose expressions NX lists for part families (not locked, interpart "
                    "or measurement expressions)",
                )
            manager.Importable = False
            manager.SaveDirectory = str(folder)
            readback.expect(
                parts.same_file(str(manager.SaveDirectory), str(folder)) or
                Path(str(manager.SaveDirectory)).resolve() == folder.resolve(),
                "save_directory", directory=str(manager.SaveDirectory),
            )  # fmt: skip
            chosen = list(manager.GetChosenAttributes())
            manager.AddToChosenAttributes(
                list(expressions), [kinds.Expression] * len(expressions), len(chosen)
            )
            tx.non_undoable("part family table created on the template part")
            family = manager.CreatePartFamily()
            family_made = True
            committed = [str(a.Name) for a in family.GetAttributes()]
            readback.expect(
                all(n in committed for n in expressions), "columns", committed=committed
            )
            chosen = list(manager.GetChosenAttributes())
            definitions = []
            previous = None
            for name, texts in rows:
                definition = manager.AddInstanceDefinition(name, previous, "")
                for column_name, text in zip(expressions, texts, strict=True):
                    definition.SetValueOfAttribute(_column(chosen, column_name), text)
                stored = {c: str(definition.GetValueOfAttribute(_column(chosen, c)))
                          for c in expressions}  # fmt: skip
                readback.expect(
                    all(stored[c] == t for c, t in zip(expressions, texts, strict=True)),
                    "member_values", member=name, stored=stored,
                )  # fmt: skip
                definitions.append(definition)
                previous = definition
            with parts.without_cgm([part]):  # no modal "Save CGM" dialog per member
                codes = [int(c) for c in manager.SaveFamilyAndCreateMembers(definitions)]
            created = [p for p in paths if p.is_file()]
            if any(codes) or len(created) != len(paths):
                messages = [str(m) for m in (manager.GetInfoMessages() or [])][:20]
                fail(
                    "NX_ERROR",
                    "NX did not create every family member",
                    details={"codes": dict(zip([n for n, _ in rows], codes, strict=False)),
                             "messages": messages},
                )  # fmt: skip
            members_named = [str(m) for m in family.GetMembers()]
            readback.expect(
                [m.casefold() for m in members_named] == [n.casefold() for n, _ in rows],
                "members", members=members_named,
            )  # fmt: skip
    except NXError as error:
        _undo_members(tx, created or [p for p in paths if p.is_file()])
        if family_made:
            error.details.setdefault("template_note", TEMPLATE_NOTE)
        raise
    finally:
        if manager is not None:
            with contextlib.suppress(Exception):
                manager.Dispose()
    try:
        _saving.verified_save(tx, part)
    except NXError as error:
        _undo_members(tx, created)
        error.details.setdefault("template_note", TEMPLATE_NOTE)
        raise
    try:
        out = []
        for (name, texts), path in zip(rows, paths, strict=True):
            values, current = _read_member(tx, path, expressions)
            for column, text in zip(expressions, texts, strict=True):
                wanted = _plain_value(text)
                if wanted is not None:
                    readback.expect_close(values[column], wanted, f"member {name} {column}")
            out.append({
                "name": name,
                "path": files.relative(path, tx.workspace.root),
                "values": values,
                "up_to_date": current,
            })  # fmt: skip
    except NXError as error:
        error.details.setdefault("template_note", SAVED_NOTE)
        raise
    return {
        "template": tx.ref(part, "part"),
        "template_saved": True,
        "directory": files.relative(folder, tx.workspace.root),
        "expressions": list(expressions),
        "members": out,  # type: ignore[typeddict-item]
    }


def _undo_members(tx, paths) -> None:
    """Remove member files this call wrote (they did not exist before it)."""
    for path in paths:
        loaded = parts.loaded(tx, path)
        if loaded is not None:
            _close_quiet(tx, loaded)
        with contextlib.suppress(OSError):
            Path(path).unlink()


def _shown(tx, path: Path) -> str:
    try:
        return files.relative(path, tx.workspace.root)
    except Exception:
        return str(path)


def _template_info(tx, part) -> tuple[str | None, str | None, list[str], list[FamilyRow]]:
    """Name, member folder, columns and member rows of the part's family (UF reads only:
    a template manager would mark the part modified)."""
    uf = tx.uf
    with native(default="NX_ERROR"):
        _count, families = uf.Part.AskFamilies(part.Tag)
        if not families:
            return None, None, [], []
        family = families[0]
        data = uf.Fam.AskFamilyData(family)
        names = [str(uf.Fam.AskAttributeData(a).Name) for a in data.Attributes]
        folder = None
        with contextlib.suppress(Exception):
            folder = str(uf.Part.AskFamilySaveDir(family)) or None
        rows: list[FamilyRow] = []
        for row in range(int(data.MemberCount)):
            values = [str(v) for v in uf.Fam.AskMemberRowData(family, row).Values]
            table = dict(zip(names, values, strict=False))
            name = table.pop(NAME_COLUMN, values[0] if values else "")
            path = Path(folder) / f"{name}.prt" if folder else None
            rows.append({
                "name": name,
                "values": table,
                "path": _shown(tx, path) if path else None,
                "exists": path.is_file() if path else None,
            })  # fmt: skip
    columns = [n for n in names if n != NAME_COLUMN]
    return str(data.Name), _shown(tx, Path(folder)) if folder else None, columns, rows


@tool(
    effect="read",
    label="Get part family",
    summary="Get the part family of the work part",
    keywords=("part family", "family table", "family members", "template part", "member of"),
    requires=("work_part",),
    cost="light",
)
def nx_get_part_family(tx: Tx) -> FamilyInfo:
    """Get the part family of the work part: its role (template, member or none). A template
    lists its columns (expressions) and member rows with each member file's path and whether
    it exists; a member names its template and says whether it is up to date. Create a
    family with nx_create_part_family."""
    part = tx.part
    info: FamilyInfo = {
        "role": "none", "part": tx.ref(part, "part"), "template": None, "directory": None,
        "columns": [], "members": [], "up_to_date": None,
    }  # fmt: skip
    if _uf_flag(tx, "IsFamilyTemplate", part):
        name, folder, columns, rows = _template_info(tx, part)
        info["role"], info["template"], info["directory"] = "template", name, folder
        info["columns"], info["members"] = columns, rows
    elif _uf_flag(tx, "IsFamilyInstance", part):
        info["role"] = "member"
        with contextlib.suppress(Exception):  # "Family is not loaded": template not loaded
            family = part.GetFamilyInstance().Family
            info["template"] = str(family.TemplateName) if family is not None else None
        info["up_to_date"] = _current(tx, part)
    return info
