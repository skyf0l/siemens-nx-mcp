"""Associative measurements: a measure feature whose expression other features can use.

NX 2406 lessons kept here (probed live on blocks and a drafted block):

- ``MeasureManager.CreateMeasureDistanceBuilder`` / ``CreateMeasureBodyBuilder`` only back
  the Measure dialog ("not required for creation", NXOpen docs). The feature comes from
  the measurement objects: ``NewDistance(unit, MeasureType.Minimum, a, b)``,
  ``NewLength(unit, objects)`` or ``NewMassProperties(units, accuracy, bodies)``, then
  ``CreateFeature()`` (``DISTANCE MEASUREMENT``, ``LENGTH MEASUREMENT``,
  ``BODY_MEASUREMENT``); the measurement object is disposed after.
- Distance and length features own one expression (formula ``distance8``/``length10``,
  a measured value); a body feature owns five: surface_area, volume, mass, weight and
  radius_of_gyration. The measured objects' features become the feature's parents.
- They are associative: editing a block's height from 10 to 20 and updating the model
  moved a face-to-face minimum distance from 100.4988 to 101.9804 (equal to
  ``Measurement.GetMinimumDistance``) and doubled a body volume 64000 -> 128000.
- ``Expressions.Rename`` gives the measured value a name other formulas can use.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import BodyRef, Doc, Items, RefTo
from nx_mcp.domains.modeling.tree import ExpressionName
from nx_mcp.kit import expr, measure, readback, result, select, units

Kind = Literal["distance", "length", "body"]
DISTANCE_KINDS = ("body", "face", "edge", "curve", "point", "datum_plane", "datum_axis")
Measurable = Annotated[
    str, RefTo(*DISTANCE_KINDS, doc="distance: body/face/edge/curve/point/datum 'fc:12'")
]
LengthRef = Annotated[str, RefTo("curve", "edge")]
# NewMassProperties units per part unit system: area, volume, mass, length, force.
BODY_UNITS = {
    "mm": ("SquareMilliMeter", "CubicMilliMeter", "Kilogram", "MilliMeter", "Newton"),
    "in": ("SquareInch", "CubicInch", "PoundMass", "Inch", "PoundForce"),
}
LENGTH_UNITS = {"mm": "MilliMeter", "in": "Inch"}
ACCURACY = 0.999
# Relative agreement of the feature's value with an independent measurement.
AGREE_REL = 1e-6
BODY_KEYS = ("surface_area", "volume", "mass", "weight", "radius_of_gyration")


class MeasuredExpression(TypedDict):
    expression: result.Row
    quantity: str  # distance, length, surface_area, volume, mass, weight, radius_of_gyration
    formula: str
    value: float
    units: str


class MeasurementData(TypedDict):
    feature: result.Row
    kind: Kind
    value: float  # distance, total length, or body volume
    units: str
    expression: MeasuredExpression  # the main value (renamed when name was given)
    expressions: list[MeasuredExpression]
    parents: list[result.Row]


def _quantity(kind: str, exp) -> str:
    if kind != "body":
        return kind
    text = f"{exp.RightHandSide} {getattr(exp, 'Description', '')}"
    return next((k for k in BODY_KEYS if k in text), "other")


def _row(tx, kind, exp) -> MeasuredExpression:
    return {
        "expression": tx.ref(exp, kind="expression"),
        "quantity": _quantity(kind, exp),
        "formula": str(exp.RightHandSide),
        "value": result.measured(exp.Value),
        "units": expr.units(exp),
    }


def _check(kind, first, second, curves, bodies) -> None:
    given = {"first": first, "second": second, "curves": curves, "bodies": bodies}
    wanted = {"distance": ("first", "second"), "length": ("curves",), "body": ("bodies",)}[kind]
    for name in wanted:
        if given[name] is None:
            fail("NX_INVALID_ARGUMENT", f"kind {kind} needs {' and '.join(wanted)}",
                 field="/" + name)  # fmt: skip
    for name, value in given.items():
        if value is not None and name not in wanted:
            fail("NX_INVALID_ARGUMENT", f"{name} is not used by kind {kind}", field="/" + name,
                 hint=f"kind {kind} takes {' and '.join(wanted)}")  # fmt: skip
    if kind == "distance" and first == second:
        fail("NX_INVALID_ARGUMENT", "first and second must differ", field="/second")


def _create(tx, kind, first, second, curves, bodies):
    """``(measure feature, independent value)`` for the measurement ``kind``."""
    part, system = tx.part, units.part_units(tx)
    manager = part.MeasureManager
    if kind == "distance":
        a = select.one(tx, first, *DISTANCE_KINDS, field="/first")
        b = select.one(tx, second, *DISTANCE_KINDS, field="/second")
        with native(default="NX_ERROR"):
            check = float(tx.session.Measurement.GetMinimumDistance(a, b)[0])
            unit = part.UnitCollection.FindObject(LENGTH_UNITS[system])
            made = manager.NewDistance(unit, tx.nx.MeasureManager.MeasureType.Minimum, a, b)
    elif kind == "length":
        objects = [
            select.one(tx, ref, "curve", "edge", field=f"/curves/{i}")
            for i, ref in enumerate(curves)
        ]
        check = measure.length(tx, objects)
        with native(default="NX_ERROR"):
            unit = part.UnitCollection.FindObject(LENGTH_UNITS[system])
            made = manager.NewLength(unit, objects)
    else:
        found = select.bodies(tx, bodies, "/bodies")
        for i, body in enumerate(found):
            if not getattr(body, "IsSolidBody", True):
                fail("NX_WRONG_GEOMETRY", "A body measurement takes solid bodies",
                     field=f"/bodies/{i}")  # fmt: skip
        check = measure.volume(tx, found)
        with native(default="NX_ERROR"):
            found_units = [part.UnitCollection.FindObject(n) for n in BODY_UNITS[system]]
            made = manager.NewMassProperties(found_units, ACCURACY, found)
    try:
        with native(default="NX_BUILD_FAILED"):
            feature = made.CreateFeature()
    finally:
        made.Dispose()
    return feature, check


@tool(
    effect="model",
    label="Create measurement",
    summary={
        "distance": "Measure distance {first} to {second}",
        "length": "Measure length of {curves}",
        "body": "Measure body {bodies}",
    },
    keywords=(
        "associative measurement",
        "measure feature",
        "measurement expression",
        "driving measurement",
        "measured distance",
        "measure body",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    variants="kind",
    required_by={"distance": ("first", "second"), "length": ("curves",), "body": ("bodies",)},
    cost="heavy",
    previewable=True,
    example={"kind": "distance", "first": "fc:4", "second": "fc:9", "name": "gap"},
)
def nx_create_measurement(
    tx: Tx,
    *,
    kind: Annotated[
        Kind, Doc("distance: minimum between two objects; length: curves; body: volume and mass")
    ],
    first: Measurable | None = None,
    second: Measurable | None = None,
    curves: Annotated[list[LengthRef], Items(1, 100), Doc("length: curves/edges, summed")]
    | None = None,
    bodies: Annotated[list[BodyRef], Items(1, 50), Doc("body: solid bodies")] | None = None,
    name: Annotated[ExpressionName, Doc("Rename the main expression (distance, length or volume)")]
    | None = None,
) -> MeasurementData:
    """Create an associative measure feature whose expression updates with the model, to drive
    other features ('gap*2'). distance: minimum between first and second; length: total of
    curves; body: surface_area, volume, mass, weight and radius_of_gyration (value: volume).
    Name the main expression to use it; nx_get_feature reads the values after edits and
    nx_update_model. For a one-off value use nx_measure_distance."""
    _check(kind, first, second, curves, bodies)
    if name is not None:
        with tx.checks():
            if expr.find(tx, name) is not None:
                fail("NX_NAME_CONFLICT", f"An expression named {name} exists", field="/name",
                     hint="Pick another name, or omit name to keep NX's")  # fmt: skip
    feature, check = _create(tx, kind, first, second, curves, bodies)
    expressions = list(feature.GetExpressions())
    rows = [_row(tx, kind, e) for e in expressions]
    readback.expect(bool(rows), "expressions", "The measurement made no expression")
    main_quantity = "volume" if kind == "body" else kind
    index = next((i for i, r in enumerate(rows) if r["quantity"] == main_quantity), 0)
    main = rows[index]
    tol = AGREE_REL * max(1.0, abs(check)) + units.part_tolerance(tx) * (kind != "body")
    readback.expect(
        math.isclose(main["value"], check, abs_tol=tol),
        "value",
        f"The measurement reads {main['value']:.9g}, the model measures {check:.9g}",
        value=main["value"],
        expected=check,
    )
    if name is not None:
        with native(default="NX_ERROR"):
            tx.part.Expressions.Rename(expressions[index], name)
        main = rows[index] = _row(tx, kind, expressions[index])
        readback.expect(str(expressions[index].Name) == name, "name", "NX kept another name")
    return {
        "feature": tx.ref(feature, kind="feature"),
        "kind": kind,
        "value": main["value"],
        "units": main["units"],
        "expression": main,
        "expressions": rows,
        "parents": [tx.ref(f, kind="feature") for f in feature.GetParents()],
    }
