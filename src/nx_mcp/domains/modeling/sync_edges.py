"""Synchronous Move Edge and Offset Edge (tool-surface 3.6): reshape a solid by moving its
edges, the faces around them following, for imported (dumb) solids too.

NX 2406 lessons (probed live): both builders come from
``Features.SynchronousEdgeCollection`` and take the edges in an ``ScCollector``. Move Edge
moves them by a ``ModlMotion`` distance (a top edge raised by 3 tilts its side face). Offset
Edge slides an edge across one of its two faces by ``Distance`` (> 0 grows that face, < 0
shrinks it); which face is NX's own choice, ``ReverseOffsetSide`` takes the other one and
``ReverseOffsetDirection`` changes nothing. Method AlongPlaneofEdge refuses straight edges
("The edges must be planar"), so it is not offered. The edges keep their tags.
"""

from __future__ import annotations

from typing import Annotated, Literal

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Direction, Doc, EdgeRef, FaceRef, Items, Length
from nx_mcp.domains.modeling import common, sync_common, sync_relate
from nx_mcp.kit import builders, expr, geom, measure, readback, select


class EditEdgesData(common.FeatureData):
    volume_change: Annotated[float, Doc("Volume after minus before")]
    moved: Annotated[list[float], Doc("How far each straight edge moved, across its line")]


def _middles(tx, edges) -> dict:
    """``{tag: (midpoint, unit direction)}`` of the straight edges."""
    linear = tx.nx.Edge.EdgeType.Linear
    out = {}
    for edge in edges:
        if getattr(edge, "SolidEdgeType", None) == linear:
            start, end = (geom.xyz(v) for v in edge.GetVertices())
            out[int(edge.Tag)] = (
                geom.scale(geom.add(start, end), 0.5),
                geom.unit(geom.sub(end, start)),
            )
    return out


def _across(d, along):
    """``d`` without its component along the edge direction ``along``."""
    return geom.sub(d, geom.scale(along, geom.dot(d, along)))


def _shifts(tx, edges, before) -> list:
    """``[(index, displacement across the edge)]`` of the straight edges' midpoints."""
    after = _middles(tx, [e for e in edges if int(e.Tag) in before])
    out = []
    for i, e in enumerate(edges):
        tag = int(e.Tag)
        if tag in after:
            out.append((i, _across(geom.sub(after[tag][0], before[tag][0]), before[tag][1])))
    return out


def _on_side(shifts, normal, scale) -> bool:
    """Every straight edge slid within the plane of ``normal`` (the along face)."""
    return all(abs(geom.dot(d, normal)) <= 1e-6 * scale for _, d in shifts)


def _build(tx, action, edges, a, reverse=False):
    if action == "move":
        factory = tx.part.Features.SynchronousEdgeCollection.CreateMoveEdgeBuilder
    else:
        factory = tx.part.Features.SynchronousEdgeCollection.CreateOffsetEdgeBuilder
    with builders.build(tx, factory, None) as b:
        if action == "move":
            builders.fill(tx, b.EdgeToMove, edges, "Edge")
            sync_common.distance_motion(tx, b.Motion, a["vector"], a["distance"])
        else:
            builders.fill(tx, b.EdgeToOffset, edges, "Edge")
            expr.drive(b.Distance, a["distance"])
            b.ReverseOffsetSide = reverse
        return b.commit()


