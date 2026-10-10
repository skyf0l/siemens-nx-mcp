"""Small neutral CAD files shaped like NX 2406 writes them (STL, IGES, Parasolid, JT, STEP).

Each builder takes axis-aligned boxes ``[x0, y0, z0, x1, y1, z1]`` (one per body) and
returns the file's bytes, so readers (``nx_mcp.domains.parts_files.neutral``) and the fake translators
(``tests/kit/parts.py``) share one source of sample files.
"""

from __future__ import annotations

import lzma
import struct

JT_PARTITION = bytes.fromhex("3e10dd10c82ad1119b6b0080c7bb5997")


def _corners(box):
    x0, y0, z0, x1, y1, z1 = box
    return [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]


def _triangles(box):
    """12 triangles of a box (normals left zero: readers ignore them)."""
    c = _corners(box)
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    out = []
    for a, b, d, e in quads:
        out += [(c[a], c[b], c[d]), (c[a], c[d], c[e])]
    return out


def stl(boxes, binary=True) -> bytes:
    tris = [t for box in boxes for t in _triangles(box)]
    if binary:
        data = b" " * 80 + struct.pack("<I", len(tris))
        for t in tris:
            data += struct.pack("<12fH", 0, 0, 0, *t[0], *t[1], *t[2], 0)
        return data
    lines = ["solid"]
    for t in tris:
        lines += ["  facet normal +0.0E+00 +0.0E+00 +1.0E+00", "    outer loop"]
        lines += [f"      vertex   {x:+.7E} {y:+.7E} {z:+.7E}" for x, y, z in t]
        lines += ["    endloop", "  endfacet"]
    return ("\n".join([*lines, "endsolid", ""])).encode()


def _iges_line(text, section, seq):
    return f"{text:<72}{section}{seq:>7}"


