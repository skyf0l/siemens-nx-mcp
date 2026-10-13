"""Repair features: patch a body with a sheet, delete a body in history, unsew, untrim.

NX 2406 lessons kept here (probed live on 40 x 30 x 20 blocks):

- Patch (``CreatePatchBuilder``): ``Target`` and ``Tool`` are ``SelectObject``s. Every
  edge of the tool sheet must lie on the target within ``Tolerance`` and form closed
  loops, else NX refuses ("Tool sheet edges are not close to target body or do not form
  a closed loop"). The target faces on the side the sheet normal points to are removed:
  a plane sheet at z=15 spanning the block (normal +z) leaves 18000 of 24000,
  ``ReverseDirection`` keeps the top slab (6000). ``CreateHolePatch`` with a cylinder
  sheet from the bottom to the top face cuts a hole (24000 - pi 5^2 20 = 22429.2);
  without it the same sheet keeps the inside (1570.8). The tool sheet is consumed.
- Delete Body (``CreateDeleteBodyBuilder``): ``BodyToDelete`` starts as None and takes a
  new collector; the feature's ``GetBodies`` lists the removed bodies, which leave
  ``Part.Bodies`` (suppress or delete the feature to bring them back).
- Unsew (``CreateUnsewBuilder``): ``ToolOption`` Face (default) or Edge; ``Output``
  OneBodyForConnectedFaces (default) or OneBodyForEachFace. Top + side faces of a solid:
  connected gives 2 sheets (4 + 2 faces), each face 3 sheets (4 + 1 + 1). The 4 edges
  round the top face split it off (2 sheets). ``KeepOriginal`` leaves the source body.
- Untrim (``CreateUntrimBuilder``): ``FacesToUntrim`` collector; one sheet per face. An
  unbounded surface comes back larger than its face: the plane of a 40 x 30 face gave an
  80 x 60 sheet (4800), a 20 mm tall hole's cylinder a 40 mm tall one.
- A refused Patch leaves NX's modeler state broken until its undo mark is rolled back
  (the next build fails "Cannot find NX object from Parasolid tag"); the kernel always
  rolls a failed call back.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import BodyRef, Doc, EdgeRef, FaceRef, Items, Length
from nx_mcp.domains.modeling import common
from nx_mcp.kit import builders, measure, readback, result, select, units

# Relative band for area sums that must match (unsew keeps every face).
AREA_REL = 1e-6
PATCH_HINT = (
    "Every edge of the sheet must lie on the body's faces (within tolerance) and close into "
    "loops: e.g. a plane sheet trimmed exactly to the body's side faces; raise tolerance "
    "for a small gap"
)


def _sheetlike(body) -> bool:
    flag = getattr(body, "IsSheetBody", None)
    return bool(flag) if flag is not None else not getattr(body, "IsSolidBody", True)


def _size(tx, body) -> float:
    """A solid's volume, or a sheet's area."""
    if _sheetlike(body):
        return measure.area(tx, list(body.GetFaces()))
    return measure.volume(tx, body)


def _alive(tx) -> set[int]:
    return {int(b.Tag) for b in tx.part.Bodies}


# ---------------------------------------------------------------------------
# Patch


class PatchData(TypedDict):
    feature: result.Row
    bodies: list[result.Row]
    sheet: bool  # the target is a sheet body (volume is then its area)
    volume: float  # target after: volume of a solid, area of a sheet
    volume_before: float
    faces: int


@tool(
    effect="model",
    label="Patch body with sheet",
    summary=lambda a, say: (
        f"Patch {say('body')} with {say('sheet')}" + (" (hole)" if a.get("hole") else "")
    ),
    keywords=(
        "patch body",
        "replace faces with sheet",
        "repair import",
        "sheet replaces faces",
        "hole patch",
        "add sheet region",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"body": "b:3", "sheet": "b:5"},
)
def nx_create_patch_body(
    tx: Tx,
    *,
    body: Annotated[BodyRef, Doc("Target solid or sheet body to patch")],
    sheet: Annotated[BodyRef, Doc("Tool sheet whose edges all lie on the target; consumed")],
    reverse: Annotated[
        bool, Doc("Remove the target faces against the sheet normal instead of along it")
    ] = False,
    hole: Annotated[
        bool, Doc("Hole patch: a closed tube sheet cuts a hole through the body")
    ] = False,
    tolerance: Annotated[Length, Doc("Largest gap between sheet edges and body")] | None = None,
) -> PatchData:
    """Replace faces of a body with a sheet whose edges all lie on it (Patch). The target
    faces on the side the sheet normal points to go; reverse drops the other side. A plane
    sheet spanning a block at z=15 (normal +z) trims the top off. hole=true with an open
    cylinder sheet from face to face cuts a hole. volume is a sheet target's area."""
    with tx.checks():
        target = select.one(tx, body, "body", field="/body")
        tool_sheet = select.one(tx, sheet, "body", field="/sheet")
        if int(tool_sheet.Tag) == int(target.Tag):
            fail("NX_INVALID_ARGUMENT", "The sheet must be another body", field="/sheet")
        if not _sheetlike(tool_sheet):
            fail("NX_WRONG_GEOMETRY", "The tool must be a sheet body", field="/sheet",
                 hint="Make a sheet (extrude curves as a sheet, offset or extract faces)")  # fmt: skip
        tol = units.part_tolerance(tx) if tolerance is None else float(tolerance)
        if tol <= 0:
            fail("NX_INVALID_ARGUMENT", "tolerance must be positive", field="/tolerance")
    sheet_target = _sheetlike(target)
    before = _size(tx, target)
    try:
        with builders.build(tx, tx.part.Features.CreatePatchBuilder, None) as b:
            b.Target.Value = target
            b.Tool.Value = tool_sheet
            b.Tolerance = tol
            b.ReverseDirection = bool(reverse)
            b.CreateHolePatch = bool(hole)
            feature = b.commit()
    except NXError as error:
        if error.code == "NX_BUILD_FAILED" and not error.hint:
            error.hint = PATCH_HINT
        raise
    alive = _alive(tx)
    made = [x for x in common.feature_bodies(feature) if int(x.Tag) in alive]
    if int(target.Tag) in alive and all(int(x.Tag) != int(target.Tag) for x in made):
        made.insert(0, target)
    readback.expect(bool(made), "bodies", "NX left no patched body")
    common.sound(tx, made)
    after = sum(_size(tx, x) for x in made)
    common.changed(before, after, "differs", check="area" if sheet_target else "volume")
    return {
        "feature": tx.ref(feature, kind="feature"),
        "bodies": common.body_rows(tx, made),
        "sheet": sheet_target,
        "volume": after,
        "volume_before": before,
        "faces": sum(len(list(x.GetFaces())) for x in made),
    }


