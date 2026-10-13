"""Synchronous Copy Face, Cut Face and Paste Face (tool-surface 3.6): copy or move a face
set such as a boss or a hole to another place on its body, or keep the copy as a sheet
and paste it into another body.

NX 2406 lessons (probed live): Copy/Cut Face take the faces in ``FaceToCopy``/``FaceToCut``
(``FaceRecognitionBuilder.FaceCollector``) and a ``ModlMotion``; with ``DoPaste`` the copy
is pasted into the source body (a copied boss adds its volume, a copied hole removes its
own), without it the faces become a new sheet body (Cut then removes and heals them).
Pasting into another body through ``TargetBody`` fails "A complete intersection cannot be
found", so a copy for another body is a sheet pasted with Paste Face, which consumes the
sheet. Paste Face Subtract with a sheet whose normals point out of the material keeps the
inside instead: the kept-material read-back refuses that.
"""

from __future__ import annotations

from typing import Annotated, Literal

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import BodyRef, Doc, FaceRef, Items
from nx_mcp.domains.modeling import bodies, common, sync_common, sync_relate
from nx_mcp.kit import builders, geom, measure, readback, result, select


class CopyFacesData(common.FeatureData):
    volume_change: Annotated[float, Doc("Volume of the source body after minus before")]
    sheet: Annotated[result.Row | None, Doc("paste false: the new sheet body holding the faces")]


@tool(
    effect="model",
    label="Copy or cut faces",
    summary="{mode} {faces}",
    keywords=(
        "copy face",
        "cut face",
        "paste face",
        "duplicate boss",
        "copy hole",
        "move feature",
        "synchronous",
    ),
    requires=("work_part", "no_active_sketch"),
    variants="mode",
    cost="heavy",
    previewable=True,
    example={"faces": ["fc:9", "fc:10"], "mode": "copy", "transform": {"translation": [20, 0, 0]}},
)
def nx_create_face_copy(
    tx: Tx,
    *,
    faces: Annotated[list[FaceRef], Items(1, 200), Doc("Faces of one solid (a boss, a hole)")],
    mode: Annotated[
        Literal["copy", "cut"], Doc("copy keeps the faces; cut removes them from their place")
    ],
    transform: bodies.Motion,
    paste: Annotated[
        bool, Doc("Paste into the same body; false: keep them as a sheet body")
    ] = True,
) -> CopyFacesData:
    """Copy or cut a face set (a boss, a hole, a pocket) of a solid to another place on its body.
    Synchronous: imported solids too. transform: rotate (matrix about [0, 0, 0] or angle
    about an axis), then translate. paste false keeps the moved faces as a sheet body (cut also
    heals the hole they leave) for nx_create_face_paste into another body."""
    translation, rotation = bodies._pose(tx, transform)
    if not bodies._moves(translation, rotation) and paste:
        fail("NX_INVALID_ARGUMENT", "This transform would not move the faces",
             field="/transform", hint="For example " + bodies.MOTION_EXAMPLE)  # fmt: skip
    with tx.checks():  # lookups and refusals: nothing changed yet (not_started)
        selected = select.faces(tx, faces, "/faces")
        body = common.one_body(selected, "/faces")
        if not getattr(body, "IsSolidBody", True):
            fail("NX_WRONG_GEOMETRY", "Select faces of a solid body", field="/faces")
        sync_relate.require_synchronous(tx, f"{mode.capitalize()} Face")
    low, high = measure.bounds(tx, selected)
    before = measure.volume(tx, body)
    known = common.body_tags(tx)
    factory = tx.part.Features.CreateCopyFaceBuilder
    if mode == "cut":
        factory = tx.part.Features.CreateCutFaceBuilder
    with builders.build(tx, factory, None) as b:
        held = b.FaceToCopy if mode == "copy" else b.FaceToCut
        builders.fill(tx, held.FaceCollector, selected, "Face")
        sync_common.csys_motion(tx, b.Motion, translation, rotation)
        b.DoPaste = paste
        feature = b.commit()
    sync_common.consistent(tx, body)
    sheet = None
    if not paste:
        made = [x for x in tx.part.Bodies if int(x.Tag) not in known]
        sheets = [x for x in made if not getattr(x, "IsSolidBody", False)]
        readback.expect(len(sheets) == 1, "sheet", "NX made no sheet body of the faces",
                        bodies=len(made))  # fmt: skip
        if sync_common.is_identity(rotation):
            wanted = sync_common.translated_box(low, high, translation)
            got = measure.bounds(tx, sheets)
            tol = 1e-6 * max(1.0, *map(abs, [*wanted[0], *wanted[1]]))
            readback.expect_close([*got[0], *got[1]], [*wanted[0], *wanted[1]], "placed", tol=tol)
        sheet = tx.ref(sheets[0], kind="body")
    out = common.result_of(tx, feature, [body])
    if mode == "copy" and paste:
        common.changed(before, out["volume"], "differs")
    if mode == "cut" and not paste:
        common.changed(before, out["volume"], "differs")
    return {**out, "volume_change": out["volume"] - before, "sheet": sheet}


