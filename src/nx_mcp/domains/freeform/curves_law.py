"""Law curves (x, y and z each a law of t from 0 to 1) and general conics (five points, or
two ends, an anchor and rho: ellipse, parabola or hyperbola).

NX 2406 lessons kept here (probed live):

- ``CreateLawCurveBuilder``: ``XLaw``/``YLaw``/``ZLaw`` are ``LawBuilder``s whose
  defaults are ``ByEquation`` with ``Parameter`` ``t`` and ``Function`` ``xt``/``yt``/
  ``zt`` (names of expressions that must exist). ``Constant`` uses ``Value``, ``Linear``
  and ``Cubic`` ``StartValue``..``EndValue`` (expressions in part units). x linear 0..100,
  y cubic 0..40 and z constant 5 give a spline from (0, 0, 5) through (50, 20, 5) to
  (100, 40, 5), length 109.02508; x = 100 t, y = 20 cos(360 t), z = 20 sin(360 t) one
  helix turn of length 160.5934. NX leaves ``t`` at its value (0) afterwards.
- ``CreateGeneralConicBuilder``: ``TwoPointsAnchorRho`` takes ``StartPoint``,
  ``EndPoint``, ``AnchorPoint`` and the ``Rho`` expression; the curve is an NX
  ``Ellipse`` (rho < 0.5), ``Parabola`` (0.5) or ``Hyperbola`` (> 0.5) whose shoulder is
  M + rho (anchor - M): ends 100 apart with the anchor 40 off give lengths 104.40809
  (0.3), 109.82301 (0.5), 116.44644 (0.7). ``FivePoints`` takes ``StartPoint``,
  ``InteriorPoint1..3`` and ``EndPoint`` (a 50 x 30 half ellipse: 127.63499). The plane is
  ``DrawingPlaneOption`` ``General`` with ``GeneralDrawingPlane`` through the points.

Read back: a law curve starts and ends where its constant, linear and cubic laws say;
a conic passes through its points (five points) or through its ends and its shoulder
(rho).
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Length, Point3, Range, Text
from nx_mcp.domains.freeform import curves_common as cc
from nx_mcp.kit import args, builders, curve, expr, geom, readback, units

AXES = ("x", "y", "z")
PARAMETER = "t"
IDENTIFIER = re.compile(r"(?<![\w.])[A-Za-z_]\w*")
CONSTANTS = ("pi", "PI", "Pi")


class _Ramp(TypedDict):
    start: Annotated[Length, Doc("Value at t = 0")]
    end: Annotated[Length, Doc("Value at t = 1")]


class LawRamp(_Ramp, total=False):
    cubic: Annotated[bool, Doc("true: cubic, flat at both ends; default linear")]


class LawEquation(TypedDict):
    formula: Annotated[
        str,
        Text(200, r"^[^=;]+$"),
        Doc("NX formula of t (0..1) in part units, angles in degrees: '20*sin(360*t)'"),
    ]


Law = Annotated[
    Length | LawRamp | LawEquation,
    Doc("A number (constant), {start, end, cubic?} or {formula}; omitted: 0"),
]


class LawData(cc.CurvesData):
    expected_start: list[float | None]  # x, y, z of the laws at t = 0 (None: a formula)
    expected_end: list[float | None]  # at t = 1
    gap: float  # largest difference of the curve ends from the expected values
    expressions: list[str]  # expressions created for formulas (t, xt...): edit them later


def _value(law, at_end):
    """A law's value at t = 0 or 1, None for a formula."""
    if isinstance(law, dict):
        if "formula" in law:
            return None
        return float(law["end" if at_end else "start"])
    return 0.0 if law is None else float(law)


def _varies(law) -> bool:
    """Whether a law changes with t: a formula, or a ramp whose ends differ."""
    if not isinstance(law, dict):
        return False
    return "formula" in law or not geom.close(law["start"], law["end"], 1e-9)


def _free_name(tx, base):
    """``base``, else ``base_1``, ``base_2``... (the first name no expression has)."""
    name, i = base, 0
    while expr.find(tx, name) is not None:
        i += 1
        name = f"{base}_{i}"
    return name


