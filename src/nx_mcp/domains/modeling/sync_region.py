"""Synchronous Offset Region and Pull Face (tool-surface 3.6) for imported (dumb) solids.

NX 2406 lessons (probed live): Offset Region takes ``FaceToOffset``
(``FaceRecognitionBuilder.FaceCollector``), a positive ``Distance`` and ``ReverseDirection``
for inward; the adjacent faces stretch to follow (a block's top offset by 2 raises it by 2).
Pull Face takes ``FaceToPull`` (``ScCollector``) and a ``ModlMotion`` distance: it moves the
face and builds new side faces instead of moving the neighbours. Both keep the face tags.
"""

from __future__ import annotations

from typing import Annotated, Literal

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Direction, Doc, FaceRef, Items, Length
from nx_mcp.domains.modeling import common, sync_common, sync_relate
from nx_mcp.kit import builders, expr, geom, measure, select


class FaceRegionData(common.FeatureData):
    volume_change: Annotated[float, Doc("Volume after minus before")]
    offsets: Annotated[list[float], Doc("How far each planar face moved along its normal")]


@tool(
    effect="model",
    label="Offset or pull a face region",
    summary="{action} {faces}",
    keywords=("offset region", "pull face", "push pull", "thicken wall", "synchronous"),
    requires=("work_part", "no_active_sketch"),
    variants="action",
    cost="heavy",
    previewable=True,
    example={"faces": ["fc:9"], "action": "offset", "distance": 2},
)
def nx_edit_face_region(
    tx: Tx,
    *,
    faces: Annotated[list[FaceRef], Items(1, 200), Doc("Faces of one solid")],
    action: Annotated[
        Literal["offset", "pull"],
        Doc("offset: along the face normals; pull: along direction, new side faces"),
    ],
    distance: Annotated[Length, Doc("offset: > 0 outward, < 0 inward; pull: along direction")],
    direction: Annotated[Direction, Doc("pull only (required): the pull direction")] | None = None,
) -> FaceRegionData:
    """Offset a face region along its normals, or pull faces along a direction (synchronous).
    offset (Offset Region): adjacent faces stretch to follow; pull (Pull Face): new side faces,
    the neighbours stay. Imported solids too. For an offset face feature or a move use
    nx_edit_faces. Reads back each planar face's offset."""
    if (direction is not None) != (action == "pull"):
        verb = "needs" if action == "pull" else "does not take"
        fail("NX_INVALID_ARGUMENT", f"{action} {verb} direction", field="/direction",
             hint="pull takes distance and direction; offset takes distance only")  # fmt: skip
    if float(distance) == 0.0:
        fail("NX_INVALID_ARGUMENT", "distance must not be zero", field="/distance")
    with tx.checks():  # lookups and refusals: nothing changed yet (not_started)
        selected = select.faces(tx, faces, "/faces")
        body = common.one_body(selected, "/faces")
        vector = select.direction(tx, direction, "/direction") if direction is not None else None
    before = measure.volume(tx, body)
    planes = sync_common.planes(tx, selected)
    d = float(distance)
    if action == "offset":
        with (
            sync_relate.synchronous(tx),
            builders.build(tx, tx.part.Features.CreateAdmOffsetRegionBuilder, None) as b,
        ):
            builders.fill(tx, b.FaceToOffset.FaceCollector, selected, "Face")
            expr.drive(b.Distance, -d if d < 0 else distance)
            b.ReverseDirection = d < 0
            feature = b.commit()
        offsets = sync_common.moved_planes(tx, selected, planes, lambda n: d)
    else:
        with (
            sync_relate.synchronous(tx),
            builders.build(tx, tx.part.Features.CreatePullFaceBuilder, None) as b,
        ):
            builders.fill(tx, b.FaceToPull, selected, "Face")
            sync_common.distance_motion(tx, b.Motion, vector, distance)
            feature = b.commit()
        u = geom.unit(vector)
        offsets = sync_common.moved_planes(tx, selected, planes, lambda n: d * geom.dot(u, n))
    sync_common.consistent(tx, body)
    out = common.result_of(tx, feature, [body])
    common.changed(before, out["volume"], "differs")
    return {**out, "volume_change": out["volume"] - before, "offsets": offsets}
