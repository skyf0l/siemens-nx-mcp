"""Gate 1 on the NX side: check arguments against a tool's core-type signature.

Stdlib only. The checker mirrors what the sidecar's generated Pydantic adapters
accept (extra fields forbidden, finite numbers, bounded lists and strings, unit
strings of the right dimension). It also records *where* quantities, paths and
references occur, so the kernel can resolve paths (gate 2) and convert units
(gate 9) without walking the schema again.

Natural spellings pass and become canonical values the handler sees: an enum option
in any case and with spaces, ``-`` or ``_`` (``"Through All"`` -> ``"through_all"``),
a declared alias (``Aliases``), a plural of a singular option, ``"true"``/``"false"``
for a boolean, a numeric string for a number and one string where a list of strings
is expected. A quantity may also be an NX expression name or formula (evaluated by
gate 9). A union prefers an exact fit, then a natural spelling, then a formula, so
``"through_all"`` stays the option rather than an expression of that name.

Errors say what to send: valid options with a did-you-mean, the accepted fields of an
object, the accepted forms of a union and the shape of a reference.
"""

from __future__ import annotations

import difflib
import math
import re
import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Union

from nx_mcp.core.errors import NXError
from nx_mcp.core.references import PREFIX
from nx_mcp.core.types import (
    PLACEHOLDER_PATTERN,
    SHORTHANDS,
    Aliases,
    ByVariant,
    Doc,
    Items,
    Json,
    PathArg,
    Quantity,
    Range,
    RefKind,
    Text,
    check_json,
    expand_kinds,
    quantity_formula,
)

_NONE = type(None)
# Leniency a union tries in turn: an exact fit, then a natural spelling, then (for a
# quantity) an NX expression formula. A single type takes all three.
EXACT, NATURAL, FORMULA = 0, 1, 2
_NUMERIC = re.compile(r"^\s*[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?\s*$")
_UNIT_STRING = re.compile(r"^\s*[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?\s*[A-Za-z]+\s*$")
_SEPARATORS = re.compile(r"(?<=[a-z0-9])[\s_\-]+(?=[a-z0-9])")
# Field names agents send for another field: checked before close spellings.
FIELD_SYNONYMS = {
    "name": ("label", "names", "query"),
    "title": ("label", "name"),
    "description": ("label", "comment"),
    "comment": ("label", "description"),
    "name_contains": ("query", "name"),
    "contains": ("query",),
    "filter": ("query", "kind"),
    "search": ("query",),
    "pattern": ("query",),
    "match": ("query",),
    "value": ("formula", "distance", "size", "radius"),
    "expression": ("formula", "name"),
    "unit": ("units",),
    "units": ("unit",),
    "type": ("kind", "method", "form", "layout", "operation"),
    "kind": ("type",),
    "mode": ("method", "kind", "type"),
    "method": ("type", "kind"),
    "count": ("steps", "number"),
    "n": ("count", "steps"),
    "number": ("count",),
    "older_than": ("max_age_seconds",),
    "age": ("max_age_seconds",),
    "component": ("owner", "components"),
    "part": ("owner", "path"),
    "view": ("orientation", "drawing_view", "model_view"),
    "object": ("objects", "bodies", "target"),
    "objects": ("bodies", "faces", "edges", "targets"),
    "target": ("targets", "bodies", "body"),
    "body": ("bodies", "target", "targets"),
    "face": ("faces",),
    "edge": ("edges",),
    "distance": ("end", "offset", "value"),
    "length": ("end", "distance"),
    "depth": ("end",),
    "sketches": ("sections",),
    "profile": ("sketch", "section"),
    "suppress": ("suppressed",),
    "point1": ("start", "origin"),
    "point2": ("end",),
    "file": ("path",),
    "filename": ("path",),
}