def _parameter(tx, created):
    """The part's unitless ``t`` expression NX varies from 0 to 1 (created when missing)."""
    found = expr.find(tx, PARAMETER)
    if found is None:
        created.append(PARAMETER)
        return expr.create(tx, PARAMETER, "0")
    if getattr(found, "Type", "Number") != "Number" or expr.units(found) != "unitless":
        fail(
            "NX_NAME_CONFLICT",
            "Expression t exists with units: a law formula needs t as its unitless parameter",
            hint="Rename the part's t expression (nx_edit_expression) or use ramps",
        )
    return found


def _unknown_names(tx, formula) -> list[str]:
    """Names in ``formula`` that are neither ``t``, a function call nor an expression."""
    names = []
    for match in IDENTIFIER.finditer(formula):
        word = match.group(0)
        after = formula[match.end() :].lstrip()
        if word in (PARAMETER, *CONSTANTS) or after.startswith("(") or word in names:
            continue
        if expr.find(tx, word) is None:
            names.append(word)
    return names


def _formula(tx, name, formula, axis):
    """The ``name = formula`` expression of one axis; a bad formula names the axis field,
    the formula and its unknown names."""
    field = f"/{axis}/formula"
    try:
        return expr.create(tx, name, formula, units.part_units(tx))
    except NXError as error:
        if error.code != "NX_INVALID_FORMULA":
            raise
        unknown = _unknown_names(tx, formula)
        reason = (
            f"names {', '.join(unknown)}: neither t nor an expression of the part"
            if unknown
            else "is not a valid NX formula"
        )
        error.message = f"{axis} formula {formula!r} {reason}"
        error.field = field
        error.hint = (
            "Write the law in t (0..1), part units and degrees, e.g. '20*sin(360*t)'; "
            "other names must be expressions (nx_list_objects kind 'expression')"
        )
        error.details = {**(error.details or {}), "formula": formula, "unknown": unknown}
        raise


def _set_law(tx, law_builder, law, axis, created):
    types = tx.nx.GeometricUtilities.LawBuilder.Type
    if not isinstance(law, dict):
        law_builder.LawType = types.Constant
        expr.drive(law_builder.Value, 0.0 if law is None else law)
        return
    if "formula" in law:
        _parameter(tx, created)
        name = _free_name(tx, f"{axis}t")
        function = _formula(tx, name, law["formula"], axis)
        created.append(name)
        law_builder.LawType = types.ByEquation
        law_builder.Parameter = PARAMETER
        law_builder.Function = str(getattr(function, "Name", name))
        return
    law_builder.LawType = types.Cubic if law.get("cubic") else types.Linear
    expr.drive(law_builder.StartValue, law["start"])
    expr.drive(law_builder.EndValue, law["end"])


@tool(
    effect="model",
    label="Create law curve",
    summary="Law curve x {x}, y {y}, z {z}",
    keywords=(
        "law curve",
        "curve by equation",
        "parametric curve",
        "helix by formula",
        "x(t) y(t) z(t)",
        "linear cubic law",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"x": {"start": 0, "end": 100}, "y": {"formula": "20*sin(360*t)"}, "z": 5},
)
def nx_create_law_curve(
    tx: Tx,
    *,
    x: Law | None = None,
    y: Law | None = None,
    z: Law | None = None,
) -> LawData:
    """Create a spline whose absolute x, y and z each follow a law of t from 0 to 1. A law
    is a number (constant), {start, end} (linear; cubic: true for flat ends) or {formula}
    in t, part units and degrees ('20*sin(360*t)'; creates expressions t and xt/yt/zt,
    listed in expressions). At least one law must vary. Returns the curve, its ends and
    the expected ends."""
    laws = {"x": x, "y": y, "z": z}
    if not any(_varies(v) for v in laws.values()):
        ramp = next((a for a, v in laws.items() if isinstance(v, dict)), None)
        fail(
            "NX_INVALID_ARGUMENT",
            f"{ramp}: start equals end, a constant law; constant x, y and z make a point"
            if ramp
            else "Constant x, y and z make a point: give {start, end} or {formula} for one",
            field=f"/{ramp or 'x'}",
            hint="Give one law different start and end values, or a formula of t",
        )
    cc.licensed(tx)
    created: list[str] = []
    with builders.build(tx, tx.part.Features.CreateLawCurveBuilder, None) as b:
        b.DistanceTolerance = units.part_tolerance(tx)
        for axis, law in laws.items():
            _set_law(tx, getattr(b, axis.upper() + "Law"), law, axis, created)
        feature = b.commit()
    curves = cc.output_curves(tx, feature)
    data = cc.describe(tx, feature, curves)
    if data["length"] <= cc.gap_tolerance(tx):
        formulas = [a for a, v in laws.items() if isinstance(v, dict) and "formula" in v]
        fail(
            "NX_INVALID_ARGUMENT",
            "The laws make a point (length 0): a formula does not vary with t",
            field=f"/{formulas[0]}/formula" if formulas else "/x",
            hint="Use t in the formula (t runs from 0 to 1), e.g. '100*t'",
            details={"length": data["length"]},
        )
    first, last = data["curves"][0]["start"], data["curves"][-1]["end"]
    want0 = [_value(laws[a], False) for a in AXES]
    want1 = [_value(laws[a], True) for a in AXES]
    gaps = [
        abs(got[i] - want[i])
        for got, want in ((first, want0), (last, want1))
        for i in range(3)
        if want[i] is not None
    ]
    gap = max(gaps, default=0.0)
    readback.expect(
        gap <= cc.gap_tolerance(tx, first, last), "ends", "The curve ends off its laws", gap=gap
    )
    return {
        **data,
        "expected_start": want0,
        "expected_end": want1,
        "gap": gap,
        "expressions": created,
    }


