"""User attributes (tool-surface 4, reserved family): read and write NX attributes.

Attributes live on any NX object: the part (title-block data such as part number,
description, material, revision), bodies, faces, features and components. A
component carries its own attributes and reports its prototype part's as
``inherited``. Types map to ``NXObject.AttributeType``: string, integer, number
(real), boolean, date (time). Locked and system-owned attributes are refused.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import math
import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

from nx_mcp.core.catalogue import restorer, tool
from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.core.target import file_name
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Json, Ref, Text, forbid_extra
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
# ISO dates; also YYYY/MM/DD, one-digit fields, a space for T, fractions and a final Z.
DATE = re.compile(
    r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?)?Z?$"
)
NX_DATE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})(?: (\d{1,2}):(\d{2})(?::(\d{2}))?)?$")
NX_TEXT_DATE = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})(?: (\d{1,2}):(\d{2})(?::(\d{2}))?)?$")
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")

# Titles and categories are trimmed (``_title``), so " Qty" reads and writes QTY's row.
Title = Annotated[str, Text(50), Doc("Attribute title")]
Category = Annotated[str, Text(50)]
BOOLEAN_TEXT = {"true": True, "yes": True, "false": False, "no": False}
NUMBER_TEXT = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
# Kinds NX 2406 refuses user attributes on (580023 "Functionality attempted not possible in
# the current version of the OM" for an expression).
NO_ATTRIBUTES = frozenset({"expression"})
UNSUPPORTED_OBJECT = 580023


class AttributeRow(TypedDict, total=False):
    name: str
    type: str
    value: str | int | float | bool | None
    category: str
    locked: bool
    inherited: bool
    system: bool


class _ObjectAttributes(TypedDict):
    object: result.Row
    attributes: list[AttributeRow]


class ObjectAttributes(_ObjectAttributes, total=False):
    missing: list[str]  # requested names the object does not carry


class GetData(TypedDict):
    items: list[ObjectAttributes]


class SetRow(TypedDict):
    object: result.Row
    attributes: list[AttributeRow]
    created: list[str]  # titles that did not exist before (the others were updated)
    deleted: list[str]


class SetData(TypedDict):
    items: list[SetRow]


Kind = Literal["string", "integer", "number", "boolean", "date"]


class _ValueRequired(TypedDict):
    name: Title
    value: Annotated[str, Text(MAX_STRING)] | bool | int | float


class AttributeValue(_ValueRequired, total=False):
    type: Annotated[Kind, Doc("Default: existing or from value; date YYYY-MM-DD")]
    category: Category


forbid_extra(_ValueRequired, AttributeValue)


@dataclass
class _Want:
    """One attribute to write: trimmed title, NX type, checked value and its field."""

    name: str
    kind: str
    value: Any
    category: str
    field: str  # "/values/0" or "/attributes/QTY"
    raw: Any  # the value as sent, for an omitted type that adopts the existing one
    explicit: bool  # type given by the caller

    @property
    def listed(self) -> bool:
        return self.field.startswith("/values/")

    @property
    def name_field(self) -> str:
        return f"{self.field}/name" if self.listed else self.field

    @property
    def type_field(self) -> str:
        return f"{self.field}/type" if self.listed else self.field

    @property
    def value_field(self) -> str:
        return f"{self.field}/value" if self.listed else self.field


# ---- values --------------------------------------------------------------------------
def _title(text: str, field: str) -> str:
    """``text`` trimmed: one visible line (NX keeps titles and categories on one line)."""
    title = text.strip()
    if not title or any(c in title for c in "\r\n\t"):
        fail(
            "NX_INVALID_ARGUMENT",
            f"{field}: a title is one line of visible text, at most 50 characters",
            field=field,
        )
    return title


def _infer(value) -> str:
    """The attribute type a JSON value stands for: text is a string, never a date."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    return "number" if isinstance(value, float) else "string"


def _convert(kind: str, value):
    """``value`` as an NX value of ``kind``, else ValueError with what to send. Numeric
    text converts to numbers and true/false/yes/no to booleans; numbers become text."""
    if kind == "string":
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError("a string attribute needs text")
        text = value if isinstance(value, str) else repr(value)
        if len(text) > MAX_STRING:
            raise ValueError(f"at most {MAX_STRING} characters")
        return text
    if kind in ("integer", "number"):
        number = value
        if isinstance(value, str) and NUMBER_TEXT.match(value.strip()):
            number = float(value.strip())
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            article = "an" if kind == "integer" else "a"
            raise ValueError(f"{article} {kind} attribute needs a number, e.g. 4")
        if not math.isfinite(float(number)):
            raise ValueError("must be finite")
        if kind == "number":
            return float(number)
        if number != int(number):
            raise ValueError("an integer attribute needs a whole number, e.g. 4")
        if abs(int(number)) > INT32:
            raise ValueError("integer attributes are 32-bit")
        return int(number)
    if kind == "boolean":
        if isinstance(value, str) and value.strip().casefold() in BOOLEAN_TEXT:
            return BOOLEAN_TEXT[value.strip().casefold()]
        if not isinstance(value, bool):
            raise ValueError("a boolean attribute needs true or false")
        return value
    match = DATE.match(value.strip()) if isinstance(value, str) else None
    if match is None:
        raise ValueError("a date is YYYY-MM-DD or YYYY-MM-DDThh:mm[:ss]")
    y, mo, d, h, mi, s = (int(g) if g else 0 for g in match.groups())
    try:
        when = _dt.datetime(y, mo, d, h, mi, s)
    except ValueError as error:
        raise ValueError(str(error)) from None
    if not 1970 <= when.year <= 2105:
        raise ValueError("NX dates run from 1970 to 2105")
    return when


