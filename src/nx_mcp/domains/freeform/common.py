"""Result shapes and small checks shared by the freeform tools (this domain only)."""

from __future__ import annotations

from typing import TypedDict

from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.types import Point3
from nx_mcp.kit import curve, licenses, measure, readback, result, select, units

# At most this many bodies are measured for a failure diagnostic.
MAX_DIAGNOSED = 20


class Bounds(TypedDict):
    min: Point3
    max: Point3


class SheetData(TypedDict):
    feature: result.Row
    bodies: list[result.Row]
    bounds: Bounds
    sheet: bool  # every result body is a sheet (false: a solid)
    faces: int
    area: float  # total face area, part units^2


def is_sheet(body) -> bool:
    flag = getattr(body, "IsSheetBody", None)
    return bool(flag) if flag is not None else not getattr(body, "IsSolidBody", True)


def sheet(tx, ref, field="/body"):
    """A sheet body of the work part (a solid is ``NX_WRONG_GEOMETRY``)."""
    body = select.one(tx, ref, "body", field=field)
    if not is_sheet(body):
        fail("NX_WRONG_GEOMETRY", "Select a sheet body, not a solid", field=field)
    return body


def sheets(tx, refs, field):
    out = select.bodies(tx, refs, field)
    for i, body in enumerate(out):
        if not is_sheet(body):
            fail("NX_WRONG_GEOMETRY", "Select sheet bodies, not solids", field=f"{field}/{i}")
    return out


def feature_bodies(feature):
    return list(feature.GetBodies()) if hasattr(feature, "GetBodies") else []


def bounds(tx, bodies) -> Bounds:
    low, high = measure.bounds(tx, list(bodies), exact=True)
    return {"min": low, "max": high}


def result_of(tx, feature, bodies=None) -> SheetData:
    """``{feature, bodies, bounds, sheet, faces, area}`` of a committed feature (NX must
    have built a body)."""
    bodies = feature_bodies(feature) if bodies is None else list(bodies)
    readback.expect(bool(bodies), "bodies", "NX built no body")
    faces = [f for b in bodies for f in b.GetFaces()]
    return {
        "feature": tx.ref(feature, kind="feature"),
        "bodies": [tx.ref(b, kind="body") for b in bodies],
        "bounds": bounds(tx, bodies),
        "sheet": all(is_sheet(b) for b in bodies),
        "faces": len(faces),
        "area": measure.area(tx, faces) if faces else 0.0,
    }


def given_tolerance(tx, value, field="/tolerance"):
    """A positive modeling tolerance in part units (0.001 mm in part units when omitted)."""
    if value is None:
        return units.part_tolerance(tx)
    if not value > 0:
        fail("NX_INVALID_ARGUMENT", "tolerance must be positive", field=field)
    return float(value)


def gap(tx, a, b) -> float | None:
    """Minimum distance between two NX objects, None when NX cannot measure it (a
    diagnostic only: it never fails the call)."""
    try:
        return curve.min_distance(tx, a, b)[0]
    except NXError:
        return None


def point_gap(tx, obj, point) -> float | None:
    """Distance from a point to an NX object, None when NX cannot measure it."""
    try:
        return curve.point_distance(tx, obj, point)[0]
    except NXError:
        return None


FREEFORM_LICENSE = "nx_freeform_1"  # basic freeform: offset surface, N-sided, ruled


def licensed(tx, feature: str = FREEFORM_LICENSE) -> None:
    """``NX_LICENSE_UNAVAILABLE`` before any builder when the seat lacks ``feature``
    (``kit.licenses``; NX cannot tell: the builder decides)."""
    licenses.require(tx, (feature,), f"This NX seat has no {feature} license for this command")


def explain(error: NXError, hint: str, **details) -> NXError:
    """Add a hint and diagnostic details to a native refusal (keeps nx_code)."""
    error.hint = hint
    error.details = {**(error.details or {}), **details}
    return error
