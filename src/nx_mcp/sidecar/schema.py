"""Core argument types -> Pydantic adapters and JSON Schema (architecture.md 1.1).

The catalogue describes arguments with stdlib ``Annotated`` aliases and
TypedDicts carrying the markers of :mod:`nx_mcp.core.types`. This module turns
the same metadata into

- a Pydantic ``TypeAdapter`` per tool (sidecar gate 1); a parity test asserts it
  accepts and rejects exactly what :func:`nx_mcp.core.validate.validate` does;
- the tool's JSON Schema for listings and ``nx_get_tool_schemas``, kept compact
  (tool-surface 1.7): bounded arrays and strings, no titles; references are
  bounded strings whose description names the accepted kinds and quantities are
  ``number | string`` with the unit spelled in the description (their regular
  expressions are checked by gate 1, Pydantic here and ``core.validate`` in NX,
  not repeated in every listing); a subschema that occurs more than once (a
  ``Plane``, ``Length`` or ``Point3`` used by several fields) is shared once
  under ``$defs`` when that makes the definition shorter;
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
from pydantic import (
    AfterValidator,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    Strict,
    StringConstraints,
    Tag,
    TypeAdapter,
)

from nx_mcp.core import types as core_types
from nx_mcp.core.receipts import OPERATION_ID_CHARS, OPERATION_ID_MAX, OPERATION_ID_MIN
from nx_mcp.core.types import (
    QUANTITY_MAX,
    QUANTITY_PATTERNS,
    REF_PATTERN,
    Aliases,
    Doc,
    Items,
    Json,
    Quantity,
    Range,
    Text,
    quantity_formula,
)
from nx_mcp.core.validate import (
    is_typeddict,
    is_union,
    match_option,
    natural_scalar,
    options_message,
    typeddict_fields,
    unwrap,
    wraps_scalar,
)

# Keys a caller may add next to a tool's own arguments; never handler arguments.
CALL_KEYS = ("operation_id", "intent", "detail")
# Explained once in the server instructions (sidecar/server.py), not in every listing.
OPERATION_ID = {
    "type": "string",
    "minLength": OPERATION_ID_MIN,
    "maxLength": OPERATION_ID_MAX,
    "pattern": OPERATION_ID_CHARS,
}
INTENT = {"type": "string", "maxLength": 120}
# Regular expressions gate 1 checks but listings omit (references, unit strings).
LISTING_UNSHOWN_PATTERNS = frozenset({REF_PATTERN, *QUANTITY_PATTERNS.values()})
# Core types named in ``$defs`` when shared (others are named after their field).
NAMED_TYPES = (
    "Plane",
    "Axis",
    "Direction",
    "Limit",
    "Combine",
    "Pose",
    "SketchPoint",
    "Point3",
    "Point2",
    "SheetPoint",
    "Vec3",
    "Mat3",
    "Length",
    "SheetLength",
    "Angle",
    "PrincipalAxis",
    "PrincipalPlane",
    "PlaneByNormal",
    "AxisByPoint",
)
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
    return Annotated[(base, *meta)] if meta else base


def _integral(value):
    """``2.0`` is the integer 2 (as in ``core.validate``); strings and bools stay invalid."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _literal_check(options: tuple, aliases: tuple = ()):
    """The canonical option of an exact or natural spelling (as ``core.validate``)."""

    def check(value):
        if any(value == o and type(value) is type(o) for o in options):
            return value
        hit = match_option(value, options, aliases)
        if hit is None:
            raise ValueError(options_message(value, options, aliases))
        return hit

    return check


def _natural(base):
    return lambda value: natural_scalar(value, base)


