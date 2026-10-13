"""Blend corner: an edge blend of the edges meeting at one vertex with setbacks there.

NX 2406 lessons kept here (probed live on a 40 mm cube corner, R5):

- Three edges at a vertex blended in one Edge Blend give a plain spherical corner (4
  blend faces). ``EdgeBlendBuilder.AddSetbackData(edges, at_start, distances)`` before
  commit pulls the three blends back from the vertex by each distance (measured along
  the edge; ``at_start`` says whether the vertex is the edge's first vertex) and NX
  fills a smooth corner patch: with 12 mm setbacks 9 blend faces
  (``SeparateFromCorner``) or 18 (``IncludeWithCorner``, the default) instead of 4,
  volume 63207.19 / 63139.34 against 63377.12; the body stays consistent.
- The freeform Blend Corner command (``FreeformSurfaceCollection.CreateBlendCornerBuilder``)
  needs an advanced freeform license for its tolerances (default 0) and its setback
  curve builders (``Part.CreateEmptyBlendSetbackBuilder`` returns nothing on 2406): not
  used, so this tool needs only the modeling license.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, EdgeRef, Items, Length
from nx_mcp.domains.modeling import common
from nx_mcp.kit import builders, expr, geom, measure, readback, result, select, units

SETBACK_HINT = (
    "Give a setback above the radius and below each edge's length; the edges must be "
    "convex or concave edges of one body meeting at the corner"
)
SETBACK_FACES = {"with_corner": "IncludeWithCorner", "separate": "SeparateFromCorner"}


class BlendCornerData(TypedDict):
    feature: result.Row
    bodies: list[result.Row]
    volume: float
    corner: list[float]  # [x, y, z] the edges met at (gone after the blend)
    blend_faces: int  # faces of the blend, corner patch included
    area: float  # the blend faces' total area


def _corner(tx, picked) -> tuple[list[float], list[bool]]:
    """The vertex every edge ends at, and per edge whether it is the edge's start."""
    ends = [[geom.xyz(p) for p in e.GetVertices()] for e in picked]
    tol = max(units.part_tolerance(tx), 1e-9)
    for candidate in ends[0]:
        at_start = []
        for start, end in ends:
            if geom.dist(start, candidate) <= tol:
                at_start.append(True)
            elif geom.dist(end, candidate) <= tol:
                at_start.append(False)
            else:
                break
        else:
            return candidate, at_start
    fail("NX_INVALID_ARGUMENT", "The edges do not meet at one vertex", field="/edges",
         hint="Pick the 3 or more edges that end at the corner to blend")  # fmt: skip


@tool(
    effect="model",
    label="Blend corner with setbacks",
    summary="Blend corner of {edges} R{radius}, setback {setback}",
    keywords=(
        "blend corner",
        "corner setback",
        "vertex blend",
        "three blends meet",
        "smooth corner patch",
        "setback fillet",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"edges": ["e:4", "e:7", "e:9"], "radius": 5, "setback": 12},
)
def nx_create_blend_corner(
    tx: Tx,
    *,
    edges: Annotated[
        list[EdgeRef], Items(3, 8), Doc("Unblended edges of one body that meet at one vertex")
    ],
    radius: Annotated[Length, Doc("Blend radius of every edge, > 0")],
    setback: Annotated[
        Length,
        Doc("Distance along each edge from the vertex where the corner patch starts; > radius"),
    ],
    setback_faces: Annotated[
        Literal["with_corner", "separate"],
        Doc("with_corner: setback zones merge into the corner patch; separate: own faces"),
    ] = "with_corner",
) -> BlendCornerData:
    """Blend the edges meeting at a corner and set the blends back for a smooth corner patch.
    Every edge gets radius; each blend stops setback from the vertex (along the edge) and
    NX fills the corner where the blends meet (a cube corner: R5, setback 12). Edges
    already blended: delete or edit that blend first. Plain blends: nx_create_blend."""
    r, s = float(radius), float(setback)
    if r <= 0:
        fail("NX_INVALID_ARGUMENT", "radius must be positive", field="/radius")
    if s <= r:
        fail("NX_INVALID_ARGUMENT", "setback must be larger than radius", field="/setback",
             hint="A setback at or under the radius leaves no room for a corner patch")  # fmt: skip
    with tx.checks():
        picked = select.edges(tx, edges, "/edges")
        body = common.one_body(picked, "/edges")
        corner, at_start = _corner(tx, picked)
        for i, e in enumerate(picked):
            length = measure.length(tx, e)
            if s >= length:
                fail("NX_INVALID_ARGUMENT", f"setback {s:g} is not shorter than edge {i} "
                     f"({length:g})", field="/setback", hint=SETBACK_HINT)  # fmt: skip
    before = measure.volume(tx, body)
    part = tx.part
    E = tx.nx.Features.EdgeBlendBuilder
    try:
        with builders.build(tx, part.Features.CreateEdgeBlendBuilder, None) as b:
            b.Tolerance = units.part_tolerance(tx)
            b.AddChainset(builders.collector(tx, picked, "Edge"), expr.rhs(radius))
            b.SetbackOption = getattr(E.Setback, SETBACK_FACES[setback_faces])
            b.AddSetbackData(list(picked), at_start, [expr.rhs(setback)] * len(picked))
            feature = b.commit()
    except NXError as error:
        if error.code == "NX_BUILD_FAILED" and not error.hint:
            error.hint = SETBACK_HINT
        raise
    alive = {int(x.Tag) for x in part.Bodies}
    made = [x for x in common.feature_bodies(feature) if int(x.Tag) in alive] or [body]
    common.sound(tx, made)
    out = common.result_of(tx, feature, made)
    common.changed(before, out["volume"], "differs")
    faces = list(feature.GetFaces()) if hasattr(feature, "GetFaces") else []
    # a plain vertex blend has one face per edge plus one corner face
    readback.expect(
        len(faces) > len(picked) + 1, "corner_faces", "NX made no setback corner patch",
        blend_faces=len(faces),
    )  # fmt: skip
    return {
        **out,
        "corner": corner,
        "blend_faces": len(faces),
        "area": measure.area(tx, faces),
    }
