"""Pure vector and frame math shared by every domain (no NX, stdlib only).

Vectors are lists of floats. Points read from NX objects go through ``xyz`` and
matrices through ``rows`` so callers never touch ``.X``/``.Xx`` themselves.
Functions that validate caller input take a ``field`` and raise
``invalid_argument`` with that JSON-pointer field.
"""

from __future__ import annotations

import math

from nx_mcp.core.errors import fail

# Row-major identity rotation (list of lists, matching the legacy constant).
IDENTITY = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]

# Relative tolerance used for coordinate comparisons (scaled by ``tol``).
REL_TOL = 1e-6


def add(a, b):
    return [x + y for x, y in zip(a, b, strict=True)]


def sub(a, b):
    return [x - y for x, y in zip(a, b, strict=True)]


def scale(v, factor):
    return [x * factor for x in v]


def dot(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def cross2(a, b):
    """z component of the cross product of two 2D vectors."""
    return a[0] * b[1] - a[1] * b[0]


def norm(v):
    return math.sqrt(dot(v, v))


def dist(a, b):
    return norm(sub(a, b))


def _name(field):
    return str(field).lstrip("/")


def unit(v, field="vector"):
    """Unit vector of a 3-vector; a malformed or zero vector is ``invalid_argument``."""
    name = _name(field)
    if not isinstance(v, (list, tuple)) or len(v) != 3:
        fail("NX_INVALID_ARGUMENT", f"{name} requires three finite numbers", field=field)
    try:
        values = [float(x) for x in v]
    except (TypeError, ValueError):
        fail("NX_INVALID_ARGUMENT", f"{name} requires three finite numbers", field=field)
    length = math.sqrt(sum(x * x for x in values))
    if not math.isfinite(length) or length < 1e-12:
        fail("NX_INVALID_ARGUMENT", f"{name} must be finite and nonzero", field=field)
    return [x / length for x in values]


def tol(*points):
    """Absolute length tolerance scaled to the coordinates involved (1e-6 relative)."""
    scale_ = max([1.0] + [abs(c) for p in points for c in p])
    return REL_TOL * scale_


_scaled_tol = tol


def close(a, b, rel=REL_TOL):
    """Scalars equal within ``rel`` of the larger magnitude (at least 1)."""
    a, b = float(a), float(b)
    return abs(a - b) <= rel * max(1.0, abs(a), abs(b))


def parallel(a, b, tol=REL_TOL):
    """True when unit vectors a and b are parallel or antiparallel."""
    return norm(cross(a, b)) <= tol


def same_direction(a, b, tol=REL_TOL):
    """True when unit vectors a and b are parallel and point the same way."""
    return parallel(a, b, tol) and dot(a, b) > 0


def on_plane(point, origin, normal, tol=None):
    """True when point lies on the plane (origin, unit normal)."""
    if tol is None:
        tol = _scaled_tol(point, origin)
    return abs(dot(sub(point, origin), normal)) <= tol


def plane_through(points, field="points"):
    """Unit normal of the plane through three points; rejects collinear points."""
    from nx_mcp.kit.args import vec3

    if not isinstance(points, (list, tuple)) or len(points) != 3:
        fail("NX_INVALID_ARGUMENT", "three_points needs exactly three points", field=field)
    base = str(field).rstrip("/")
    a, b, c = (vec3(p, f"{base}[{i}]") for i, p in enumerate(points))
    normal = cross(sub(b, a), sub(c, a))
    length = norm(normal)
    if length <= tol(a, b, c) * max(norm(sub(b, a)), norm(sub(c, a)), 1.0):
        fail("NX_INVALID_ARGUMENT", "The three points are collinear", field=field)
    return [v / length for v in normal]


def orthonormal_frame(x_axis, y_axis, field="x_axis"):
    """Validated right-handed (x, y, z) rows from two orthonormal axes."""
    from nx_mcp.kit.args import vec3

    x, y = vec3(x_axis, "x_axis"), vec3(y_axis, "y_axis")
    if abs(norm(x) - 1) > 1e-8 or abs(norm(y) - 1) > 1e-8 or abs(dot(x, y)) > 1e-8:
        fail("NX_INVALID_ARGUMENT", "x_axis and y_axis must be orthonormal", field=field)
    return x, y, cross(x, y)


def arc_plane_normal(center, points):
    """Unit normal of an arc's plane from points on it; None if they are degenerate."""
    vectors = [sub(p, center) for p in points]
    best = (0.0, None)
    for i, a in enumerate(vectors):
        for b in vectors[i + 1 :]:
            c = cross(a, b)
            length = norm(c)
            if length > best[0]:
                best = (length, c)
    if best[1] is None or best[0] <= 1e-12:
        return None
    return [x / best[0] for x in best[1]]


def frame_from_normal(normal):
    """Orthonormal (x, y, z) rows with z = unit(normal) and a stable x."""
    z = unit(normal, "normal")
    helper = [1.0, 0.0, 0.0] if abs(z[0]) < 0.9 else [0.0, 1.0, 0.0]
    x = unit(cross(helper, z), "normal")
    return x, cross(z, x), z


def matvec(m, p):
    return [dot(row, p) for row in m]


def matmul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def transpose(m):
    return [list(v) for v in zip(*m, strict=True)]


def xyz(p):
    """[x, y, z] floats of an NX Point3d/Vector3d (or anything with X, Y, Z)."""
    return [float(p.X), float(p.Y), float(p.Z)]


def rows(m):
    """Row-major rotation R (p_abs = R p_local) of an NX Matrix3x3: its X, Y and Z
    axes become the columns."""
    return [[m.Xx, m.Yx, m.Zx], [m.Xy, m.Yy, m.Zy], [m.Xz, m.Yz, m.Zz]]


def rotation(m, field="rotation"):
    """A right-handed orthonormal row-major 3x3 rotation as float rows (1e-8 tolerance)."""
    from nx_mcp.kit.args import vec3

    if not isinstance(m, (list, tuple)) or len(m) != 3:
        fail("NX_INVALID_ARGUMENT", f"{_name(field)} must be 3x3", field=field)
    r = [vec3(row, f"{str(field).rstrip('/')}/{i}") for i, row in enumerate(m)]
    orthonormal = all(
        abs(dot(r[i], r[j]) - (1.0 if i == j else 0.0)) <= 1e-8 for i in range(3) for j in range(3)
    )
    if not orthonormal or abs(dot(r[0], cross(r[1], r[2])) - 1.0) > 1e-8:
        fail(
            "NX_INVALID_ARGUMENT",
            f"{_name(field)} must be a right-handed orthonormal matrix",
            field=field,
        )
    return r
