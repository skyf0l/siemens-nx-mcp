"""Decal stickers: an image from the workspace placed on a face (tool-surface 3.12).

``part.Decals.CreateDecalStickerBuilder`` (NX 2406) places an image file on faces with a
planar, cylindrical or spherical projection. NX 2406 measured: ``Commit`` returns None and
the decal is stored as a visual material named ``DecalName`` (listed by
``GetMaterialsInPart``, ``Type`` 1 where library materials are 0), assigned to its faces
(UF ``Disp.AskGeometryOfMaterial``), with the image copied into the part (``ImagesData``),
so the part keeps it when the file changes. The sticker is placed by the manipulator
frame (``SetManipulatorData``: origin, X = image width direction, Z = face normal); the
``RotationAngle`` expression is ignored there, so ``rotation`` turns the frame instead.
Width and height are expressions in part units; ``LockAspectRatio`` keeps the image's
aspect when only the width is given. Read back: a builder opened on the decal gives its
name, image, faces, size and frame.

Decals show in the plain Studio style (``nx_capture_view(style='studio')``; NX's session
preference shows them in shaded views only when set); true shading hides them (NX 2406
measured, ``domains/display/visual.py``).
They are part data: model changes under the kernel's undo mark (``nx_undo_operation``
reverts one); ``nx_delete_decal`` removes them. NXOpen documents no licence for these
calls.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    Angle,
    Doc,
    FaceRef,
    Items,
    Length,
    Name,
    PathTo,
    Point3,
    Text,
)
from nx_mcp.domains.display.visual import is_decal, part_materials
from nx_mcp.kit import builders, display, facts, geom, readback, select
from nx_mcp.kit.result import Row

PROJECTIONS = {"planar": "Planar", "cylindrical": "Cylindrical", "spherical": "Spherical"}
ImagePath = Annotated[str, PathTo("read", ".png", ".jpg", ".bmp")]
DecalName = Annotated[str, Text(64), Doc("Decal name (nx_create_decal's data.decal.name)")]


class Decal(TypedDict):
    name: str
    image: str
    face: Row
    center: list[float]
    normal: list[float]
    width_direction: list[float]
    up: list[float]
    width: float
    height: float
    projection: str


class DecalData(TypedDict):
    decal: Decal
    projected: float
    marks_part_modified: bool


class DeletedDecals(TypedDict):
    deleted: list[str]
    count: int


def _value(expression) -> float:
    value = getattr(expression, "Value", None)
    return float(value if value is not None else expression.RightHandSide)


def _surface(tx, face, center):
    """``(point on the face nearest center, unit outward normal there)``."""
    modeling = tx.uf.Modeling
    with native(default="NX_ERROR"):
        uv, on = modeling.AskFaceParm(face.Tag, [float(v) for v in center])
        props = modeling.AskFaceProps(face.Tag, uv)
    return [float(v) for v in on], geom.unit([float(v) for v in props[5]], "normal")


def frame(normal, up=None, rotation=0.0, field="/up"):
    """Rows (width direction, up, normal) of a decal: ``up`` (default world +Z, or +Y on a
    face facing +-Z) projected onto the face plane, then turned ``rotation`` degrees
    counter-clockwise about the normal."""
    z = geom.unit(normal, "normal")
    wanted = list(up) if up is not None else ([0.0, 0.0, 1.0] if abs(z[2]) < 0.9 else [0, 1.0, 0])
    flat = geom.sub(wanted, geom.scale(z, geom.dot(wanted, z)))
    if geom.norm(flat) < 1e-6 * max(geom.norm(wanted), 1e-12):
        fail("NX_INVALID_ARGUMENT", "up is along the face normal", field=field,
             hint="Give an up direction that lies in the face (the image's top)")  # fmt: skip
    y = geom.unit(flat, field)
    x = geom.cross(y, z)
    angle = math.radians(float(rotation))
    c, s = math.cos(angle), math.sin(angle)
    x, y = (
        geom.add(geom.scale(x, c), geom.scale(y, s)),
        geom.add(geom.scale(x, -s), geom.scale(y, c)),
    )
    return [x, y, z]


def _find(tx, name):
    return next((m for m, n in part_materials(tx) if n == name and is_decal(m)), None)


def _read(tx, decal, face_kind="face") -> Decal:
    """Name, image, face, size and frame of ``decal`` from a builder opened on it."""
    sticker = tx.nx.Display.DecalStickerBuilder
    with builders.build(tx, tx.part.Decals.CreateDecalStickerBuilder, decal) as b:
        origin, m = b.GetManipulatorData()
        faces = list(b.FaceOrBody.GetArray())
        projection = next(
            (
                k
                for k, v in PROJECTIONS.items()
                if getattr(sticker.ProjectionTypes, v) == b.ProjectionType
            ),
            str(b.ProjectionType),
        )
        row: Decal = {
            "name": str(b.DecalName),
            "image": str(b.ImageName),
            "face": tx.ref(faces[0], face_kind) if faces else {},
            "center": geom.xyz(origin),
            "normal": [float(m.Zx), float(m.Zy), float(m.Zz)],
            "width_direction": [float(m.Xx), float(m.Xy), float(m.Xz)],
            "up": [float(m.Yx), float(m.Yy), float(m.Yz)],
            "width": _value(b.Width),
            "height": _value(b.Height),
            "projection": projection,
        }
    return row


@tool(
    effect="model",
    label="Create decal",
    summary=lambda a, say: f"Place decal {say('image')} on {say('face')}",
    keywords=("decal", "sticker", "logo", "image on face", "hull name", "livery", "label image"),
    requires=("work_part",),
    cost="light",
    invalidates="deleted",  # display data only: every reference stays valid
    example={"face": "fc:12", "image": "images/logo.png", "width": 40},
)
def nx_create_decal(
    tx: Tx,
    *,
    face: FaceRef,
    image: ImagePath,
    width: Length,
    height: Length | None = None,
    center: Point3 | None = None,
    up: Annotated[tuple[float, float, float], Doc("Image top direction")] | None = None,
    rotation: Angle = 0.0,
    name: Name | None = None,
    projection: Literal["planar", "cylindrical", "spherical"] = "planar",
) -> DecalData:
    """Place a workspace image (.png/.jpg/.bmp) on a face as a decal: logo, hull name, label.
    center: snapped onto the face (default: inside it). height omitted keeps the image's
    aspect. up: image top (default +Z, or +Y on faces facing +-Z), turned rotation degrees
    CCW about the normal. Shown in the Studio style with true shading off
    (nx_capture_view style=studio). Remove: nx_delete_decal."""
    w = float(width)
    h = None if height is None else float(height)
    if w <= 0 or (h is not None and h <= 0):
        fail("NX_INVALID_ARGUMENT", "width and height must be positive",
             field="/width" if w <= 0 else "/height")  # fmt: skip
    with tx.checks():
        display.shown_part(tx, work=True)
        target = select.resolve(tx, face, "face", field="/face")
        select.owned(tx, target, "/face", "face")
        if name is not None and any(n == name for _, n in part_materials(tx)):
            fail("NX_NAME_CONFLICT", f"The part already has a decal or material named {name!r}",
                 field="/name")  # fmt: skip
        start = list(center) if center is not None else facts.face_point(tx, target)
        point, normal = _surface(tx, target, start)
        rows = frame(normal, up, rotation)
    before = {int(m.Tag) for m, _ in part_materials(tx)}
    sticker = tx.nx.Display.DecalStickerBuilder
    was_modified = bool(tx.part.IsModified)
    factory = tx.part.Decals.CreateDecalStickerBuilder
    with builders.build(tx, factory, tx.nx.TaggedObject.Null) as b:
        b.FaceOrBody.Add(target)
        b.ImageName = str(Path(image))
        if name is not None:
            b.DecalName = name
        chosen = str(b.DecalName)
        b.ProjectionType = getattr(sticker.ProjectionTypes, PROJECTIONS[projection])
        b.AnchorType = sticker.AnchorTypes.MiddleCenter
        b.Origin = builders.point(tx, point)
        b.SetManipulatorData(tx.nx.Point3d(*point), builders.matrix(tx, geom.transpose(rows)))
        b.ScalingMethodType = sticker.ScalingMethodTypes.UserDefined
        b.LockAspectRatio = h is None
        b.Width.RightHandSide = repr(w)
        if h is not None:
            b.Height.RightHandSide = repr(h)
        b.UpdateDecalStickerOnImageDimensionChange()
        b.commit()
    decal = next(
        (m for m, n in part_materials(tx) if int(m.Tag) not in before and n == chosen), None
    ) or _find(tx, chosen)
    readback.expect(decal is not None, "decal", name=chosen)
    row = _read(tx, decal)
    readback.expect_close(row["width"], w, "width")
    if h is not None:
        readback.expect_close(row["height"], h, "height")
    readback.expect_same_direction(row["normal"], rows[2], "normal")
    readback.expect_same_direction(row["width_direction"], rows[0], "width_direction")
    readback.expect(row["face"].get("id") == tx.ref(target, "face")["id"], "face")
    return {
        "decal": row,
        "projected": geom.dist(start, point),
        "marks_part_modified": not was_modified and bool(tx.part.IsModified),
    }


@tool(
    effect="model",
    label="Delete decals",
    summary=lambda a, say: f"Delete decals {', '.join(a.get('names') or [])}",
    keywords=("delete decal", "remove decal", "remove sticker", "remove logo"),
    requires=("work_part",),
    cost="light",
    invalidates="deleted",  # display data only: every reference stays valid
    example={"names": ["HULL_LOGO"]},
)
def nx_delete_decal(
    tx: Tx,
    *,
    names: Annotated[list[DecalName], Items(1, 50)],
) -> DeletedDecals:
    """Delete decals of the work part by name (nx_list_visual_materials lists them in in_part
    with kind=decal). The image goes with them."""
    with tx.checks():
        found = []
        for i, text in enumerate(names):
            decal = _find(tx, text)
            if decal is None:
                fail(
                    "NX_NOT_FOUND",
                    f"The work part has no decal named {text!r}",
                    field=f"/names/{i}",
                    hint="nx_list_visual_materials lists the part's decals (kind decal)",
                )
            found.append(decal)
    collection = tx.part.VisualMaterialCollection
    for decal in found:
        with native(default="NX_ERROR"):
            collection.DeleteMaterial(decal)
    for text in names:
        readback.expect(_find(tx, text) is None, "deleted", name=text)
    return {"deleted": list(names), "count": len(names)}
