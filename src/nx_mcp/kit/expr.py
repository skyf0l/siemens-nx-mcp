"""NX expressions: create, edit, read, bind and semantic feature parameters.

NX refuses a bad formula with a native error; that becomes
``NX_INVALID_FORMULA`` (license failures stay ``NX_LICENSE_UNAVAILABLE``).
NX 2406 lacks ``Expression.GetExpressionParents``; dependents are still
available through ``GetReferencingExpressions``.

Expressions as values: a Length, Angle or count argument may name an expression or
give a formula (``"thickness"``, ``"thickness/2"``). Gate 9 evaluates it with
:func:`evaluate` (``Services.evaluate_formula``) into a ``core.types.Formula``; a
handler passes :func:`rhs` of the value to a builder expression's ``RightHandSide``
(or calls :func:`drive`), so the feature follows later edits of the expression.
"""

from __future__ import annotations

import contextlib
import re

from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.core.types import MM_PER_IN

UNIT_NAMES = {"mm": "MilliMeter", "in": "Inch", "deg": "Degrees", "rad": "Radian", "unitless": None}
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")
MAX_FORMULA = 4096
# NX 2406 "Dimension error": an expression of the wrong dimension (angle on a length).
DIMENSION_ERROR = 3270081
# NX 2406: a formula naming an expression the part does not have / a syntax error.
NO_SUCH_EXPRESSION, SYNTAX_ERROR = 1050007, 3270008
# NX unit names (Unit.Name) -> (dimension, factor to mm or degrees).
UNIT_SCALE = {
    "MilliMeter": ("length", 1.0),
    "Inch": ("length", MM_PER_IN),
    "CentiMeter": ("length", 10.0),
    "Meter": ("length", 1000.0),
    "Foot": ("length", 12 * MM_PER_IN),
    "MicroMeter": ("length", 0.001),
    "Degrees": ("angle", 1.0),
    "Radian": ("angle", 57.29577951308232),
}
_TARGET_UNIT = {"mm": "MilliMeter", "in": "Inch", "deg": "Degrees", "rad": "Radian"}
_NOUN = {
    "length": "a length",
    "sheet_length": "a length",
    "angle": "an angle",
    "count": "a count",
    "scalar": "a number",
}

# Semantic parameter names -> words of the NX expression description, in order
# of preference. Descriptions look like "Extrude(3) End Limit" or "Blend(5) Radius 1".
ALIASES = {
    "start": ("Start Limit", "Start Distance"),
    "end": ("End Limit", "End Distance"),
    "distance": ("End Limit", "Distance", "Offset"),
    "radius": ("Radius",),
    "diameter": ("Diameter",),
    "depth": ("Depth",),
    "angle": ("Angle",),
    "thickness": ("Thickness",),
    "count": ("Count", "Number of Copies"),
    "spacing": ("Pitch Distance", "Spacing", "Pitch"),
    "offset": ("Offset", "Distance"),
}
# Aliases of one feature kind: ``{name: (description word naming the kind, words)}``. NX 2406
# describes a tube's diameters as "(Tube(2) Limit 1)" (outer, live) and "Limit 2" (inner).
FEATURE_ALIASES = {
    "outer_diameter": ("Tube(", ("Limit 1",)),
    "inner_diameter": ("Tube(", ("Limit 2",)),
}


def _formula(formula, field="/formula"):
    if not isinstance(formula, str) or not formula.strip() or len(formula) > MAX_FORMULA:
        fail(
            "NX_INVALID_ARGUMENT",
            f"formula must contain 1-{MAX_FORMULA} characters of NX expression syntax",
            field=field,
        )
    return formula


def editable(exp):
    """A local, unlocked expression the agent may edit."""
    return not (
        getattr(exp, "IsNoEdit", False)
        or getattr(exp, "IsRightHandSideLockedFromEdit", False)
        or getattr(exp, "IsInterpartExpression", False)
    )


def find(tx, name):
    """The work-part expression called ``name`` (case-sensitive), or None."""
    return next((e for e in tx.part.Expressions if e.Name == name), None)


def create(tx, name, formula, units="unitless"):
    """Create a Number expression ``name = formula`` in ``units``."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        fail("NX_INVALID_ARGUMENT", "New expression requires a simple identifier", field="/name")
    _formula(formula)
    if units not in UNIT_NAMES:
        fail("NX_INVALID_ARGUMENT", "units must be mm, in, deg, rad or unitless", field="/units")
    part = tx.part
    if find(tx, name) is not None:
        fail("NX_NAME_CONFLICT", f"Expression {name} already exists", field="/name")
    unit = part.UnitCollection.FindObject(UNIT_NAMES[units]) if UNIT_NAMES[units] else None
    with native(default="NX_INVALID_FORMULA"):
        return part.Expressions.CreateNumberExpression(name + "=" + formula, unit)


def set_formula(tx, exp, formula):
    """Edit an editable expression's formula; returns the expression."""
    _formula(formula)
    if not editable(exp):
        fail("NX_READ_ONLY", f"Expression {exp.Name} is locked or interpart")
    with native(default="NX_INVALID_FORMULA"):
        tx.part.Expressions.EditExpression(exp, formula)
    return exp


