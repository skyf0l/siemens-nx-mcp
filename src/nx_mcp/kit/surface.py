"""Surface differential geometry and sampled edge continuity (G0/G1/G2).

    normal, shape = surface.differential(tx, face, point)        # unit normal, 3x3 shape operator
    angle, k = surface.continuity_difference(first, second)      # degrees, 1/part unit
    report = surface.edge_continuity(tx, edge, face, other_edge, other_face)
    points = surface.iso_line_points(tx, face, 0, 3)             # 3 lines of constant u

The shape operator is coordinate invariant (parameterisation and orientation
free), so G2 compares the Frobenius norm of the difference of two shape operators
with the second one's sign aligned to the first normal. Every check is sampled,
not a global continuity certificate.

NX 2406 findings encoded here:

- UF ``Modeling.AskFaceParm(face, point)`` returns ``(uv, point on face)``;
  ``Modeling.EvaluateFace(face, UF_MODL_EVAL_DERIV2, uv)`` returns ``SrfDu``,
  ``SrfDv``, ``SrfD2u``, ``SrfDudv``, ``SrfD2v``. ``UF_MODL_EVAL_DERIV2`` is 2.
- UF expects float coordinate lists ("Expecting List of double type").
- A planar face's UV box (``AskFaceUvMinmax``) can be wider than the face: a disc of
  radius 20 has the box +-20.24 (in NX's metres); ``AskPointContainment`` answers 1
  inside, 3 on the boundary, 2 outside.
"""

from __future__ import annotations

import math

from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.kit import curve, geom

EVAL_DERIV2 = 2  # UF_MODL_EVAL_DERIV2 on NX 2406


def _singular(message):
    fail("NX_INCOMPLETE_RESULT", message, details={"reason": "singular"})


def shape_operator(du, dv, duu, duv, dvv):
    """``(unit normal, 3x3 shape operator)`` from first and second surface derivatives.

    A degenerate parameterisation raises ``NX_INCOMPLETE_RESULT`` (``details.reason``
    ``singular``).
    """
    normal = geom.cross(du, dv)
    area = geom.norm(normal)
    if not area > 1e-14:
        _singular("Surface parameterization is singular at a sample")
    normal = [v / area for v in normal]
    e, f, g = geom.dot(du, du), geom.dot(du, dv), geom.dot(dv, dv)
    determinant = e * g - f * f
    if not determinant > 1e-24 * max(1.0, e * g):
        _singular("Surface metric is singular at a sample")
    inverse = [[g / determinant, -f / determinant], [-f / determinant, e / determinant]]
    second = [
        [geom.dot(normal, duu), geom.dot(normal, duv)],
        [geom.dot(normal, duv), geom.dot(normal, dvv)],
    ]
    tangent = [[du[i], dv[i]] for i in range(3)]
    dual = [
        [sum(tangent[i][k] * inverse[k][j] for k in range(2)) for j in range(2)] for i in range(3)
    ]
    shape = [
        [
            sum(dual[i][k] * second[k][c] * dual[j][c] for k in range(2) for c in range(2))
            for j in range(3)
        ]
        for i in range(3)
    ]
    return normal, shape


def continuity_difference(first, second):
    """``(normal angle in degrees, curvature difference)`` of two ``shape_operator`` results.

    Normals are compared unsigned (face sense does not matter); the curvature
    difference is the Frobenius norm with the second operator's sign aligned.
    """
    normal_a, shape_a = first
    normal_b, shape_b = second
    cosine = geom.dot(normal_a, normal_b)
    sign = 1 if cosine >= 0 else -1
    angle = math.degrees(math.acos(min(1.0, abs(cosine))))
    curvature = math.sqrt(
        sum((shape_a[i][j] - sign * shape_b[i][j]) ** 2 for i in range(3) for j in range(3))
    )
    return angle, curvature


def differential(tx, face, point):
    """``shape_operator`` of ``face`` at the face point nearest ``point`` (part units)."""
    modeling = tx.uf.Modeling
    with native(default="NX_ERROR"):
        uv = modeling.AskFaceParm(face.Tag, [float(v) for v in point])[0]
        data = modeling.EvaluateFace(face.Tag, EVAL_DERIV2, [float(v) for v in uv])
        derivatives = [
            [float(v) for v in getattr(data, name)]
            for name in ("SrfDu", "SrfDv", "SrfD2u", "SrfDudv", "SrfD2v")
        ]
    return shape_operator(*derivatives)


