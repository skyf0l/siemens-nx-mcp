"""Synchronous Reorder Blends (tool-surface 3.6): where a convex and a concave blend overflow
each other at a corner, swap which one NX applies first, so the corner takes the other shape.
Works on imported (unparameterized) solids and on blends in history alike.

NX 2406 lessons (probed live): ``ReorderBlendsBuilder`` takes one face of each blend in
``BlendFace1``/``BlendFace2`` (``ScCollector``); the order of the two does not matter.
``GetBlendface2Candidate(face)`` returns the one overflowing blend face of opposite convexity,
None when there are several, and refuses 3190182 "Blend 1 does not overflow a blend of
opposite convexity" for a face without one (a plane, two blends of the same convexity).
A pair that does not interact fails the commit with 3190185 "The blends cannot be
reordered". A reorder keeps the face count and changes volume and area; reordering the new
pair again restores the first corner exactly (measured to 1e-6). ``Validate`` is not
implemented on this builder.
"""

from __future__ import annotations

from typing import Annotated

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, FaceRef
from nx_mcp.domains.modeling import common, sync_common, sync_relate
from nx_mcp.kit import bodies as kbodies
from nx_mcp.kit import builders, licenses, measure, readback, select

MODELING_LICENSES = ("solid_modeling", "cam_base", "insp_programming")
NO_OVERFLOW = 3190182  # GetBlendface2Candidate: no blend of opposite convexity overflows it
NOT_REORDERABLE = 3190185  # Commit: the two blends do not interact
CODES = {NO_OVERFLOW: "NX_WRONG_GEOMETRY", NOT_REORDERABLE: "NX_WRONG_GEOMETRY"}
HINT = (
    "Pass a face of each of two blends of opposite convexity (one convex, one concave) that "
    "run into each other at a corner; nx_find_geometry lists a body's faces"
)
CLASH_HINT = "NX made the corner overlap itself; the reorder was rolled back"


class BlendOrderData(common.FeatureData):
    volume_change: Annotated[float, Doc("Volume after minus before (the corner's change)")]
    area_change: Annotated[float, Doc("Surface area after minus before")]
    face_count: Annotated[int, Doc("Faces of the body after the reorder")]


def _partner(tx, builder, face):
    """The blend face NX finds overflowing ``face`` (opposite convexity), refusing as
    ``NX_WRONG_GEOMETRY`` (none) or ``NX_INVALID_ARGUMENT`` on ``/other`` (several)."""
    try:
        found = builder.GetBlendface2Candidate(face)
    except Exception as error:
        if int(getattr(error, "ErrorCode", 0) or 0) != NO_OVERFLOW:
            raise
        fail("NX_WRONG_GEOMETRY", "No blend of opposite convexity overflows this face",
             field="/face", hint=HINT)  # fmt: skip
    if found is None:
        fail("NX_INVALID_ARGUMENT", "Several blends overflow this face: pass other",
             field="/other", hint="Pass other: a face of the blend to swap with")  # fmt: skip
    return found


@tool(
    effect="model",
    label="Reorder blends",
    summary="Reorder the blends at {face}",
    keywords=(
        "reorder blends",
        "blend order",
        "swap blends",
        "fillet order",
        "blend corner shape",
        "overflow blend",
        "synchronous",
    ),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"face": "fc:12"},
)
def nx_edit_blend_order(
    tx: Tx,
    *,
    face: Annotated[FaceRef, Doc("A face of one blend (convex or concave) at the corner")],
    other: Annotated[
        FaceRef, Doc("A face of the opposite-convexity blend it overflows (default: NX finds it)")
    ]
    | None = None,
) -> BlendOrderData:
    """Swap which of two overflowing blends a solid applies first, so their corner changes shape.
    Synchronous Reorder Blends: the blends must be of opposite convexity (one convex, one
    concave) and run into each other; imported solids too. Repeat on the new pair to restore
    the first shape. Not the feature tree order (nx_set_feature_order)."""
    with tx.checks():  # lookups and refusals: nothing changed yet (not_started)
        chosen = select.faces(tx, [face], "/face")
        partner = select.faces(tx, [other], "/other") if other is not None else []
        body = common.one_body([*chosen, *partner], "/other" if partner else "/face")
        if not getattr(body, "IsSolidBody", True):
            fail("NX_WRONG_GEOMETRY", "Select faces of a solid body", field="/face")
        if partner and int(partner[0].Tag) == int(chosen[0].Tag):
            fail("NX_INVALID_ARGUMENT", "other must be a face of the other blend",
                 field="/other", hint=HINT)  # fmt: skip
        sync_relate.require_synchronous(tx, "Reorder Blends")
        licenses.require(tx, MODELING_LICENSES, "Reorder Blends needs a solid modeling license")
    before = measure.mass(tx, [body])
    faulty = kbodies.faulty(tx, [body])
    overlapping = kbodies.clashing(tx, [body])
    with builders.build(tx, tx.part.Features.CreateReorderBlendsBuilder, None, codes=CODES) as b:
        if not partner:
            with tx.checks():  # a lookup on the builder: nothing changed yet
                partner = [_partner(tx, b, chosen[0])]
        builders.fill(tx, b.BlendFace1, chosen, "Face")
        builders.fill(tx, b.BlendFace2, partner, "Face")
        feature = b.commit()
    readback.expect(feature is not None, "feature", "NX built no Reorder Blends feature")
    sync_common.consistent(tx, body, "The reorder")
    common.sound(tx, [body], known=faulty)
    common.no_clash(tx, [body], overlapping, hint=CLASH_HINT)
    out = common.result_of(tx, feature, [body])
    common.changed(before["volume"], out["volume"], "differs")
    after = measure.mass(tx, [body])
    area = 0.0
    if before["area"] is not None and after["area"] is not None:
        area = after["area"] - before["area"]
    return {
        **out,
        "volume_change": out["volume"] - before["volume"],
        "area_change": area,
        "face_count": len(body.GetFaces()),
    }
