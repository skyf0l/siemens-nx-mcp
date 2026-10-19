"""PNG captures of the displayed view: NX viewport export or Studio Image Capture.

    size = capture.studio_png(tx, path, 960, 600, style="shaded_with_edges")
    size = capture.viewport_png(tx, path, 960, 600)        # actual size, see below
    size = capture.fit_png(path, 960, 600)  # that PNG scaled down into 960x600, padded

NX 2406 findings encoded here:

- ``StudioImageCaptureBuilder`` honours the requested pixel size, also while a
  drawing sheet is shown (letterboxed to the window's aspect).
- ``ImageExportBuilder`` (and UF ``Disp.CreateImage``) rasterise the graphics
  window at its own size and ignore ``DeviceWidth``/``DeviceHeight``: callers
  report the requested and the actual resolution.
- Both export the displayed drawing sheet when one is shown; only the model view
  has a rendering style (``part.ModelingViews.WorkView`` raises on a sheet).
- Every capture goes through these builders, never a desktop screenshot.
- ``fit_png`` makes a window-size export the requested size afterwards: the image is
  scaled down by area averaging (never up) keeping its aspect and centred on its own
  corner colour (the view background). It reads the 8-bit RGB/RGBA non-interlaced PNGs
  NX writes with the standard library only (NX's Python has no imaging package).

The view's rendering style and the lighting preset are restored; a file that is
not a PNG (or, for Studio, not of the requested size) is removed and reported.
"""

from __future__ import annotations

import array
import contextlib
import struct
import sys
import zlib
from pathlib import Path

from nx_mcp.core.errors import fail, native
from nx_mcp.kit import files

STYLES = {
    "studio": "Studio",
    "shaded": "Shaded",
    "shaded_with_edges": "ShadedWithEdges",
    "wireframe": "StaticWireframe",
}
BACKGROUNDS = {
    "white": "CustomColor",
    "color": "CustomColor",
    "original": "Original",
    "transparent": "Transparent",
}
WHITE = [1.0, 1.0, 1.0]


def _part(tx):
    if tx.session.IsBatch:
        fail("NX_VIEWPORT_UNAVAILABLE", "Captures need the interactive NX session")
    part = tx.session.Parts.Display
    if part is None:
        fail("NX_NO_WORK_PART", "No display part to capture")
    return part


def _background(builder, background, color):
    builder.BackgroundOption = getattr(builder.BackgroundOptions, BACKGROUNDS[background])
    if background in ("white", "color"):
        builder.SetCustomBackgroundColor([float(v) for v in (color or WHITE)])


@contextlib.contextmanager
def _styled(tx, part, style):
    """Apply a rendering style to the model work view for the block (None: unchanged)."""
    if style is None:
        yield
        return
    view = part.ModelingViews.WorkView
    original = view.RenderingStyle
    try:
        with native(default="NX_CAPTURE_FAILED"):
            view.RenderingStyle = getattr(tx.nx.View.RenderingStyleType, STYLES[style])
            view.UpdateDisplay()
        yield
    finally:
        with contextlib.suppress(Exception):
            view.RenderingStyle = original
            view.UpdateDisplay()


@contextlib.contextmanager
def _lighting(part, preset):
    """Select a shaded-view lighting preset (1-5) for the block (None: unchanged)."""
    if preset is None:
        yield
        return
    lights = previous = None
    try:
        with native(default="NX_CAPTURE_FAILED"):
            lights = part.Views.CreateLighting(None)
            previous = lights.LightsShadedViewsLightingCollection
            user = getattr(lights.LightingCollectionType, "UserDefined", None)
            # Hand-set lights (nx_set_light): re-committing a UserDefined preset crashes
            # NX's lighting builder, so the part's own lights stay as set.
            keep = user is not None and previous == user
            if keep:
                previous = None
            else:
                lights.LightsShadedViewsLightingCollection = getattr(
                    lights.LightingCollectionType, f"Lighting{int(preset)}"
                )
                lights.Commit()
        yield
    finally:
        if lights is not None:
            with contextlib.suppress(Exception):
                if previous is not None:
                    lights.LightsShadedViewsLightingCollection = previous
                    lights.Commit()
            with contextlib.suppress(Exception):
                lights.Destroy()


