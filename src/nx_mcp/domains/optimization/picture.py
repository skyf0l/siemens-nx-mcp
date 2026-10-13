"""Read-back of a captured contour PNG: how much of it is the coloured result body.

NX 2406 (live, 100x20x10 mm cantilever, graphics window 1460x1794, white background):
outside the Topology Optimization task a plotted contour draws only its legend (the
colour bar, every band labelled 0, all blue); inside the task the result body is drawn
in the band colours. Pixels whose channel spread (max - min) exceeds ``CHROMA`` are
contour colours (greys, text, edges and the white background stay below). Columns
saturated over ``LEGEND_RUN`` of the height are the legend's colour bar and are left
out. Measured share of the picture: legend only 0.00026 (the view triad), result body
at the task's own zoom 0.0079, fitted 0.157.

The decoder reads 8-bit RGB/RGBA non-interlaced PNGs (what NX writes) with the standard
library only (NX's Python has no imaging package); every second row and column is
counted.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIGNATURE = b"\x89PNG\r\n\x1a\n"
CHROMA = 80
LEGEND_RUN = 0.3
STEP = 2  # count every second row and column (the share is a ratio)


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


def png_rows(path):
    """``(width, height, bpp, [row bytes])`` of an 8-bit RGB/RGBA PNG."""
    raw = Path(path).read_bytes()
    if raw[:8] != SIGNATURE:
        raise ValueError("not a PNG")
    pos, chunks, header = 8, [], None
    while pos + 8 <= len(raw):
        size, kind = struct.unpack(">I4s", raw[pos : pos + 8])
        body = raw[pos + 8 : pos + 8 + size]
        if kind == b"IHDR":
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
    bpp = 3 if colour == 2 else 4
    data = zlib.decompress(b"".join(chunks))
    stride = width * bpp
    prev, out = bytes(stride), []
    for y in range(height):
        start = y * (stride + 1)
        prev = _unfilter(data[start], data[start + 1 : start + 1 + stride], prev, bpp)
        out.append(prev)
    return width, height, bpp, out


def contour_share(path) -> float:
    """Share of the PNG covered by contour colours outside the legend's colour bar."""
    width, height, bpp, lines = png_rows(path)
    columns = [0] * len(range(0, width, STEP))
    counted = 0
    for line in lines[::STEP]:
        counted += 1
        step = bpp * STEP
        for x, (r, g, b) in enumerate(
            zip(line[0::step], line[1::step], line[2::step], strict=True)
        ):
            if max(r, g, b) - min(r, g, b) > CHROMA:
                columns[x] += 1
    if not counted or not columns:
        return 0.0
    body = sum(c for c in columns if c < LEGEND_RUN * counted)
    return body / (counted * len(columns))
