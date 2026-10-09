"""Rigid poses: composition, NX component moves, rotation about an axis, interpolation.

A pose is ``(t, R)``: translation ``t`` and row-major rotation ``R`` with
``p_out = R p_local + t`` (surface 1.4 ``Pose``). Pure math, stdlib only.

NX 2406 findings encoded here:

- ``ComponentAssembly.MoveComponent(c, shift, delta)`` rotates the occurrence
  about the *assembly origin*, then translates (measured: zero shift with
  rz 90 moved [20, 0, 0] to [0, 20, 0]). Reaching an absolute pose ``(t, R)``
  from ``(t0, R0)`` therefore needs ``delta = R R0^T`` and ``shift = t - delta t0``
  (:func:`move_args`).
- An explosion stores a component-local post-transform relative to the pose the
  occurrence inherits from its exploded parent (:func:`local_delta`).
"""

from __future__ import annotations

import math

from nx_mcp.kit import geom


def identity():
    return [row[:] for row in geom.IDENTITY]


def as_pose(value, field="/pose"):
    """``(t, R)`` floats of a ``Pose`` argument; rotation defaults to identity."""
    t = [float(v) for v in value["translation"]]
    rotation = value.get("rotation")
    r = geom.rotation(rotation, field + "/rotation") if rotation is not None else identity()
    return t, r


def compose(outer, inner):
    """The pose ``outer o inner``: first ``inner``, then ``outer``."""
    (t1, r1), (t2, r2) = outer, inner
    return geom.add(geom.matvec(r1, t2), t1), geom.matmul(r1, r2)


def inverse(pose):
    t, r = pose
    rt = geom.transpose(r)
    return geom.scale(geom.matvec(rt, t), -1.0), rt


def move_args(current, target):
    """``(shift, delta)`` for NX ``MoveComponent`` from pose ``current`` to ``target``."""
    (t0, r0), (t, r) = current, target
    delta = geom.matmul(r, geom.transpose(r0))
    return geom.sub(t, geom.matvec(delta, t0)), delta


def relative(current, translation, rotation=None):
    """A relative move: rotate about the component origin, then translate (assembly axes)."""
    t0, r0 = current
    r = r0 if rotation is None else geom.matmul(rotation, r0)
    return geom.add(t0, translation), r


def local_delta(base, target):
    """4x4 row-major matrix ``M`` with ``target = base o M`` (explosion post-transform)."""
    t, r = compose(inverse(base), target)
    return [r[i] + [t[i]] for i in range(3)] + [[0.0, 0.0, 0.0, 1.0]]


def axis_rotation(axis, angle_deg):
    """Row-major rotation by ``angle_deg`` about unit ``axis`` (right hand)."""
    x, y, z = geom.unit(axis, "axis")
    a = math.radians(angle_deg)
    c, s, k = math.cos(a), math.sin(a), 1.0 - math.cos(a)
    return [
        [c + x * x * k, x * y * k - z * s, x * z * k + y * s],
        [y * x * k + z * s, c + y * y * k, y * z * k - x * s],
        [z * x * k - y * s, z * y * k + x * s, c + z * z * k],
    ]


def rotate_about(pose, origin, axis, angle_deg):
    """``pose`` rotated by ``angle_deg`` about the line (``origin``, ``axis``)."""
    rot = axis_rotation(axis, angle_deg)
    t, r = pose
    moved = geom.add(geom.matvec(rot, geom.sub(t, origin)), origin)
    return moved, geom.matmul(rot, r)


def close(a, b, tol=1e-6):
    """Same pose within ``tol`` on every translation and rotation component."""
    (ta, ra), (tb, rb) = a, b
    return all(abs(x - y) <= tol for x, y in zip(ta, tb, strict=True)) and all(
        abs(ra[i][j] - rb[i][j]) <= tol for i in range(3) for j in range(3)
    )


def quaternion(m):
    """Unit (w, x, y, z) quaternion of a row-major rotation."""
    trace = m[0][0] + m[1][1] + m[2][2]
    if trace > 0:
        s = math.sqrt(trace + 1.0) * 2.0
        q = [s / 4, (m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s]
    else:
        i = max(range(3), key=lambda k: m[k][k])
        j, k = (i + 1) % 3, (i + 2) % 3
        s = math.sqrt(1.0 + m[i][i] - m[j][j] - m[k][k]) * 2.0
        q = [(m[k][j] - m[j][k]) / s, 0.0, 0.0, 0.0]
        q[i + 1] = s / 4
        q[j + 1] = (m[j][i] + m[i][j]) / s
        q[k + 1] = (m[k][i] + m[i][k]) / s
    length = math.sqrt(sum(v * v for v in q))
    return [v / length for v in q]


def slerp(first, second, fraction):
    """Shortest-arc interpolation between two rotations (``fraction`` 0..1)."""
    a, b = quaternion(first), quaternion(second)
    cosine = sum(x * y for x, y in zip(a, b, strict=True))
    if cosine < 0:
        b, cosine = [-v for v in b], -cosine
    if cosine > 0.9995:
        q = [x + fraction * (y - x) for x, y in zip(a, b, strict=True)]
    else:
        angle = math.acos(max(-1.0, min(1.0, cosine)))
        q = [
            (math.sin((1 - fraction) * angle) * x + math.sin(fraction * angle) * y)
            / math.sin(angle)
            for x, y in zip(a, b, strict=True)
        ]
    length = math.sqrt(sum(v * v for v in q))
    w, x, y, z = (v / length for v in q)
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]


def interpolate(first, second, fraction):
    """Pose between ``first`` and ``second``: linear translation, slerp rotation."""
    (t1, r1), (t2, r2) = first, second
    return [a + fraction * (b - a) for a, b in zip(t1, t2, strict=True)], slerp(r1, r2, fraction)
