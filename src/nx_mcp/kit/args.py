"""Argument rules the schema cannot express, plus the shared value validators.

Every failure raises ``invalid_argument`` (``NX_INVALID_ARGUMENT``) whose
``field`` is a JSON pointer. ``field`` may be given as a plain name
(``"radius"``) or a pointer (``"/radius"``); messages always use the plain
name.
"""

from __future__ import annotations

import math

from nx_mcp.core.errors import fail


def pointer(field):
    """JSON pointer for a field name: ``radius`` -> ``/radius``, ``a[1]`` -> ``/a/1``."""
    text = str(field)
    if text.startswith("/"):
        return text
    return "/" + text.replace("[", "/").replace("]", "").replace(".", "/")


def _name(field):
    return str(field).lstrip("/")


def _bad(message, field):
    fail("NX_INVALID_ARGUMENT", message, field=pointer(field))


def _is_number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


# ---- cross-field rules ------------------------------------------------------------
def need(**fields):
    """Every named field is present (not None)."""
    for name, value in fields.items():
        if value is None:
            _bad(f"{name} is required", name)


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


def point2(value, field="center"):
    """Sketch-local [x, y] as finite floats."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        _bad(_name(field) + " must be local [x, y]", field)
    return [finite(v, field) for v in value]


def sheet_point(value, field="origin"):
    """Drawing-sheet [x, y] (sheet units) as finite floats."""
    name = _name(field)
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        _bad(f"{name} must be [x, y] in sheet units", field)
    if not all(_is_number(v) for v in value):
        _bad(f"{name} must contain finite numbers", field)
    return [float(v) for v in value]


def points3(values, field="points", minimum=1, maximum=1000):
    """A list of minimum..maximum [x, y, z] points."""
    if not isinstance(values, list) or not minimum <= len(values) <= maximum:
        _bad(f"Expected {minimum}..{maximum} points", field)
    result = []
    for i, value in enumerate(values):
        if not isinstance(value, list) or len(value) != 3:
            _bad("Each point must contain x, y, z", f"{pointer(field)}/{i}")
        result.append([finite(v, "coordinate") for v in value])
    return result


def ref(value, field, noun="an object ID"):
    """One non-empty reference string."""
    if not isinstance(value, str) or not value.strip():
        _bad(f"{_name(field)} must be {noun}", field)
    return value


def refs(values, field, minimum=1, maximum=None, noun="object IDs"):
    """A list of minimum..maximum distinct non-empty reference strings."""
    name = _name(field)
    count = len(values) if isinstance(values, (list, tuple)) else -1
    span = (
        noun
        if maximum is None
        else f"{minimum} {noun}"
        if minimum == maximum
        else f"{minimum}-{maximum} {noun}"
    )
    if count < minimum:
        if minimum == 1:
            _bad(f"{name} must be a non-empty list of {span}", field)
        _bad(f"{name} must list {span}", field)
    if maximum is not None and count > maximum:
        if minimum == maximum:
            _bad(f"{name} must list {span}", field)
        _bad(f"{name} accepts at most {maximum} {noun}", field)
    if not all(isinstance(v, str) and v.strip() for v in values):
        _bad(f"{name} must contain non-empty {noun}", field)
    distinct(field, values, noun)
    return list(values)


def choice(value, field, options):
    """One of a closed set of values."""
    if value not in options:
        allowed = ", ".join(sorted(map(str, options)))
        _bad(f"{_name(field)} must be one of {allowed}", field)
    return value