@tool(
    effect="model",
    label="Move or offset edges",
    summary="{action} {edges}",
    keywords=("move edge", "offset edge", "tilt face", "taper face", "synchronous", "imported"),
    requires=("work_part", "no_active_sketch"),
    variants="action",
    cost="heavy",
    previewable=True,
    example={"edges": ["ed:4"], "action": "move", "distance": 3, "direction": "z"},
)
def nx_edit_edges(
    tx: Tx,
    *,
    edges: Annotated[list[EdgeRef], Items(1, 50), Doc("Edges of one solid")],
    action: Annotated[
        Literal["move", "offset"],
        Doc("move: along direction; offset: slide across a face of the edge"),
    ],
    distance: Annotated[
        Length, Doc("move: along direction; offset: > 0 grows the face, < 0 shrinks")
    ],
    direction: Annotated[Direction, Doc("move only (required): the motion direction")]
    | None = None,
    along: Annotated[FaceRef, Doc("offset only: planar face of every edge to slide across")]
    | None = None,
) -> EditEdgesData:
    """Move edges of a solid along a direction, or offset them across one of their faces
    (synchronous: imported solids too). The faces meeting at the edges tilt or stretch to follow,
    e.g. moving a top edge up 3 tilts the side face. offset without along slides across the face
    NX picks. Reads back each straight edge's displacement."""
    if (direction is not None) != (action == "move"):
        verb = "needs" if action == "move" else "does not take"
        fail("NX_INVALID_ARGUMENT", f"{action} {verb} direction", field="/direction",
             hint="move takes distance and direction; offset takes distance and along")  # fmt: skip
    if along is not None and action != "offset":
        fail("NX_INVALID_ARGUMENT", "along applies to offset", field="/along")
    if float(distance) == 0.0:
        fail("NX_INVALID_ARGUMENT", "distance must not be zero", field="/distance")
    with tx.checks():  # lookups and refusals: nothing changed yet (not_started)
        selected = select.edges(tx, edges, "/edges")
        body = common.one_body(selected, "/edges")
        a = {"distance": distance, "vector": None}
        if direction is not None:
            a["vector"] = select.direction(tx, direction, "/direction")
        normal = None
        if along is not None:
            face = select.one(tx, along, "face", field="/along")
            kind, _, normal = select.face_data(tx, face)
            if kind != select.FACE_PLANE:
                fail("NX_WRONG_GEOMETRY", "along must be a planar face", field="/along")
            normal = geom.unit(normal)
            for i, edge in enumerate(selected):
                if int(face.Tag) not in {int(f.Tag) for f in edge.GetFaces()}:
                    fail("NX_WRONG_GEOMETRY", "along must be a face of every edge",
                         field=f"/edges/{i}")  # fmt: skip
        sync_relate.require_synchronous(tx, f"{action.capitalize()} Edge")
    before = measure.volume(tx, body)
    middles = _middles(tx, selected)
    scale = max([1.0, *(abs(c) for m, _ in middles.values() for c in m)])
    if normal is None:
        feature = _build(tx, action, selected, a)
    else:  # NX picks the face: try its choice, take the other face when it missed along
        session = tx.session
        mark = session.SetUndoMark(tx.nx.Session.MarkVisibility.Invisible, "NX MCP offset edge")
        try:
            feature = _build(tx, action, selected, a)
            if not _on_side(_shifts(tx, selected, middles), normal, scale):
                session.UndoToMark(mark, None)
                feature = _build(tx, action, selected, a, reverse=True)
        finally:
            session.DeleteUndoMark(mark, None)
    sync_common.consistent(tx, body)
    shifts = _shifts(tx, selected, middles)
    tol = 1e-6 * scale
    for i, d in shifts:
        if action == "move":
            line = middles[int(selected[i].Tag)][1]
            wanted = _across(geom.scale(geom.unit(a["vector"]), float(distance)), line)
            readback.expect_close(d, wanted, f"moved[{i}]", tol=tol)
        else:
            readback.expect_close(geom.norm(d), abs(float(distance)), f"moved[{i}]", tol=tol)
    if normal is not None:
        readback.expect(_on_side(shifts, normal, scale), "along",
                        "NX slid the edges across the other face")  # fmt: skip
    out = common.result_of(tx, feature, [body])
    common.changed(before, out["volume"], "differs")
    return {
        **out,
        "volume_change": out["volume"] - before,
        "moved": [geom.norm(d) for _, d in shifts],
    }