def _written(target):
    size = files.png_size(target) if target.is_file() else None
    if size is None:
        fail("NX_CAPTURE_FAILED", "NX did not write a PNG image")
    return tuple(size)


def studio_png(
    tx,
    path,
    width,
    height,
    *,
    style="shaded_with_edges",
    background="white",
    color=None,
    lighting=None,
):
    """Render the displayed work view (or sheet) to ``path``; returns ``(width, height)``.

    ``style`` None keeps the rendering style (required while a drawing sheet is shown);
    ``background`` is white, original, transparent or color (``color`` = RGB 0..1);
    ``lighting`` selects a shaded-view lighting preset 1-5 for the render.
    """
    part = _part(tx)
    target = Path(path)
    builder = None
    try:
        with _styled(tx, part, style), _lighting(part, lighting):
            try:
                with native(default="NX_CAPTURE_FAILED"):
                    builder = part.Views.CreateStudioImageCaptureBuilder()
                    builder.Source = builder.SourceType.WorkView
                    builder.UnitsEnum = builder.UnitsEnumType.Pixels
                    builder.SetImageDimensionsInteger([int(height), int(width)])
                    builder.NativeFileBrowser = str(target)
                    _background(builder, background, color)
                    builder.Commit()
            finally:
                with contextlib.suppress(Exception):
                    if builder is not None:
                        builder.Destroy()
        size = _written(target)
        if size != (int(width), int(height)):
            fail("NX_CAPTURE_FAILED", f"NX rendered {size}, not {width}x{height}")
        return size
    except BaseException:
        with contextlib.suppress(OSError):
            target.unlink(missing_ok=True)
        raise


def viewport_png(tx, path, width, height, *, style=None, background="white", color=None):
    """Export the displayed graphics window (model view or drawing sheet) to ``path``.

    Returns the actual ``(width, height)``: NX 2406 exports the window at its own
    size, so it can differ from the request. ``style`` None keeps the rendering style.
    """
    part = _part(tx)
    target = Path(path)
    builder = None
    try:
        with _styled(tx, part, style):
            try:
                with native(default="NX_CAPTURE_FAILED"):
                    builder = part.Views.CreateImageExportBuilder()
                    builder.FileName = str(target)
                    builder.FileFormat = builder.FileFormats.Png
                    builder.RegionMode = False
                    builder.DeviceWidth = int(width)
                    builder.DeviceHeight = int(height)
                    _background(builder, background, color)
                    builder.EnhanceEdges = True
                    builder.Commit()
            finally:
                with contextlib.suppress(Exception):
                    if builder is not None:
                        builder.Destroy()
        return _written(target)
    except BaseException:
        with contextlib.suppress(OSError):
            target.unlink(missing_ok=True)
        raise


# ---- PNG scaling (standard library only) -----------------------------------------------------

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _unfilter(kind: int, line: bytes, prev: bytes, bpp: int) -> bytes:
    """One PNG scanline without its filter (types 0-4)."""
    if kind == 0:
        return line
    size = len(line)
    if kind == 2:  # Up: bytewise sum with the previous row, without carries between bytes
        low = int.from_bytes(b"\x7f" * size, "big")
        high = int.from_bytes(b"\x80" * size, "big")
        a, b = int.from_bytes(line, "big"), int.from_bytes(prev, "big")
        return (((a & low) + (b & low)) ^ ((a ^ b) & high)).to_bytes(size, "big")
    out = bytearray(line)
    if kind == 1:
        for i in range(bpp, size):
            out[i] = (out[i] + out[i - bpp]) & 255
    elif kind == 3:
        for i in range(size):
            left = out[i - bpp] if i >= bpp else 0
            out[i] = (out[i] + ((left + prev[i]) >> 1)) & 255
    elif kind == 4:
        for i in range(size):
            if i >= bpp:
                out[i] = (out[i] + _paeth(out[i - bpp], prev[i], prev[i - bpp])) & 255
            else:
                out[i] = (out[i] + prev[i]) & 255
    else:
        raise ValueError(f"unknown PNG filter {kind}")
    return bytes(out)


