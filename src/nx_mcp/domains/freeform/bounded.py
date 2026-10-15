"""Bounded Plane: a planar sheet inside closed coplanar loops (transom, bulkhead, cap).

NX 2406 lessons kept here:

- ``CreateBoundedPlaneBuilder`` needs the basic freeform license (``nx_freeform_1``),
  asked before the builder; its only input is its own ``BoundingCurves`` section.
- Every closed loop goes into that one section; a loop inside another becomes a hole
  (a 40 x 20 rectangle with a circle r 5 inside: one face, area 800 - 25 pi).
- A native refusal is ``NX_BUILD_FAILED`` with ``nx_code``.

Read back: one planar sheet; every boundary curve lies on it (sampled gap).
"""

from __future__ import annotations

from typing import Annotated

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Length, RefTo, Vec3
from nx_mcp.domains.freeform import common, surface_loops
from nx_mcp.kit import bodies as kbodies
from nx_mcp.kit import builders, curve, geom, readback, select, units

StringRef = Annotated[str, RefTo("curve", "edge", doc="Curve or edge 'c:7'/'e:21'")]
FACE_PLANE = 22


class BoundedPlaneData(common.SheetData):
    loops: int  # closed loops found (1 outer + holes)
    normal: Vec3  # plane normal of the boundary (sign arbitrary)
    gap: float  # largest sampled distance from the boundary to the sheet


def _ordered(tx, objs, loop):
    """Sample points round one closed loop, in walking order."""
    points: list = []
    for k, i in enumerate(loop):
        p = surface_loops.curve_points(tx, objs[i])
        if points:
            if geom.dist(p[-1], points[-1]) < geom.dist(p[0], points[-1]):
                p = p[::-1]
        elif len(loop) > 1:
            following = curve.ends(tx, objs[loop[k + 1]])
            if min(geom.dist(p[0], q) for q in following) < min(
                geom.dist(p[-1], q) for q in following
            ):
                p = p[::-1]
        points.extend(p)
    return points


def _apart(tx, objs, loops, refs, limit) -> None:
    """``NX_WRONG_GEOMETRY`` when each loop is planar but they lie in different planes."""
    fits = [surface_loops.plane_fit(_ordered(tx, objs, loop)) for loop in loops]
    if all(normal is None or deviation <= limit for _o, normal, deviation in fits):
        fail(
            "NX_WRONG_GEOMETRY",
            f"The {len(loops)} loops are each planar but lie in different planes",
            field="/boundary",
            hint="A bounded plane needs coplanar loops: make one call per loop",
            details={"loops": [[refs[i] for i in loop] for loop in loops]},
        )


def _inside(polygon, point) -> bool:
    """Ray casting in 2D: whether ``point`` lies inside the closed ``polygon``."""
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1], strict=True):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _one_outer(tx, objs, loops, refs, origin, normal) -> None:
    """``NX_WRONG_GEOMETRY`` unless one loop holds every other loop (the holes)."""
    u = geom.unit(geom.cross(normal, [1.0, 0.0, 0.0] if abs(normal[0]) < 0.9 else [0.0, 1.0, 0.0]))
    v = geom.cross(normal, u)
    flat = [
        [(geom.dot(geom.sub(p, origin), u), geom.dot(geom.sub(p, origin), v)) for p in points]
        for points in (_ordered(tx, objs, loop) for loop in loops)
    ]
    if any(
        all(k == j or _inside(flat[k], flat[j][len(flat[j]) // 2]) for j in range(len(flat)))
        for k in range(len(flat))
    ):
        return
    fail(
        "NX_WRONG_GEOMETRY",
        f"The boundary forms {len(loops)} separate loops: one outer loop per call",
        field="/boundary",
        hint="Make one call per outer loop; only loops inside the outer loop become holes",
        details={"loops": [[refs[i] for i in loop] for loop in loops]},
    )


@tool(
    effect="model",
    label="Create bounded plane",
    summary="Bounded plane on {boundary}",
    keywords=(
        "bounded plane",
        "planar sheet",
        "fill planar loop",
        "transom",
        "bulkhead",
        "flat cap",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"boundary": ["e:4", "e:5", "e:6", "e:7"]},
)
def nx_create_bounded_plane(
    tx: Tx,
    *,
    boundary: Annotated[
        list[StringRef], Items(1, 200), Doc("Curves/edges of closed coplanar loops, any order")
    ],
    tolerance: Annotated[Length, Doc("Coplanarity and chaining tolerance")] | None = None,
) -> BoundedPlaneData:
    """Create a planar sheet bounded by closed coplanar loops of curves or edges (transom,
    bulkhead, flat cap). A loop inside the outer loop becomes a hole. For a curved patch
    use nx_create_n_sided_surface; to close a sheet's opening use nx_create_fill_hole."""
    if len(set(boundary)) != len(boundary):
        fail("NX_INVALID_ARGUMENT", "A boundary curve is listed twice", field="/boundary")
    with tx.checks():
        tol = common.given_tolerance(tx, tolerance)
        objs = [
            select.one(tx, ref, "curve", "edge", field=f"/boundary/{i}")
            for i, ref in enumerate(boundary)
        ]
        if len({int(o.Tag) for o in objs}) != len(objs):
            fail("NX_INVALID_ARGUMENT", "A boundary curve is listed twice", field="/boundary")
        loops = surface_loops.loops_of(tx, objs, surface_loops.chaining(tol))
        points = [p for o in objs for p in surface_loops.curve_points(tx, o)]
        _origin, normal, deviation = surface_loops.plane_fit(points)
        if normal is None:
            fail("NX_WRONG_GEOMETRY", "The boundary lies on a line: it bounds no plane",
                 field="/boundary")  # fmt: skip
        if deviation > surface_loops.chaining(tol) and len(loops) > 1:
            _apart(tx, objs, loops, boundary, surface_loops.chaining(tol))
        if deviation > surface_loops.chaining(tol):
            fail(
                "NX_WRONG_GEOMETRY",
                f"The boundary is not planar: it leaves its plane by {deviation:.4g}",
                field="/boundary",
                hint="Use nx_create_n_sided_surface or nx_create_fill_hole for a 3D loop",
                details={"deviation": deviation},
            )
        if len(loops) > 1:
            _one_outer(tx, objs, loops, boundary, _origin, normal)
    common.licensed(tx)
    with builders.build(tx, tx.part.Features.CreateBoundedPlaneBuilder, None) as b:
        builders.curve_section(tx, objs, into=b.BoundingCurves)
        feature = b.commit()
    bodies = common.feature_bodies(feature)
    out = common.result_of(tx, feature, bodies)
    kbodies.expect_sound(tx, bodies)
    faces = [f for x in bodies for f in x.GetFaces()]
    planar = all(select.face_data(tx, f)[0] == FACE_PLANE for f in faces)
    readback.expect(planar and out["sheet"], "planar", "NX built no planar sheet",
                    faces=len(faces))  # fmt: skip
    readback.expect(out["area"] > 0, "area", "The bounded plane has no area", area=out["area"])
    gap = surface_loops.sampled_gap(tx, objs, bodies)
    limit = 2 * tol + 10 * units.part_tolerance(tx)
    readback.expect(gap <= limit, "boundary", "The sheet misses the boundary", gap=gap)
    n = (normal[0], normal[1], normal[2])
    return {**out, "loops": len(loops), "normal": n, "gap": gap}