def value(tx, exp):
    """Numeric value in the expression's own units (Integer as int, else None)."""
    kind = getattr(exp, "Type", "Number")
    if kind == "Number":
        return float(exp.GetValueUsingUnits(tx.nx.Expression.UnitsOption.Expression))
    if kind == "Integer":
        return int(exp.IntegerValue)
    return None


def value_or_none(tx, exp):
    """:func:`value`, or None when NX gives no numeric value (a string or list
    expression, or one NX cannot evaluate)."""
    try:
        return value(tx, exp)
    except Exception:  # a string or list expression has no numeric value
        return None


# NX unit names -> the short names the tools take (NX 2406 abbreviates Degrees as "°").
SHORT_UNITS = {"MilliMeter": "mm", "Inch": "in", "Degrees": "deg", "Radian": "rad"}


def units(exp):
    """``"mm"``, ``"in"``, ``"deg"``, ``"rad"``, another NX abbreviation or ``"unitless"``:
    the names the tools take (an angle in degrees is ``"deg"``, never NX's ``"°"``)."""
    unit = getattr(exp, "Units", None)
    if not unit:
        return "unitless"
    short = SHORT_UNITS.get(str(getattr(unit, "Name", "") or ""))
    if short:
        return short
    abbreviation = str(unit.Abbreviation)
    return "deg" if abbreviation == "°" else abbreviation


def dependents(exp):
    """Expressions that reference ``exp`` ([] where NX lacks the query)."""
    query = getattr(exp, "GetReferencingExpressions", None)
    return list(query()) if query is not None else []


def alias(feature, name):
    """The feature expression for a semantic parameter name (``"distance"``...).

    Matches the words in ``ALIASES`` against each expression's NX description (a
    ``FEATURE_ALIASES`` name only on that feature kind); raises ``NX_NOT_FOUND``
    (listing the feature's expressions) if none match.
    """
    words = ALIASES.get(name, (name.replace("_", " "),))
    expressions = list(feature.GetExpressions())
    described = [(e, str(getattr(e, "Description", "") or "")) for e in expressions]
    if name in FEATURE_ALIASES:  # only the expressions of that feature kind match
        kind, words = FEATURE_ALIASES[name]
        described = [(e, t) for e, t in described if kind.casefold() in t.casefold()]
    for word in words:
        hits = [e for e, text in described if word.casefold() in text.casefold()]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            fail(
                "NX_AMBIGUOUS_REFERENCE",
                f"{len(hits)} expressions of the feature match {name!r}",
                details={"candidates": [e.Name for e in hits]},
            )
    listed = [(e, str(getattr(e, "Description", "") or "")) for e in expressions]
    fail(
        "NX_NOT_FOUND",
        f"The feature has no {name!r} parameter",
        details={"expressions": [{"name": e.Name, "description": t} for e, t in listed]},
    )


def alias_names() -> tuple[str, ...]:
    """Every semantic parameter name :func:`alias` resolves on some feature."""
    return (*ALIASES, *FEATURE_ALIASES)


def bind(tx, target, source):
    """Drive a builder/feature expression ``target`` by ``source``.

    ``source`` is an NX expression (bound by name), a number or a formula. A
    dimension mismatch (NX 2406 code 3270081) is ``NX_INVALID_ARGUMENT``.
    """
    if isinstance(source, bool):
        fail("NX_INVALID_ARGUMENT", "A boolean cannot drive an expression")
    if isinstance(source, (int, float)):
        formula = repr(float(source))
    elif isinstance(source, str):
        formula = _formula(source)
    else:
        formula = source.Name
    with native({DIMENSION_ERROR: "NX_INVALID_ARGUMENT"}, default="NX_INVALID_FORMULA"):
        tx.part.Expressions.EditExpression(target, formula)
    return target


# ---- expressions as values (gate 9) ------------------------------------------------
def rhs(value, integer: bool = False) -> str:
    """The ``RightHandSide`` text for a builder expression: an evaluated
    ``core.types.Formula`` keeps its formula (the feature follows the expression), a
    number becomes its exact text (``integer``: a whole number)."""
    formula = getattr(value, "formula", None)
    if formula:
        return str(formula)
    if integer:
        return str(int(value))
    return repr(float(value))