def _want(name: str, value, kind: str | None, category: str | None, field: str) -> _Want:
    """A checked attribute (before NX): explicit ``kind`` or the value's own type."""
    want = _Want("", kind or _infer(value), None, "", field, value, kind is not None)
    want.name = _title(name, want.name_field)
    want.category = _title(category, f"{field}/category") if category else ""
    try:
        want.value = _convert(want.kind, value)
    except ValueError as error:
        fail("NX_INVALID_ARGUMENT", f"{want.value_field}: {error}", field=want.value_field)
    return want


def _wants(values: list[AttributeValue], attributes: dict) -> list[_Want]:
    """``values`` then ``attributes`` (``{title: value}``) as checked writes; a title
    listed twice is refused."""
    out = [
        _want(v["name"], v["value"], v.get("type"), v.get("category"), f"/values/{i}")
        for i, v in enumerate(values)
    ]
    for name, value in attributes.items():
        field = f"/attributes/{name}"
        if not isinstance(name, str) or len(name) > 50:
            fail("NX_INVALID_ARGUMENT", f"{field}: titles are at most 50 characters", field=field)
        if value is None or isinstance(value, (list, dict)):
            fail(
                "NX_INVALID_ARGUMENT",
                f"{field}: a value is text, a number or true/false; use values for a type",
                field=field,
            )
        out.append(_want(name, value, None, None, field))
    seen: set[str] = set()
    for want in out:
        if want.name.casefold() in seen:
            fail("NX_INVALID_ARGUMENT", f"{want.name!r} is listed twice", field=want.field)
        seen.add(want.name.casefold())
    return out


def _adopt(want: _Want, have: str) -> _Want:
    """``want`` in the existing attribute's type ``have`` when the caller gave no type and
    the value converts (4 for a number, "4" for an integer, "2027-01-31" for a date)."""
    if want.explicit or want.kind == have or have not in TYPES:
        return want
    try:
        value = _convert(have, want.raw)
    except ValueError as error:
        if have == "date" and isinstance(want.raw, str) and DATE.match(want.raw.strip()):
            # A date-shaped text for a date attribute: its own problem, not a type clash.
            fail("NX_INVALID_ARGUMENT", f"{want.value_field}: {error}", field=want.value_field)
        return want
    return _Want(want.name, have, value, want.category, want.field, want.raw, False)


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
    kind = None
    try:
        kind = tx.kind_of(obj)
    except Exception:
        kind = None
    if not hasattr(obj, "GetUserAttributes") or kind in NO_ATTRIBUTES:
        fail(
            "NX_WRONG_KIND",
            f"{ref} cannot carry user attributes" + (f" (a {kind})" if kind else ""),
            field=field,
            hint="Attributes go on parts, bodies, faces, edges, features and components; an "
            "expression's value is set with nx_set_expression",
            details={"kind": kind},
        )
    return obj


def _row_of(tx, obj) -> result.Row:
    return tx.ref(obj)


