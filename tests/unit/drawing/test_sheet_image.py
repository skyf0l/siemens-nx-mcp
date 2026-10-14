"""Sheet images (nx_set_sheet_image) on the fake: place, edit, file checks and read-back."""

import math
import struct
import zlib

import pytest

from nx_mcp.domains.drawing import sheet_image
from tests.kit import run
from tests.kit.kernel import services
from tests.kit.objects import Object, point
from tests.unit.drawing.fakes import drawing_nx


def png_bytes(width, height):
    def chunk(kind, data):
        crc = struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + kind + data + crc

    raw = b"".join(b"\x00" + b"\x10\x20\x30" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))  # fmt: skip


def jpeg_bytes(width, height):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def tiff_bytes(width, height, order="<"):
    head = (b"II" if order == "<" else b"MM") + struct.pack(order + "HI", 42, 8)
    entries = [(256, 3, 1, width), (257, 4, 1, height)]
    ifd = struct.pack(order + "H", len(entries))
    for tag, kind, count, value in entries:
        packed = struct.pack(order + "H", value) + b"\x00\x00" if kind == 3 else struct.pack(
            order + "I", value)  # fmt: skip
        ifd += struct.pack(order + "HHI", tag, kind, count) + packed
    return head + ifd + struct.pack(order + "I", 0)


class Raster:
    """``builder.Image``: corners exist only once set (NX 2406 raises before)."""

    def __init__(self, image):
        self.ImageFile = image.file if image else ""
        self.LockAspectRatio = image.lock if image else True
        self.corners = list(image.corners) if image else None

    def SetCornerPoints(self, *points):
        self.corners = [(p.X, p.Y) for p in points]

    def GetCornerPoints(self):
        if self.corners is None:
            raise RuntimeError("Corner points do not form a rectangular boundary")
        return [point(x, y, 0.0) for x, y in self.corners]


class Image(Object):
    def __init__(self, nx):
        super().__init__("IMAGE")
        self.nx = nx
        self.OwningPart, self.IsOccurrence = nx.part, False
        self.AnnotationOrigin = point(0, 0, 0)
        self.file, self.lock, self.corners = "", True, None

    def GetFileType(self):
        suffix = self.file.rsplit(".", 1)[-1].lower()
        member = {"png": "Png", "jpg": "Jpg", "jpeg": "Jpg", "tif": "Tif", "tiff": "Tif"}[suffix]
        return getattr(self.nx.nx.Annotations.DraftingImage.FileType, member)


DraftingImage = type("DraftingImage", (Image,), {})


class Builder:
    def __init__(self, images, existing):
        self.images, self.existing = images, existing
        # NX 2406: a builder on a null image creates the image at once
        self.target = existing if isinstance(existing, Image) else images.make()
        self.Image = Raster(existing if isinstance(existing, Image) else None)
        self.destroyed = False

    def Commit(self):
        if self.images.stretch:  # NX misplacing the image (read-back must fail)
            self.Image.corners = [(x * 1.1, y) for x, y in self.Image.corners]
        t = self.target
        t.file, t.lock, t.corners = self.Image.ImageFile, self.Image.LockAspectRatio, list(
            self.Image.corners)  # fmt: skip
        return t

    def Destroy(self):
        self.destroyed = True


class Images(list):
    def __init__(self, nx, sheet):
        super().__init__()
        self.nx, self.sheet, self.builders, self.stretch = nx, sheet, [], False

    def make(self):
        image = DraftingImage(self.nx)
        self.append(image)
        self.nx.dependent[int(image.Tag)] = (1, "SHT1@0")
        return image

    def CreateDraftingImageBuilder(self, existing):
        b = Builder(self, existing)
        self.builders.append(b)
        return b


def box(tag, images):
    image = next(i for i in images if int(i.Tag) == int(tag))
    xs, ys = [c[0] for c in image.corners], [c[1] for c in image.corners]
    return [min(xs), min(ys), 0.0, max(xs), max(ys), 0.0]


def image_nx(units="mm"):
    nx = drawing_nx(units)
    sheet = nx.make_sheet(units=units, size=(420.0, 297.0) if units == "mm" else (17.0, 11.0))
    sheet.GetDraftingViews = lambda: list(sheet.views)
    nx.images = Images(nx, sheet)
    nx.part.Annotations.DraftingImages = nx.images
    nx.uf.ModlGeneral = type("UF", (), {"AskBoundingBox": staticmethod(
        lambda tag: box(tag, nx.images))})()  # fmt: skip
    sheet.Open()
    return nx