def _quantity_text(dim: str, bounds: Range | None):
    """A unit string, numeric string or expression formula of ``dim`` (as gate 1)."""

    def check(value):
        formula = quantity_formula(value, dim)
        if formula is None and dim == "count" and bounds is not None:
            number = float(value)
            for limit, ok in (
                (bounds.ge, lambda b: number >= b),
                (bounds.le, lambda b: number <= b),
                (bounds.gt, lambda b: number > b),
                (bounds.lt, lambda b: number < b),
            ):
                if limit is not None and not ok(limit):
                    raise ValueError(f"out of range ({value})")
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
            dim = markers[Quantity].dim
            number: Any = (
                _annotated(int, [*self._range(markers), BeforeValidator(_integral), Strict()])
                if dim == "count"
                else Annotated[float, Strict(), Field(allow_inf_nan=False)]
            )
            text = Annotated[
                str,
                Strict(),
                StringConstraints(max_length=QUANTITY_MAX),
                AfterValidator(_quantity_text(dim, markers.get(Range))),
            ]
            return _annotated(Union[number, text], meta)  # noqa: UP007
        if base is dict and Json in markers:
            marker = markers[Json]
            bound = Field(max_length=marker.max_keys)
            check = BeforeValidator(lambda v, m=marker: core_types.check_json(v, m))
            return _annotated(dict[str, Any], [*meta, bound, check])
        if is_union(base):
            alternatives = tuple(self(alt) for alt in typing.get_args(base))
            return _annotated(Union[alternatives], meta)  # noqa: UP007
        if base is type(None):
            return _annotated(type(None), meta)
        if base is Any:
            return _annotated(Any, meta)
        if typing.get_origin(base) is Literal:
            options = typing.get_args(base)
            aliases = markers[Aliases].pairs if Aliases in markers else ()
            return _annotated(base, [*meta, BeforeValidator(_literal_check(options, aliases))])
        if base is bool:
            return _annotated(bool, [*meta, BeforeValidator(_natural(bool)), Strict()])
        if base is int:
            return _annotated(
                int,
                [
                    *meta,
                    *self._range(markers),
                    BeforeValidator(_integral),
                    BeforeValidator(_natural(int)),
                    Strict(),
                ],
            )
        if base is float:
            return _annotated(
                float,
                [
                    *meta,
                    *self._range(markers),
                    BeforeValidator(_natural(float)),
                    Strict(),
                    Field(allow_inf_nan=False),
                ],
            )
        if base is str:
            limits = markers.get(Text)
            constraint = (
                [StringConstraints(max_length=limits.max, pattern=limits.pattern)] if limits else []
            )
            return _annotated(str, [*meta, Strict(), *constraint])
        origin = typing.get_origin(base)
        if origin is list:
            (item,) = typing.get_args(base)
            items = markers.get(Items)
            bounds = [Field(min_length=items.min, max_length=items.max)] if items else []
            wrap = BeforeValidator(lambda v, h=item: [v] if wraps_scalar(h, v) else v)
            return _annotated(list[self(item)], [*meta, *bounds, wrap])
        if origin is tuple:
            args = tuple(self(a) for a in typing.get_args(base))
            return _annotated(tuple[args], meta)
        if origin is dict:
            key, value = typing.get_args(base) or (str, Any)
            return _annotated(dict[key, self(value)], meta)
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
        converted = te.TypedDict(td.__name__, fields)
        if not self.output:
            converted.__pydantic_config__ = ConfigDict(extra="forbid")
        self.typeddicts[td] = converted
        return converted


def _arguments_type(spec):
    convert = _Converter()
    fields = {
        name: (te.Required if p.required else te.NotRequired)[convert(p.hint)]
        for name, p in spec.params.items()
    }
    model = te.TypedDict(f"{spec.name}_arguments", fields)
    model.__pydantic_config__ = ConfigDict(extra="forbid")
    return model


def _by_variant(spec) -> dict:
    """``{argument: ByVariant}`` of a tool whose argument's fields depend on its variant."""
    found = {n: unwrap(p.hint)[1].get(core_types.ByVariant) for n, p in spec.params.items()}
    return {n: by for n, by in found.items() if by is not None}


def _variant_union(spec, by: dict):
    """The arguments as a union discriminated by the variant field, each branch with its
    variant's own forms (what gate 1 checks; the listing keeps the free-form object)."""
    convert = _Converter()
    branches = []
    values = spec.variant_values()
    aliases = unwrap(spec.params[spec.variants].hint)[1].get(Aliases)
    pairs = aliases.pairs if aliases is not None else ()

    def tag(args):
        """The variant a call names, in any natural spelling (as ``core.validate``)."""
        raw = args.get(spec.variants) if isinstance(args, dict) else None
        if raw in values:
            return raw
        return match_option(raw, values, pairs) or (raw if isinstance(raw, str) else None)

    for value in values:
        fields = {
            name: (te.Required if p.required else te.NotRequired)[
                Annotated[Literal[value], BeforeValidator(_literal_check(values, pairs))]
                if name == spec.variants
                else convert(by[name].forms[value] if name in by else p.hint)
            ]
            for name, p in spec.params.items()
        }
        branch = te.TypedDict(f"{spec.name}_{value}", fields)  # type: ignore[misc]  # built per variant
        branch.__pydantic_config__ = ConfigDict(extra="forbid")  # type: ignore[attr-defined]  # read by Pydantic
        branches.append(Annotated[branch, Tag(value)])
    return Annotated[Union[tuple(branches)], Discriminator(tag)]  # noqa: UP007


