"""Mass properties and bounds of bodies, in work-part units (no NX import).

    m = measure.mass(tx, [body])          # {"volume", "area", "centroid"}
    v = measure.volume(tx, body)          # part units^3
    low, high = measure.bounds(tx, bodies)  # absolute axis-aligned box

NX measures in millimetres here (``MeasureBodies`` with ``KilogramMillimeter``,
as the NX 2406 legacy tools did); values are converted to the work part's
units, so an inch part reports in^3, in^2 and inches. Bounds come from UF
``ModlGeneral.AskBoundingBox`` (absolute coordinates, part units).
"""

from __future__ import annotations

from nx_mcp.core.errors import fail, native
from nx_mcp.kit import geom, units

# Units handed to NewMassProperties: area, volume, mass, length, force.
MM_UNITS = ("SquareMilliMeter", "CubicMilliMeter", "Kilogram", "MilliMeter", "Newton")
ACCURACY = 0.999


def _scale(tx):
    """Part units per millimetre."""
    return 1.0 / units.MM_PER[units.part_units(tx)]


def mass(tx, bodies, accuracy=ACCURACY):
    """``{"volume", "area", "centroid"}`` of solid bodies, in work-part units.

    ``area`` and ``centroid`` are None where NX does not report them.
    """
    bodies = list(bodies)
    if not bodies:
        fail("NX_INVALID_ARGUMENT", "No bodies to measure")
    part = tx.part
    k = _scale(tx)
    with native(default="NX_ERROR"):
        found = [part.UnitCollection.FindObject(name) for name in MM_UNITS]
        props = part.MeasureManager.NewMassProperties(found, accuracy, bodies)
    try:
        with native(default="NX_ERROR"):
            analysis = getattr(getattr(tx.nx, "MeasureBodies", None), "AnalysisUnit", None)
            if analysis is not None:
                props.InformationUnit = analysis.KilogramMillimeter
            volume = float(props.Volume) * k**3
            area = getattr(props, "Area", None)
            centroid = getattr(props, "Centroid", None)
            return {
                "volume": volume,
                "area": None if area is None else float(area) * k**2,
                "centroid": None if centroid is None else geom.scale(geom.xyz(centroid), k),
            }
    finally:
        props.Dispose()


def volume(tx, bodies):
    """Total volume of one body or a list of bodies, in part units^3."""
    if not isinstance(bodies, (list, tuple)):
        bodies = [bodies]
    return mass(tx, bodies)["volume"]


def bounds(tx, objects):
    """``(low, high)`` absolute axis-aligned bounds of bodies or faces, part units."""
    objects = objects if isinstance(objects, (list, tuple)) else [objects]
    if not objects:
        fail("NX_INVALID_ARGUMENT", "No objects to bound")
    low, high = [float("inf")] * 3, [float("-inf")] * 3
    with native(default="NX_ERROR"):
        for obj in objects:
            box = [float(v) for v in tx.uf.ModlGeneral.AskBoundingBox(obj.Tag)]
            low = [min(a, b) for a, b in zip(low, box[:3], strict=True)]
            high = [max(a, b) for a, b in zip(high, box[3:6], strict=True)]
    return low, high