def write(nx, name, data):
    (services(nx).root / name).write_bytes(data)
    return name


def test_a_new_png_keeps_its_aspect_and_lands_with_its_anchor_on_position():
    nx = image_nx()
    write(nx, "logo.png", png_bytes(120, 60))
    ok = run(nx, "nx_set_sheet_image", path="logo.png", position=[410, 10],
             anchor="bottom_right", width=40)  # fmt: skip
    assert ok.ok, ok
    d = ok.data
    assert d["format"] == "png" and d["pixels"] == [120, 60] and d["file"] == "logo.png"
    assert d["width"] == pytest.approx(40) and d["height"] == pytest.approx(20)
    assert d["position"] == pytest.approx([410, 10]) and d["rotation"] == pytest.approx(0)
    assert d["corners"] == [pytest.approx(c) for c in ([370, 10], [410, 10], [410, 30], [370, 30])]
    assert d["bounds"] == pytest.approx([370, 10, 410, 30])
    assert d["lock_aspect"] is True and d["edited"] is False
    assert d["image"]["kind"] == "annotation"
    assert len(nx.images) == 1 and all(b.destroyed for b in nx.images.builders)
    assert nx.images[0].file.endswith("logo.png")


def test_height_only_center_anchor_and_rotation():
    nx = image_nx()
    write(nx, "logo.jpg", jpeg_bytes(200, 100))
    ok = run(nx, "nx_set_sheet_image", path="logo.jpg", position=[200, 150], anchor="center",
             height=30, rotation=90)  # fmt: skip
    assert ok.ok, ok
    d = ok.data
    assert d["format"] == "jpg" and d["width"] == pytest.approx(60) and d["rotation"] == 90
    # turned 90 deg about the center: 30 wide and 60 tall on the sheet
    assert d["bounds"] == pytest.approx([185, 120, 215, 180])
    assert d["position"] == pytest.approx([200, 150])


def test_both_sizes_stretch_and_tiff_big_endian_is_read():
    nx = image_nx()
    write(nx, "logo.tiff", tiff_bytes(64, 64, order=">"))
    ok = run(nx, "nx_set_sheet_image", path="logo.tiff", position=[10, 10], width=50, height=20,
             lock_aspect=False)  # fmt: skip
    assert ok.ok, ok
    assert ok.data["format"] == "tif" and ok.data["pixels"] == [64, 64]
    assert (ok.data["width"], ok.data["height"]) == pytest.approx((50, 20))
    assert ok.data["lock_aspect"] is False and nx.images[0].lock is False


def test_edit_resizes_about_the_anchor_and_keeps_the_rest():
    nx = image_nx()
    write(nx, "logo.png", png_bytes(100, 50))
    made = run(nx, "nx_set_sheet_image", path="logo.png", position=[100, 100], width=40,
               rotation=30)  # fmt: skip
    ref = made.data["image"]["id"]
    ok = run(nx, "nx_set_sheet_image", image=ref, anchor="center", width=80)
    assert ok.ok, ok
    d = ok.data
    assert d["edited"] is True and d["image"]["id"] == ref and len(nx.images) == 1
    assert (d["width"], d["height"]) == pytest.approx((80, 40))  # current proportions
    assert d["rotation"] == pytest.approx(30)
    before = sheet_image.placement(made.data["corners"], "center")[0]
    assert d["position"] == pytest.approx(before)  # the center stayed
    assert d["file"] == "logo.png" and "pixels" not in d
    moved = run(nx, "nx_set_sheet_image", image=ref, position=[300, 200], rotation=0)
    assert moved.ok and moved.data["corners"][0] == pytest.approx([300, 200])


def test_edit_replaces_the_file():
    nx = image_nx()
    write(nx, "a.png", png_bytes(100, 50))
    write(nx, "b.jpg", jpeg_bytes(50, 50))
    ref = run(nx, "nx_set_sheet_image", path="a.png", position=[10, 10], width=40).data["image"]
    ok = run(nx, "nx_set_sheet_image", image=ref["id"], path="b.jpg", width=30)
    assert ok.ok, ok
    assert ok.data["format"] == "jpg" and ok.data["height"] == pytest.approx(30)
    assert nx.images[0].file.endswith("b.jpg")