@functools.cache
def adapter(spec) -> TypeAdapter:
    """The Pydantic validator of a tool's own arguments (no call keys)."""
    by = _by_variant(spec)
    return TypeAdapter(_variant_union(spec, by) if by else _arguments_type(spec))


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


def _unshown_patterns(node, *, in_properties: bool = False):
    """Drop reference and unit-string patterns (gate 1 checks them) from a listing.

    A quantity's ``number | string`` pair then reads ``type: [number, string]``;
    ``maxLength`` constrains only the string, so the meaning is unchanged.
    """
    if isinstance(node, list):
        return [_unshown_patterns(v) for v in node]
    if not isinstance(node, dict):
        return node
    if in_properties:  # a map of field names, any of which may be called "pattern"
        return {k: _unshown_patterns(v) for k, v in node.items()}
    out = {
        k: _unshown_patterns(v, in_properties=k in ("properties", "$defs"))
        for k, v in node.items()
        if not (k == "pattern" and isinstance(v, str) and v in LISTING_UNSHOWN_PATTERNS)
    }
    branches = out.get("anyOf")
    if (
        isinstance(branches, list)
        and len(branches) == 2
        and {"type": "number"} in branches
        and any(
            isinstance(b, dict) and b.get("type") == "string" and set(b) <= {"type", "maxLength"}
            for b in branches
        )
    ):
        text = next(b for b in branches if b.get("type") == "string")
        rest = {k: v for k, v in out.items() if k != "anyOf"}
        out = {"type": ["number", "string"], **{k: v for k, v in text.items() if k != "type"}}
        out.update(rest)
    return out


def _field(prop: dict, param) -> dict:
    prop = _hoist_description(prop)
    if not param.required:
        prop = _without_null(prop) if param.default is None else {**prop, "default": param.default}
    return _hoist_description(prop)


# -- shared definitions ------------------------------------------------------

_SCHEMA_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")
_SCHEMA_MAPS = ("properties", "$defs")


def _sites(node):
    """``(container, key)`` of every subschema below ``node`` (not ``node`` itself)."""
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            for name, sub in value.items():
                if isinstance(sub, dict):
                    yield value, name
                    yield from _sites(sub)
        elif key in _SCHEMA_LISTS and isinstance(value, list):
            for i, sub in enumerate(value):
                if isinstance(sub, dict):
                    yield value, i
                    yield from _sites(sub)
        elif key in ("items", "additionalProperties", "not") and isinstance(value, dict):
            yield node, key
            yield from _sites(value)


def _key(node: dict) -> str:
    return json.dumps(node, sort_keys=True, separators=(",", ":"))


def _body(node: dict, keep_description: bool) -> dict:
    """The shareable part of a subschema: without its default (and description)."""
    drop = {"default"} if keep_description else {"default", "description"}
    return {k: v for k, v in node.items() if k not in drop}


@functools.cache
def _known_names() -> dict:
    """Canonical subschema -> core type name, for readable ``$defs`` keys. A subschema
    two core types share (``Length`` and ``Angle`` without their descriptions) gets no
    core name: an angle field must not point at ``#/$defs/Length``."""
    names = {_key({"maxLength": 128, "type": "string"}): "Ref"}
    shared: set = set()
    for name in NAMED_TYPES:
        hint = getattr(core_types, name)
        node = _hoist_description(
            _without_null(
                _unshown_patterns(tidy(_inline(TypeAdapter(_Converter()(hint)).json_schema())))
            )
        )
        for key in {_key(_body(node, True)), _key(_body(node, False))}:
            if "Angle" in {names.setdefault(key, name), name} and names[key] != name:
                shared.add(key)
    return {k: v for k, v in names.items() if k not in shared}