def edge_continuity(tx, edge, face, other_edge, other_face, samples=9):
    """Sampled continuity from ``edge`` (bounding ``face``) to ``other_edge`` (bounding
    ``other_face``).

    ``samples`` interior points of ``edge`` (parameters (i + 0.5) / samples) are
    projected onto ``other_edge``: ``gap`` is the largest distance. With both
    faces, ``angle`` (degrees) and ``curvature`` (1/part unit) are the largest
    normal angle and shape-operator difference over the resolved samples; they are
    None without faces or when no sample resolved. ``unresolved`` counts singular
    samples. ``gap_at``, ``angle_at`` and ``curvature_at`` locate each worst sample:
    ``{point, parameter}`` on ``edge`` (``parameter`` normalised 0..1), None when that
    value is None.
    """
    if not isinstance(samples, int) or samples < 1:
        raise ValueError("samples must be a positive integer")
    gaps, angles, curvatures, unresolved = [], [], [], 0
    with curve.evaluator(tx, edge) as source, curve.evaluator(tx, other_edge) as target:
        for i in range(samples):
            t = (i + 0.5) / samples
            point = source.at(t)[0]
            closest = target.closest(point)[0]
            gaps.append((geom.dist(point, closest), point, t))
            if face is None or other_face is None:
                continue
            try:
                angle, k = continuity_difference(
                    differential(tx, face, point), differential(tx, other_face, closest)
                )
            except NXError:
                unresolved += 1
                continue
            if not (math.isfinite(angle) and math.isfinite(k)):
                unresolved += 1
                continue
            angles.append((angle, point, t))
            curvatures.append((k, point, t))
    worst = {
        name: max(values, key=lambda v: v[0]) if values else None
        for name, values in (("gap", gaps), ("angle", angles), ("curvature", curvatures))
    }
    out = {name: None if w is None else w[0] for name, w in worst.items()}
    return {
        **out,
        "samples": samples,
        "unresolved": unresolved,
        **{
            f"{name}_at": None if w is None else {"point": list(w[1]), "parameter": w[2]}
            for name, w in worst.items()
        },
    }


# UF_MODL_ask_point_containment: 1 inside the trimmed face, 3 on its boundary.
ON_FACE = (1, 3)
SCAN = 33  # parameter values tried across a face's UV box
EDGE = 0.005  # share of the UV range within which the end of a family is located


def _crossing(tx, face, box, k, value):
    """``(point, uv)`` on the trimmed face where face parameter ``k`` (0 u, 1 v) equals
    ``value``, the one nearest the middle of the other parameter's range; None when the
    line of constant ``value`` misses the face."""
    modeling = tx.uf.Modeling
    low, high = box[2 * (1 - k)], box[2 * (1 - k) + 1]
    order = sorted(range(SCAN), key=lambda i: (abs(2 * i - (SCAN - 1)), i))
    for i in order:
        t = low + (high - low) * i / (SCAN - 1)
        uv = [float(value), t] if k == 0 else [t, float(value)]
        with native(default="NX_ERROR"):
            point = [float(v) for v in modeling.AskFaceProps(face.Tag, uv)[0][:3]]
            inside = int(modeling.AskPointContainment(point, face.Tag))
        if inside in ON_FACE:
            return point, uv
    return None


def iso_line_points(tx, face, k, count) -> list[list[float]]:
    """One point per line of constant face parameter ``k`` (0 u, 1 v): ``count`` values
    spread evenly over the trimmed face's real extent in that parameter, both ends
    included. Where the face's UV box is wider than the face (a disc), an end moves in
    to ``EDGE`` to ``2 EDGE`` of the range inside where the lines leave the face: a short
    chord near the rim, not a degenerate one. A value whose line misses a non-convex face
    is skipped."""
    with native(default="NX_ERROR"):
        box = [float(v) for v in tx.uf.Modeling.AskFaceUvMinmax(face.Tag)]
    low, high = box[2 * k], box[2 * k + 1]

    def hits(value):
        return _crossing(tx, face, box, k, value) is not None

    order = sorted(range(SCAN), key=lambda i: (abs(2 * i - (SCAN - 1)), i))
    seed = next((v for v in (low + (high - low) * i / (SCAN - 1) for i in order) if hits(v)), None)
    if seed is None:
        fail("NX_WRONG_GEOMETRY", "No point inside the face was found", field="/face")

    def end(outer):
        if hits(outer):
            return outer
        good, bad = seed, outer
        while abs(good - bad) > EDGE * abs(high - low):
            tx.checkpoint()
            middle = (good + bad) / 2
            good, bad = (middle, bad) if hits(middle) else (good, middle)
        inward = min(EDGE * abs(high - low), abs(seed - good))  # clear of the rim
        return good + math.copysign(inward, seed - good)

    a, b = end(low), end(high)
    values = [(a + b) / 2] if count == 1 else [a + (b - a) * i / (count - 1) for i in range(count)]
    found = (_crossing(tx, face, box, k, v) for v in values)
    return [hit[0] for hit in found if hit is not None]