# ---- tools ---------------------------------------------------------------------------
@tool(
    target="assembly",
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
    titles = [_title(n, f"/names/{i}") for i, n in enumerate(names or [])]
    with tx.checks():
        targets = (
            [_target(tx, ref, f"/objects/{i}") for i, ref in enumerate(objects)]
            if objects
            else [tx.part]
        )
    wanted = {n.casefold() for n in titles} if names else None
    items: list[ObjectAttributes] = []
    for obj in targets:
        tx.checkpoint()
        rows, found = [], set()
        for info in _scalars(obj):
            if wanted is not None and not _keys(info) & wanted:
                continue
            if getattr(info, "OwnedBySystem", False) and not include_system and wanted is None:
                continue
            rows.append(_row(tx, info))
            found |= _keys(info)
        rows.sort(key=lambda r: (r["name"].casefold(), r["type"]))
        item: ObjectAttributes = {"object": _row_of(tx, obj), "attributes": rows}
        if wanted is not None:
            item["missing"] = [n for n in titles if n.casefold() not in found]
        items.append(item)
    return {"items": items}


def _summary(a, say):
    values, deleted = a.get("values") or [], a.get("delete") or []
    names = [v["name"] for v in values] + list(a.get("attributes") or {})
    parts = []
    if names:
        parts.append("set " + ", ".join(str(n) for n in names[:3]))
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


def _plan(tx, wants: list[_Want], existing: dict, dropping: set, label: str) -> list[_Want]:
    """``wants`` for one object: an omitted type adopts the existing attribute's; a
    read-only title or one of another type is refused before NX is touched."""
    planned = []
    for want in wants:
        info = existing.get(want.name.casefold())
        if info is not None and want.name.casefold() not in dropping:
            _refuse_read_only(info, label, want.name, want.name_field)
            have = OUT_TYPE.get(_type_name(tx, info), "string")
            want = _adopt(want, have)
            if have != want.kind:
                fail(
                    "NX_ALREADY_EXISTS",
                    f"{label} has {want.name!r} as a {have} attribute, not {want.kind}",
                    field=want.type_field,
                    hint=f"Send a {have} value, or list {want.name!r} in delete in the same call",
                    details={"object": label, "name": want.name, "type": have},
                )
        planned.append(want)
    return planned


@tool(
    target="assembly",
    effect="model",
    label="Set attributes",
    summary=_summary,
    keywords=("set attribute", "property", "part number", "title block", "revision", "metadata"),
    requires=("work_part",),
    cost="light",
    invalidates="deleted",  # attributes only: every reference stays valid
    example={"attributes": {"DB_PART_NO": "100-200", "QTY": 4}},
)
def nx_set_attributes(
    tx: Tx,
    *,
    objects: Annotated[list[Ref], Items(1, 50), Doc("Default: the work part")] | None = None,
    attributes: Annotated[dict, Json(100, 16000), Doc("{title: value}, typed by value")]
    | None = None,
    values: Annotated[list[AttributeValue], Items(1, 100)] | None = None,
    delete: Annotated[list[Title], Items(1, 100), Doc("Titles to remove first")] | None = None,
) -> SetData:
    """Set user attributes of the work part or of parts, bodies, faces, features, components.
    delete runs first; delete a title to change its type."""
    wants = _wants(list(values or []), dict(attributes or {}))
    delete = [_title(n, f"/delete/{i}") for i, n in enumerate(delete or [])]
    if not wants and not delete:
        fail("NX_INVALID_ARGUMENT", "Pass attributes, values or delete", field="/attributes")
    seen = {w.name.casefold() for w in wants}
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
            targets.append((obj, existing, _plan(tx, wants, existing, dropping, label)))
    now = tx.nx.Update.Option.Now
    items: list[SetRow] = []
    restore: list[dict[str, Any]] = []  # per object: touched titles' rows before/after
    for i, ((obj, existing, planned), ref) in enumerate(zip(targets, refs, strict=True)):
        tx.checkpoint()
        field = f"/objects/{i}" if ref else None
        titles = [w.name for w in planned] + [n for n in delete if n.casefold() not in seen]
        restore.append(
            {"object": ref, "titles": titles, "before": _title_rows(tx, existing, titles)}
        )
        deleted = []
        for name in delete:
            info = _own(existing, name)
            if info is None:  # already absent: deleting is idempotent, and said
                tx.warn("NX_NOTE", f"{ref or 'The work part'} has no attribute {name!r} to delete")
                continue
            with _native_on(ref, field):
                obj.DeleteUserAttribute(info.Type, str(info.Title), True, now)
            deleted.append(str(info.Title))
        created = []
        for want in planned:
            info = tx.nx.NXObject.AttributeInformation()
            info.Title = want.name
            info.Type = getattr(types, NATIVE[want.kind])
            info.Array = False
            old = _own(existing, want.name)
            kept = old is not None and want.name.casefold() not in dropping
            if not kept:
                created.append(want.name)
            category = want.category or (getattr(old, "Category", "") if kept else "")
            if category:
                info.Category = category  # NX clears a category that is not passed again
            setattr(info, FIELD[want.kind],
                    _nx_date(want.value) if want.kind == "date" else want.value)  # fmt: skip
            with _native_on(ref, field):
                obj.SetUserAttribute(info, now)
        after = _existing(obj)
        rows = []
        for want in planned:
            got = _own(after, want.name)
            readback.expect(got is not None, "attribute", f"{want.name} was not set")
            if str(got.Title).casefold() != want.name.casefold():
                fail(
                    "NX_READ_ONLY",
                    f"{want.name!r} is NX's reserved alias of {got.Title}",
                    field=want.name_field,
                    hint="Use another title; assign materials with nx_set_material",
                    details={"name": want.name, "reserved": str(got.Title)},
                )
            row = _row(tx, got)
            _verify(row, want)
            rows.append(row)
        for name in deleted:
            if name.casefold() not in seen:  # deleted and set again: checked above
                gone = _own(after, name) is None
                readback.expect(gone, "delete", f"{name} is still present")
        items.append(
            {"object": _row_of(tx, obj), "attributes": rows, "created": created, "deleted": deleted}
        )
        restore[-1]["after"] = _title_rows(tx, after, titles)
    tx.note_restore({"items": restore})
    return {"items": items}


@contextlib.contextmanager
def _native_on(ref, field: str | None):
    """``native()`` for a write on one object: NX's "not possible on this object" is the
    object's kind (``NX_WRONG_KIND`` on its field), never an unclassified NX failure."""
    try:
        with native(codes={UNSUPPORTED_OBJECT: "NX_WRONG_KIND"}):
            yield
    except NXError as error:
        if error.nx_code == UNSUPPORTED_OBJECT:
            error.message = f"{ref or 'The work part'} cannot carry user attributes: {error}"
            error.field = error.field or field
            error.hint = "Attributes go on parts, bodies, faces, edges, features and components"
        raise


def _title_rows(tx, existing: dict, titles: list[str]) -> dict:
    """``{title: own row or None}`` of ``titles`` (the before/after of a compensation)."""
    out = {}
    for title in titles:
        info = _own(existing, title)
        out[title] = _row(tx, info) if info is not None else None
    return out


def _same(row, want) -> bool:
    if row is None or want is None:
        return row is None and want is None
    if row["type"] != want["type"]:
        return False
    if row["type"] == "number":
        return math.isclose(float(row["value"]), float(want["value"]), rel_tol=1e-9, abs_tol=1e-12)
    return row["value"] == want["value"]


def _show(row) -> str:
    return "absent" if row is None else repr(row["value"])


@restorer("nx_set_attributes")
def _restore_attributes(tx: Tx, data: dict, *, apply: bool) -> str:
    """Set back the attributes an ``nx_set_attributes`` set or deleted (the compensating
    undo): titles it created are deleted, the others get their previous type and value.
    Refused when one of them changed since, or was not a plain value."""
    said = []
    for item in data["items"]:
        ref = item["object"]
        obj = _target(tx, ref, "/objects/0") if ref else tx.part
        label = ref or file_name(tx.part)
        current = _title_rows(tx, _existing(obj), item["titles"])
        values, drop = [], []
        for title in item["titles"]:
            before, after, now = item["before"][title], item["after"][title], current[title]
            if not _same(now, after):
                fail(
                    "NX_DEPENDENCY_ORDER",
                    f"Attribute {title} of {label} changed since: it is {_show(now)}, the "
                    f"operation left it {_show(after)}",
                    details={"object": label, "name": title, "value": now},
                )
            if before is not None and before["type"] not in TYPES:
                fail("NX_UNSUPPORTED_OPTION",
                     f"Attribute {title} of {label} was a {before['type']} value",
                     details={"object": label, "name": title})  # fmt: skip
            if _same(now, before):
                continue
            if before is None or (now is not None and now["type"] != before["type"]):
                drop.append(title)
            if before is not None:
                value = {"name": title, "value": before["value"], "type": before["type"]}
                if before.get("category"):
                    value["category"] = before["category"]
                values.append(value)
            said.append(f"{title} of {label} " + ("removed" if before is None
                        else f"back to {before['value']!r}"))  # fmt: skip
        if apply and (values or drop):
            nx_set_attributes(
                tx, objects=[ref] if ref else None, values=values or None, delete=drop or None
            )
            got = _title_rows(tx, _existing(obj), item["titles"])
            wrong = [t for t in item["titles"] if not _same(got[t], item["before"][t])]
            readback.expect(not wrong, "attributes", f"{', '.join(wrong)} of {label} did not "
                            "read back their previous values", names=wrong)  # fmt: skip
    return "; ".join(said) or "the attributes (unchanged)"


def _verify(row: AttributeRow, want: _Want) -> None:
    name, kind, value = want.name, want.kind, want.value
    readback.expect(row["type"] == kind, "type", f"{name} is a {row['type']}", type=row["type"])
    got = row["value"]
    if kind == "number":
        readback.expect_close(got, value, f"value:{name}", tol=1e-9 * max(1.0, abs(value)))
    elif kind == "date":
        text = f"{value:%Y-%m-%dT%H:%M:%S}"
        readback.expect(got == text, f"value:{name}", f"{name} reads {got}", value=got)
    else:
        readback.expect(got == value, f"value:{name}", f"{name} reads {got!r}", value=got)
    if want.category:
        readback.expect(
            row.get("category") == want.category,
            f"category:{name}",
            f"{name} has category {row.get('category')!r}",
            category=row.get("category"),
        )
