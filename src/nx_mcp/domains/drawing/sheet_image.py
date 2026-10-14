"""Raster images on a drawing sheet (a company logo in the title block): place, move, resize,
rotate or replace one. Delete with nx_delete_objects (kind ``annotation``).

NX 2406 findings encoded here (``Annotations.DraftingImages.CreateDraftingImageBuilder``;
``builder.Image`` is an ``Annotations.RasterImageBuilder``):

- The image is sized and placed only by ``SetCornerPoints`` (four corners counter-clockwise,
  the image's bottom-left first, part units on the sheet); NX stores them as given, also a
  rotated rectangle and a ratio other than the pixels' (``LockAspectRatio`` is only the flag
  NX's dialog uses for later resizing). NX offers no natural size: ``GetCornerPoints``,
  ``ResetImageSize`` and ``SizeOption.ImageSize`` raise "Corner points do not form a
  rectangular boundary" before corners are set, so the pixel size comes from the file header.
- A builder made with a null image creates a ``DraftingImage`` at once; ``Destroy()`` without
  a commit leaves it as a corrupt image ("Image object is corrupt. Try Part Cleanup"). The
  commit returns that same object; a failed call is undone to the call's mark, which removes
  it. A builder on an existing image creates nothing and commits the same tag.
- NX 2406 reads PNG, JPEG and TIFF (``DraftingImage.FileType``); a BMP commit crashes NX
  ("Internal error: memory access violation"), so other formats are refused before NX.
- ``RotateAngleOfReferenceVector`` reads 0 for a rotated image and ``AnnotationOrigin`` is
  (0, 0, 0): the placement is read back from the corners. UF ``ModlGeneral.AskBoundingBox``
  boxes the image on the sheet; the image is view dependent in the sheet's own view.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Angle, Doc, PathTo, RefTo, SheetLength, SheetPoint
from nx_mcp.domains.drawing.common import SheetArg, open_or_only_sheet, sheet_of, update
from nx_mcp.kit import builders, drawing, files, licenses, readback, result, select

SUFFIXES = (".png", ".jpg", ".jpeg", ".tif", ".tiff")
FORMATS = {".png": "png", ".jpg": "jpg", ".jpeg": "jpg", ".tif": "tif", ".tiff": "tif"}
FILE_TYPES = {"png": "Png", "jpg": "Jpg", "tif": "Tif"}  # DraftingImage.FileType members
MAX_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 20000
# Anchor -> its place on the image as fractions of (width, height) from the bottom-left.
ANCHORS = {
    "bottom_left": (0.0, 0.0),
    "bottom_center": (0.5, 0.0),
    "bottom_right": (1.0, 0.0),
    "middle_left": (0.0, 0.5),
    "center": (0.5, 0.5),
    "middle_right": (1.0, 0.5),
    "top_left": (0.0, 1.0),
    "top_center": (0.5, 1.0),
    "top_right": (1.0, 1.0),
}
Anchor = Literal[
    "bottom_left",
    "bottom_center",
    "bottom_right",
    "middle_left",
    "center",
    "middle_right",
    "top_left",
    "top_center",
    "top_right",
]
ImageRef = Annotated[str, RefTo("annotation"), Doc("Sheet image to edit (from an earlier call)")]
SIZE_TOL = 1e-6  # relative, of the sheet size
ANGLE_TOL_DEG = 1e-4


class ImageData(TypedDict, total=False):
    image: result.Row
    sheet: result.Row
    file: Annotated[str, Doc("Workspace path of the file NX read (edit: name kept)")]
    format: Literal["png", "jpg", "tif"]
    pixels: Annotated[list[int], Doc("[width, height] of the file in pixels")]
    anchor: str
    position: Annotated[list[float], Doc("The anchor point on the sheet, sheet units")]
    width: float
    height: float
    rotation: Annotated[float, Doc("Degrees counter-clockwise from the sheet's x axis")]
    corners: Annotated[list[list[float]], Doc("Image corners CCW from its bottom-left")]
    bounds: Annotated[list[float], Doc("[x0, y0, x1, y1] extent on the sheet")]
    lock_aspect: bool
    edited: bool


# ---- image files --------------------------------------------------------------------------


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    i = 2
    while i + 9 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # markers without a length
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2 : i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # a start of frame
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return int(width), int(height)
        i += 2 + length
    return None


def _tiff_size(data: bytes) -> tuple[int, int] | None:
    order = {b"II": "<", b"MM": ">"}.get(data[:2])
    if order is None or struct.unpack(order + "H", data[2:4])[0] != 42:
        return None
    offset = struct.unpack(order + "I", data[4:8])[0]
    if offset + 2 > len(data):
        return None
    count = struct.unpack(order + "H", data[offset : offset + 2])[0]
    found: dict[int, int] = {}
    for n in range(count):
        entry = data[offset + 2 + 12 * n : offset + 14 + 12 * n]
        if len(entry) < 12:
            return None
        tag, kind = struct.unpack(order + "HH", entry[:4])
        if tag in (256, 257):
            fmt = "H" if kind == 3 else "I"
            found[tag] = struct.unpack(order + fmt, entry[8 : 8 + struct.calcsize(fmt)])[0]
    if 256 in found and 257 in found:
        return int(found[256]), int(found[257])
    return None


def pixel_size(path, field="/path") -> tuple[str, tuple[int, int]]:
    """``(format, (width, height))`` of a PNG, JPEG or TIFF file, from its header; refuses a
    file whose content is not the format its suffix names."""
    file = Path(path)
    kind = FORMATS.get(file.suffix.lower())
    if kind is None:
        fail("NX_UNSUPPORTED_OPTION", f"{file.suffix or 'A file without a suffix'} is not a "
             "supported image: use PNG, JPEG or TIFF (convert BMP/GIF to PNG first)",
             field=field)  # fmt: skip
    if file.stat().st_size > MAX_BYTES:
        fail("NX_INVALID_ARGUMENT", f"The image file is over {MAX_BYTES // (1024 * 1024)} MB",
             field=field)  # fmt: skip
    if kind == "png":
        size = files.png_size(file)
    else:
        data = file.read_bytes() if kind == "tif" else file.read_bytes()[: 1024 * 1024]
        size = _jpeg_size(data) if kind == "jpg" and data[:2] == b"\xff\xd8" else None
        size = _tiff_size(data) if kind == "tif" else size
    if size is None or not (0 < size[0] <= MAX_PIXELS and 0 < size[1] <= MAX_PIXELS):
        fail("NX_INVALID_ARGUMENT", f"{file.name} is not a readable {kind.upper()} image (its "
             "header does not match the suffix)", field=field)  # fmt: skip
    return kind, size


# ---- geometry -------------------------------------------------------------------------------


def corners_of(position, anchor, width, height, rotation):
    """The four corners ``[[x, y], ...]`` CCW from the image's bottom-left, for an image whose
    ``anchor`` sits at ``position``, turned ``rotation`` degrees about it."""
    fx, fy = ANCHORS[anchor]
    c, s = math.cos(math.radians(rotation)), math.sin(math.radians(rotation))
    out = []
    for u, v in ((0.0, 0.0), (width, 0.0), (width, height), (0.0, height)):
        du, dv = u - fx * width, v - fy * height
        out.append([float(position[0]) + du * c - dv * s, float(position[1]) + du * s + dv * c])
    return out


def placement(corners, anchor):
    """``(position, width, height, rotation)`` of an image from its corners."""
    (x0, y0), (x1, y1), _c2, (x3, y3) = corners
    fx, fy = ANCHORS[anchor]
    width, height = math.hypot(x1 - x0, y1 - y0), math.hypot(x3 - x0, y3 - y0)
    rotation = math.degrees(math.atan2(y1 - y0, x1 - x0))
    position = [x0 + fx * (x1 - x0) + fy * (x3 - x0), y0 + fx * (y1 - y0) + fy * (y3 - y0)]
    return position, width, height, rotation


def _box(corners):
    xs, ys = [c[0] for c in corners], [c[1] for c in corners]
    return [min(xs), min(ys), max(xs), max(ys)]


def _round(values):
    return [round(float(v), 9) + 0.0 for v in values]


def _sized(width, height, aspect, field="/width"):
    """``(width, height)`` with an omitted one from ``aspect`` (height / width)."""
    if width is None and height is None:
        fail("NX_INVALID_ARGUMENT", "Give width or height (or both)", field=field)
    for name, value in (("width", width), ("height", height)):
        if value is not None and not float(value) > 0:
            fail("NX_INVALID_ARGUMENT", f"{name} must be positive", field=f"/{name}")
    if width is None:
        return float(height) / aspect, float(height)
    if height is None:
        return float(width), float(width) * aspect
    return float(width), float(height)


def _inside(box, length, height_):
    tol = 1e-6 * max(length, height_)
    return box[0] >= -tol and box[1] >= -tol and box[2] <= length + tol and box[3] <= height_ + tol


# ---- NX -----------------------------------------------------------------------------------


def _read(tx, image, sh):
    """``(corners in sheet units, lock_aspect, file)`` of an existing image."""
    collection = tx.part.Annotations.DraftingImages
    with builders.build(tx, collection.CreateDraftingImageBuilder, image) as b:
        raster = b.Image
        corners = [sh.to_sheet(p) for p in raster.GetCornerPoints()]
        return corners, bool(raster.LockAspectRatio), str(raster.ImageFile or "")


def _file_type(tx, image):
    kinds = tx.nx.Annotations.DraftingImage.FileType
    try:
        got = image.GetFileType()
    except Exception:  # a corrupt image
        return None
    return next((k for k, member in FILE_TYPES.items() if got == getattr(kinds, member)), None)


def _existing(tx, ref):
    image = select.resolve(tx, ref, "annotation", field="/image", work_part=True)
    if type(image).__name__ != "DraftingImage":
        fail("NX_WRONG_KIND", f"{ref} is not a sheet image", field="/image",
             hint="Pass an image ref from nx_set_sheet_image or nx_list_objects "
             "(kind annotation, type DraftingImage)")  # fmt: skip
    sheet = drawing.sheet_of_annotation(tx, image)
    if sheet is None:
        fail("NX_NOT_FOUND", f"{ref} lies on no drawing sheet", field="/image")
    return image, sheet


def _plan(tx, path, image, sheet, position, anchor, width, height, rotation, lock_aspect):
    """Everything the change needs, read and checked before NX changes anything."""
    plan: dict = {"anchor": anchor, "path": path}
    if path is not None:
        plan["format"], plan["pixels"] = pixel_size(path)
    if image is None:
        if path is None or position is None:
            fail("NX_INVALID_ARGUMENT", "A new image needs path and position",
                 field="/path" if path is None else "/position",
                 hint="Pass image to edit an image already on a sheet")  # fmt: skip
        target = sheet_of(tx, sheet, "/sheet") if sheet is not None else open_or_only_sheet(tx)
        pixels = plan["pixels"]
        w, h = _sized(width, height, pixels[1] / pixels[0])
        plan.update(sheet=target, position=[float(v) for v in position], width=w, height=h,
                    rotation=float(rotation or 0.0), lock=True if lock_aspect is None else
                    bool(lock_aspect), current=None)  # fmt: skip
    else:
        found, target = _existing(tx, image)
        if sheet is not None and int(sheet_of(tx, sheet, "/sheet").Tag) != int(target.Tag):
            fail("NX_INVALID_ARGUMENT", "An image cannot move to another sheet: delete it and "
                 "place a new one there", field="/sheet")  # fmt: skip
        sh = drawing.Sheet(tx, target)
        corners, lock, current = _read(tx, found, sh)
        at, w0, h0, turn = placement(corners, anchor)
        if path is not None:
            aspect = plan["pixels"][1] / plan["pixels"][0]
        else:
            aspect = h0 / w0 if w0 > 0 else 1.0
        w, h = (w0, h0) if width is None and height is None else _sized(width, height, aspect)
        plan.update(sheet=target, image=found, position=at if position is None else
                    [float(v) for v in position], width=w, height=h,
                    rotation=turn if rotation is None else float(rotation),
                    lock=lock if lock_aspect is None else bool(lock_aspect),
                    current=current)  # fmt: skip
    plan["corners"] = corners_of(plan["position"], anchor, plan["width"], plan["height"],
                                 plan["rotation"])  # fmt: skip
    length, tall = drawing.size(plan["sheet"])
    box = _box(plan["corners"])
    if not _inside(box, length, tall):
        fail("NX_INVALID_ARGUMENT", f"The image would lie off the {length:g} x {tall:g} sheet "
             f"(bounds {_round(box)})", field="/position",
             hint="position is the anchor point; check anchor, width/height and rotation",
             details={"bounds": _round(box)})  # fmt: skip
    return plan


def _commit(tx, plan, sh):
    """Create or edit the image; the committed image."""
    collection = tx.part.Annotations.DraftingImages
    existing = plan.get("image")
    null = tx.nx.Annotations.DraftingImage.Null
    with builders.build(tx, collection.CreateDraftingImageBuilder,
                        existing if existing is not None else null) as b:  # fmt: skip
        raster = b.Image
        if plan["path"] is not None:
            raster.ImageFile = str(plan["path"])
        raster.LockAspectRatio = plan["lock"]
        raster.SetCornerPoints(*(sh.point(c) for c in plan["corners"]))
        made = b.commit()
    if made is None:
        fail("NX_BUILD_FAILED", "NX returned no image")
    return made


def _verify(tx, plan, made, sh, before):
    """Read the image back: one image more (new), corners, size, file type."""
    tags = [int(i.Tag) for i in tx.part.Annotations.DraftingImages]
    if plan.get("image") is None:
        added = [t for t in tags if t not in before]
        readback.expect(added == [int(made.Tag)], "images", "NX made another number of images",
                        added=len(added))  # fmt: skip
    else:
        readback.expect(int(made.Tag) == int(plan["image"].Tag), "image",
                        "NX replaced the image instead of editing it")  # fmt: skip
    corners, lock, current = _read(tx, made, sh)
    length, tall = drawing.size(sh.sheet)
    tol = SIZE_TOL * max(length, tall)
    readback.expect_close([v for c in corners for v in c],
                          [v for c in plan["corners"] for v in c], "corners", tol)  # fmt: skip
    readback.expect(lock == plan["lock"], "lock_aspect", "NX stored another aspect lock")
    kind = _file_type(tx, made)
    wanted = plan.get("format") or kind
    readback.expect(kind is not None and kind == wanted, "format",
                    "NX holds another image format", format=kind, expected=wanted)  # fmt: skip
    bounds, _text = drawing.annotation_box(tx, made, sh)
    box = _box(plan["corners"])
    if bounds is not None:
        readback.expect_close(bounds, box, "bounds", tol)
    return corners, lock, current, kind, bounds or box


@tool(
    effect="model",
    label="Set sheet image",
    summary=lambda a, say: (
        f"Edit sheet image {say('image')}" if a.get("image") else f"Place image {say('path')}"
    ),
    keywords=(
        "insert image",
        "logo",
        "picture",
        "raster image",
        "png",
        "jpg",
        "company logo",
        "title block logo",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    cost="light",
    batchable=True,
    invalidates="deleted",
    example={
        "path": "inbox/logo.png",
        "sheet": "SHT1",
        "position": [410, 10],
        "anchor": "bottom_right",
        "width": 40,
    },  # fmt: skip
)
def nx_set_sheet_image(
    tx: Tx,
    *,
    path: Annotated[str, PathTo("read", *SUFFIXES), Doc("PNG, JPEG or TIFF file")] | None = None,
    image: ImageRef | None = None,
    sheet: SheetArg | None = None,
    position: Annotated[SheetPoint, Doc("Where the anchor point goes, sheet units")] | None = None,
    anchor: Anchor = "bottom_left",
    width: Annotated[SheetLength, Doc("Image width; omitted: from height, keeping the aspect")]
    | None = None,
    height: Annotated[SheetLength, Doc("Image height; omitted: from width, keeping the aspect")]
    | None = None,
    rotation: Annotated[Angle, Doc("CCW about the anchor point; default 0 (new)")] | None = None,
    lock_aspect: Annotated[bool, Doc("NX's Lock Aspect Ratio flag for later resizing")]
    | None = None,
) -> ImageData:
    """Place a PNG/JPEG/TIFF image from the workspace on a drawing sheet, e.g. a logo in the
    title block. New: path, position and width and/or height (both: stretched to them).
    anchor names the image point put at position (bottom_left default, center...). Edit:
    image + any of the fields (path replaces the file); omitted keep the current values.
    Refuses an image off the sheet. Delete: nx_delete_objects."""
    with tx.checks():
        before = {int(i.Tag) for i in tx.part.Annotations.DraftingImages}
        plan = _plan(tx, path, image, sheet, position, anchor, width, height, rotation,
                     lock_aspect)  # fmt: skip
    licenses.require(tx, ("drafting",), "Sheet images need a drafting license")
    with drawing.on_sheet(tx, plan["sheet"]) as sh:
        made = _commit(tx, plan, sh)
        update(tx)
        corners, lock, current, kind, bounds = _verify(tx, plan, made, sh, before)
        position, w, h, turn = placement(corners, anchor)
        views, _off = drawing.crowding(tx, sh.sheet, bounds)
        if views:
            names = ", ".join(str(r.get("name") or r.get("id")) for r in views[:5])
            tx.warn("NX_VIEW_OVERLAP", f"The image lies over view {names}: move it with "
                    "nx_set_sheet_image image + position")  # fmt: skip
    shown = plan["path"] if plan["path"] is not None else current
    out: ImageData = {
        "image": tx.ref(made, "annotation"),
        "sheet": tx.ref(plan["sheet"], "drawing_sheet"),
        "file": files.relative(shown, tx.workspace.root) if plan["path"] is not None
        else Path(str(shown)).name,
        "format": kind,
        "anchor": anchor,
        "position": _round(position),
        "width": round(w, 9),
        "height": round(h, 9),
        "rotation": round(turn, 9) + 0.0,
        "corners": [_round(c) for c in corners],
        "bounds": _round(bounds),
        "lock_aspect": lock,
        "edited": image is not None,
    }  # fmt: skip
    if "pixels" in plan:
        out["pixels"] = list(plan["pixels"])
    return out
