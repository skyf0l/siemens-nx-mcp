"""Core argument types -> Pydantic adapters and JSON Schema (architecture.md 1.1).

The catalogue describes arguments with stdlib ``Annotated`` aliases and
TypedDicts carrying the markers of :mod:`nx_mcp.core.types`. This module turns
the same metadata into

- a Pydantic ``TypeAdapter`` per tool (sidecar gate 1); a parity test asserts it
  accepts and rejects exactly what :func:`nx_mcp.core.validate.validate` does;
- the tool's JSON Schema for listings and ``nx_get_tool_schemas``: bounded
  arrays, unit strings as ``number | pattern``, references as patterned strings
  whose description names the accepted kinds, no titles, nested objects inlined;
- the output schema of the result envelope, ``data`` derived from the returned
  ``TypedDict``.
"""

from __future__ import annotations

import copy
import functools
import json
import typing
from typing import Annotated, Any, Literal, Union

import typing_extensions as te
from pydantic import BeforeValidator, ConfigDict, Field, Strict, StringConstraints, TypeAdapter

from nx_mcp.core.types import QUANTITY_PATTERNS, Doc, Items, Quantity, Range, Text
from nx_mcp.core.validate import is_typeddict, is_union, typeddict_fields, unwrap

# Keys a caller may add next to a tool's own arguments; never handler arguments.
CALL_KEYS = ("operation_id", "intent", "detail")
OPERATION_ID = {"type": "string", "maxLength": 128, "description": "Reuse to retry safely"}
INTENT = {
    "type": "string",
    "maxLength": 120,
    "description": "Why, in the engineer's terms (NX panel)",
}
OUTCOMES = (
    "not_applicable",
    "committed",
    "previewed",
    "not_started",
    "rolled_back",
    "partial",
    "unknown",
)


def _annotated(base, meta: list):
    return Annotated[(base, *meta)] if meta else base  # type: ignore[valid-type]


def _integral(value):
    """``2.0`` is the integer 2 (as in ``core.validate``); strings and bools stay invalid."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _literal_check(options: tuple):
    def check(value):
        if not any(value == o and type(value) is type(o) for o in options):
            raise ValueError("expected one of " + ", ".join(repr(o) for o in options))
        return value

    return check


class _Converter:
    """Rebuilds a core-type hint with Pydantic constraints (``output`` relaxes bounds)."""

    def __init__(self, *, output: bool = False) -> None:
        self.output = output
        self.typeddicts: dict = {}

    def __call__(self, hint):
        base, markers = unwrap(hint)
        meta: list = []
        doc = markers.get(Doc)
        if doc is not None:
            meta.append(Field(description=doc.text))
        if Quantity in markers:
            pattern = QUANTITY_PATTERNS[markers[Quantity].dim]
            number = Annotated[float, Strict(), Field(allow_inf_nan=False)]
            text = Annotated[str, Strict(), StringConstraints(pattern=pattern)]
            return _annotated(Union[number, text], meta)  # noqa: UP007
        if is_union(base):
            alternatives = tuple(self(alt) for alt in typing.get_args(base))
            return _annotated(Union[alternatives], meta)  # noqa: UP007
        if base is type(None):
            return _annotated(type(None), meta)
        if base is Any:
            return _annotated(Any, meta)
        if typing.get_origin(base) is Literal:
            options = typing.get_args(base)
            return _annotated(base, [*meta, BeforeValidator(_literal_check(options))])
        if base is bool:
            return _annotated(bool, [*meta, Strict()])
        if base is int:
            return _annotated(
                int, [*meta, *self._range(markers), BeforeValidator(_integral), Strict()]
            )
        if base is float:
            return _annotated(
                float, [*meta, *self._range(markers), Strict(), Field(allow_inf_nan=False)]
            )
        if base is str:
            text = markers.get(Text)
            constraint = (
                [StringConstraints(max_length=text.max, pattern=text.pattern)] if text else []
            )
            return _annotated(str, [*meta, Strict(), *constraint])
        origin = typing.get_origin(base)
        if origin is list:
            (item,) = typing.get_args(base)
            items = markers.get(Items)
            bounds = [Field(min_length=items.min, max_length=items.max)] if items else []
            return _annotated(list[self(item)], [*meta, *bounds])  # type: ignore[misc]
        if origin is tuple:
            args = tuple(self(a) for a in typing.get_args(base))
            return _annotated(tuple[args], meta)  # type: ignore[valid-type]
        if origin is dict:
            key, value = typing.get_args(base) or (str, Any)
            return _annotated(dict[key, self(value)], meta)  # type: ignore[valid-type]
        if is_typeddict(base):
            return _annotated(self._typeddict(base), meta)
        if self.output:
            return _annotated(Any, meta)
        raise TypeError(f"{base!r} is not built from core types")

    @staticmethod
    def _range(markers) -> list:
        bounds = markers.get(Range)
        if bounds is None:
            return []
        return [Field(ge=bounds.ge, le=bounds.le, gt=bounds.gt, lt=bounds.lt)]

    def _typeddict(self, td):
        if td in self.typeddicts:
            return self.typeddicts[td]
        required = td.__required_keys__
        fields = {
            name: (te.Required if name in required else te.NotRequired)[self(sub)]
            for name, sub in typeddict_fields(td).items()
        }
        converted = te.TypedDict(td.__name__, fields)  # type: ignore[operator]
        if not self.output:
            converted.__pydantic_config__ = ConfigDict(extra="forbid")  # type: ignore[attr-defined]
        self.typeddicts[td] = converted
        return converted


def _arguments_type(spec):
    convert = _Converter()
    fields = {
        name: (te.Required if p.required else te.NotRequired)[convert(p.hint)]
        for name, p in spec.params.items()
    }
    model = te.TypedDict(f"{spec.name}_arguments", fields)  # type: ignore[operator]
    model.__pydantic_config__ = ConfigDict(extra="forbid")  # type: ignore[attr-defined]
    return model


@functools.cache
def adapter(spec) -> TypeAdapter:
    """The Pydantic validator of a tool's own arguments (no call keys)."""
    return TypeAdapter(_arguments_type(spec))


