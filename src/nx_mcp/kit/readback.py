"""Read-back checks: NX built what was asked, or ``verification_failed``.

Each check raises ``NX_VERIFICATION_FAILED`` with ``details.check`` naming
the failed read-back, so the kernel rolls the change back. Tolerances scale
with the magnitudes involved (``geom.tol``) unless one is given.
"""

from __future__ import annotations

import math

from nx_mcp.core.errors import fail
from nx_mcp.kit import geom


def _fail(check, message, **details):
    fail("NX_VERIFICATION_FAILED", message, details={"check": check, **details})


def expect(ok, check, message=None, **actual):
    """Fail unless ``ok``; ``actual`` values go into ``details``."""
    if not ok:
        _fail(check, message or f"NX read-back failed: {check}", actual=actual)


def _vector(value):
    return [float(v) for v in value] if isinstance(value, (list, tuple)) else None


def _unit(v, check):
    values = [float(x) for x in v]
    length = geom.norm(values)
    if not math.isfinite(length) or length < 1e-12:
        _fail(check, f"{check}: NX returned a zero or invalid direction", actual=values)
    return [x / length for x in values]


def expect_close(actual, wanted, check, tol=None):
    """Scalar or vector ``actual`` equals ``wanted`` within ``tol`` (default scaled)."""
    a, w = _vector(actual), _vector(wanted)
    if (a is None) != (w is None) or (a is not None and len(a) != len(w)):
        _fail(check, f"{check}: read-back shape differs", actual=actual, wanted=wanted)
    if a is None:
        a, w = [float(actual)], [float(wanted)]
    limit = geom.tol(a, w) if tol is None else tol
    error = max((abs(x - y) for x, y in zip(a, w, strict=True)), default=0.0)
    if not error <= limit:  # NaN fails too
        _fail(
            check,
            f"{check}: NX gave {actual}, expected {wanted}",
            actual=actual,
            wanted=wanted,
            error=error,
            tolerance=limit,
        )
    return actual


def expect_same_direction(actual, wanted, check, tol=1e-6):
    """Unit vectors are parallel and point the same way."""
    a, w = _unit(actual, check), _unit(wanted, check)
    if not geom.same_direction(a, w, tol):
        angle = math.degrees(math.acos(max(-1.0, min(1.0, geom.dot(a, w)))))
        _fail(
            check,
            f"{check}: NX direction differs by {angle:.6g} degrees",
            actual=a,
            wanted=w,
            angle_deg=angle,
        )
    return a


def expect_parallel(actual, wanted, check, tol=1e-6):
    """Unit vectors are parallel or antiparallel."""
    a, w = _unit(actual, check), _unit(wanted, check)
    if not geom.parallel(a, w, tol):
        _fail(check, f"{check}: NX direction is not parallel", actual=a, wanted=w)
    return a


def expect_on_plane(point, origin, normal, check, tol=None):
    """A point lies on the plane (origin, unit normal)."""
    p = [float(v) for v in point]
    distance = geom.dot(geom.sub(p, list(origin)), list(normal))
    limit = geom.tol(p, origin) if tol is None else tol
    if not abs(distance) <= limit:
        _fail(
            check,
            f"{check}: point is {distance:.6g} off the plane",
            point=p,
            distance=distance,
            tolerance=limit,
        )
    return distance
