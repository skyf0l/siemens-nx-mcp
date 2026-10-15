"""Result rows, pages, measured values (:func:`denoise`), names and native enum names."""

from __future__ import annotations

import math

from nx_mcp.core.errors import fail
from nx_mcp.core.references import Row as Row  # the row type lives with its producer

MAX_PAGE = 200


def rows(tx, objs, kind=None) -> list[Row]:
    """Reference rows for live objects via ``tx.ref``."""
    return [tx.ref(o, kind=kind) for o in objs]


def page(items, offset=0, limit=20, maximum=MAX_PAGE):
    """``(selected, page)`` with ``page = {offset, limit, returned, total, next_offset}``."""
    if type(offset) is not int or offset < 0:
        fail("NX_INVALID_ARGUMENT", "offset must be a nonnegative integer", field="/offset")
    if type(limit) is not int or not 1 <= limit <= maximum:
        fail("NX_INVALID_ARGUMENT", f"limit must be 1-{maximum}", field="/limit")
    items = list(items)
    selected = items[offset : offset + limit]
    end = offset + len(selected)
    return selected, {
        "offset": offset,
        "limit": limit,
        "returned": len(selected),
        "total": len(items),
        "next_offset": end if end < len(items) else None,
    }


def measured(value, dim="length"):
    """A finite float read back from NX; NaN or infinity is ``NX_INCOMPLETE_RESULT``."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = math.nan
    if not math.isfinite(number):
        fail("NX_INCOMPLETE_RESULT", f"NX returned no finite {dim}", details={"value": str(value)})
    return number


DIGITS = 12  # significant digits a read-back keeps (NX's doubles carry ~15, noise below)


def denoise(value, scale: float = 1.0, digits: int = DIGITS):
    """A read-back without float noise: every float rounded to ``digits`` significant
    digits (``50.00000000000001`` -> 50.0, ``76.19999999999999`` -> 76.2), and a float
    smaller than ``scale * 10**-digits`` is 0.0 (``7.2e-32`` beside lengths of about 1).
    ``scale`` is the size of the values compared (a body's extent for its coordinates).
    Lists, tuples (as lists) and dicts are cleaned item by item; other values pass."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return value
        if abs(value) < abs(float(scale or 1.0)) * 10.0 ** (-digits):
            return 0.0
        return float(f"{value:.{digits}g}")
    if isinstance(value, dict):
        return {k: denoise(v, scale, digits) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [denoise(v, scale, digits) for v in value]
    return value


def rename(tx, obj, name):
    """Name an NX object; returns the name as NX normalised it (e.g. upper case)."""
    obj.SetName(str(name))
    return str(getattr(obj, "Name", name))


def enum_int(value) -> int:
    """A native enum member (or plain int) as int: Python NXOpen members convert with
    ``int()`` or print as their value; a ``.value`` attribute is used when present."""
    value = getattr(value, "value", value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(str(value))


def _same_integer(member, value):
    """NX 2406 enums stringify as their integer: compare by that text."""
    text = str(value)
    return text.lstrip("-").isdigit() and str(member) == text


def enum_name(value, enum, names=None, default=None):
    """The member name of a native enum value.

    ``names`` limits the search (and keeps its order); otherwise every public
    attribute of ``enum`` is a candidate. NXOpen for Python cannot expose a
    member called ``None``: listed in ``names`` it matches the value 0. NX 2406
    enum values also match a member with the same integer text. Without a
    match the result is ``default``, or ``"unknown_<value>"``.
    """
    candidates = names if names is not None else [n for n in dir(enum) if not n.startswith("_")]
    for name in candidates:
        member = getattr(enum, name, None)
        if member is None:
            if name == "None" and str(value) == "0":
                return name
            continue
        if member == value or _same_integer(member, value):
            return name
    return default if default is not None else "unknown_" + str(value)