# ---------------------------------------------------------------------------
# JSON Schema


def _inline(schema: dict) -> dict:
    """Replace every ``$ref`` with its definition (core types are never recursive)."""
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = walk(copy.deepcopy(defs[ref.split("/")[-1]]))
                extra = {k: walk(v) for k, v in node.items() if k != "$ref"}
                return {**target, **extra}
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def tidy(node, *, in_properties: bool = False):
    """Drop titles, collapse homogeneous tuples and single-branch unions."""
    if isinstance(node, list):
        return [tidy(v) for v in node]
    if not isinstance(node, dict):
        return node
    if in_properties:
        return {k: tidy(v) for k, v in node.items()}
    out = {}
    for key, value in node.items():
        if key == "title":
            continue
        out[key] = tidy(value, in_properties=key == "properties")
    if ("enum" in out or "const" in out) and isinstance(out.get("type"), str):
        out.pop("type")  # implied by the values
    prefix = out.get("prefixItems")
    if isinstance(prefix, list) and prefix and all(p == prefix[0] for p in prefix):
        out.pop("prefixItems")
        out["items"] = prefix[0]
    if out.get("required") == []:
        out.pop("required")
    for key in ("anyOf", "oneOf"):
        branches = out.get(key)
        if isinstance(branches, list) and len(branches) == 1:
            out.pop(key)
            out = {**branches[0], **out}
    return out


def _without_null(schema: dict) -> dict:
    """An optional (``= None``) field is documented by omission, not by ``null``."""
    branches = schema.get("anyOf")
    if isinstance(branches, list) and {"type": "null"} in branches:
        rest = [b for b in branches if b != {"type": "null"}]
        schema = {k: v for k, v in schema.items() if k != "anyOf"}
        if len(rest) == 1:
            schema = {**rest[0], **schema}
        else:
            schema["anyOf"] = rest
    return schema


def _hoist_description(schema: dict) -> dict:
    """Move a description shared by every union branch up to the field."""
    branches = schema.get("anyOf")
    if isinstance(branches, list) and "description" not in schema:
        texts = {b.get("description") for b in branches}
        if len(texts) == 1 and None not in texts:
            schema = {**schema, "description": texts.pop()}
            schema["anyOf"] = [{k: v for k, v in b.items() if k != "description"} for b in branches]
    return schema


