"""Surface differential geometry and sampled edge continuity (G0/G1/G2).

    normal, shape = surface.differential(tx, face, point)        # unit normal, 3x3 shape operator
    angle, k = surface.continuity_difference(first, second)      # degrees, 1/part unit
    report = surface.edge_continuity(tx, edge, face, other_edge, other_face)

The shape operator is coordinate invariant (parameterisation and orientation
free), so G2 compares the Frobenius norm of the difference of two shape operators
with the second one's sign aligned to the first normal. Every check is sampled,
not a global continuity certificate.

NX 2406 findings encoded here:

- UF ``Modeling.AskFaceParm(face, point)`` returns ``(uv, point on face)``;
  ``Modeling.EvaluateFace(face, UF_MODL_EVAL_DERIV2, uv)`` returns ``SrfDu``,
  ``SrfDv``, ``SrfD2u``, ``SrfDudv``, ``SrfD2v``. ``UF_MODL_EVAL_DERIV2`` is 2.
- UF expects float coordinate lists ("Expecting List of double type").
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
    samples.
    """
    if not isinstance(samples, int) or samples < 1:
        raise ValueError("samples must be a positive integer")
    gaps, angles, curvatures, unresolved = [], [], [], 0
    with curve.evaluator(tx, edge) as source, curve.evaluator(tx, other_edge) as target:
        for i in range(samples):
            point = source.at((i + 0.5) / samples)[0]
            closest = target.closest(point)[0]
            gaps.append(geom.dist(point, closest))
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
            angles.append(angle)
            curvatures.append(k)
    return {
        "gap": max(gaps),
        "angle": max(angles) if angles else None,
        "curvature": max(curvatures) if curvatures else None,
        "samples": samples,
        "unresolved": unresolved,
    }
