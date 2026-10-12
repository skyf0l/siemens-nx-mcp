"""Facts about model edges and faces: line/arc data of an edge, points inside a face.

    f = facts.edge(tx, edge)        # {"kind": "line"/"arc"/"other", "start", "end", ...}
    p = facts.face_point(tx, face)  # a point inside a trimmed face, clear of holes
    q = facts.on_object(tx, obj)    # edge midpoint or interior face point
    s = facts.interior_samples(tx, face, 3)  # [(point, unit normal)] inside a face

Points are work-part coordinates in part units; edges are evaluated through
``kit.curve`` (UF ``Eval``). NX 2406 findings encoded here:

- A full circle has no end vertices: its evaluator start and end coincide
  (``closed``); dimensions and section cuts must use its center.
- UF ``Modeling.AskPointContainment`` returns 1 for a point inside a trimmed
  face; the face's parameter-box center can lie in a hole.
"""

from __future__ import annotations

import math

from nx_mcp.kit import curve, geom

FACE_INSIDE = 1  # UF_MODL_ask_point_containment: inside the trimmed face.


def _xyz(values):
    return [float(v) for v in values[:3]]


def edge(tx, obj):
    """``kind``, ``start``, ``end``; arcs add ``center``, ``radius``, ``x_axis``,
    ``normal`` (or None), ``samples`` and ``closed``."""
    with curve.evaluator(tx, obj) as e:
        start, end = e.at(0.0)[0], e.at(1.0)[0]
        out = {"kind": "other", "start": start, "end": end}
        if e.ev.IsLine(e.handle):
            out["kind"] = "line"
        elif e.ev.IsArc(e.handle):
            arc = e.ev.AskArc(e.handle)
            radius = float(arc.Radius)
            center = _xyz(arc.Center)
            samples = [e.at(1 / 3)[0], e.at(2 / 3)[0]]
            out.update(
                kind="arc",
                center=center,
                radius=radius,
                x_axis=_xyz(arc.XAxis),
                samples=samples,
                normal=geom.arc_plane_normal(center, [start, *samples]),
                closed=math.dist(start, end) <= 1e-6 * max(1.0, radius),
            )
        return out


def _uv_samples(steps=8):
    """Normalised (u, v) samples ordered from the parameter-box center outwards."""
    values = [i / steps for i in range(1, steps)]
    grid = [(u, v) for u in values for v in values]
    return sorted(grid, key=lambda uv: ((uv[0] - 0.5) ** 2 + (uv[1] - 0.5) ** 2, uv))


def face_point(tx, face, steps=8):
    """A point inside a trimmed face, preferring one whose neighbours are inside too."""
    modeling = tx.uf.Modeling
    u0, u1, v0, v1 = modeling.AskFaceUvMinmax(face.Tag)
    step = 1 / steps

    def at(fu, fv):
        uv = [u0 + fu * (u1 - u0), v0 + fv * (v1 - v0)]
        return _xyz(modeling.AskFaceProps(face.Tag, uv)[0])

    def inside(fu, fv):
        return int(modeling.AskPointContainment(at(fu, fv), face.Tag)) == FACE_INSIDE

    samples = _uv_samples(steps)
    interior = [uv for uv in samples if inside(*uv)]
    for fu, fv in interior:
        near = [(fu + du, fv + dv) for du, dv in ((step, 0), (-step, 0), (0, step), (0, -step))]
        if all(0 < a < 1 and 0 < b < 1 and inside(a, b) for a, b in near):
            return at(fu, fv)
    return at(*(interior or samples)[0])


def interior_samples(tx, face, steps=3) -> list[tuple[list[float], list[float]]]:
    """``[(point, unit normal)]`` at the centres of a ``steps`` x ``steps`` UV grid cell
    layout of a face; points outside the trimmed face are skipped. The normal is UF
    ``AskFaceProps``' (NX 2406: it follows the face sense, outward on a solid)."""
    modeling = tx.uf.Modeling
    u0, u1, v0, v1 = (float(v) for v in modeling.AskFaceUvMinmax(face.Tag))
    out = []
    for i in range(steps):
        for j in range(steps):
            uv = [u0 + (u1 - u0) * (i + 0.5) / steps, v0 + (v1 - v0) * (j + 0.5) / steps]
            props = modeling.AskFaceProps(face.Tag, uv)
            point = _xyz(props[0])
            if int(modeling.AskPointContainment(point, face.Tag)) != FACE_INSIDE:
                continue
            normal = _xyz(props[5])
            size = geom.norm(normal)
            if size > 1e-12:
                out.append((point, [v / size for v in normal]))
    return out


def is_edge(obj):
    return hasattr(obj, "SolidEdgeType") or type(obj).__name__ == "Edge"


def on_object(tx, obj):
    """A point on an edge (its parametric midpoint) or inside a face."""
    if is_edge(obj):
        return curve.point_at(tx, obj, 0.5)
    return face_point(tx, obj)
