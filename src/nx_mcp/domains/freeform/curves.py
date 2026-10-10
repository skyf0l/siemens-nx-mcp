"""Studio Spline: create through points or by poles, edit through ``nx_edit_feature``.

NX 2406 lessons kept here: a Studio Spline is ``CreateStudioSplineBuilderEx``
with no plane constraint (a free 3D curve); its feature type is ``SPLINE``
(class ``StudioSpline``). Handing another feature type to the Studio Spline
builder crashed NX 2406 natively (memory access violation), so the edit refuses
anything that is not a ``StudioSpline`` before the builder is created. A native
refusal (coincident points, impossible degree) is ``NX_BUILD_FAILED`` with
``nx_code``. An omitted degree is 3, capped at points - 1 so that two or three
points still make a spline (degree 1 or 2).
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import definition, tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Point3, Range
from nx_mcp.kit import builders, curve, geom, measure, readback, result, units

METHODS = ("through_points", "poles")
DEFAULT_DEGREE = 3


class SplineData(TypedDict):
    feature: result.Row
    curve: result.Row
    degree: int
    periodic: bool
    start: Point3
    end: Point3
    length: float


class _Shape(TypedDict):  # what _verify reads back
    degree: int
    periodic: bool
    start: Point3
    end: Point3


def _points(values, field):
    points = [[float(c) for c in p] for p in values]
    for i in range(1, len(points)):
        if geom.dist(points[i], points[i - 1]) <= geom.tol(points[i], points[i - 1]):
            fail("NX_INVALID_ARGUMENT", "Consecutive points coincide", field=f"{field}/{i}")
    return points


def _degree(points, degree):
    """The given degree, or the default 3 capped at points - 1."""
    return min(DEFAULT_DEGREE, len(points) - 1) if degree is None else int(degree)


def _check_degree(points, degree, field):
    if len(points) <= degree:
        fail(
            "NX_INVALID_ARGUMENT",
            f"A degree {degree} spline needs at least {degree + 1} points, got {len(points)}",
            field=field,
            hint=f"Send degree {len(points) - 1} or lower (or omit it), or add points",
        )


def _clean(p):
    """A point with round-off (|v| < 1e-9, e.g. -6e-16) snapped to 0."""
    return [0.0 if abs(v) < 1e-9 else v for v in p]


def _is_studio_spline(tx, feature):
    cls = getattr(tx.nx.Features, "StudioSpline", None)
    if isinstance(cls, type):
        return isinstance(feature, cls)
    return type(feature).__name__ == "StudioSpline"


def _configure(tx, b, points, method, degree, periodic):
    types = tx.nx.Features.StudioSplineBuilderEx.Types
    b.IsAssociative = True
    b.HasPlaneConstraint = False
    b.Degree = int(degree)
    b.IsPeriodic = bool(periodic)
    b.Type = types.ThroughPoints if method == "through_points" else types.ByPoles
    manager = b.ConstraintManager
    manager.Clear()
    for p in points:
        data = manager.CreateGeometricConstraintData()
        data.Point = builders.point(tx, p)
        manager.Append(data)


def _verify(tx, spline, points, method, degree, periodic) -> _Shape:
    """Read back degree, periodicity and the defining points or poles."""
    got = int(spline.Order) - 1
    readback.expect(got == degree, "degree", f"NX built degree {got}", degree=got)
    readback.expect(
        bool(spline.Periodic) == bool(periodic), "periodic", periodic=bool(spline.Periodic)
    )
    tol = max(units.part_tolerance(tx), geom.tol(*points))
    if method == "through_points":
        with curve.evaluator(tx, spline) as e:
            for i, p in enumerate(points):
                gap = geom.dist(e.closest(p)[0], p)
                readback.expect(gap <= tol, f"point[{i}]", "The spline misses a point", gap=gap)
    else:
        poles = [geom.xyz(q) for q in spline.Get3DPoles()]
        if not periodic:
            readback.expect(len(poles) == len(points), "poles", poles=len(poles))
        for i, p in enumerate(points):
            near = poles[i : i + 1] if not periodic else poles
            readback.expect(
                any(geom.dist(q, p) <= tol for q in near), f"pole[{i}]", "A pole moved", pole=p
            )
    start, end = curve.ends(tx, spline)
    return {
        "degree": got,
        "periodic": bool(spline.Periodic),
        "start": _clean(start),
        "end": _clean(end),
    }


@tool(
    effect="model",
    label="Create spline",
    summary={
        "through_points": "Spline through {points}",
        "poles": "Spline with {points} as poles",
    },
    keywords=("spline", "studio spline", "3d curve", "through points", "control poles", "nurbs"),
    requires=("work_part", "no_active_sketch"),
    variants="method",
    cost="heavy",
    previewable=True,
    example={"points": [[0, 0, 0], [10, 5, 0], [20, 0, 5]], "method": "through_points"},
)
def nx_create_spline(
    tx: Tx,
    *,
    points: Annotated[list[Point3], Items(2, 1000)],
    method: Literal["through_points", "poles"] = "through_points",
    degree: Annotated[int, Range(ge=1, le=7), Doc("Default min(3, points-1)")] | None = None,
    periodic: Annotated[bool, Doc("Closed curve")] = False,
) -> SplineData:
    """Create a 3D Studio Spline through the points or with them as control poles. Edit
    with nx_edit_feature definition {spline: {points, method, degree, periodic}}."""
    values = _points(points, "/points")
    degree = _degree(values, degree)
    _check_degree(values, degree, "/degree")
    with builders.build(tx, tx.part.Features.CreateStudioSplineBuilderEx, None) as b:
        _configure(tx, b, values, method, degree, periodic)
        feature = b.commit(validate=True)
        spline = b.Curve
    out = _verify(tx, spline, values, method, degree, periodic)
    return {
        "feature": tx.ref(feature, kind="feature"),
        "curve": tx.ref(spline, kind="curve"),
        **out,
        "length": measure.length(tx, spline),
    }


SPLINE_KEYS = frozenset({"points", "method", "degree", "periodic"})


def _definition(value):
    """Validate ``{"spline": {points, method?, degree?, periodic?}}`` (the edit schema)."""
    field = "/definition/spline"
    spec = value.get("spline") if isinstance(value, dict) else None
    if not isinstance(spec, dict) or "points" not in spec or set(spec) - SPLINE_KEYS:
        fail(
            "NX_INVALID_ARGUMENT",
            "A Studio Spline takes {spline: {points, method?, degree?, periodic?}}",
            field="/definition",
        )
    raw = spec["points"]
    if (
        not isinstance(raw, (list, tuple))
        or not 2 <= len(raw) <= 1000
        or not all(
            isinstance(p, (list, tuple))
            and len(p) == 3
            and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in p)
            for p in raw
        )
    ):
        fail("NX_INVALID_ARGUMENT", "points: 2-1000 [x, y, z] points", field=field + "/points")
    method = spec.get("method")
    if method is not None and method not in METHODS:
        fail("NX_INVALID_ARGUMENT", "method: through_points or poles", field=field + "/method")
    degree = spec.get("degree")
    if degree is not None and (type(degree) is not int or not 1 <= degree <= 7):
        fail("NX_INVALID_ARGUMENT", "degree: an integer 1-7", field=field + "/degree")
    periodic = spec.get("periodic")
    if periodic is not None and not isinstance(periodic, bool):
        fail("NX_INVALID_ARGUMENT", "periodic: true or false", field=field + "/periodic")
    return _points(raw, field + "/points"), method, degree, periodic


@definition("SPLINE")
def edit_spline(tx, feature, value):
    """``nx_edit_feature(definition={"spline": {...}})``: replace a Studio Spline's points;
    omitted method, degree and periodic keep their current values."""
    with tx.checks():
        if not _is_studio_spline(tx, feature):
            # NX 2406 crashes natively when the Studio Spline builder edits another type.
            fail(
                "NX_WRONG_KIND", "Only a Studio Spline takes a spline definition", field="/feature"
            )
        points, method, degree, periodic = _definition(value)
    types = tx.nx.Features.StudioSplineBuilderEx.Types
    with builders.build(tx, tx.part.Features.CreateStudioSplineBuilderEx, feature) as b:
        if method is None:
            method = "poles" if b.Type == types.ByPoles else "through_points"
        degree = int(b.Degree) if degree is None else degree
        periodic = bool(b.IsPeriodic) if periodic is None else periodic
        _check_degree(points, degree, "/definition/spline/degree")
        _configure(tx, b, points, method, degree, periodic)
        b.commit(validate=True)
        spline = b.Curve
    _verify(tx, spline, points, method, degree, periodic)
