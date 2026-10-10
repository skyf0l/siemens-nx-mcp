"""User attributes (tool-surface 4, reserved family): read and write NX attributes.

Attributes live on any NX object: the part (title-block data such as part number,
description, material, revision), bodies, faces, features and components. A
component carries its own attributes and reports its prototype part's as
``inherited``. Types map to ``NXObject.AttributeType``: string, integer, number
(real), boolean, date (time). Locked and system-owned attributes are refused.
"""

from __future__ import annotations

import datetime as _dt
import math
import re
from typing import Annotated, Literal, NoReturn, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Ref, Text, forbid_extra
from nx_mcp.kit import readback, result

TYPES = ("string", "integer", "number", "boolean", "date")
NATIVE = {"string": "String", "integer": "Integer", "number": "Real", "boolean": "Boolean",
          "date": "Time"}  # fmt: skip
FIELD = {"string": "StringValue", "integer": "IntegerValue", "number": "RealValue",
         "boolean": "BooleanValue", "date": "TimeValue"}  # fmt: skip
READ_TYPES = ("String", "Integer", "Real", "Boolean", "Time", "Reference", "Null")
OUT_TYPE = {"String": "string", "Integer": "integer", "Real": "number", "Boolean": "boolean",
            "Time": "date", "Reference": "reference", "Null": "null"}  # fmt: skip
INT32 = 2**31 - 1
MAX_STRING = 1024
DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?$")
NX_DATE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})(?: (\d{1,2}):(\d{2})(?::(\d{2}))?)?$")
NX_TEXT_DATE = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})(?: (\d{1,2}):(\d{2})(?::(\d{2}))?)?$")
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")

Title = Annotated[str, Text(50, r"^\S(.{0,48}\S)?$"), Doc("Attribute title")]
Category = Annotated[str, Text(50, r"^\S(.{0,48}\S)?$"), Doc("Attribute category")]


class AttributeRow(TypedDict, total=False):
    name: str
    type: str
    value: str | int | float | bool | None
    category: str
    locked: bool
    inherited: bool
    system: bool


class ObjectAttributes(TypedDict):
    object: result.Row
    attributes: list[AttributeRow]


class GetData(TypedDict):
    items: list[ObjectAttributes]


class SetRow(TypedDict):
    object: result.Row
    attributes: list[AttributeRow]
    deleted: list[str]


class SetData(TypedDict):
    items: list[SetRow]


class _ValueRequired(TypedDict):
    name: Title
    type: Annotated[
        Literal["string", "integer", "number", "boolean", "date"],
        Doc("date: YYYY-MM-DD[Thh:mm[:ss]]"),
    ]
    value: Annotated[str, Text(MAX_STRING)] | bool | int | float


class AttributeValue(_ValueRequired, total=False):
    category: Category


forbid_extra(_ValueRequired, AttributeValue)


# ---- values --------------------------------------------------------------------------
def _check_value(spec: AttributeValue, field: str):
    """The NX value for ``spec`` (validated before NX), else invalid_argument."""
    kind, value = spec["type"], spec["value"]

    def bad(message) -> NoReturn:
        fail("NX_INVALID_ARGUMENT", f"{field}: {message}", field=f"{field}/value")
        raise AssertionError  # fail() always raises

    if kind == "string":
        if not isinstance(value, str):
            bad("a string attribute needs a string value")
        if len(value) > MAX_STRING:
            bad(f"at most {MAX_STRING} characters")
        return value
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
            bad("an integer attribute needs a whole number")
        if abs(int(value)) > INT32:
            bad("integer attributes are 32-bit")
        return int(value)
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            bad("a number attribute needs a number")
        if not math.isfinite(float(value)):
            bad("must be finite")
        return float(value)
    if kind == "boolean":
        if not isinstance(value, bool):
            bad("a boolean attribute needs true or false")
        return value
    # date
    match = DATE.match(value) if isinstance(value, str) else None
    if match is None:
        bad("a date is YYYY-MM-DD or YYYY-MM-DDThh:mm[:ss]")
    y, mo, d, h, mi, s = (int(g) if g else 0 for g in match.groups())
    try:
        when = _dt.datetime(y, mo, d, h, mi, s)
    except ValueError as error:
        bad(str(error))
    if not 1970 <= when.year <= 2105:
        bad("NX dates run from 1970 to 2105")
    return when


