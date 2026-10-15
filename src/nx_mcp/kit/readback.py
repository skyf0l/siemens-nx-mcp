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


def draft_angle(normal, direction) -> float:
    """Degrees between a plane (its ``normal``) and a ``direction``: 0 for a wall the
    direction runs along, 90 for a face square to it."""
    n, d = geom.unit(normal), geom.unit(direction)
    return math.degrees(math.asin(min(1.0, abs(geom.dot(n, d)))))


# UF AskFaceData surface types (``kit.select`` has the same codes).
PLANE, CYLINDER, CONE, SPHERE = 22, 16, 17, 18


def face_frame(tx, face) -> dict | None:
    """``{type, point, dir, radius}`` of a face from UF face data (a plane's point and
    normal, an axial face's axis point and direction, a sphere's centre); None when NX
    cannot read the face (consumed by the change)."""
    try:
        data = tx.uf.Modeling.AskFaceData(face.Tag)
        kind = int(data[0])
        direction = [float(v) for v in data[2]]
        return {
            "type": kind,
            "point": [float(v) for v in data[1]],
            "dir": geom.unit(direction) if geom.norm(direction) > 1e-12 else direction,
            "radius": float(data[4]),
        }
    except Exception:  # a dead tag or a face UF cannot describe
        return None


def _measurable(frame, vector):
    """The part of ``vector`` a face of this type shows: along a plane's normal, across an
    axial face's axis, all of it for a sphere; None for other faces."""
    kind = frame["type"]
    if kind == PLANE:
        return geom.scale(frame["dir"], geom.dot(vector, frame["dir"]))
    if kind in (CYLINDER, CONE):
        return geom.sub(vector, geom.scale(frame["dir"], geom.dot(vector, frame["dir"])))
    if kind == SPHERE:
        return list(vector)
    return None


def expect_moved(tx, before, faces, vector, check="moved") -> int:
    """Each face of ``faces`` moved by ``vector`` from its ``before`` frame
    (:func:`face_frame`), as far as its geometry shows: a plane along its normal, a
    cylinder or cone across its axis, a sphere's centre. Faces NX consumed, other surface
    types and moves the face cannot show (a plane slid in itself) are skipped. Returns how
    many faces were checked."""
    checked = 0
    for i, (old, face) in enumerate(zip(before, faces, strict=True)):
        new = face_frame(tx, face) if old is not None else None
        if new is None or new["type"] != old["type"]:
            continue
        want = _measurable(old, vector)
        if want is None:
            continue
        limit = 10.0 * geom.tol(old["point"], new["point"], vector)  # NX modeling tolerance
        if geom.norm(want) <= limit:
            continue
        got = _measurable(old, geom.sub(new["point"], old["point"]))
        expect_close(got, want, f"{check}[{i}]", tol=limit)
        checked += 1
    return checked


def same_surface(a, b, tol=None) -> bool:
    """Whether two face frames lie on one surface: coplanar planes, coaxial cylinders or
    cones of one radius, concentric spheres of one radius."""
    if a["type"] != b["type"]:
        return False
    limit = 10.0 * geom.tol(a["point"], b["point"]) if tol is None else tol
    gap = geom.sub(b["point"], a["point"])
    if a["type"] == PLANE:
        return geom.parallel(a["dir"], b["dir"], 1e-6) and abs(geom.dot(gap, a["dir"])) <= limit
    if a["type"] in (CYLINDER, CONE):
        across = geom.norm(geom.cross(gap, a["dir"]))
        return (
            geom.parallel(a["dir"], b["dir"], 1e-6)
            and across <= limit
            and abs(a["radius"] - b["radius"]) <= limit
        )
    if a["type"] == SPHERE:
        return geom.norm(gap) <= limit and abs(a["radius"] - b["radius"]) <= limit
    return False


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