# ---------------------------------------------------------------------------
# General conic

Kind = Literal["ellipse", "parabola", "hyperbola", "conic"]
KINDS: dict[str, Kind] = {"Ellipse": "ellipse", "Parabola": "parabola", "Hyperbola": "hyperbola"}


class ConicData(cc.CurvesData):
    kind: Kind  # NX's curve type (conic: another curve type)
    gap: float  # largest distance of a defining point (rho: ends and shoulder) from the curve


def _kind(obj) -> Kind:
    for cls in type(obj).__mro__:
        if cls.__name__ in KINDS:
            return KINDS[cls.__name__]
    return "conic"


def _distinct(points, field):
    for i in range(len(points)):
        for j in range(i):
            if geom.dist(points[i], points[j]) <= geom.tol(points[i], points[j]):
                fail("NX_INVALID_ARGUMENT", "Two points coincide", field=f"{field}/{i}")


def _conic_plane(points, field):
    """``(origin, unit normal)`` of the plane through ``points`` (all must lie on it)."""
    a = points[0]
    far = max(points, key=lambda p: geom.dist(p, a))
    third = max(points, key=lambda p: geom.norm(geom.cross(geom.sub(far, a), geom.sub(p, a))))
    try:
        normal = geom.plane_through([a, far, third], field)
    except NXError as error:
        error.message = (
            "The five points are collinear: no conic passes through them"
            if len(points) == 5
            else "start, end and anchor are collinear: put the anchor off the start-end line"
        )
        raise
    size = max(geom.dist(p, a) for p in points)
    if max(abs(geom.dot(geom.sub(p, a), normal)) for p in points) > 1e-6 * max(size, 1.0):
        fail("NX_INVALID_ARGUMENT", "The points are not in one plane", field=field)
    return a, normal


ORDER_HINT = "Order the points along one convex arc: start, three inside in order, end"


def _convex(points, normal, field):
    """Refuse five points that do not run round one convex arc in order (a conic arc and
    its chord bound a convex region, so the closed row turns one way at every point)."""
    turns = []
    for i in range(len(points)):
        a, b, c = points[i - 1], points[i], points[(i + 1) % len(points)]
        turn = geom.dot(geom.cross(geom.sub(b, a), geom.sub(c, b)), normal)
        size = geom.dist(a, b) * geom.dist(b, c)
        turns.append(0.0 if abs(turn) <= 1e-9 * size else turn)
    if any(t > 0 for t in turns) and any(t < 0 for t in turns):
        major = 1.0 if sum(t > 0 for t in turns) >= sum(t < 0 for t in turns) else -1.0
        bad = next(i for i, t in enumerate(turns) if t * major < 0)
        fail("NX_INVALID_ARGUMENT", f"No conic arc passes through these points in order: the "
             f"row turns the other way at point {bad}", field=f"{field}/{bad}",
             hint=ORDER_HINT)  # fmt: skip


