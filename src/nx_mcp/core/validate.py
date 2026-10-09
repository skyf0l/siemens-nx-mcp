"""Gate 1 on the NX side: check arguments against a tool's core-type signature.

Stdlib only. The checker mirrors what the sidecar's generated Pydantic adapters
accept (extra fields forbidden, finite numbers, bounded lists and strings, unit
strings of the right dimension). It also records *where* quantities, paths and
references occur, so the kernel can resolve paths (gate 2) and convert units
(gate 9) without walking the schema again.
"""

from __future__ import annotations

import math
import re
import types
import typing
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Union

from nx_mcp.core.errors import NXError
from nx_mcp.core.types import (
    PLACEHOLDER_PATTERN,
    Items,
    PathArg,
    Quantity,
    Range,
    RefKind,
    Text,
    parse_quantity,
)

_NONE = type(None)


def unwrap(hint) -> tuple[Any, dict]:
    """``(base, {marker_type: marker})`` of a possibly ``Annotated`` hint."""
    markers: dict = {}
    while typing.get_origin(hint) is Annotated:
        base, *extra = typing.get_args(hint)
        for marker in extra:
            markers.setdefault(type(marker), marker)
        hint = base
    return hint, markers


def is_union(hint) -> bool:
    origin = typing.get_origin(hint)
    return origin is Union or origin is getattr(types, "UnionType", Union)


def is_typeddict(hint) -> bool:
    return isinstance(hint, type) and issubclass(hint, dict) and hasattr(hint, "__required_keys__")


_TD_HINTS: dict = {}


def typeddict_fields(td) -> dict:
    if td not in _TD_HINTS:
        _TD_HINTS[td] = typing.get_type_hints(td, include_extras=True)
    return _TD_HINTS[td]


def pointer(path) -> str:
    return "/" + "/".join(str(p) for p in path) if path else ""


@dataclass
class Checked:
    """Validated arguments plus the concrete locations of special fields."""

    args: dict
    quantities: list = field(default_factory=list)  # [(path, dim)]
    paths: list = field(default_factory=list)  # [(path, PathArg)]
    refs: list = field(default_factory=list)  # [(path, kinds)]
    placeholders: list = field(default_factory=list)  # [(path, "$2.data.x")]


def _bad(path, message):
    return NXError(
        "NX_INVALID_ARGUMENT",
        f"{pointer(path) or 'arguments'}: {message}",
        field=pointer(path) or None,
    )