def number(value):
    """``float(value)``, except that an evaluated formula stays itself (a float that
    remembers its formula for :func:`rhs`)."""
    return value if getattr(value, "formula", None) and isinstance(value, float) else float(value)


def drive(expression, value, integer: bool = False):
    """Set a builder expression (``builder.X.Expression``-like) from a number or an
    evaluated formula; returns the expression."""
    expression.RightHandSide = rhs(value, integer)
    return expression


def _unit_of(exp):
    unit = getattr(exp, "Units", None)
    return getattr(unit, "Name", None) if unit else None


def _file_of(part) -> str:
    """The part's file name for messages (``bracket.prt``), else ``the work part``."""
    full = str(getattr(part, "FullPath", "") or "").replace("\\", "/")
    return full.rsplit("/", 1)[-1] if full else "the work part"


def evaluate(services, formula, dim, unit, *, field=None, scratch=False) -> float:
    """``formula`` (an expression name or formula) of the work part, in ``unit``.

    A bare name is read without changing the part: its expression must be a Number of
    dimension ``dim`` or unitless (taken as a plain number). Another formula needs
    ``scratch``: NX evaluates it in a temporary expression of ``unit``, deleted at once,
    so NX itself checks the names, the syntax and the dimension.
    """
    part = services.work_part()
    if part is None:
        fail("NX_NO_WORK_PART", "No work part is open", hint="Open a part with nx_open_part")
    noun = _NOUN.get(dim, "a value")
    listing = "List them with nx_list_objects kind 'expression'; create one with nx_set_expression"
    where = _file_of(part)
    if NAME.fullmatch(formula):
        exp = next((e for e in part.Expressions if e.Name == formula), None)
        if exp is None:
            fail(
                "NX_INVALID_ARGUMENT",
                f"{field or 'value'}: {where} has no expression {formula!r}",
                field=field,
                hint=listing,
            )
        if getattr(exp, "Type", "Number") not in ("Number", "Integer"):
            fail(
                "NX_INVALID_ARGUMENT",
                f"{field or 'value'}: expression {formula!r} is not a number",
                field=field,
            )
        own = _unit_of(exp)
        if getattr(exp, "Type", "Number") == "Integer":
            number = float(exp.IntegerValue)
        else:
            number = float(exp.GetValueUsingUnits(services.nx.Expression.UnitsOption.Expression))
        if own is None or dim in ("count", "scalar"):
            return number  # unitless: a plain number in the target units, as NX takes it
        kind, scale = UNIT_SCALE.get(own, (None, None))
        want = "angle" if dim == "angle" else "length"
        if kind != want or scale is None:
            fail(
                "NX_INVALID_ARGUMENT",
                f"{field or 'value'}: expression {formula!r} is in {own}, not {noun}",
                field=field,
            )
        target = UNIT_SCALE[_TARGET_UNIT[unit]][1]
        return number * scale / target
    if not scratch:
        fail(
            "NX_INVALID_ARGUMENT",
            f"{field or 'value'}: this tool takes an expression name, not a formula ({formula!r})",
            field=field,
            hint="Create the formula as an expression with nx_set_expression, then pass its name",
        )
    if len(formula) > MAX_FORMULA:
        fail("NX_INVALID_ARGUMENT", f"{field or 'value'}: formula too long", field=field)
    unit_name = _TARGET_UNIT.get(unit)
    nx_unit = part.UnitCollection.FindObject(unit_name) if unit_name else None
    codes = {
        DIMENSION_ERROR: "NX_INVALID_ARGUMENT",
        NO_SUCH_EXPRESSION: "NX_INVALID_ARGUMENT",
        SYNTAX_ERROR: "NX_INVALID_FORMULA",
    }
    try:
        with native(codes, default="NX_INVALID_FORMULA"):
            temporary = part.Expressions.CreateSystemExpressionWithUnits(formula, nx_unit)
    except NXError as error:
        if error.code not in ("NX_INVALID_ARGUMENT", "NX_INVALID_FORMULA"):
            raise  # a license or session failure stays what it is
        reason = {
            DIMENSION_ERROR: f"is not {noun}",
            NO_SUCH_EXPRESSION: f"names an expression {where} does not have",
        }.get(error.nx_code or 0, "is not a valid NX formula")
        fail(
            error.code,
            f"{field or 'value'}: formula {formula!r} {reason}",
            field=field,
            hint=listing,
            nx_code=error.nx_code,
        )
    try:
        return float(temporary.GetValueUsingUnits(services.nx.Expression.UnitsOption.Expression))
    finally:
        with contextlib.suppress(Exception):
            part.Expressions.Delete(temporary)
