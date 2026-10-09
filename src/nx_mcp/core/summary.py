"""Render a tool's panel line from its arguments (architecture.md 1.2).

A summary is a format string, a map keyed by variant value, or a callable
``(args, say) -> str``. Placeholders render by type: a reference becomes its NX
name in quotes, a list a count, a length gets its unit and a path is shown
workspace-relative. Rendering never raises; it falls back to the label.
"""

from __future__ import annotations

import string
import typing
from collections.abc import Callable, Mapping

from nx_mcp.core.types import PathArg, Quantity, RefKind, format_quantity, parse_quantity
from nx_mcp.core.validate import is_union, unwrap

MAX_SUMMARY = 60


def _markers(hint) -> tuple[object, dict]:
    base, markers = unwrap(hint)
    if is_union(base):
        alts = [a for a in typing.get_args(base) if a is not type(None)]
        if len(alts) == 1:
            inner, more = unwrap(alts[0])
            return inner, {**more, **markers}
    return base, markers


def _number(value) -> str:
    return f"{float(value):.6g}"


def _singular(word: str) -> str:
    return word[:-1] if word.endswith("s") and not word.endswith("ss") else word


class _Say:
    def __init__(self, spec, args, name_of, path_of, units):
        self.spec, self.args = spec, args
        self.name_of, self.path_of, self.units = name_of, path_of, units or {}

    def __call__(self, field: str) -> str:
        value = self.args.get(field)
        if value is None:
            raise KeyError(field)
        param = self.spec.params[field]
        base, markers = _markers(param.hint)
        if RefKind in markers and isinstance(value, str):
            name = self.name_of(value) if self.name_of else None
            return f'"{name}"' if name else value
        if Quantity in markers:
            dim = markers[Quantity].dim
            number, unit = parse_quantity(value, dim)
            if unit is None:
                unit = (
                    "deg"
                    if dim == "angle"
                    else self.units.get("sheet_length" if dim == "sheet_length" else "length")
                )
            return format_quantity(number, unit)
        if PathArg in markers and isinstance(value, str):
            return self.path_of(value) if self.path_of else value
        if isinstance(value, (list, tuple)):
            if typing.get_origin(base) is tuple or all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in value
            ):
                return "(" + ", ".join(_number(v) for v in value) + ")"
            noun = field if len(value) != 1 else _singular(field)
            return f"{len(value)} {noun}"
        if isinstance(value, bool):
            return "on" if value else "off"
        if isinstance(value, (int, float)):
            return _number(value)
        return str(value)


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_SUMMARY else text[: MAX_SUMMARY - 1] + "…"


def render(
    spec,
    args: Mapping,
    *,
    name_of: Callable[[str], str | None] | None = None,
    path_of: Callable[[str], str] | None = None,
    units: Mapping[str, str] | None = None,
    strict: bool = False,
) -> str:
    """The panel line for ``args``; the label on any failure (``strict`` re-raises)."""
    say = _Say(spec, dict(args), name_of, path_of, units)
    try:
        template = spec.summary
        if isinstance(template, Mapping):
            template = template.get(spec.variant_of(args))
        if template is None:
            if strict:
                raise LookupError("no summary template for these arguments")
            return spec.label
        if callable(template):
            return _clip(template(dict(args), say))
        out = []
        for literal, field, _spec, _conv in string.Formatter().parse(template):
            out.append(literal)
            if field:
                out.append(say(field))
        return _clip("".join(out))
    except Exception:
        if strict:
            raise
        return spec.label
