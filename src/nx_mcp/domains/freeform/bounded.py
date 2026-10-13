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
from nx_mcp.kit import builders, readback, select, units

StringRef = Annotated[str, RefTo("curve", "edge", doc="Curve or edge 'c:7'/'e:21'")]
FACE_PLANE = 22


class BoundedPlaneData(common.SheetData):
    loops: int  # closed loops found (1 outer + holes)
    normal: Vec3  # plane normal of the boundary (sign arbitrary)
    gap: float  # largest sampled distance from the boundary to the sheet


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
        if deviation > surface_loops.chaining(tol):
            fail(
                "NX_WRONG_GEOMETRY",
                f"The boundary is not planar: it leaves its plane by {deviation:.4g}",
                field="/boundary",
                hint="Use nx_create_n_sided_surface or nx_create_fill_hole for a 3D loop",
                details={"deviation": deviation},
            )
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