def _nx_date(when: _dt.datetime) -> str:
    """The NX date text (``2026/10/09 12:30:00``) NX 2406 parses in any locale."""
    return f"{when:%Y/%m/%d %H:%M:%S}"


def _iso_date(info) -> str | None:
    """``YYYY-MM-DDThh:mm:ss`` from an NX time attribute (TimeValue or StringValue)."""
    for text in (str(getattr(info, "TimeValue", "") or ""), str(info.StringValue or "")):
        text = text.strip()
        match = NX_DATE.match(text)
        if match:
            y, mo, d, h, mi, s = (int(g) if g else 0 for g in match.groups())
            return f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}"
        match = NX_TEXT_DATE.match(text)
        if match and match.group(2).lower() in MONTHS:
            d, mon, y = int(match.group(1)), match.group(2).lower(), int(match.group(3))
            h, mi, s = (int(g) if g else 0 for g in match.groups()[3:])
            mo = MONTHS.index(mon) + 1
            return f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}"
    text = str(info.StringValue or "")
    return text or None


def _type_name(tx, info) -> str:
    return result.enum_name(info.Type, tx.nx.NXObject.AttributeType, READ_TYPES, "String")


def _row(tx, info) -> AttributeRow:
    native_type = _type_name(tx, info)
    kind = OUT_TYPE.get(native_type, "string")
    value: str | int | float | bool | None
    if kind == "integer":
        value = int(info.IntegerValue)
    elif kind == "number":
        value = float(info.RealValue)
    elif kind == "boolean":
        value = bool(info.BooleanValue)
    elif kind == "date":
        value = _iso_date(info)
    elif kind == "null":
        value = None
    else:
        value = str(info.StringValue or "")
    row: AttributeRow = {"name": str(info.Title), "type": kind, "value": value}
    if getattr(info, "Category", None):
        row["category"] = str(info.Category)
    for key, member in (("locked", "Locked"), ("inherited", "Inherited"),
                        ("system", "OwnedBySystem")):  # fmt: skip
        if getattr(info, member, False):
            row[key] = True  # type: ignore[literal-required]  # key: one of the flag names above
    return row


def _scalars(obj) -> list:
    """The object's attributes without array elements (NX lists those one by one)."""
    with native():
        infos = list(obj.GetUserAttributes())
    # NX 2406 returns [None] for an object without attributes.
    return [i for i in infos if i is not None and not getattr(i, "Array", False)]


def _keys(info) -> set[str]:
    """Case-insensitive titles of ``info``: NX matches the title and its alias
    (``MATERIAL`` is the alias of the system attribute ``NX_Material``)."""
    return {str(t).casefold() for t in (info.Title, getattr(info, "TitleAlias", "")) if t}


def _target(tx, ref: str, field: str):
    obj = tx.resolve(ref)
    if not hasattr(obj, "GetUserAttributes"):
        fail("NX_WRONG_KIND", f"{ref} cannot carry attributes", field=field)
    return obj


def _row_of(tx, obj) -> result.Row:
    return tx.ref(obj)