def iges(boxes, units="mm") -> bytes:
    """Two bilinear B-spline surfaces (entity 128, bottom and top) per box."""
    flag, name = {"mm": (2, "2HMM"), "in": (1, "4HINCH")}[units]
    glob = (
        f"1H,,1H;,8Hpart.prt,5Hx.igs,8HNX V2406,4HTEST,32,38,16,38,16,8Hpart.prt,1.0,"
        f"{flag},{name},300,2.0,15H20261009.234256,1.0E-05,10000.0,,,11,0,"
        "15H20261009.234200;"
    )
    g_lines = [glob[i : i + 72] for i in range(0, len(glob), 72)]
    d_lines, p_lines = [], []
    for box in boxes:
        x0, y0, z0, x1, y1, z1 = box
        for z in (z0, z1):
            points = [(x0, y0, z), (x1, y0, z), (x0, y1, z), (x1, y1, z)]
            values = ["128", "1", "1", "1", "1", "0", "0", "1", "0", "0"]
            values += ["0", "0", "1", "1", "0", "0", "1", "1", "1", "1", "1", "1"]
            values += [f"{v:g}" for p in points for v in p] + ["0", "1", "0", "1"]
            text = ",".join(values) + ";"
            chunks = [text[i : i + 64] for i in range(0, len(text), 64)]
            de = 2 * (len(d_lines) // 2) + 1
            first_p = len(p_lines) + 1
            p_lines += [f"{c:<64} {de:>7}" for c in chunks]
            d_lines.append(
                f"{128:>8}{first_p:>8}{0:>8}{1:>8}{0:>8}{0:>8}{0:>8}{0:>8}{'00000000':>8}"
            )
            d_lines.append(f"{128:>8}{0:>8}{0:>8}{len(chunks):>8}{0:>8}{'':>8}{'':>8}{'':>8}{0:>8}")
    lines = [_iges_line("", "S", 1)]
    lines += [_iges_line(t, "G", i + 1) for i, t in enumerate(g_lines)]
    lines += [_iges_line(t, "D", i + 1) for i, t in enumerate(d_lines)]
    lines += [f"{t}P{i + 1:>7}" for i, t in enumerate(p_lines)]
    tail = f"S{1:>7}G{len(g_lines):>7}D{len(d_lines):>7}P{len(p_lines):>7}"
    lines.append(_iges_line(tail, "T", 1))
    return ("\n".join(lines) + "\n").encode()


def parasolid(boxes, binary=False) -> bytes:
    fmt = "binary" if binary else "text"
    header = (
        "**ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz**************************\n"
        "**PARASOLID !\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~0123456789**************************\n"
        f"**PART1;\nAPPL=unigraphics;\nFORMAT={fmt};\nGUISE=transmit;\n"
        "**PART2;\nSCH=SCH_3601233_36001;\nUSFLD_SIZE=0;\n**PART3;\n"
        "**END_OF_HEADER*****************************************************************\n"
    )
    body = " ".join(f"{v / 1000:g}" for box in boxes for v in box) or "0"
    return header.encode() + b"T51 : TRANSMIT FILE " + body.encode() + b"\n"


def _mbstring(text):
    return struct.pack("<i", len(text)) + text.encode("utf-16-le")


def _compressed(payload):
    packed = lzma.compress(payload)
    return struct.pack("<iiB", 3, len(packed) + 1, 3) + packed


def jt(boxes, units="millimeters", name="part.jt") -> bytes:
    """A monolithic JT 10.9 file: LSG with the partition bounds, metadata, one shape
    LOD segment per box."""
    low = [min(b[i] for b in boxes) for i in range(3)] if boxes else [0.0] * 3
    high = [max(b[3 + i] for b in boxes) for i in range(3)] if boxes else [0.0] * 3
    triangles = 12 * len(boxes)
    element = JT_PARTITION + b"\x01" + b"\x00" * 23 + struct.pack("<i", 1) + _mbstring(name)
    element += struct.pack("<6f", *low, *high) + struct.pack("<f", 1.0)
    element += struct.pack("<6i", 8, 8, 1, 1, triangles, triangles)
    lsg = struct.pack("<i", len(element)) + element + struct.pack("<i", 0)
    meta = _mbstring("JT_PROP_MEASUREMENT_UNITS::") + _mbstring(units)
    segments = [(1, _compressed(lsg)), (4, _compressed(meta))]
    segments += [(7, struct.pack("<i", 576) + bytes(32)) for _ in boxes]
    data = bytearray(b"Version 10.9 JT  DM 10.6.0.0".ljust(75) + b" \n\r\n ")
    data += b"\x00" + struct.pack("<i", 0) + struct.pack("<Q", 0) + bytes(16)
    toc = []
    for kind, payload in segments:
        offset = len(data)
        length = 24 + len(payload)
        data += bytes(16) + struct.pack("<ii", kind, length) + payload
        toc.append((offset, length, kind))
    toc_offset = len(data)
    data += struct.pack("<i", len(toc))
    for offset, length, kind in toc:
        data += bytes(16) + struct.pack("<QII", offset, length, kind << 24)
    struct.pack_into("<Q", data, 85, toc_offset)
    return bytes(data)


def step(solids, units="mm", schema="AUTOMOTIVE_DESIGN { 1 0 10303 214 3 1 1 1 }") -> bytes:
    unit = {
        "mm": "#2=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.));",
        "in": "#2=(CONVERSION_BASED_UNIT('INCH',#3)LENGTH_UNIT()NAMED_UNIT(#4));",
    }[units]
    records = "".join(f"#{10 + i}=MANIFOLD_SOLID_BREP('',#9);\n" for i in range(solids))
    return (
        f"ISO-10303-21;\nHEADER;\nFILE_SCHEMA (('{schema}'));\nENDSEC;\nDATA;\n{unit}\n"
        f"{records}ENDSEC;\nEND-ISO-10303-21;\n"
    ).encode()
