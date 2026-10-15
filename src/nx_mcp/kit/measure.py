"""Mass properties and bounds of bodies, in work-part units (no NX import).

    m = measure.mass(tx, [body])          # {"volume", "area", "centroid"}
    s = measure.solid_props(tx, body)     # {"volume", "area", "mass"}: one UF call
    v = measure.volume(tx, body)          # part units^3
    low, high = measure.bounds(tx, bodies)  # absolute axis-aligned box
    n = measure.length(tx, curves)        # arc length, part units

NX measures in millimetres here (``MeasureBodies`` with ``KilogramMillimeter``);
values are converted to the work part's units, so an inch part reports in^3,
in^2 and inches. Bounds come from UF
``ModlGeneral.AskBoundingBox`` (absolute coordinates, part units).
"""

from __future__ import annotations

import math

from nx_mcp.core.errors import fail, native
from nx_mcp.kit import curve, geom, units

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


def bounds(tx, objects, exact=False):
    """``(low, high)`` absolute axis-aligned bounds of bodies or faces, part units.

    ``AskBoundingBox`` is conservative (a curved sheet's box can exceed the surface).
    ``exact`` uses ``AskBoundingBoxExact``, which is aligned to the work CSYS: its eight
    corners are projected to an absolute box (exact while the WCS is absolute).
    """
    objects = objects if isinstance(objects, (list, tuple)) else [objects]
    if not objects:
        fail("NX_INVALID_ARGUMENT", "No objects to bound")
    low, high = [float("inf")] * 3, [float("-inf")] * 3
    with native(default="NX_ERROR"):
        for obj in objects:
            box = _exact_box(tx, obj) if exact else tx.uf.ModlGeneral.AskBoundingBox(obj.Tag)
            box = [float(v) for v in box]
            low = [min(a, b) for a, b in zip(low, box[:3], strict=True)]
            high = [max(a, b) for a, b in zip(high, box[3:6], strict=True)]
    return low, high


def _exact_box(tx, obj):
    corner, directions, lengths = tx.uf.ModlGeneral.AskBoundingBoxExact(obj.Tag, 0)
    directions = [float(v) for v in directions] if len(directions) == 9 else directions
    if len(directions) == 9:
        directions = [directions[i : i + 3] for i in range(0, 9, 3)]
    points = [
        [
            float(corner[i])
            + sum(float(directions[j][i]) * float(lengths[j]) * ((mask >> j) & 1) for j in range(3))
            for i in range(3)
        ]
        for mask in range(8)
    ]
    return [min(p[i] for p in points) for i in range(3)] + [
        max(p[i] for p in points) for i in range(3)
    ]


def area(tx, faces):
    """Total area of faces in part units^2 (``NewFaceProperties`` in mm^2, then converted)."""
    faces = list(faces)
    if not faces:
        fail("NX_INVALID_ARGUMENT", "No faces to measure")
    part = tx.part
    k = _scale(tx)
    with native(default="NX_ERROR"):
        found = [part.UnitCollection.FindObject(n) for n in ("SquareMilliMeter", "MilliMeter")]
        props = part.MeasureManager.NewFaceProperties(found[0], found[1], ACCURACY, faces)
    try:
        with native(default="NX_ERROR"):
            return float(props.Area) * (k * _shown_units_slip(tx)) ** 2
    finally:
        props.Dispose()


def _shown_units_slip(tx) -> float:
    """``NewFaceProperties`` of a work part that is not displayed: NX 2406 answers in the
    work part's units but labels them the displayed part's (an inch part worked quietly
    beside a displayed mm part read 6 mm^2 for 6 in^2, live 2026-10-12). The factor that
    turns such a reading into the requested units; 1 when the part is displayed."""
    shown = getattr(getattr(tx.session, "Parts", None), "Display", None)
    part = tx.part
    if shown is None or part is None or int(shown.Tag) == int(part.Tag):
        return 1.0
    try:
        return units.MM_PER[units.part_units(tx)] / units.MM_PER[units.part_units(tx, shown)]
    except Exception:  # an unknown display: keep NX's reading
        return 1.0


# ---------------------------------------------------------------------------
# WCS and mass/inertia (UF AskMassProps3d)

UF_SOLID = 1  # AskMassProps3d: solid bodies
UF_KG_M = 4  # AskMassProps3d units: kilograms and metres
M_PER = {"mm": 1e-3, "in": 0.0254}


LENGTH_SEGMENTS = 16  # chords of the sampled length that checks a GetLength reading
_SLIPS = (1.0, 25.4, 1.0 / 25.4)


def length(tx, curves):
    """Total arc length of one curve or edge, or a list of them, in part units.

    NX 2406 ``GetLength`` answers millimetres, but its units follow hidden session state:
    a 2 in line of a quietly worked inch part beside a displayed mm part read 50.8 (mm)
    in one session and 2 in another, and 2 while the mm part was the work part (live
    2026-10-13; UF ``AskArcLength`` slips the same way). UF ``Eval`` points are always in
    the part's units, so each reading is converted as millimetres, then checked against
    the curve's sampled chord length: when it is off by the inch factor (25.4 either
    way), the reading in the other units is taken. Exact lengths stay NX's.
    """
    if not isinstance(curves, (list, tuple)):
        curves = [curves]
    k = _scale(tx)
    total = 0.0
    for c in curves:
        with native(default="NX_ERROR"):
            reading = float(c.GetLength()) * k
        total += _checked_length(tx, c, reading)
    return total


