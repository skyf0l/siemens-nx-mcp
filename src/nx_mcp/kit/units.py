"""Length and angle units: ``"mm"`` and ``"in"`` everywhere (never ``"inch"``).

A plain number is already in the target units; a string ``"<number> <unit>"``
is converted explicitly. Part units come from ``part.PartUnits``, sheet units
from ``sheet.Units``. NXOpen placement points on a drawing sheet take *part*
units, so sheet coordinates are converted with ``part_to_sheet``.
"""

from __future__ import annotations

import math
import re

from nx_mcp.core.errors import fail

MM_PER = {"mm": 1.0, "in": 25.4}
RAD_PER = {"deg": math.pi / 180.0, "rad": 1.0}
DIMENSIONS = {"length": MM_PER, "sheet_length": MM_PER, "angle": RAD_PER}
# Default modeling tolerance (distance, position, chord) in millimetres.
TOLERANCE_MM = 0.001

_QUANTITY = re.compile(r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([A-Za-z]+)\s*$")
_SPELLINGS = {"mm": "mm", "in": "in", "inch": "in", "inches": "in", "deg": "deg", "rad": "rad"}


def normalize(units):
    """Canonical unit spelling: ``"inch"``/``"inches"`` -> ``"in"``."""
    try:
        return _SPELLINGS[str(units).strip().lower()]
    except KeyError:
        fail("NX_UNSUPPORTED_OPTION", f"Unknown units {units!r}; use mm or in")


def part_units(tx, part=None):
    """``"mm"`` or ``"in"`` for the work part (or ``part``)."""
    part = part if part is not None else tx.part
    return "mm" if part.PartUnits == tx.nx.BasePart.Units.Millimeters else "in"


def sheet_units(tx, sheet):
    """``"mm"`` or ``"in"`` for a drawing sheet."""
    unit = tx.nx.Drawings.DrawingSheet.Unit
    if sheet.Units == unit.Millimeters:
        return "mm"
    if sheet.Units == unit.Inches:
        return "in"
    fail("NX_UNSUPPORTED_OPTION", "Unknown native drawing units")


def factor(source, target):
    """Multiply a length in ``source`` units by this to get ``target`` units."""
    return MM_PER[normalize(source)] / MM_PER[normalize(target)]


def part_to_sheet(tx, sheet, part=None):
    """Sheet units per part unit (1/25.4 for an mm part on an inch sheet)."""
    return factor(part_units(tx, part), sheet_units(tx, sheet))


def tolerance(units, mm=TOLERANCE_MM):
    """A millimetre tolerance expressed in ``units`` (0.001 mm = 0.001/25.4 in)."""
    return mm / MM_PER[normalize(units)]


def part_tolerance(tx, mm=TOLERANCE_MM, part=None):
    """A millimetre tolerance in work-part units (builders' Tolerance fields)."""
    return tolerance(part_units(tx, part), mm)


def parse(text, dim="length", field=None):
    """``(value, unit)`` of a ``"<number> <unit>"`` string of dimension ``dim``."""
    table = DIMENSIONS[dim]
    match = _QUANTITY.match(text) if isinstance(text, str) else None
    unit = _SPELLINGS.get(match.group(2).lower()) if match else None
    if unit not in table:
        allowed = "/".join(table)
        kind = dim.replace("_", " ")
        fail("NX_INVALID_ARGUMENT", f"{text!r} is not a {kind} in {allowed}", field=field)
    value = float(match.group(1))
    if not math.isfinite(value):
        fail("NX_INVALID_ARGUMENT", f"{text!r} is not finite", field=field)
    return value, unit


def convert(value, dim, to, field=None):
    """A float in ``to`` units; plain numbers are taken as already in ``to``."""
    table = DIMENSIONS[dim]
    to = normalize(to)
    if isinstance(value, str):
        number, unit = parse(value, dim, field)
        return number * table[unit] / table[to]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        fail("NX_INVALID_ARGUMENT", f"{dim} must be a finite number or unit string", field=field)
    return float(value)


def say(value, units):
    """``"10 mm"``: shortest exact text of a value with its unit."""
    return f"{float(value):.6g} {units}"
