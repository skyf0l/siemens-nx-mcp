"""Curve and edge evaluation through UF ``Eval``: points, tangents, closest points.

    start, end = curve.ends(tx, line)              # work-part coordinates
    p = curve.point_at(tx, spline, 0.5)            # normalised parameter 0..1
    foot, tangent = curve.closest(tx, edge, [x, y, z])
    gap, on_a, on_b = curve.min_distance(tx, a, b)

NX 2406 findings encoded here:

- ``UF.Modeling`` has no ``AskCurveProps`` in Python; ``UF.Eval`` (``Initialize``,
  ``AskLimits``, ``EvaluateUnitVectors``, ``EvaluateClosestPoint``) works for
  sketch curves and model edges alike. The evaluator handle is freed afterwards.
- NXOpen point and vector constructors refuse Python ``int`` arguments
  ("Expecting double type"): every coordinate handed to NX is a ``float``.
"""

from __future__ import annotations

import contextlib

from nx_mcp.core.errors import native


class Evaluator:
    """An initialised UF evaluator of one curve or edge."""

    def __init__(self, ev, handle):
        self.ev = ev
        self.handle = handle
        low, high = (float(v) for v in ev.AskLimits(handle))
        self.low, self.high = low, high

    def parameter(self, t):
        """The native parameter of the normalised parameter ``t`` (0 start, 1 end)."""
        return self.low + float(t) * (self.high - self.low)

    def at(self, t):
        """``(point, unit tangent)`` at normalised parameter ``t``."""
        values = self.ev.EvaluateUnitVectors(self.handle, self.parameter(t))
        return [float(v) for v in values[0]], [float(v) for v in values[1]]

    def closest(self, point):
        """``(point on the curve, unit tangent there)`` closest to ``point``."""
        found = self.ev.EvaluateClosestPoint(self.handle, [float(v) for v in point])
        values = self.ev.EvaluateUnitVectors(self.handle, float(found[0]))
        return [float(v) for v in values[0]], [float(v) for v in values[1]]


@contextlib.contextmanager
def evaluator(tx, curve):
    """An :class:`Evaluator` for ``curve`` (a curve or an edge); always freed."""
    ev = tx.uf.Eval
    with native(default="NX_ERROR"):
        handle = ev.Initialize(curve.Tag)
    try:
        with native(default="NX_ERROR"):
            yield Evaluator(ev, handle)
    finally:
        free = getattr(ev, "Free", None)
        if free is not None:
            with contextlib.suppress(Exception):
                free(handle)


def point_at(tx, curve, t):
    """Work-part point at normalised parameter ``t`` of a curve or edge."""
    with evaluator(tx, curve) as e:
        return e.at(t)[0]


def ends(tx, curve):
    """``(start, end)`` work-part points of a curve or edge."""
    with evaluator(tx, curve) as e:
        return e.at(0.0)[0], e.at(1.0)[0]


def closest(tx, curve, point):
    """``(foot, unit tangent)``: the point of ``curve`` closest to ``point``."""
    with evaluator(tx, curve) as e:
        return e.closest(point)


def min_distance(tx, a, b):
    """``(distance, point on a, point on b)`` between two NX objects (UF minimum distance)."""
    zero = [0.0, 0.0, 0.0]
    with native(default="NX_ERROR"):
        out = tx.uf.Modeling.AskMinimumDist(a.Tag, b.Tag, 0, zero, 0, zero)
    return float(out[0]), [float(v) for v in out[1]], [float(v) for v in out[2]]