@tool(
    effect="model",
    label="Create general conic",
    summary={
        "five_points": "Conic through {points}",
        "rho": "Conic from {start} to {end}, anchor {anchor}, rho {rho}",
    },
    keywords=(
        "general conic",
        "ellipse",
        "parabola",
        "hyperbola",
        "rho conic",
        "deck camber",
        "conic section",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    variants="method",
    required_by={"five_points": ("points",), "rho": ("start", "end", "anchor", "rho")},
    cost="heavy",
    previewable=True,
    example={
        "method": "rho",
        "start": [0, 0, 0],
        "end": [100, 0, 0],
        "anchor": [50, 20, 0],
        "rho": 0.5,
    },  # fmt: skip
)
def nx_create_general_conic(
    tx: Tx,
    *,
    method: Literal["five_points", "rho"],
    points: Annotated[
        list[Point3], Items(5, 5), Doc("five_points: start, three inside in order, end; coplanar")
    ]
    | None = None,
    start: Annotated[Point3, Doc("rho: first end")] | None = None,
    end: Annotated[Point3, Doc("rho: second end")] | None = None,
    anchor: Annotated[Point3, Doc("rho: where the end tangents meet")] | None = None,
    rho: Annotated[float, Range(gt=0, lt=1), Doc("rho: <0.5 ellipse, 0.5 parabola, >0.5 hyperbola")]
    | None = None,
) -> ConicData:
    """Create an associative conic arc in the plane of its points. five_points: through 5
    coplanar points in order. rho: from start to end, tangent there to the lines to
    anchor, through the shoulder M + rho (anchor - M), M the chord middle (rho 0.5: a
    parabola, e.g. deck camber with the anchor at twice the camber height). Returns the
    curve, its kind and ends."""
    with tx.checks():
        if method == "five_points":
            pts = [args.vec3(p) for p in args.required(points, "points")]
            _distinct(pts, "/points")
            origin, normal = _conic_plane(pts, "/points")
            _convex(pts, normal, "/points")
        else:
            args.need(start=start, end=end, anchor=anchor, rho=rho)
            pts = [args.vec3(p) for p in (start, end, anchor)]
            _distinct(pts, "/anchor")
            origin, normal = _conic_plane(pts, "/anchor")
    cc.licensed(tx)
    G = tx.nx.Features.GeneralConicBuilder
    with builders.build(tx, tx.part.Features.CreateGeneralConicBuilder, None) as b:
        b.AssociativeToggle = True
        b.DrawingPlaneOption = G.DrawingPlaneOptions.General
        b.GeneralDrawingPlane = cc.plane_object(tx, origin, normal)
        b.StartPoint = builders.point(tx, pts[0])
        if method == "five_points":
            b.Type = G.Types.FivePoints
            b.InteriorPoint1 = builders.point(tx, pts[1])
            b.InteriorPoint2 = builders.point(tx, pts[2])
            b.InteriorPoint3 = builders.point(tx, pts[3])
            b.EndPoint = builders.point(tx, pts[4])
        else:
            b.Type = G.Types.TwoPointsAnchorRho
            b.EndPoint = builders.point(tx, pts[1])
            b.AnchorPoint = builders.point(tx, pts[2])
            expr.drive(b.Rho, float(rho))  # type: ignore[arg-type]
        feature = b.commit()
    curves = cc.output_curves(tx, feature)
    if method == "five_points":
        through = pts
    else:
        middle = geom.scale(geom.add(pts[0], pts[1]), 0.5)
        shoulder = geom.add(middle, geom.scale(geom.sub(pts[2], middle), float(rho)))  # type: ignore[arg-type]
        through = [pts[0], pts[1], shoulder]
    gap = max(min(curve.point_distance(tx, c, p)[0] for c in curves) for p in through)
    if method == "five_points" and gap > cc.gap_tolerance(tx, *through):
        fail("NX_INVALID_ARGUMENT", f"No single conic arc passes through these five points in "
             f"order (NX's arc misses one by {gap:.4g})", field="/points", hint=ORDER_HINT,
             details={"gap": gap})  # fmt: skip
    readback.expect(
        gap <= cc.gap_tolerance(tx, *through), "points", "The conic misses a point", gap=gap
    )
    return {**cc.describe(tx, feature, curves), "kind": _kind(curves[0]), "gap": gap}