def unwrap(hint) -> tuple[Any, dict]:
    """``(base, {marker_type: marker})`` of a possibly ``Annotated`` hint; a shorthand
    such as ``RefTo`` counts as the markers it stands for, in place.

    The first marker of a type wins, except ``Doc``: an explicit ``Doc`` beats one a
    shorthand implies, and a later explicit ``Doc`` beats an earlier one, so
    ``Annotated[Length, Doc("Gap")]`` and ``Annotated[str, PathTo("new"), Doc("Out")]``
    show their own text."""
    markers: dict = {}
    while typing.get_origin(hint) is Annotated:
        base, *extra = typing.get_args(hint)
        for marker in extra:
            if isinstance(marker, SHORTHANDS):
                for part in marker.expand():
                    markers.setdefault(type(part), part)
            elif isinstance(marker, Doc):
                markers[Doc] = marker
            else:
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


def _bad(path, message, **extra):
    return NXError(
        "NX_INVALID_ARGUMENT",
        f"{pointer(path) or 'arguments'}: {message}",
        field=pointer(path) or None,
        **extra,
    )


# ---------------------------------------------------------------------------
# Natural spellings of options and fields


def option_key(text: str) -> str:
    """Comparison key of an option: case and spaces, ``-`` or ``_`` between letters and
    digits ignored (``"Through All"``, ``"through-all"`` -> ``"throughall"``); a leading
    sign stays (``"-X"`` -> ``"-x"``)."""
    return _SEPARATORS.sub("", str(text).strip().casefold())


def _alias_pairs(aliases) -> tuple:
    if isinstance(aliases, Aliases):
        return aliases.pairs
    if isinstance(aliases, Mapping):
        return tuple(aliases.items())
    return tuple(aliases or ())


def match_option(value, options, aliases=()):
    """The one option ``value`` names in a natural spelling (case, separators, a declared
    alias, a plural of the option), else None. Exact matches are the caller's."""
    if not isinstance(value, str):
        return None
    table: dict[str, set] = {}
    for option in options:
        if isinstance(option, str):
            table.setdefault(option_key(option), set()).add(option)
    for alias, target in _alias_pairs(aliases):
        if target in options:
            table.setdefault(option_key(alias), set()).add(target)
    key = option_key(value)
    for candidate in (key, key[:-3] + "y" if key.endswith("ies") else None, key[:-1]):
        if candidate is None or (candidate == key[:-1] and not key.endswith("s")):
            continue
        hits = table.get(candidate)
        if hits:
            return next(iter(hits)) if len(hits) == 1 else None
    return None


def close_options(value, options, aliases=()) -> list:
    """Options (or aliased options) spelled close to ``value``, best first."""
    keys: dict[str, Any] = {}
    for option in options:
        if isinstance(option, str):
            keys.setdefault(option_key(option), option)
    for alias, target in _alias_pairs(aliases):
        if target in options:
            keys.setdefault(option_key(alias), target)
    near = difflib.get_close_matches(option_key(value), list(keys), n=3, cutoff=0.6)
    out: list = []
    for key in near:
        if keys[key] not in out:
            out.append(keys[key])
    return out


def options_message(value, options, aliases=()) -> str:
    """``expected one of 'a', 'b'; did you mean 'a'?``"""
    listed = ", ".join(repr(o) for o in options)
    text = f"expected one of {listed}"
    if isinstance(value, str):
        near = close_options(value, options, aliases)
        if near:
            text += f"; did you mean {near[0]!r}?"
    return text


def natural_scalar(value, base):
    """``value`` as a ``bool``/``int``/``float`` when it is a natural spelling of one
    (``"true"``, ``"12"``, ``"+0.1"``), else unchanged (both gate-1 validators)."""
    if not isinstance(value, str):
        return value
    if base is bool and value.strip().casefold() in ("true", "false"):
        return value.strip().casefold() == "true"
    if base in (int, float) and _NUMERIC.match(value):
        number = float(value)
        if base is float:
            return number
        if math.isfinite(number) and number.is_integer():
            return int(number)
    return value