# ---- tools ---------------------------------------------------------------------------
@tool(
    effect="read",
    label="Get attributes",
    summary=lambda a, say: (
        f"Attributes of {len(a['objects'])} object(s)" if a.get("objects") else "Part attributes"
    ),
    keywords=("attribute", "property", "part number", "title block", "metadata", "revision"),
    requires=("work_part",),
    cost="light",
    example={"objects": ["p:1"], "names": ["DB_PART_NO"]},
)
def nx_get_attributes(
    tx: Tx,
    *,
    objects: Annotated[list[Ref], Items(1, 50), Doc("Default: the work part")] | None = None,
    names: Annotated[list[Title], Items(1, 100), Doc("Only these titles")] | None = None,
    include_system: Annotated[bool, Doc("Also NX-owned attributes (NX_...)")] = False,
) -> GetData:
    """Read user attributes of the work part or of parts, bodies, faces, features and components.
    A component also lists its part's attributes as inherited."""
    with tx.checks():
        targets = (
            [_target(tx, ref, f"/objects/{i}") for i, ref in enumerate(objects)]
            if objects
            else [tx.part]
        )
    wanted = {n.casefold() for n in names} if names else None
    items: list[ObjectAttributes] = []
    for obj in targets:
        tx.checkpoint()
        rows = []
        for info in _scalars(obj):
            if wanted is not None and not _keys(info) & wanted:
                continue
            if getattr(info, "OwnedBySystem", False) and not include_system and wanted is None:
                continue
            rows.append(_row(tx, info))
        rows.sort(key=lambda r: (r["name"].casefold(), r["type"]))
        items.append({"object": _row_of(tx, obj), "attributes": rows})
    return {"items": items}


def _summary(a, say):
    values, deleted = a.get("values") or [], a.get("delete") or []
    parts = []
    if values:
        parts.append("set " + ", ".join(v["name"] for v in values[:3]))
    if deleted:
        parts.append("delete " + ", ".join(deleted[:3]))
    target = f"{len(a['objects'])} object(s)" if a.get("objects") else "part"
    return f"Attributes {target}: " + "; ".join(parts or ["none"])


def _existing(obj) -> dict:
    """``{casefolded title or alias: info}``; an object's own attribute wins over an
    inherited one (a component lists its part's attributes as inherited)."""
    found: dict = {}
    for info in sorted(_scalars(obj), key=lambda i: not getattr(i, "Inherited", False)):
        for key in _keys(info):
            found[key] = info
    return found


def _own(existing: dict, name: str):
    info = existing.get(name.casefold())
    return None if info is None or getattr(info, "Inherited", False) else info


def _refuse_read_only(info, ref, name, field):
    if getattr(info, "Locked", False) or getattr(info, "OwnedBySystem", False):
        why = "locked" if getattr(info, "Locked", False) else "owned by NX"
        fail(
            "NX_READ_ONLY",
            f"Attribute {name!r} of {ref} is {why}",
            field=field,
            hint="Ask the engineer to unlock it in NX, or use another title",
            details={"object": ref, "name": name},
        )


