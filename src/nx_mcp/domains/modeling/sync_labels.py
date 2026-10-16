"""Synchronous Label Chamfer and Group Face (tool-surface 3.6): mark faces of an imported
(dumb) solid as a chamfer or a face group, so later synchronous edits treat them as one
feature (a labelled chamfer keeps its size when its faces move; a rigid group moves as one).

NX 2406 lessons (probed live): Label Chamfer needs the chamfer face and its two construction
faces (the faces it was cut between), each in its own ``ScCollector``, else it refuses
"Incorrect object for this operation". Group Face takes ``FaceToGroup``
(``FaceRecognitionBuilder.FaceCollector``) and ``Rigid``. Neither changes the volume.

Not offered: Label Notch Blend refused every face tried, the concave round end of a slot and
a full-round convex end alike ("A face does not match the shape of a notch blend").
"""

from __future__ import annotations

from typing import Annotated, Literal

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, FaceRef, Items
from nx_mcp.domains.modeling import common, sync_common, sync_relate
from nx_mcp.kit import builders, measure, readback, result, select


class LabelData(common.FeatureData):
    faces: Annotated[list[result.Row], Doc("chamfer: the construction faces used (given or found)")]


def _construction(tx, chamfer):
    """The two faces the chamfer face meets along its longest edges (its strip sides)."""
    shared: dict[int, list] = {}
    for edge in chamfer.GetEdges():
        size = measure.length(tx, edge)
        for face in edge.GetFaces():
            if int(face.Tag) != int(chamfer.Tag):
                entry = shared.setdefault(int(face.Tag), [face, 0.0])
                entry[1] += size
    ranked = sorted(shared.values(), key=lambda e: -e[1])
    if len(ranked) < 2:
        fail("NX_WRONG_GEOMETRY", "The chamfer face has fewer than two neighbouring faces",
             field="/faces/0")  # fmt: skip
    return [ranked[0][0], ranked[1][0]]


def _adjacent(chamfer, sides) -> None:
    """Each ``between`` face must share an edge with the chamfer face (NX refuses others
    with 3190163 and no field)."""
    near = {int(f.Tag) for e in chamfer.GetEdges() for f in e.GetFaces()}
    for i, side in enumerate(sides):
        if int(side.Tag) not in near:
            fail("NX_WRONG_GEOMETRY", f"between[{i}] does not meet the chamfer face",
                 field=f"/between/{i}",
                 hint="Pass the two faces the chamfer face shares edges with, or omit between")  # fmt: skip


def _warn_duplicate(tx, feature, faces) -> None:
    """Warn when an earlier label of the same kind already holds one of ``faces``: NX
    accepts a second label of a labelled face silently."""
    kind = getattr(feature, "FeatureType", None)
    tags = {int(f.Tag) for f in faces}
    for other in tx.part.Features:
        tx.checkpoint()
        if other is feature or int(other.Tag) == int(feature.Tag):
            continue
        if kind is None or getattr(other, "FeatureType", None) != kind:
            continue
        try:
            held = {int(f.Tag) for f in other.GetFaces()}
        except Exception:  # NX cannot list the faces of this label: nothing to compare
            continue
        if held & tags:
            row = tx.ref(other, kind="feature")
            tx.warn("NX_NOTE", f"{row['name']} ({row['id']}) already labels these faces; this "
                    "call added a second label (delete one with nx_delete_objects)")  # fmt: skip
            return


@tool(
    effect="model",
    label="Label chamfer or group faces",
    summary="Label {faces} as {kind}",
    keywords=("label chamfer", "group face", "recognize chamfer", "face group", "synchronous"),
    requires=("work_part", "no_active_sketch"),
    variants="kind",
    cost="heavy",
    previewable=True,
    example={"faces": ["fc:9"], "kind": "chamfer"},
)
def nx_create_face_label(
    tx: Tx,
    *,
    faces: Annotated[
        list[FaceRef], Items(1, 100), Doc("chamfer: the one chamfer face; group: 2+ faces")
    ],
    kind: Annotated[
        Literal["chamfer", "group"], Doc("chamfer: a chamfer face; group: faces as one feature")
    ],
    between: Annotated[
        list[FaceRef], Items(2, 2), Doc("chamfer: the 2 faces it was cut between (default found)")
    ]
    | None = None,
    rigid: Annotated[bool, Doc("group: the faces keep their shape and move as one")] = False,
) -> LabelData:
    """Label a face of a solid as a chamfer, or several faces as one group (synchronous
    feature labels, no geometry change). Later synchronous edits such as nx_edit_faces then
    treat a labelled chamfer as a chamfer and a group as one feature. chamfer: without
    between, the two faces the chamfer face meets along its longest edges."""
    if kind == "chamfer" and len(faces) != 1:
        fail("NX_INVALID_ARGUMENT", "chamfer labels one face per call", field="/faces")
    if kind == "group" and len(faces) < 2:
        fail("NX_INVALID_ARGUMENT", "group needs at least two faces", field="/faces")
    if kind == "group" and between is not None:
        fail("NX_INVALID_ARGUMENT", "between applies to chamfer", field="/between")
    if kind == "chamfer" and rigid:
        fail("NX_INVALID_ARGUMENT", "rigid applies to group", field="/rigid")
    with tx.checks():  # lookups and refusals: nothing changed yet (not_started)
        selected = select.faces(tx, faces, "/faces")
        body = common.one_body(selected, "/faces")
        sides = []
        if kind == "chamfer":
            if between is not None:
                sides = select.faces(tx, between, "/between")
                for i, side in enumerate(sides):
                    if int(side.Tag) == int(selected[0].Tag):
                        fail("NX_INVALID_ARGUMENT", "between must not hold the chamfer face",
                             field=f"/between/{i}")  # fmt: skip
                common.one_body([selected[0], *sides], "/between")
                _adjacent(selected[0], sides)
            else:
                sides = _construction(tx, selected[0])
    before = measure.volume(tx, body)
    count = len(body.GetFaces())
    if kind == "chamfer":
        with (
            sync_relate.synchronous(tx),
            builders.build(tx, tx.part.Features.CreateLabelChamferBuilder, None) as b,
        ):
            builders.fill(tx, b.ChamferFace, selected, "Face")
            builders.fill(tx, b.ConstructionFace1, [sides[0]], "Face")
            builders.fill(tx, b.ConstructionFace2, [sides[1]], "Face")
            feature = b.commit()
    else:
        with (
            sync_relate.synchronous(tx),
            builders.build(tx, tx.part.Features.CreateGroupFaceBuilder, None) as b,
        ):
            builders.fill(tx, b.FaceToGroup.FaceCollector, selected, "Face")
            b.Rigid = rigid
            feature = b.commit()
    readback.expect(feature is not None, "feature", "NX built no label feature")
    _warn_duplicate(tx, feature, selected)
    sync_common.consistent(tx, body, "The label")
    out = common.result_of(tx, feature, [body])
    common.changed(before, out["volume"], "same")
    after = len(body.GetFaces())
    readback.expect(after == count, "faces", "The label changed the faces", before=count,
                    after=after)  # fmt: skip
    return {**out, "faces": [tx.ref(f, kind="face") for f in sides]}