@pytest.mark.parametrize(
    ("args", "files", "field", "code"),
    [
        ({"path": "x.png", "position": [10, 10]}, {"x.png": png_bytes(4, 2)}, "/width",
         "NX_INVALID_ARGUMENT"),
        ({"path": "x.png", "width": 10}, {"x.png": png_bytes(4, 2)}, "/position",
         "NX_INVALID_ARGUMENT"),
        ({"position": [10, 10], "width": 10}, {}, "/path", "NX_INVALID_ARGUMENT"),
        ({"path": "x.png", "position": [400, 10], "width": 40}, {"x.png": png_bytes(4, 2)},
         "/position", "NX_INVALID_ARGUMENT"),
        ({"path": "x.png", "position": [10, 10], "width": -4}, {"x.png": png_bytes(4, 2)},
         "/width", "NX_INVALID_ARGUMENT"),
        ({"path": "fake.png", "position": [10, 10], "width": 10}, {"fake.png": b"GIF89a...."},
         "/path", "NX_INVALID_ARGUMENT"),
        ({"path": "x.jpg", "position": [10, 10], "width": 10}, {"x.jpg": png_bytes(4, 2)},
         "/path", "NX_INVALID_ARGUMENT"),
        ({"path": "logo.bmp", "position": [10, 10], "width": 10}, {"logo.bmp": b"BM" + bytes(60)},
         "/path", None),
        ({"path": "x.png", "position": [10, 10], "width": 10, "sheet": "NOPE"},
         {"x.png": png_bytes(4, 2)}, "/sheet", "NX_NOT_FOUND"),
    ],
)  # fmt: skip
def test_bad_calls_are_refused_before_nx(args, files, field, code):
    nx = image_nx()
    for name, data in files.items():
        write(nx, name, data)
    bad = run(nx, "nx_set_sheet_image", **args)
    assert not bad.ok and bad.error["field"] == field, bad
    assert code is None or bad.error["code"] == code, bad
    assert bad.mutation_outcome == "not_started" and not nx.images.builders


def test_an_edit_needs_a_sheet_image_on_its_own_sheet():
    nx = image_nx()
    note = Object("NOTE")
    note.OwningPart, note.IsOccurrence = nx.part, False
    ref = nx.ref_of(note, "annotation")
    bad = run(nx, "nx_set_sheet_image", image=ref, width=10)
    assert bad.error["code"] == "NX_WRONG_KIND" and bad.error["field"] == "/image"
    write(nx, "x.png", png_bytes(4, 2))
    made = run(nx, "nx_set_sheet_image", path="x.png", position=[10, 10], width=10)
    other = nx.make_sheet("SHT2")
    other.GetDraftingViews = lambda: []
    moved = run(nx, "nx_set_sheet_image", image=made.data["image"]["id"], sheet="SHT2")
    assert moved.error["field"] == "/sheet" and moved.mutation_outcome == "not_started"


def test_a_misplaced_image_fails_the_read_back():
    nx = image_nx()
    write(nx, "x.png", png_bytes(4, 2))
    nx.images.stretch = True
    bad = run(nx, "nx_set_sheet_image", path="x.png", position=[10, 10], width=40)
    assert bad.error["code"] == "NX_VERIFICATION_FAILED" and bad.mutation_outcome == "rolled_back"
    assert bad.error["details"]["check"] == "corners"


def test_an_inch_sheet_takes_sheet_units():
    nx = image_nx(units="in")
    write(nx, "x.png", png_bytes(200, 100))
    ok = run(nx, "nx_set_sheet_image", path="x.png", position=[15, 0.5], anchor="bottom_right",
             width=2)  # fmt: skip
    assert ok.ok, ok
    assert ok.data["bounds"] == pytest.approx([13, 0.5, 15, 1.5])


def test_geometry_helpers():
    corners = sheet_image.corners_of([0, 0], "top_right", 4, 2, 180)
    assert corners[0] == pytest.approx([4, 2])  # turned half a turn about the top right
    position, w, h, turn = sheet_image.placement(corners, "top_right")
    assert position == pytest.approx([0, 0]) and (w, h) == pytest.approx((4, 2))
    assert abs(abs(turn) - 180) < 1e-9
    assert math.isclose(sheet_image._sized(None, 10, 0.5)[0], 20)
