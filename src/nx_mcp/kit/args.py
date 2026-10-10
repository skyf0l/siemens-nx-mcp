"""Argument rules the schema cannot express, plus the shared value validators.

Every failure raises ``invalid_argument`` (``NX_INVALID_ARGUMENT``) whose
``field`` is a JSON pointer. ``field`` may be given as a plain name
(``"radius"``) or a pointer (``"/radius"``); messages always use the plain
name.
"""

from __future__ import annotations

import math
from typing import NoReturn, TypeVar

from nx_mcp.core.errors import fail

T = TypeVar("T")


def pointer(field):
    """JSON pointer for a field name: ``radius`` -> ``/radius``, ``a[1]`` -> ``/a/1``."""
    text = str(field)
    if text.startswith("/"):
        return text
    return "/" + text.replace("[", "/").replace("]", "").replace(".", "/")


def _name(field):
    return str(field).lstrip("/")


def _bad(message, field) -> NoReturn:
    fail("NX_INVALID_ARGUMENT", message, field=pointer(field))


def _is_number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


# ---- cross-field rules ------------------------------------------------------------
def need(**fields: object) -> None:
    """Every named field is present (not None)."""
    for name, value in fields.items():
        required(value, name)


def required(value: T | None, field: str) -> T:
    """``value``, which must be present (not None): ``need`` for one field, returned so
    that type checkers see it as given (``origin = args.required(origin, "origin")``)."""
    if value is None:
        _bad(f"{_name(field)} is required", field)
    return value


def exactly_one(**fields):
    """Exactly one of the named fields is present; returns its name."""
    given = [name for name, value in fields.items() if value is not None]
    if len(given) != 1:
        names = ", ".join(fields)
        _bad(f"Give exactly one of {names}", given[1] if len(given) > 1 else next(iter(fields)))
    return given[0]


def distinct(field, values, noun="values"):
    """No value repeats (by equality); returns the values as a list."""
    seen = []
    for i, value in enumerate(values):
        if value in seen:
            _bad(
                f"{_name(field)} contains duplicates: {value!r} repeats; list distinct {noun}",
                f"{pointer(field)}/{i}",
            )
        seen.append(value)
    return list(values)


# ---- single values ----------------------------------------------------------------
def finite(value, field, positive=False):
    """A finite real number (bools rejected), optionally > 0, as float."""
    if not _is_number(value) or (positive and value <= 0):
        _bad(_name(field) + " must be finite" + (" and positive" if positive else ""), field)
    return float(value)


def number(value, field, positive=False, maximum=None, allow_none=False):
    """A finite real number with separate messages for type, sign and upper bound."""
    if value is None and allow_none:
        return None
    name = _name(field)
    if not _is_number(value):
        _bad(f"{name} must be a finite number", field)
    if positive and (value <= 0 or (maximum is not None and value > maximum)):
        limit = f" and at most {maximum:g}" if maximum is not None else ""
        _bad(f"{name} must be positive{limit}", field)
    if maximum is not None and value > maximum:
        _bad(f"{name} must be at most {maximum:g}", field)
    return float(value)


def vec3(value, field="vector"):
    """Three finite numbers as a float list."""
    name = _name(field)
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        _bad(name + " must contain three numbers", field)
    if not all(_is_number(v) for v in value):
        _bad(name + " must be finite", field)
    return [float(v) for v in value]