def wraps_scalar(item_hint, value) -> bool:
    """Whether one ``value`` stands for a one-item list of ``item_hint`` (a string where
    a list of strings, such as refs, is expected)."""
    return isinstance(value, str) and "string" in hint_json_types(item_hint)


def suggest_field(name: str, fields) -> str | None:
    """The accepted field an unknown ``name`` most likely means: a known synonym, its
    singular or plural, then the closest spelling."""
    fields = [str(f) for f in fields]
    if not fields:
        return None
    typed = re.sub(r"[\s\-]+", "_", str(name).strip().casefold())
    for candidate in FIELD_SYNONYMS.get(typed, ()):
        if candidate in fields:
            return candidate
    key = option_key(name)
    by_key = {option_key(f): f for f in fields}
    for candidate in (key, key + "s", key + "es", key[:-1], key[:-3] + "y", key + "_id"):
        if candidate in by_key and by_key[candidate] != name:
            return by_key[candidate]
    near = difflib.get_close_matches(name, fields, n=1, cutoff=0.6)
    return near[0] if near else None


def unknown_field(path, name: str, fields) -> NXError:
    """``/x: unknown field; did you mean 'y'? Fields: y, z`` with ``details.accepted``."""
    fields = [str(f) for f in fields]
    near = suggest_field(name, fields)
    if not fields:
        message = "unknown field; this takes no arguments"
    elif near:
        message = f"unknown field; did you mean {near!r}? Fields: {', '.join(fields)}"
    else:
        message = f"unknown field; fields: {', '.join(fields)}"
    return _bad(
        [*path, name],
        message,
        hint=f"Send {near} instead of {name}" if near else None,
        details={"accepted": fields, "close_matches": [near] if near else []},
    )


def _missing(path, missing: list) -> NXError:
    more = f"; also missing: {', '.join(missing[1:])}" if len(missing) > 1 else ""
    return _bad([*path, missing[0]], "required field is missing" + more)


# ---------------------------------------------------------------------------
# Shapes of hints (for union errors)


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


def hint_json_types(hint) -> set:
    """JSON types a hint accepts (``number``, ``string``, ...)."""
    base, markers = unwrap(hint)
    if Quantity in markers:
        return {"number", "string"}
    if is_union(base):
        out: set = set()
        for alt in typing.get_args(base):
            out |= hint_json_types(alt)
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
    if is_typeddict(base) or base is dict:
        return {"object"}
    return set()


_QUANTITY_FORMS = {
    "length": "a length (10, '10 mm' or an expression)",
    "sheet_length": "a sheet length (10 or '10 mm')",
    "angle": "an angle (90, '90 deg' or an expression)",
    "count": "a whole number or an expression",
}


def ref_example(kinds) -> str:
    """``'e:12'``: a short ref of the first kind (``'b:12'`` for any kind)."""
    exact = sorted(expand_kinds(kinds)) if kinds else []
    return f"'{PREFIX.get(exact[0], 'b') if exact else 'b'}:12'"


def form_text(hint) -> str | None:
    """A short description of what a hint accepts (None for null)."""
    base, markers = unwrap(hint)
    if Quantity in markers:
        return _QUANTITY_FORMS[markers[Quantity].dim]
    if is_union(base):
        parts = [form_text(alt) for alt in typing.get_args(base)]
        return ", ".join(p for p in parts if p) or None
    if base is _NONE:
        return None
    if typing.get_origin(base) is Literal:
        return ", ".join(repr(o) for o in typing.get_args(base))
    if base is str:
        if RefKind in markers:
            kinds = markers[RefKind].kinds
            return f"a {'/'.join(kinds) or 'any'} ref like {ref_example(kinds)}"
        return "a string"
    if base is bool:
        return "true or false"
    if base is int:
        return "a whole number"
    if base is float:
        return "a number"
    origin = typing.get_origin(base)
    if origin is list:
        return "an array"
    if origin is tuple:
        items = typing.get_args(base)
        if all(unwrap(i)[0] is float for i in items):
            return f"[{len(items)} numbers]"
        return f"an array of {len(items)}"
    if is_typeddict(base):
        keys = sorted(base.__required_keys__) or sorted(typeddict_fields(base))
        return "{" + ", ".join(keys) + "}"
    if base is dict:
        return "an object"
    return None