def read_png(path) -> tuple[int, int, int, list[bytes]]:
    """``(width, height, channels, rows)`` of an 8-bit RGB (3) or RGBA (4) PNG; ValueError
    for any other kind."""
    raw = Path(path).read_bytes()
    if raw[:8] != PNG_SIGNATURE:
        raise ValueError("not a PNG")
    pos, chunks, header = 8, [], None
    while pos + 8 <= len(raw):
        size, kind = struct.unpack(">I4s", raw[pos : pos + 8])
        body = raw[pos + 8 : pos + 8 + size]
        if kind == b"IHDR":
            if len(body) != 13:
                raise ValueError("PNG header cut short")
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            chunks.append(body)
        elif kind == b"IEND":
            break
        pos += 12 + size
    if header is None:
        raise ValueError("PNG without a header")
    width, height, depth, colour, _method, _filter, interlace = header
    if depth != 8 or interlace or colour not in (2, 6):
        raise ValueError("only 8-bit non-interlaced RGB/RGBA PNGs are read")
    channels = 3 if colour == 2 else 4
    try:
        data = zlib.decompress(b"".join(chunks))
    except zlib.error as error:
        raise ValueError(f"PNG data unreadable: {error}") from error
    stride = width * channels
    if len(data) < height * (stride + 1):
        raise ValueError("PNG data cut short")
    prev, rows = bytes(stride), []
    for y in range(height):
        start = y * (stride + 1)
        prev = _unfilter(data[start], data[start + 1 : start + 1 + stride], prev, channels)
        rows.append(prev)
    return width, height, channels, rows


def write_png(path, width: int, height: int, channels: int, rows) -> None:
    """Write 8-bit RGB (3) or RGBA (4) rows as an unfiltered PNG."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2 if channels == 3 else 6, 0, 0, 0)
    data = zlib.compress(b"".join(b"\0" + bytes(r) for r in rows), 6)
    Path(path).write_bytes(
        PNG_SIGNATURE + chunk(b"IHDR", header) + chunk(b"IDAT", data) + chunk(b"IEND", b"")
    )


def _bands(source: int, target: int) -> list[tuple[int, int]]:
    """``[(start, stop)]`` of the source pixels each of ``target`` output pixels averages."""
    out = []
    for i in range(target):
        start = i * source // target
        out.append((start, max(start + 1, (i + 1) * source // target)))
    return out


def _lanes(row: bytes) -> int:
    """``row`` as one integer with a 16-bit lane per byte: rows then add lane by lane."""
    wide = bytearray(2 * len(row))
    wide[1::2] = row
    return int.from_bytes(wide, "big")


def _unlanes(total: int, size: int) -> array.array:
    values = array.array("H")
    values.frombytes(total.to_bytes(2 * size, "big"))
    if sys.byteorder == "little":
        values.byteswap()
    return values


def fit_png(path, width: int, height: int) -> tuple[int, int]:
    """Make the PNG at ``path`` exactly ``width`` x ``height`` in place: scaled down (area
    average, never up) to fit with its aspect kept, centred on its top-left pixel's colour.
    Returns the new size; ValueError for a PNG kind it does not read."""
    src_w, src_h, channels, rows = read_png(path)
    if (src_w, src_h) == (width, height):
        return width, height
    scale = min(width / src_w, height / src_h, 1.0)
    out_w, out_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    columns = _bands(src_w, out_w)
    fill = rows[0][:channels]
    left, top = (width - out_w) // 2, (height - out_h) // 2
    blank = fill * width
    stride = src_w * channels
    out: list[bytes] = [blank] * top
    for start, stop in _bands(src_h, out_h):
        if stop - start > 257:  # 16-bit lanes hold 257 rows of 255
            raise ValueError("image too large to scale down")
        sums = _unlanes(sum(_lanes(r) for r in rows[start:stop]), stride)
        tall = stop - start
        line = bytearray(fill * left)
        for x0, x1 in columns:
            count = tall * (x1 - x0)
            for c in range(channels):
                part = sums[x0 * channels + c : x1 * channels : channels]
                line.append((sum(part) + count // 2) // count)
        line += fill * (width - left - out_w)
        out.append(bytes(line))
    out += [blank] * (height - top - out_h)
    write_png(path, width, height, channels, out)
    return width, height