def _def_name(body: dict, taken: set) -> str:
    name = _known_names().get(_key(body))
    if name is None:  # named by its JSON type: a field name could mislead other fields
        kind = body.get("type")
        name = {
            "string": "Text",
            "number": "Number",
            "integer": "Integer",
            "boolean": "Flag",
            "array": "List",
            "object": "Object",
        }.get(kind if isinstance(kind, str) else "", "Choice" if "anyOf" in body else "Value")
    unique, n = name, 2
    while unique in taken:
        unique, n = f"{name}{n}", n + 1
    return unique


def share_definitions(schema: dict) -> dict:
    """Move a subschema that occurs more than once to ``$defs`` when that saves characters.

    Greedy: the candidate with the largest saving goes first; a field keeps its
    own description and default next to the ``$ref``.
    """
    defs: dict = {}
    while True:
        found: dict = {}
        tree = {"properties": schema.get("properties", {}), "$defs": defs}
        for container, key in _sites(tree):
            if container is defs:
                continue  # a definition itself is never replaced by a reference to it
            node = container[key]
            for keep in (True, False) if "description" in node else (True,):
                body = _body(node, keep)
                if not body or set(body) == {"$ref"}:
                    continue
                text = _key(body)
                slot = found.setdefault(text, {"body": body, "sites": []})
                slot["sites"].append((container, key))
        best, best_name, saving = None, "", 20  # not worth a definition below this
        for text, slot in found.items():
            sites = slot["sites"]
            if len({id(c[k]) for c, k in sites}) < 2:
                continue
            name = _def_name(slot["body"], set(defs))
            # Each site becomes {"$ref":"#/$defs/<name>"}; the body is written once.
            gain = len(sites) * (len(text) - 15 - len(name)) - (len(text) + len(name) + 4)
            if gain > saving:
                best, saving, best_name = slot, gain, name
        if best is None:
            break
        name = best_name
        defs[name] = best["body"]
        ref = {"$ref": "#/$defs/" + name}
        for container, key in best["sites"]:
            node = container[key]
            extra = {k: v for k, v in node.items() if k not in best["body"]}
            container[key] = {**ref, **extra}
    if defs:
        schema["$defs"] = defs
    return schema


@functools.cache
def _arguments_schema(spec) -> str:
    listed = TypeAdapter(_arguments_type(spec)).json_schema()
    schema = _unshown_patterns(tidy(_inline(listed)))
    properties = schema.get("properties", {})
    for name, param in spec.params.items():
        properties[name] = _field(properties[name], param)
    schema["additionalProperties"] = False
    return json.dumps(share_definitions(schema))


def input_schema(spec, *, call_keys: bool = True) -> dict:
    """The tool's input JSON Schema; ``call_keys`` adds ``operation_id``/``intent``."""
    schema = json.loads(_arguments_schema(spec))
    if call_keys and spec.effect != "read":
        schema["properties"]["operation_id"] = dict(OPERATION_ID)
        schema["properties"]["intent"] = dict(INTENT)
    return schema


def narrow_variants(schema: dict, spec, allowed) -> dict:
    """Keep only the ``allowed`` values in the variant field's enum (listings show only
    the variants usable on the connected NX build)."""
    if not isinstance(spec.variants, str) or allowed is None:
        return schema
    allowed = list(allowed)
    if set(allowed) >= set(spec.variant_values()):
        return schema

    def cut(node):
        if not isinstance(node, dict):
            return
        if isinstance(node.get("enum"), list):
            node["enum"] = [v for v in node["enum"] if v is None or v in allowed]
        if "$ref" in node:
            cut(schema.get("$defs", {}).get(node["$ref"].rsplit("/", 1)[-1]))
        for branch in node.get("anyOf", ()):
            cut(branch)

    cut(schema.get("properties", {}).get(spec.variants))
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
        for name, param in spec.params.items():
            by = unwrap(param.hint)[1].get(core_types.ByVariant)
            if by is not None:  # the variant's own fields replace the free-form object
                form = TypeAdapter(_Converter()(by.forms[variant])).json_schema()
                schema["properties"][name] = _unshown_patterns(tidy(_inline(form)))
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
                    # after a rollback that renumbered refs (error.details.references
                    # is then "renumbered"; changes.references stays part_invalidated)
                    "stale_references": {"type": "integer"},
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
            "expand_with": {"const": "nx_get_result"},
        },
        "required": ["ok", "tool", "mutation_outcome"],
    }


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