PASTE_TYPES = {"auto": "Automatic", "add": "Add", "subtract": "Subtract"}


def _inside(tx, body, point) -> bool:
    """Whether ``point`` lies inside the solid (UF point containment 1)."""
    return int(tx.uf.Modeling.AskPointContainment([float(c) for c in point], body.Tag)) == 1


@tool(
    effect="model",
    label="Paste faces into a body",
    summary="Paste {tools} into {target}",
    keywords=("paste face", "paste sheet", "transplant boss", "add sheet to solid", "synchronous"),
    requires=("work_part", "no_active_sketch"),
    variants="operation",
    cost="heavy",
    previewable=True,
    example={"target": "b:3", "tools": ["b:7"], "operation": "add"},
)
def nx_create_face_paste(
    tx: Tx,
    *,
    target: Annotated[BodyRef, Doc("Solid that receives the faces")],
    tools: Annotated[
        list[BodyRef], Items(1, 20), Doc("Sheet bodies whose open edges lie on target")
    ],
    operation: Annotated[
        Literal["auto", "add", "subtract"],
        Doc("add: grow material; subtract: cut it; auto: by the sheet normals"),
    ] = "auto",
) -> common.FeatureData:
    """Paste sheet bodies into a solid (synchronous Paste Face) to add or remove material.
    A copied boss adds material, a copied hole or pocket removes it. The sheets' open edges must lie on target's faces and are
    consumed. Make the sheets with nx_create_face_copy paste false (any face set moved to
    target). Subtract needs sheet normals pointing out of the material to remove (a copied
    hole's)."""
    with tx.checks():  # lookups and refusals: nothing changed yet (not_started)
        body = common.solid(tx, target, "/target")
        sheets = select.bodies(tx, tools, "/tools")
        for i, sheet in enumerate(sheets):
            if int(sheet.Tag) == int(body.Tag):
                fail("NX_INVALID_ARGUMENT", "target cannot also be a tool", field=f"/tools/{i}")
        sync_relate.require_synchronous(tx, "Paste Face")
    before = measure.mass(tx, [body])
    box = measure.bounds(tx, sheets)
    keep = before["centroid"]
    near = geom.tol(*box) * 10.0
    if keep is None or all(box[0][k] - near <= keep[k] <= box[1][k] + near for k in range(3)):
        keep = None  # the centroid lies among the tools: no point to hold on to
    elif not _inside(tx, body, keep):
        keep = None
    with builders.build(tx, tx.part.Features.CreatePasteFaceBuilder, None) as b:
        b.TargetBody.Value = body
        b.ToolBody.Add(sheets)
        b.PasteOption = getattr(tx.nx.Features.PasteFaceBuilder.PasteOptionType,
                                PASTE_TYPES[operation])  # fmt: skip
        feature = b.commit()
    sync_common.consistent(tx, body)
    out = common.result_of(tx, feature, [body])
    how = {"auto": "differs", "add": "more", "subtract": "less"}[operation]
    common.changed(before["volume"], out["volume"], how)
    if keep is not None:
        readback.expect(_inside(tx, body, keep), "kept", "NX removed material away from the "
                        "tools (sheet normals point the other way)", point=keep)  # fmt: skip
    return out
