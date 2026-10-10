"""NX expressions: create, edit, read, bind and semantic feature parameters.

NX refuses a bad formula with a native error; that becomes
``NX_INVALID_FORMULA`` (license failures stay ``NX_LICENSE_UNAVAILABLE``).
NX 2406 lacks ``Expression.GetExpressionParents``; dependents are still
available through ``GetReferencingExpressions``.
"""

from __future__ import annotations

import re

from nx_mcp.core.errors import fail, native

UNIT_NAMES = {"mm": "MilliMeter", "in": "Inch", "deg": "Degrees", "rad": "Radian", "unitless": None}
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")
MAX_FORMULA = 4096
# NX 2406 "Dimension error": an expression of the wrong dimension (angle on a length).
DIMENSION_ERROR = 3270081

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


def units(exp):
    """``"mm"``, ``"in"``, ``"deg"``, ``"rad"``, another NX abbreviation or ``"unitless"``."""
    unit = getattr(exp, "Units", None)
    return unit.Abbreviation if unit else "unitless"


def dependents(exp):
    """Expressions that reference ``exp`` ([] where NX lacks the query)."""
    query = getattr(exp, "GetReferencingExpressions", None)
    return list(query()) if query is not None else []


def alias(feature, name):
    """The feature expression for a semantic parameter name (``"distance"``...).

    Matches the words in ``ALIASES`` against each expression's NX description;
    raises ``NX_NOT_FOUND`` (listing the feature's expressions) if none match.
    """
    words = ALIASES.get(name, (name.replace("_", " "),))
    expressions = list(feature.GetExpressions())
    described = [(e, str(getattr(e, "Description", "") or "")) for e in expressions]
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
    fail(
        "NX_NOT_FOUND",
        f"The feature has no {name!r} parameter",
        details={"expressions": [{"name": e.Name, "description": t} for e, t in described]},
    )


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