def _checked_length(tx, obj, reading: float) -> float:
    """``reading`` (part units), or the inch-factor slip of it that agrees with the
    length sampled through UF ``Eval``; ``reading`` when the curve cannot be sampled."""
    try:
        with curve.evaluator(tx, obj) as e:
            points = [e.at(i / LENGTH_SEGMENTS)[0] for i in range(LENGTH_SEGMENTS + 1)]
    except Exception:  # not evaluable: keep NX's millimetres
        return reading
    sampled = sum(geom.dist(a, b) for a, b in zip(points, points[1:], strict=False))
    if not (sampled > 0 and reading > 0):
        return reading
    return min((reading * f for f in _SLIPS), key=lambda v: abs(math.log(v / sampled)))


def wcs(tx):
    """``(origin, R)`` of the WCS: ``p_abs = origin + R p_wcs`` (part units).

    The WCS is the displayed part's: UF reports in it, and NX 2406 refuses ``WCS`` of a
    part that is not displayed ("only be performed on a displayed part"), as for a read
    of a background part (gate 4s)."""
    shown = getattr(getattr(tx.session, "Parts", None), "Display", None)
    system = (shown if shown is not None else tx.part).WCS.CoordinateSystem
    return geom.xyz(system.Origin), geom.rows(system.Orientation.Element)


def solid_props(tx, body, accuracy=ACCURACY):
    """``{"volume", "area", "mass"}`` of one solid body (part units, kg) from one UF
    ``AskMassProps3d`` (kg and metres, converted). It reads the same values as ``mass``
    (``NewMassProperties``) in a fraction of the time (NX 2406: about 1.7 against 4.5 ms
    for a block, far more for a complex body), so tools measuring every body of a part use it."""
    with native(default="NX_ERROR"):
        values = tx.uf.Modeling.AskMassProps3d(
            [body.Tag], 1, UF_SOLID, UF_KG_M, 0.0, 1, [float(accuracy)] + [0.0] * 10
        )[0]
    to_part = 1.0 / M_PER[units.part_units(tx)]
    return {
        "volume": float(values[1]) * to_part**3,
        "area": float(values[0]) * to_part**2,
        "mass": float(values[2]),
    }


def wcs_is_absolute(tx, tol=1e-8):
    """True when the WCS axes are the absolute axes (its origin may be anywhere)."""
    _origin, r = wcs(tx)
    return all(abs(r[i][j] - geom.IDENTITY[i][j]) <= tol for i in range(3) for j in range(3))


def inertia(tx, bodies, accuracy=ACCURACY):
    """``{"mass", "volume", "center", "inertia", "principal"}`` of solid bodies with their
    densities.

    ``mass`` is kg, ``volume`` part units^3, ``center`` the centre of gravity (absolute, part units), ``inertia``
    the 3x3 tensor about the centre of gravity in absolute axes and ``principal`` the
    principal moments, both kg * (part unit)^2. UF reports kg and metres in WCS axes
    (centroidal moments at 12-14, principal moments at 31-33, centroidal products of
    inertia sum(m*y*z), sum(m*x*z), sum(m*x*y) at 19, 20, 21: NX 2406, checked against the
    parallel-axis tensor of two offset blocks). The tensor's off-diagonal terms are the
    negated products (``Ixy = -sum(m*x*y)``), so its eigenvalues are the principal moments.
    """
    bodies = list(bodies)
    if not bodies:
        fail("NX_INVALID_ARGUMENT", "No bodies to measure")
    with native(default="NX_ERROR"):
        values = tx.uf.Modeling.AskMassProps3d(
            [b.Tag for b in bodies],
            len(bodies),
            UF_SOLID,
            UF_KG_M,
            0.0,
            1,
            [float(accuracy)] + [0.0] * 10,
        )[0]
    m = [float(v) for v in values]
    to_part = 1.0 / M_PER[units.part_units(tx)]
    origin, r = wcs(tx)
    center_wcs = geom.scale(m[3:6], to_part)
    center = geom.add(origin, geom.matvec(r, center_wcs))
    yz, xz, xy = m[19], m[20], m[21]
    tensor_wcs = [
        [m[12], -xy, -xz],
        [-xy, m[13], -yz],
        [-xz, -yz, m[14]],
    ]
    k2 = to_part**2
    tensor = geom.matmul(geom.matmul(r, tensor_wcs), geom.transpose(r))
    return {
        "mass": m[2],
        "volume": m[1] * to_part**3,
        "center": center,
        "inertia": [[v * k2 for v in row] for row in tensor],
        "principal": [v * k2 for v in m[31:34]],
    }