def _forms(alternatives) -> str:
    parts = [form_text(alt) for alt in alternatives]
    return "; ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Which union form a value was meant to have


_STRUCTURAL = ("unknown field", "required field is missing")


def _tags(base) -> dict:
    """``{field: (options, aliases)}`` of a TypedDict's Literal fields (its
    discriminators)."""
    if not is_typeddict(base):
        return {}
    out = {}
    for name, hint in typeddict_fields(base).items():
        inner, markers = unwrap(hint)
        if typing.get_origin(inner) is Literal:
            aliases = markers[Aliases].pairs if Aliases in markers else ()
            out[name] = (typing.get_args(inner), aliases)
    return out


def _takes(value, options, aliases) -> bool:
    exact = any(value == o and type(value) is type(o) for o in options)
    return exact or match_option(value, options, aliases) is not None


def _claims(value, base, alternatives) -> bool:
    """False when ``value`` sets a discriminator of this form to a value that another
    form takes (``{"type": "distance"}`` is not the through-all form)."""
    if not isinstance(value, dict):
        return True
    for key, (options, aliases) in _tags(base).items():
        if key not in value or _takes(value[key], options, aliases):
            continue
        for alt in alternatives:
            other = _tags(unwrap(alt)[0]).get(key)
            if other is not None and _takes(value[key], *other):
                return False
    return True


def _fit(value, alternative, error: NXError, alternatives) -> tuple:
    """How well ``value`` fits a union alternative that rejected it (higher is better).

    A form whose discriminator another form takes fits worst. Then an object form that
    knows more of the value's keys (and misses fewer of its own required keys) fits
    better; then an error found deeper inside the value; then a wrong value over an
    unknown or missing field. So ``{"path", "help_point", "percent": 150}`` reports the
    out-of-range percent of the path form, not an unknown field of the first form.
    """
    base, _ = unwrap(alternative)
    keys = 0
    if isinstance(value, dict) and is_typeddict(base):
        fields = set(typeddict_fields(base))
        missing = set(base.__required_keys__) - set(value)
        keys = len(set(value) & fields) - len(set(value) - fields) - len(missing)
    depth = (error.field or "").count("/")
    structural = any(text in str(error.message) for text in _STRUCTURAL)
    return (_claims(value, base, alternatives), keys, depth, not structural)


def _no_form_claimed(value, same, path):
    """The error when the value sets a discriminator to a value no form takes (a Limit
    ``type`` the tool does not take, a wrong ``principal``): name the accepted values."""
    if not isinstance(value, dict):
        return None
    options: dict = {}
    aliases: dict = {}
    for alt, _error in same:
        for key, (values, pairs) in _tags(unwrap(alt)[0]).items():
            if key in value:
                listed = options.setdefault(key, [])
                listed.extend(v for v in values if v not in listed)
                aliases.setdefault(key, []).extend(pairs)
    for key in sorted(options):
        if not _takes(value[key], options[key], aliases[key]):
            message = options_message(value[key], options[key], aliases[key])
            return _bad(path, f"{key} {value[key]!r} is not accepted here; {message}")
    return None


# ---------------------------------------------------------------------------
# The walker