@functools.cache
def _arguments_schema(spec) -> str:
    schema = tidy(_inline(adapter(spec).json_schema()))
    properties = schema.get("properties", {})
    for name, param in spec.params.items():
        prop = _hoist_description(properties[name])
        if not param.required:
            if param.default is None:
                prop = _without_null(prop)
            else:
                prop = {**prop, "default": param.default}
        properties[name] = _hoist_description(prop)
    schema["additionalProperties"] = False
    return json.dumps(schema)


def input_schema(spec, *, call_keys: bool = True) -> dict:
    """The tool's input JSON Schema; ``call_keys`` adds ``operation_id``/``intent``."""
    schema = json.loads(_arguments_schema(spec))
    if call_keys and spec.effect != "read":
        schema["properties"]["operation_id"] = dict(OPERATION_ID)
        schema["properties"]["intent"] = dict(INTENT)
    return schema


def variant_schema(spec, variant: str, *, call_keys: bool = True) -> dict:
    """``nx_tool#variant``: the input schema narrowed to one variant."""
    values = spec.variant_values()
    if variant not in values:
        raise KeyError(variant)
    schema = input_schema(spec, call_keys=call_keys)
    if isinstance(spec.variants, str):
        field = schema["properties"][spec.variants]
        description = field.get("description")
        schema["properties"][spec.variants] = {"const": variant}
        if description:
            schema["properties"][spec.variants]["description"] = description
    else:
        sub = spec.variants[variant]
        schema["x-variant"] = tidy(_inline(TypeAdapter(_Converter()(sub)).json_schema()))
    return schema


def _output_data(spec) -> dict:
    hint = spec.returns
    try:
        return tidy(
            _inline(TypeAdapter(_Converter(output=True)(hint)).json_schema(mode="serialization"))
        )
    except Exception:
        return {"type": "object"}


def output_schema(spec) -> dict:
    """The success/error envelope (surface 1.6) with ``data`` from the returned TypedDict."""
    row = {"type": "object"}
    return {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "tool": {"type": "string"},
            "operation_id": {"type": "string"},
            "mutation_outcome": {"enum": list(OUTCOMES)},
            "units": {
                "type": "object",
                "properties": {k: {"type": "string"} for k in ("length", "angle", "sheet_length")},
            },
            "restore_id": {"type": "string"},
            "marks_part_modified": {"type": "boolean"},
            "replayed": {"type": "boolean"},
            "frame": {"type": ["string", "null"]},
            "data": _output_data(spec),
            "changes": {
                "type": "object",
                "properties": {
                    "created": {"type": "array", "items": row},
                    "modified": {"type": "array", "items": row},
                    "deleted": {"type": "array", "items": row},
                    "references": {"enum": ["preserved", "part_invalidated", "listed"]},
                },
            },
            "warnings": {"type": "array", "items": {"type": "object"}},
            "page": {"type": "object"},
            "error": {
                "type": "object",
                "properties": {
                    k: {"type": ["string", "integer", "null"]} if k == "nx_code" else {}
                    for k in (
                        "category",
                        "code",
                        "message",
                        "hint",
                        "field",
                        "retry",
                        "nx_code",
                        "details",
                    )
                },
            },
            "result_id": {"type": "string"},
            "omitted": {"type": "object"},
        },
        "required": ["ok", "tool", "mutation_outcome"],
    }


def definition_size(name: str, description: str, schema: dict) -> int:
    """Characters of a tool definition (name + description + input schema)."""
    return len(name) + len(description) + len(json.dumps(schema, separators=(",", ":")))


# ---------------------------------------------------------------------------
# Field pointers of Pydantic errors


def error_pointer(args, loc, kind: str = "") -> str:
    """JSON pointer into ``args`` for a Pydantic error ``loc`` (union branch tags skipped)."""
    parts: list[str] = []
    current = args
    for index, item in enumerate(loc):
        if isinstance(current, dict) and isinstance(item, str) and item in current:
            parts.append(item)
            current = current[item]
        elif isinstance(current, (list, tuple)) and isinstance(item, int) and item < len(current):
            parts.append(str(item))
            current = current[item]
        elif kind == "missing" and index == len(loc) - 1:
            parts.append(str(item))  # the missing field itself
    return "/" + "/".join(parts) if parts else ""