# ---------------------------------------------------------------------------
# Delete Body


class DeleteBodyData(TypedDict):
    feature: result.Row
    deleted: list[result.Row]  # the bodies removed (gone while the feature is active)
    remaining: int  # bodies left in the work part


@tool(
    effect="model",
    label="Delete body feature",
    summary=lambda a, say: f"Delete {common.bodies_text(a)} as a feature",
    keywords=(
        "delete body",
        "remove body",
        "delete body feature",
        "remove tool body",
        "hide construction body",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    cost="light",
    previewable=True,
    example={"bodies": ["b:5"]},
)
def nx_create_delete_body(
    tx: Tx,
    *,
    bodies: Annotated[list[BodyRef], Items(1, 100), Doc("Solid or sheet bodies to remove")],
) -> DeleteBodyData:
    """Remove bodies with a Delete Body feature, keeping the features that made them (a
    construction or leftover tool body). Suppress or delete the returned feature to bring
    them back. nx_delete_objects deletes the making feature instead."""
    with tx.checks():
        targets = select.bodies(tx, bodies, "/bodies")
    rows = common.body_rows(tx, targets)
    with builders.build(tx, tx.part.Features.CreateDeleteBodyBuilder, None) as b:
        b.BodyToDelete = builders.collector(tx, targets, "Body")
        feature = b.commit()
    alive = _alive(tx)
    left = [rows[i]["id"] for i, x in enumerate(targets) if int(x.Tag) in alive]
    readback.expect(not left, "deleted", f"NX kept {', '.join(left)}", bodies=left)
    return {"feature": tx.ref(feature, kind="feature"), "deleted": rows, "remaining": len(alive)}


# ---------------------------------------------------------------------------
# Unsew


class UnsewData(TypedDict):
    feature: result.Row
    bodies: list[result.Row]  # the sheets NX made or kept (the rest of the source too)
    count: int
    area: float  # total area of the sheets
    kept_original: bool


def _one_body(objects, field):
    tags = {int(o.GetBody().Tag) for o in objects}
    if len(tags) != 1:
        fail("NX_INVALID_ARGUMENT", "Select faces or edges of one body", field=field)
    return objects[0].GetBody()


@tool(
    effect="model",
    label="Unsew body into sheets",
    summary={"faces": "Unsew {faces}", "edges": "Unsew along {edges}"},
    keywords=(
        "unsew",
        "split into sheets",
        "separate faces",
        "explode body to sheets",
        "extract faces as sheets",
        "split at edges",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    variants="by",
    required_by={"faces": ("faces",), "edges": ("edges",)},
    cost="heavy",
    previewable=True,
    example={"by": "faces", "faces": ["fc:7"]},
)
def nx_create_unsew(
    tx: Tx,
    *,
    by: Annotated[
        Literal["faces", "edges"], Doc("faces: split these faces off; edges: cut along edges")
    ] = "faces",
    faces: Annotated[list[FaceRef], Items(1, 200), Doc("by faces: faces of one body")]
    | None = None,
    edges: Annotated[
        list[EdgeRef], Items(1, 200), Doc("by edges: edges of one body; must cut regions off")
    ]
    | None = None,
    output: Annotated[
        Literal["connected", "each_face"],
        Doc("by faces: one sheet per connected face group, or one per face"),
    ] = "connected",
    keep_original: Annotated[bool, Doc("Leave the source body and unsew a copy")] = False,
) -> UnsewData:
    """Split a solid or sheet into separate sheet bodies (Unsew). by faces: the faces leave
    as sheets and the rest stays one sheet (a block's top face: 2 sheets). by edges: cut
    along edges that close round a region (the 4 edges of a top face). The source body
    becomes a sheet unless keep_original."""
    with tx.checks():
        if by == "faces":
            picked = select.faces(tx, faces or [], "/faces")
        else:
            picked = select.edges(tx, edges or [], "/edges")
        source = _one_body(picked, f"/{by}")
    before = measure.area(tx, list(source.GetFaces()))
    known = _alive(tx)
    U = tx.nx.Features.UnsewBuilder
    with builders.build(tx, tx.part.Features.CreateUnsewBuilder, None) as b:
        if by == "faces":
            b.ToolOption = U.UnsewTool.Face
            builders.fill(tx, b.FaceCollector, picked, "Face")
        else:
            b.ToolOption = U.UnsewTool.Edge
            builders.fill(tx, b.EdgeCollector, picked, "Edge")
        b.Output = (
            U.UnsewOutput.OneBodyForEachFace
            if output == "each_face"
            else U.UnsewOutput.OneBodyForConnectedFaces
        )
        b.KeepOriginal = bool(keep_original)
        feature = b.commit()
    alive = _alive(tx)
    made = [x for x in common.feature_bodies(feature) if int(x.Tag) in alive]
    have = {int(x.Tag) for x in made}
    made += [x for x in tx.part.Bodies if int(x.Tag) not in known | have]
    if not keep_original and int(source.Tag) in alive and int(source.Tag) not in have:
        made.insert(0, source)  # the rest of the source, now a sheet
    readback.expect(len(made) >= 2, "bodies", "NX did not split the body", bodies=len(made))
    readback.expect(all(_sheetlike(x) for x in made), "sheets", "Unsew left a solid body")
    area = measure.area(tx, [f for x in made for f in x.GetFaces()])
    readback.expect_close(area, before, "area", tol=AREA_REL * max(1.0, before))
    if keep_original:
        readback.expect(int(source.Tag) in alive, "kept_original", "NX removed the source body")
    return {
        "feature": tx.ref(feature, kind="feature"),
        "bodies": common.body_rows(tx, made),
        "count": len(made),
        "area": area,
        "kept_original": bool(keep_original),
    }


# ---------------------------------------------------------------------------
# Untrim


class UntrimData(TypedDict):
    feature: result.Row
    bodies: list[result.Row]  # one sheet per face
    area: float  # the untrimmed sheets' total
    trimmed_area: float  # the input faces' total


@tool(
    effect="model",
    label="Untrim faces to sheets",
    summary="Untrim {faces}",
    keywords=(
        "untrim",
        "restore underlying surface",
        "remove trim boundary",
        "full surface of face",
        "fill hole in face",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"faces": ["fc:7"]},
)
def nx_create_untrim(
    tx: Tx,
    *,
    faces: Annotated[list[FaceRef], Items(1, 100), Doc("Faces whose full surface to rebuild")],
    hide_original: Annotated[bool, Doc("Hide the faces' source body")] = False,
) -> UntrimData:
    """Make a new sheet per face from its underlying surface without trim edges or holes
    (Untrim). The source body is unchanged. Planes and cylinders, being unbounded, come
    back larger than the face (a 40 x 30 face gave 80 x 60)."""
    with tx.checks():
        picked = select.faces(tx, faces, "/faces")
    trimmed = measure.area(tx, picked)
    known = _alive(tx)
    with builders.build(tx, tx.part.Features.CreateUntrimBuilder, None) as b:
        builders.fill(tx, b.FacesToUntrim, picked, "Face")
        b.Associative = True
        b.HideOriginal = bool(hide_original)
        feature = b.commit()
    alive = _alive(tx)
    made = [x for x in common.feature_bodies(feature) if int(x.Tag) in alive]
    have = {int(x.Tag) for x in made}
    made += [x for x in tx.part.Bodies if int(x.Tag) not in known | have]
    readback.expect(bool(made), "bodies", "NX built no sheet")
    area = measure.area(tx, [f for x in made for f in x.GetFaces()])
    readback.expect(
        area >= trimmed * (1 - AREA_REL), "area", "The untrimmed sheets are smaller than the faces",
        area=area, trimmed_area=trimmed,
    )  # fmt: skip
    return {
        "feature": tx.ref(feature, kind="feature"),
        "bodies": common.body_rows(tx, made),
        "area": area,
        "trimmed_area": trimmed,
    }
