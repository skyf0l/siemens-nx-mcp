"""Result shapes and small checks shared by the freeform tools (this domain only)."""

from __future__ import annotations

from typing import TypedDict

from nx_mcp.core.errors import fail
from nx_mcp.core.types import Point3
from nx_mcp.kit import measure, readback, result, select, units


class Bounds(TypedDict):
    min: Point3
    max: Point3


class SheetData(TypedDict):
    feature: result.Row
    bodies: list[result.Row]
    bounds: Bounds


def is_sheet(body) -> bool:
    flag = getattr(body, "IsSheetBody", None)
    return bool(flag) if flag is not None else not getattr(body, "IsSolidBody", True)


def sheet(tx, ref, field="/body"):
    """A sheet body of the work part (a solid is ``NX_WRONG_GEOMETRY``)."""
    body = select.one(tx, ref, "body", field=field)
    if not is_sheet(body):
        fail("NX_WRONG_GEOMETRY", "Select a sheet body", field=field)
    return body


def sheets(tx, refs, field):
    out = select.bodies(tx, refs, field)
    for i, body in enumerate(out):
        if not is_sheet(body):
            fail("NX_WRONG_GEOMETRY", "Select sheet bodies", field=f"{field}/{i}")
    return out


def feature_bodies(feature):
    return list(feature.GetBodies()) if hasattr(feature, "GetBodies") else []


def bounds(tx, bodies) -> Bounds:
    low, high = measure.bounds(tx, list(bodies), exact=True)
    return {"min": low, "max": high}


def result_of(tx, feature, bodies=None) -> SheetData:
    """``{feature, bodies, bounds}`` of a committed feature (NX must have built a body)."""
    bodies = feature_bodies(feature) if bodies is None else list(bodies)
    readback.expect(bool(bodies), "bodies", "NX built no body")
    return {
        "feature": tx.ref(feature, kind="feature"),
        "bodies": [tx.ref(b, kind="body") for b in bodies],
        "bounds": bounds(tx, bodies),
    }


def given_tolerance(tx, value, field="/tolerance"):
    """A positive modeling tolerance in part units (0.001 mm in part units when omitted)."""
    if value is None:
        return units.part_tolerance(tx)
    if not value > 0:
        fail("NX_INVALID_ARGUMENT", "tolerance must be positive", field=field)
    return float(value)
