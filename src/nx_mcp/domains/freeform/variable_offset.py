"""Variable Offset: a sheet offset from a face by four distances (laminate thickness zones).

NX 2406 lessons kept here (probed live on a 20 x 10 planar sheet face):

- ``CreateVariableOffsetBuilder`` needs the basic freeform license (``nx_freeform_1``),
  asked before the builder.
- ``Face`` is a ``SelectObject``; setting it puts ``PointA``..``PointD`` on the face's
  corners (NX's order: (0,0), (0,10), (20,10), (20,0) here) and every ``OffsetAt``
  expression to 25.4 mm, so all four are always set. Given points replace them and the
  offsets interpolate (extrapolate beyond them): 1, 1, 3, 3 at y 2, 2, 8, 8 give z
  0.3333..3.6667 over y 0..10.
- Offsets are signed along the face normal (UF ``AskFaceProps``, outward on a solid);
  a corner may be 0 or negative (-1, 1, 2, 3: each corner + offset x normal lies on the
  result within 1e-5). ``IsReverseDirection`` flips them all: not exposed, the sign does.
- ``Method`` ``Linear`` blends bilinearly; ``Cubic`` is smoother inside (the centre of
  1, 2, 3, 4: 2.4723 from the face against 2.4876 linear); both hit the four points.
- A native refusal is ``NX_BUILD_FAILED`` with ``nx_code``.

Read back: each point moved by its offset along the face normal lies on the result
(``points[].gap``).
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, FaceRef, Items, Length, Point3
from nx_mcp.domains.freeform import common
from nx_mcp.kit import bodies as kbodies
from nx_mcp.kit import builders, curve, expr, geom, licenses, readback, select, units

LICENSE = "nx_freeform_1"
CORNERS = "ABCD"


class CornerData(TypedDict):
    point: Point3  # on the face
    distance: float
    gap: float  # point + distance x normal from the result


class VariableOffsetData(common.SheetData):
    method: Literal["linear", "cubic"]
    points: list[CornerData]


def _normal(tx, face, point):
    """``(point on the face, unit normal)`` nearest ``point`` (UF face props)."""
    modeling = tx.uf.Modeling
    with native(default="NX_ERROR"):
        uv = modeling.AskFaceParm(face.Tag, [float(v) for v in point])[0]
        props = modeling.AskFaceProps(face.Tag, [float(v) for v in uv])
    on = [float(v) for v in props[0]]
    return on, geom.unit([float(v) for v in props[5]], "normal")


@tool(
    effect="model",
    label="Create variable offset",
    summary=lambda a, say: f"Variable offset of {say('face')} by {say('distances')}",
    keywords=(
        "variable offset",
        "variable thickness",
        "laminate zones",
        "tapered offset",
        "offset by points",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={
        "face": "fc:7",
        "distances": [1, 1, 3, 3],
        "points": [[5, 2, 0], [15, 2, 0], [15, 8, 0], [5, 8, 0]],
    },  # fmt: skip
)
def nx_create_variable_offset(
    tx: Tx,
    *,
    face: FaceRef,
    distances: Annotated[
        list[Length], Items(4, 4), Doc("Signed offsets at the 4 points, + along the face normal")
    ],
    points: Annotated[
        list[Point3],
        Items(4, 4),
        Doc("4 points on the face, in order round it; default its corners"),
    ]
    | None = None,
    method: Annotated[
        Literal["linear", "cubic"], Doc("Blend between the points: linear or smoother cubic")
    ] = "linear",
) -> VariableOffsetData:
    """Offset one face into a new sheet whose distance varies between four points (laminate
    zones, tapered skins). Without points the face corners in NX's order are used, reported in
    points[]; each point + distance x normal lies on the result (gap)."""
    values = [float(d) for d in distances]
    if all(geom.close(v, 0.0, 1e-12) for v in values):
        fail("NX_INVALID_ARGUMENT", "At least one distance must not be zero", field="/distances")
    with tx.checks():
        target = select.one(tx, face, "face", field="/face")
        limit = 10 * units.part_tolerance(tx)
        placed: list[list[float]] = []
        for i, p in enumerate(points or []):
            on, _n = _normal(tx, target, p)
            gap = curve.point_distance(tx, target, p)[0]
            if gap > limit:
                fail("NX_INVALID_ARGUMENT", f"Point {i} is {gap:.4g} off the face",
                     field=f"/points/{i}", hint="Give points on the face (nx_measure_distance "
                     "to the face shows the gap)")  # fmt: skip
            if any(geom.dist(on, q) <= limit for q in placed):
                fail("NX_INVALID_ARGUMENT", f"Point {i} repeats another point",
                     field=f"/points/{i}")  # fmt: skip
            placed.append(on)
    licenses.require(tx, (LICENSE,), f"This NX seat has no {LICENSE} license for this command",
                     strict=True)  # fmt: skip
    part = tx.part
    V = tx.nx.Features.VariableOffsetBuilder
    tol = units.part_tolerance(tx)
    with builders.build(tx, part.Features.CreateVariableOffsetBuilder, None) as b:
        b.Face.Value = target
        b.DistanceTolerance = tol
        b.Method = V.MethodOptions.Cubic if method == "cubic" else V.MethodOptions.Linear
        for k, at in zip(CORNERS, placed, strict=False):
            setattr(b, "Point" + k, builders.point(tx, at))
        for k, d in zip(CORNERS, distances, strict=True):
            expr.drive(getattr(b, "OffsetAt" + k), d)
        used = [geom.xyz(getattr(b, "Point" + k).Coordinates) for k in CORNERS]
        feature = b.commit()
    bodies = common.feature_bodies(feature)
    out = common.result_of(tx, feature, bodies)
    kbodies.expect_sound(tx, bodies)
    corners: list[CornerData] = []
    for p, d in zip(used, values, strict=True):
        on, n = _normal(tx, target, p)
        wanted = geom.add(on, geom.scale(n, d))
        gap = min(curve.point_distance(tx, x, wanted)[0] for x in bodies)
        corners.append({"point": (on[0], on[1], on[2]), "distance": d, "gap": gap})
    worst = max(c["gap"] for c in corners)
    readback.expect(worst <= 2 * tol + limit, "offset", "The sheet misses an offset point",
                    gaps=[c["gap"] for c in corners])  # fmt: skip
    return {**out, "method": method, "points": corners}
