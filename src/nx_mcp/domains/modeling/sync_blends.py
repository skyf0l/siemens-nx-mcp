"""Synchronous Reorder Blends (tool-surface 3.6): where a convex and a concave blend overflow
each other at a corner, swap which one NX applies first, so the corner takes the other shape.
Works on imported (unparameterized) solids and on blends in history alike.

NX 2406 lessons (probed live): ``ReorderBlendsBuilder`` takes one face of each blend in
``BlendFace1``/``BlendFace2`` (``ScCollector``). An explicit second face must be the one
where the blends meet (a corner torus): another face of that blend fails 3190185, so the
tool also tries the swapped pair and NX's own candidates before refusing.
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
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, FaceRef
from nx_mcp.domains.modeling import common, sync_common, sync_relate
from nx_mcp.kit import bodies as kbodies
from nx_mcp.kit import builders, measure, readback, result, select

NO_OVERFLOW = 3190182  # GetBlendface2Candidate: no blend of opposite convexity overflows it
NOT_REORDERABLE = 3190185  # Commit: the two blends do not interact
CODES = {NO_OVERFLOW: "NX_WRONG_GEOMETRY", NOT_REORDERABLE: "NX_WRONG_GEOMETRY"}
HINT = (
    "Pass a face of each of two blends of opposite convexity (one convex, one concave) that "
    "run into each other at a corner; nx_find_geometry lists a body's faces"
)
CLASH_HINT = "NX made the corner overlap itself; the reorder was rolled back"
OTHER_HINT = (
    "Pass as other the other blend's face where the two blends meet (the corner face), or "
    "omit other and NX finds it; after a reorder, call again with face on the other blend"
)


class BlendOrderData(common.FeatureData):
    face: Annotated[result.Row, Doc("BlendFace1 NX reordered with")]
    partner: Annotated[result.Row, Doc("BlendFace2 NX reordered with")]
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
             field="/face", hint=HINT + "; after a reorder, pass a face of the other blend")  # fmt: skip
    if found is None:
        fail("NX_INVALID_ARGUMENT", "Several blends overflow this face: pass other",
             field="/other", hint="Pass other: the other blend's face at the corner")  # fmt: skip
    return found


def _candidate(builder, face):
    """NX's overflowing partner of ``face``, or None (none, several, or refused)."""
    try:
        return builder.GetBlendface2Candidate(face)
    except Exception:  # 3190182: no blend of opposite convexity overflows it
        return None


def _attempt(tx, first, second):
    """Commit one Reorder Blends of the pair: ``(feature, None)``, or ``(None, error)`` when
    NX refuses the pair as not reorderable (3190185)."""
    tx.checkpoint()
    try:
        with builders.build(
            tx, tx.part.Features.CreateReorderBlendsBuilder, None, codes=CODES
        ) as b:
            builders.fill(tx, b.BlendFace1, [first], "Face")
            builders.fill(tx, b.BlendFace2, [second], "Face")
            return b.commit(), None
    except NXError as error:
        if error.nx_code != NOT_REORDERABLE:
            raise
        return None, error


def _reorder(tx, chosen, partner, given: bool):
    """Commit the first pair NX reorders; ``(feature, (face1, face2))``. With an explicit
    other, a refused pair gives way to the swapped pair, then to NX's own partner of each
    face; the last refusal says what to pass."""
    last = None
    tried = set()
    pairs = [(chosen, partner), (partner, chosen)] if given else [(chosen, partner)]
    lookups = [chosen, partner] if given else []
    while pairs or lookups:
        if not pairs:
            face = lookups.pop(0)
            with builders.build(tx, tx.part.Features.CreateReorderBlendsBuilder, None) as b:
                found = _candidate(b, face)
            if found is not None:
                pairs.append((face, found))
            continue
        first, second = pairs.pop(0)
        key = (int(first.Tag), int(second.Tag))
        if key in tried or key[0] == key[1]:
            continue
        tried.add(key)
        feature, last = _attempt(tx, first, second)
        if feature is not None:
            return feature, (first, second)
    assert last is not None  # the first pair is always tried
    last.field = last.field or ("/other" if given else "/face")
    last.hint = last.hint or OTHER_HINT
    raise last


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
        FaceRef, Doc("The opposite blend's face where the two meet (corner face); omit: NX finds")
    ]
    | None = None,
) -> BlendOrderData:
    """Swap which of two overflowing blends a solid applies first, so their corner changes shape.
    Synchronous Reorder Blends: the blends must be of opposite convexity (one convex, one
    concave) and run into each other; imported solids too. To restore the first shape, call
    again with face on the other blend (partner). Not the feature tree order
    (nx_set_feature_order)."""
    with tx.checks():  # lookups and refusals: nothing changed yet (not_started)
        chosen = select.faces(tx, [face], "/face")
        partner = select.faces(tx, [other], "/other") if other is not None else []
        body = common.one_body([*chosen, *partner], "/other" if partner else "/face")
        if not getattr(body, "IsSolidBody", True):
            fail("NX_WRONG_GEOMETRY", "Select faces of a solid body", field="/face")
        if partner and int(partner[0].Tag) == int(chosen[0].Tag):
            fail("NX_INVALID_ARGUMENT", "other must be a face of the other blend",
                 field="/other", hint=HINT)  # fmt: skip
    before = measure.mass(tx, [body])
    faulty = kbodies.faulty(tx, [body])
    overlapping = kbodies.clashing(tx, [body])
    with sync_relate.synchronous(tx):
        if not partner:
            lookup = tx.part.Features.CreateReorderBlendsBuilder
            # A lookup on a builder that is never committed: nothing changed yet (not_started).
            with builders.build(tx, lookup, None, codes=CODES) as b, tx.checks():
                partner = [_partner(tx, b, chosen[0])]
        feature, used = _reorder(tx, chosen[0], partner[0], given=other is not None)
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
        "face": tx.ref(used[0], kind="face"),
        "partner": tx.ref(used[1], kind="face"),
        "volume_change": out["volume"] - before["volume"],
        "area_change": area,
        "face_count": len(body.GetFaces()),
    }