class _Walker:
    def __init__(self, placeholders: bool):
        self.placeholders = placeholders
        self.out = Checked(args={})
        self.level = FORMULA  # the leniency allowed now (unions narrow it per pass)

    def check(self, value, hint, path):
        base, markers = unwrap(hint)
        if Quantity in markers:
            return self._quantity(value, markers, path)
        if (
            RefKind in markers
            and self.placeholders
            and isinstance(value, str)
            and re.match(PLACEHOLDER_PATTERN, value)
        ):
            self.out.placeholders.append((tuple(path), value))
            self.out.refs.append((tuple(path), markers[RefKind].kinds))
            return value
        if Json in markers:
            try:
                return check_json(value, markers[Json])
            except ValueError as error:
                raise _bad(path, str(error)) from None
        if is_union(base):
            return self._union(value, base, path)
        value = self._base(value, base, path, markers)
        self._markers(value, markers, path)
        if RefKind in markers:
            self.out.refs.append((tuple(path), markers[RefKind].kinds))
        if PathArg in markers:
            self.out.paths.append((tuple(path), markers[PathArg]))
        return value

    def _quantity(self, value, markers, path):
        dim = markers[Quantity].dim
        try:
            formula = quantity_formula(value, dim)
        except ValueError as error:
            raise _bad(path, str(error)) from None
        if formula is not None and self.level < FORMULA:
            raise _bad(path, "an expression formula fits only when nothing else does")
        if isinstance(value, str) and formula is None and _NUMERIC.match(value):
            if self.level < NATURAL:
                raise _bad(path, "expected a number, not a numeric string")
            value = float(value)
        if dim == "count" and formula is None:
            value = int(value)
            self._markers(value, markers, path)
        self.out.quantities.append((tuple(path), dim))
        return value

    def _union(self, value, base, path):
        alternatives = typing.get_args(base)
        top = self.level
        errors: list = []
        for level in range(EXACT, top + 1):
            self.level = level
            errors = []
            try:
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
            finally:
                self.level = top
        kind = _json_type(value)
        same = [(alt, e) for alt, e in errors if kind in hint_json_types(alt)]
        forms = _forms(alternatives)
        if len(same) == 1:
            only = same[0][1]
            others = [a for a in alternatives if a is not same[0][0] and unwrap(a)[0] is not _NONE]
            if kind not in ("object", "array") and others and only.field == (pointer(path) or None):
                raise _bad(
                    path,
                    f"{only.message.split(': ', 1)[-1]}; accepted forms: {forms}",
                    hint=only.hint,
                    details=only.details or None,
                )
            raise only
        if same:
            unclaimed = _no_form_claimed(value, same, path)
            if unclaimed is not None:
                raise unclaimed
            scored = sorted(
                ((_fit(value, alt, e, alternatives), i, e) for i, (alt, e) in enumerate(same)),
                key=lambda item: (item[0], -item[1]),
                reverse=True,
            )
            best, runner_up = scored[0], scored[1]
            if best[0] > runner_up[0]:
                raise best[2]  # the form the value was meant to have
            raise _bad(
                path,
                f"does not match any accepted form ({forms}); closest: {best[2].message}",
            )
        expected = forms or " or ".join(sorted(hint_json_types(base)))
        raise _bad(path, f"expected {expected}, got {kind}")

    def _base(self, value, base, path, markers):
        natural = self.level >= NATURAL
        if base is _NONE:
            if value is not None:
                raise _bad(path, "expected null")
            return value
        if typing.get_origin(base) is Literal:
            options = typing.get_args(base)
            if any(value == o and type(value) is type(o) for o in options):
                return value
            aliases = markers[Aliases].pairs if Aliases in markers else ()
            if natural:
                hit = match_option(value, options, aliases)
                if hit is not None:
                    return hit
            raise _bad(path, options_message(value, options, aliases))
        if natural and base in (bool, int, float):
            value = natural_scalar(value, base)
        if base is bool:
            if not isinstance(value, bool):
                raise _bad(path, "expected true or false")
            return value
        if base is int:
            if isinstance(value, bool) or not isinstance(value, int):
                if isinstance(value, float) and value.is_integer():
                    return int(value)
                raise _bad(path, "expected a whole number")
            return value
        if base is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                if isinstance(value, str) and _UNIT_STRING.match(value):
                    raise _bad(
                        path,
                        "expected a plain number: this field takes no unit strings like "
                        "'40 mm' (lengths are in part units, angles in degrees)",
                    )
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
            (item,) = typing.get_args(base)
            if natural and wraps_scalar(item, value):
                value = [value]  # one ref where a list of refs is expected
            if not isinstance(value, (list, tuple)):
                raise _bad(path, "expected an array")
            return [self.check(v, item, [*path, i]) for i, v in enumerate(value)]
        if origin is tuple:
            items = typing.get_args(base)
            if not isinstance(value, (list, tuple)) or len(value) != len(items):
                shape = form_text(base) or f"an array of {len(items)}"
                if shape.endswith("numbers]"):
                    shape += ", e.g. [" + ", ".join("0" * len(items)) + "]"
                raise _bad(path, f"expected {shape}")
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
            raise unknown_field(path, extra[0], fields)
        missing = sorted(set(td.__required_keys__) - set(value))
        if missing:
            raise _missing(path, missing)
        return {k: self.check(v, fields[k], [*path, k]) for k, v in value.items()}

    @staticmethod
    def _markers(value, markers, path):
        text = markers.get(Text)
        if text is not None and isinstance(value, str):
            if len(value) > text.max:
                raise _bad(path, f"at most {text.max} characters")
            if text.pattern and not re.match(text.pattern, value):
                raise _bad(path, _format_message(value, markers))
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


