"""Fakes for the derived-curve tools (section, intersection, projected, combined, offset).

``curves_nx()`` is ``freeform_nx`` with the ``solid_modeling`` license, smart planes that
remember their origin and normal, and block faces that UF minimum distance measures as
flat boxes. ``made(nx, kind, polylines)`` is a committed curve feature whose
``GetEntities()`` are polyline curves (``GetLength`` in millimetres, as NX 2406).
"""

from __future__ import annotations

import math
from types import SimpleNamespace as NS

from tests.kit.objects import Object
from tests.unit.freeform.fakes import Polyline, freeform_nx
from tests.unit.modeling.fakes import feature


class Output(Object):
    """A curve a feature built: a polyline."""

    def __init__(self, points, mm=1.0):
        super().__init__("LINE")
        self.shape = Polyline(points)
        self.mm = mm

    def GetLength(self):
        pts = self.shape.points
        return self.mm * sum(math.dist(a, b) for a, b in zip(pts, pts[1:], strict=False))


def _xyz(p):
    return [float(p.X), float(p.Y), float(p.Z)]


def curves_nx(units="mm"):
    nx = freeform_nx(units)
    nx.licenses["solid_modeling"] = True
    nx.planes = []

    def create_plane(origin, normal, _option):
        plane = NS(kind="plane", origin=_xyz(origin), normal=_xyz(normal))
        nx.planes.append(plane)
        return plane

    nx.part.Planes = NS(CreatePlane=create_plane)
    make_block = nx.block

    def block(*a, **k):
        body = make_block(*a, **k)
        box = body.box
        for f in body.faces:
            axis = max(range(3), key=lambda i: abs(f.normal[i]))
            flat = list(box)
            level = box[axis + 3] if f.normal[axis] > 0 else box[axis]
            flat[axis] = flat[axis + 3] = level
            f.box = flat
            nx.remember(f)
        body.GetFaces = lambda: list(body.faces)
        return nx.remember(body)

    nx.block = block
    nx.mm = 25.4 if units == "in" else 1.0
    return nx


def made(nx, kind, polylines):
    """A committed curve feature whose entities are ``polylines`` (lists of points)."""
    f = feature(nx, kind)
    f.curves = [nx.remember(Output(p, nx.mm)) for p in polylines]
    f.GetEntities = lambda: list(f.curves)
    return f


def rectangle_x(x, y0=0.0, y1=50.0, z0=0.0, z1=20.0):
    """The section of a block at ``x``: four edges as polylines."""
    corners = [[x, y0, z0], [x, y1, z0], [x, y1, z1], [x, y0, z1]]
    return [[corners[i], corners[(i + 1) % 4]] for i in range(4)]