@tool(
    effect="model",
    label="Set attributes",
    summary=_summary,
    keywords=("set attribute", "property", "part number", "title block", "revision", "metadata"),
    requires=("work_part",),
    cost="light",
    invalidates="deleted",  # attributes only: every reference stays valid
    example={
        "objects": ["p:1"],
        "values": [{"name": "DB_PART_NO", "type": "string", "value": "100-200"}],
    },
)
def nx_set_attributes(
    tx: Tx,
    *,
    objects: Annotated[list[Ref], Items(1, 50), Doc("Default: the work part")] | None = None,
    values: Annotated[list[AttributeValue], Items(1, 100)] | None = None,
    delete: Annotated[list[Title], Items(1, 100), Doc("Titles to remove first")] | None = None,
) -> SetData:
    """Set user attributes on the work part or on parts, bodies, faces, features, components.
    `delete` runs first. Locked and NX-owned attributes are refused; to change a title's type,
    delete it in the same call."""
    values, delete = list(values or []), list(delete or [])
    if not values and not delete:
        fail("NX_INVALID_ARGUMENT", "Pass values, delete or both", field="/values")
    seen: set[str] = set()
    converted = []
    for i, spec in enumerate(values):
        if spec["name"].casefold() in seen:
            fail("NX_INVALID_ARGUMENT", f"{spec['name']!r} is listed twice", field=f"/values/{i}")
        seen.add(spec["name"].casefold())
        converted.append(_check_value(spec, f"/values/{i}"))
    dropping = {n.casefold() for n in delete}
    if len(dropping) != len(delete):
        fail("NX_INVALID_ARGUMENT", "A title is listed twice", field="/delete")
    types = tx.nx.NXObject.AttributeType
    with tx.checks():
        refs = list(objects) if objects else [None]
        targets = []
        for i, ref in enumerate(refs):
            obj = _target(tx, ref, f"/objects/{i}") if ref else tx.part
            owner = tx.services.owning_part(obj)
            if owner is not None:
                tx.require_owned(owner, field=f"/objects/{i}" if ref else None)
            label = ref or "the work part"
            existing = _existing(obj)
            for j, name in enumerate(delete):
                if _own(existing, name) is not None:
                    _refuse_read_only(existing[name.casefold()], label, name, f"/delete/{j}")
            for j, spec in enumerate(values):
                info = existing.get(spec["name"].casefold())
                if info is None or spec["name"].casefold() in dropping:
                    continue
                _refuse_read_only(info, label, spec["name"], f"/values/{j}/name")
                have = OUT_TYPE.get(_type_name(tx, info), "string")
                if have != spec["type"]:
                    fail(
                        "NX_ALREADY_EXISTS",
                        f"{label} has {spec['name']!r} as a {have} attribute",
                        field=f"/values/{j}/type",
                        hint="Use that type, or delete the title in the same call",
                        details={"object": label, "name": spec["name"], "type": have},
                    )
            targets.append((obj, existing))
    now = tx.nx.Update.Option.Now
    items: list[SetRow] = []
    for obj, existing in targets:
        tx.checkpoint()
        deleted = []
        for name in delete:
            info = _own(existing, name)
            if info is None:
                continue  # already absent: deleting is idempotent
            with native():
                obj.DeleteUserAttribute(info.Type, str(info.Title), True, now)
            deleted.append(str(info.Title))
        for spec, value in zip(values, converted, strict=True):
            info = tx.nx.NXObject.AttributeInformation()
            info.Title = spec["name"]
            info.Type = getattr(types, NATIVE[spec["type"]])
            info.Array = False
            old = _own(existing, spec["name"])
            category = spec.get("category") or (
                getattr(old, "Category", "")
                if old is not None and spec["name"].casefold() not in dropping
                else ""
            )
            if category:
                info.Category = category  # NX clears a category that is not passed again
            setattr(info, FIELD[spec["type"]],
                    _nx_date(value) if spec["type"] == "date" else value)  # fmt: skip
            with native():
                obj.SetUserAttribute(info, now)
        after = _existing(obj)
        rows = []
        for j, (spec, value) in enumerate(zip(values, converted, strict=True)):
            got = _own(after, spec["name"])
            readback.expect(got is not None, "attribute", f"{spec['name']} was not set")
            if str(got.Title).casefold() != spec["name"].casefold():
                fail(
                    "NX_READ_ONLY",
                    f"{spec['name']!r} is NX's reserved alias of {got.Title}",
                    field=f"/values/{j}/name",
                    hint="Use another title; assign materials with nx_set_material",
                    details={"name": spec["name"], "reserved": str(got.Title)},
                )
            row = _row(tx, got)
            _verify(row, spec, value)
            rows.append(row)
        for name in deleted:
            if name.casefold() not in seen:  # deleted and set again: checked above
                gone = _own(after, name) is None
                readback.expect(gone, "delete", f"{name} is still present")
        items.append({"object": _row_of(tx, obj), "attributes": rows, "deleted": deleted})
    return {"items": items}


def _verify(row: AttributeRow, spec: AttributeValue, value) -> None:
    name, kind = spec["name"], spec["type"]
    readback.expect(row["type"] == kind, "type", f"{name} is a {row['type']}", type=row["type"])
    got = row["value"]
    if kind == "number":
        readback.expect_close(got, value, f"value:{name}", tol=1e-9 * max(1.0, abs(value)))
    elif kind == "date":
        want = f"{value:%Y-%m-%dT%H:%M:%S}"
        readback.expect(got == want, f"value:{name}", f"{name} reads {got}", value=got)
    else:
        readback.expect(got == value, f"value:{name}", f"{name} reads {got!r}", value=got)
    if spec.get("category"):
        readback.expect(
            row.get("category") == spec["category"],
            f"category:{name}",
            f"{name} has category {row.get('category')!r}",
            category=row.get("category"),
        )