def _format_message(value: str, markers) -> str:
    """Why a string fails its pattern, in terms of what to send."""
    if RefKind in markers:
        kinds = markers[RefKind].kinds
        source = (
            "nx_list_components"
            if "component" in kinds
            else "an earlier result, nx_list_objects or nx_find_geometry"
        )
        return (
            f"expected a {'/'.join(kinds) or 'object'} ref like {ref_example(kinds)} (from "
            f"{source}), got {value!r}; display names are not refs"
        )
    doc = markers.get(Doc)
    pattern = markers[Text].pattern
    text = "has an invalid format"
    if doc is not None:
        text += f"; expected {doc.text}"
    if pattern and len(pattern) <= 40:
        text += f" (pattern {pattern})"
    return text


def _variant_fields(params) -> list:
    """Fields that pick the form of a ``ByVariant`` argument: checked first, so their
    canonical value chooses the form."""
    out = []
    for param in params.values():
        by = unwrap(param.hint)[1].get(ByVariant)
        if by is not None and by.field in params and by.field not in out:
            out.append(by.field)
    return out


def validate(params: Mapping[str, Any], args, *, placeholders: bool = False) -> Checked:
    """Validate ``args`` against ``params`` (``{name: Param}`` from the catalogue).

    ``Checked.args`` holds canonical values (natural spellings resolved) in the order of
    ``params``; the handler receives them."""
    if not isinstance(args, dict):
        raise _bad([], "arguments must be an object")
    walker = _Walker(placeholders)
    unknown = sorted(set(args) - set(params))
    if unknown:
        raise unknown_field([], unknown[0], params)
    first = _variant_fields(params)
    order = [*first, *(n for n in params if n not in first)]
    for name in order:
        param = params[name]
        if name in args:
            if args[name] is None and not param.required and param.default is None:
                walker.out.args[name] = None
                continue
            hint = _form(param.hint, walker.out.args)
            walker.out.args[name] = walker.check(args[name], hint, [name])
        elif param.required:
            missing = [n for n in order if params[n].required and n not in args]
            raise _missing([], missing)
    walker.out.args = {n: walker.out.args[n] for n in params if n in walker.out.args}
    return walker.out


def _form(hint, args):
    """The variant's own form of a ``ByVariant`` argument; an unknown variant value keeps
    the free-form hint (its own field reports the error)."""
    by = unwrap(hint)[1].get(ByVariant)
    value = args.get(by.field) if by is not None else None
    return by.forms[value] if isinstance(value, str) and value in by.forms else hint


def get_at(data, path):
    for key in path:
        data = data[key]
    return data


def set_at(data, path, value):
    for key in path[:-1]:
        data = data[key]
    data[path[-1]] = value