def _json_type(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _hint_json_types(hint) -> set:
    base, markers = unwrap(hint)
    if Quantity in markers:
        return {"number", "string"}
    if is_union(base):
        out: set = set()
        for alt in typing.get_args(base):
            out |= _hint_json_types(alt)
        return out
    if base is _NONE:
        return {"null"}
    if typing.get_origin(base) is Literal:
        return {_json_type(v) for v in typing.get_args(base)}
    if base is bool:
        return {"boolean"}
    if base in (int, float):
        return {"number"}
    if base is str:
        return {"string"}
    if typing.get_origin(base) in (list, tuple):
        return {"array"}
    if is_typeddict(base):
        return {"object"}
    return set()


class _Walker:
    def __init__(self, placeholders: bool):
        self.placeholders = placeholders
        self.out = Checked(args={})

    def check(self, value, hint, path):
        base, markers = unwrap(hint)
        if Quantity in markers:
            try:
                parse_quantity(value, markers[Quantity].dim)
            except ValueError as error:
                raise _bad(path, str(error)) from None
            self.out.quantities.append((tuple(path), markers[Quantity].dim))
            return value
        if (
            RefKind in markers
            and self.placeholders
            and isinstance(value, str)
            and re.match(PLACEHOLDER_PATTERN, value)
        ):
            self.out.placeholders.append((tuple(path), value))
            self.out.refs.append((tuple(path), markers[RefKind].kinds))
            return value
        if is_union(base):
            return self._union(value, base, path)
        value = self._base(value, base, path)
        self._markers(value, markers, path)
        if RefKind in markers:
            self.out.refs.append((tuple(path), markers[RefKind].kinds))
        if PathArg in markers:
            self.out.paths.append((tuple(path), markers[PathArg]))
        return value

    def _union(self, value, base, path):
        alternatives = typing.get_args(base)
        errors = []
        for alt in alternatives:
            saved = (
                len(self.out.quantities),
                len(self.out.paths),
                len(self.out.refs),
                len(self.out.placeholders),
            )
            try:
                return self.check(value, alt, path)
            except NXError as error:
                q, p, r, h = saved
                del self.out.quantities[q:], self.out.paths[p:], self.out.refs[r:]
                del self.out.placeholders[h:]
                errors.append((alt, error))
        kind = _json_type(value)
        same = [e for alt, e in errors if kind in _hint_json_types(alt)]
        if len(same) == 1:
            raise same[0]
        allowed = sorted({t for alt in alternatives for t in _hint_json_types(alt)})
        if same:
            raise _bad(path, "does not match any accepted form: " + same[0].message)
        raise _bad(path, f"expected {' or '.join(allowed)}, got {kind}")

    def _base(self, value, base, path):
        if base is _NONE:
            if value is not None:
                raise _bad(path, "expected null")
            return value
        if typing.get_origin(base) is Literal:
            options = typing.get_args(base)
            if not any(value == o and type(value) is type(o) for o in options):
                raise _bad(path, "expected one of " + ", ".join(repr(o) for o in options))
            return value
        if base is bool:
            if not isinstance(value, bool):
                raise _bad(path, "expected true or false")
            return value
        if base is int:
            if isinstance(value, bool) or not isinstance(value, int):
                if isinstance(value, float) and value.is_integer():
                    return int(value)
                raise _bad(path, "expected an integer")
            return value
        if base is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise _bad(path, "expected a number")
            if not math.isfinite(value):
                raise _bad(path, "must be finite")
            return float(value)
        if base is str:
            if not isinstance(value, str):
                raise _bad(path, "expected a string")
            return value
        origin = typing.get_origin(base)
        if origin is list:
            if not isinstance(value, (list, tuple)):
                raise _bad(path, "expected an array")
            (item,) = typing.get_args(base)
            return [self.check(v, item, [*path, i]) for i, v in enumerate(value)]
        if origin is tuple:
            items = typing.get_args(base)
            if not isinstance(value, (list, tuple)) or len(value) != len(items):
                raise _bad(path, f"expected an array of {len(items)} items")
            return [
                self.check(v, h, [*path, i])
                for i, (v, h) in enumerate(zip(value, items, strict=True))
            ]
        if is_typeddict(base):
            return self.object(value, base, path)
        raise _bad(path, f"unsupported schema type {base!r}")

    def object(self, value, td, path):
        if not isinstance(value, dict):
            raise _bad(path, "expected an object")
        fields = typeddict_fields(td)
        extra = sorted(set(value) - set(fields))
        if extra:
            raise _bad([*path, extra[0]], "unknown field")
        missing = sorted(set(td.__required_keys__) - set(value))
        if missing:
            raise _bad([*path, missing[0]], "required field is missing")
        return {k: self.check(v, fields[k], [*path, k]) for k, v in value.items()}

    @staticmethod
    def _markers(value, markers, path):
        text = markers.get(Text)
        if text is not None and isinstance(value, str):
            if len(value) > text.max:
                raise _bad(path, f"at most {text.max} characters")
            if text.pattern and not re.match(text.pattern, value):
                raise _bad(path, "has an invalid format")
        bounds = markers.get(Range)
        if bounds is not None and isinstance(value, (int, float)) and not isinstance(value, bool):
            if bounds.ge is not None and value < bounds.ge:
                raise _bad(path, f"must be >= {bounds.ge:g}")
            if bounds.le is not None and value > bounds.le:
                raise _bad(path, f"must be <= {bounds.le:g}")
            if bounds.gt is not None and value <= bounds.gt:
                raise _bad(path, f"must be > {bounds.gt:g}")
            if bounds.lt is not None and value >= bounds.lt:
                raise _bad(path, f"must be < {bounds.lt:g}")
        items = markers.get(Items)
        if (
            items is not None
            and isinstance(value, list)
            and not items.min <= len(value) <= items.max
        ):
            raise _bad(path, f"needs {items.min}..{items.max} items, got {len(value)}")


def validate(params: dict, args, *, placeholders: bool = False) -> Checked:
    """Validate ``args`` against ``params`` (``{name: Param}`` from the catalogue)."""
    if not isinstance(args, dict):
        raise _bad([], "arguments must be an object")
    walker = _Walker(placeholders)
    unknown = sorted(set(args) - set(params))
    if unknown:
        raise _bad([unknown[0]], "unknown field")
    for name, param in params.items():
        if name in args:
            if args[name] is None and param.default is None:
                walker.out.args[name] = None
                continue
            walker.out.args[name] = walker.check(args[name], param.hint, [name])
        elif param.required:
            raise _bad([name], "required field is missing")
    return walker.out


def get_at(data, path):
    for key in path:
        data = data[key]
    return data


def set_at(data, path, value):
    for key in path[:-1]:
        data = data[key]
    data[path[-1]] = value
