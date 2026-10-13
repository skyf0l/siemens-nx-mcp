"""Facts about exported 3D neutral files: STEP, STL, IGES, Parasolid and JT (no NX import).

    neutral.step_facts(data)       # {"schema": "AP214", "units": "mm", "solids": 2}
    neutral.stl_facts(data)        # {"binary", "triangles", "min", "max"}
    neutral.iges_facts(data)       # {"version", "units", "entities": {128: 12}, "surfaces",
                                   #  "min", "max"}
    neutral.parasolid_facts(data)  # {"format": "text"/"binary", "schema": "SCH_..."}
    neutral.jt_facts(data)         # {"version", "units", "shapes", "triangles", "min", "max"}
    neutral.dxf_model_facts(data)  # {"version", "units", "entities", "solids", "faces",
                                   #  "meshes", "min", "max"} (block references expanded)
    neutral.dwg_facts(data)        # {"version": "AC1018", "units": None} (header only)

Every reader takes the file's bytes and raises ``ValueError`` when the content is not
that format (or is truncated). Coordinates are the file's own, in ``units`` (an STL
states none). Bounds: STL and JT from the tessellation (within the chordal
tolerance of the true geometry), IGES from the control points of its curves and
surfaces with their transformation matrices applied (an upper bound). Parasolid
files are always in metres; only their header is read.
"""

from __future__ import annotations

import contextlib
import lzma
import math
import re
import struct
import zlib

SOLID_RECORDS = ("MANIFOLD_SOLID_BREP", "BREP_WITH_VOIDS", "FACETED_BREP")
STEP_SCHEMAS = (
    ("AP242", ("AP242", "MANAGED_MODEL_BASED_3D_ENGINEERING")),
    ("AP214", ("AUTOMOTIVE_DESIGN",)),
    ("AP203", ("CONFIG_CONTROL_DESIGN", "AP203")),
)
SI_PREFIX = {"MILLI": "mm", "CENTI": "cm", "MICRO": "um", "KILO": "km", "": "m"}
NAMED_UNITS = {"INCH": "in", "FOOT": "ft", "MILLIMETRE": "mm", "MILLIMETER": "mm"}
# IGES global parameter 14 (units flag).
IGES_UNITS = {
    1: "in", 2: "mm", 4: "ft", 5: "mi", 6: "m", 7: "km", 8: "mil", 9: "um", 10: "cm",
    11: "uin",
}  # fmt: skip
IGES_SURFACES = (108, 114, 118, 120, 122, 128, 140, 143, 144, 186, 190, 192, 194, 196, 198)
JT_UNITS = {"millimeters": "mm", "inches": "in", "meters": "m", "centimeters": "cm", "feet": "ft"}
# Partition Node Element object type 10dd103e-2ac8-11d1-9b6b-0080c7bb5997 (little endian).
JT_PARTITION = bytes.fromhex("3e10dd10c82ad1119b6b0080c7bb5997")
JT_SHAPE_SEGMENTS = range(6, 17)
JT_LSG = 1
JT_METADATA = 4


def _box(points):
    points = list(points)
    if not points:
        return None, None
    return [min(p[i] for p in points) for i in range(3)], [
        max(p[i] for p in points) for i in range(3)
    ]


# --- STEP ----------------------------------------------------------------------------


def step_facts(data: bytes) -> dict:
    """Schema (AP203/AP214/AP242), length units and solid count of a STEP file."""
    text = data.decode("latin-1")
    if not text.lstrip("﻿ \t\r\n").upper().startswith("ISO-10303-21"):
        raise ValueError("not a STEP file (no ISO-10303-21 marker)")
    if not re.search(r"END-ISO-10303-21\s*;\s*$", text, re.IGNORECASE):
        raise ValueError("the STEP file is truncated (no END-ISO-10303-21)")
    found = re.search(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']*)'", text, re.IGNORECASE)
    raw = found.group(1).upper() if found else ""
    schema = next((n for n, keys in STEP_SCHEMAS if any(k in raw for k in keys)), raw or None)
    solids = sum(len(re.findall(r"\b" + name + r"\s*\(", text)) for name in SOLID_RECORDS)
    return {"schema": schema, "units": _step_units(text), "solids": solids}


def _step_units(text: str) -> str | None:
    for group in re.findall(r"\(([^;]*?LENGTH_UNIT\s*\(\s*\)[^;]*?)\)\s*;", text):
        si = re.search(r"SI_UNIT\s*\(\s*(?:\.(\w+)\.|\$)\s*,\s*\.METRE\.", group)
        if si:
            return SI_PREFIX.get((si.group(1) or "").upper(), si.group(1))
        named = re.search(r"CONVERSION_BASED_UNIT\s*\(\s*'([^']*)'", group)
        if named:
            name = named.group(1).upper()
            return NAMED_UNITS.get(name, name.lower())
    return None


# --- STL -----------------------------------------------------------------------------


def stl_facts(data: bytes) -> dict:
    """Triangle count and vertex bounds of a binary or ASCII STL file."""
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if len(data) == 84 + 50 * count:
            return _stl_binary(data, count)
    head = data[:512].lstrip().lower()
    if head.startswith(b"solid"):
        return _stl_ascii(data.decode("latin-1"))
    raise ValueError("not an STL file (size does not match its triangle count, no 'solid')")


def _stl_binary(data, count):
    lows, highs = [float("inf")] * 3, [float("-inf")] * 3
    for i in range(count):
        v = struct.unpack_from("<12f", data, 84 + 50 * i)
        for k in range(3):
            values = (v[3 + k], v[6 + k], v[9 + k])
            lows[k] = min(lows[k], *values)
            highs[k] = max(highs[k], *values)
    low, high = (lows, highs) if count else (None, None)
    return {"binary": True, "triangles": count, "min": low, "max": high}


def _stl_ascii(text):
    if not re.search(r"endsolid", text[-512:], re.IGNORECASE):
        raise ValueError("the ASCII STL file is truncated (no endsolid)")
    facets = len(re.findall(r"\bfacet\b", text, re.IGNORECASE))
    number = r"([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)"
    points = [
        tuple(float(v) for v in m)
        for m in re.findall(r"\bvertex\s+" + r"\s+".join([number] * 3), text, re.IGNORECASE)
    ]
    if len(points) != 3 * facets:
        raise ValueError(f"the ASCII STL file has {facets} facets but {len(points)} vertices")
    low, high = _box(points)
    return {"binary": False, "triangles": facets, "min": low, "max": high}


# --- IGES ----------------------------------------------------------------------------


def _iges_tokens(text: str, delim: str, end: str) -> list:
    """Parameters of one record: strings (Hollerith ``nH...``), numbers or None."""
    out: list = []
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] == " ":
            i += 1
        hol = re.match(r"(\d+)H", text[i:])
        if hol:
            start = i + hol.end()
            out.append(text[start : start + int(hol.group(1))])
            i = start + int(hol.group(1))
        else:
            j = i
            while j < n and text[j] not in (delim, end):
                j += 1
            token = text[i:j].strip()
            out.append(_iges_number(token) if token else None)
            i = j
        while i < n and text[i] == " ":
            i += 1
        if i >= n or text[i] == end:
            break
        i += 1  # the parameter delimiter
    return out


def _iges_number(token: str):
    try:
        return float(token.replace("D", "E").replace("d", "e"))
    except ValueError:
        return token


def _iges_sections(data: bytes) -> dict:
    sections: dict = {}
    for line in data.decode("latin-1").splitlines():
        if len(line) < 73:
            continue
        sections.setdefault(line[72], []).append(line)
    if not sections.get("G") or not sections.get("D") or not sections.get("T"):
        raise ValueError("not an IGES file (no G, D and T sections) or truncated")
    return sections


def iges_facts(data: bytes) -> dict:
    """Version, units, entity counts and control-point bounds of an IGES file."""
    sections = _iges_sections(data)
    glob = "".join(line[:72] for line in sections["G"])
    delim, end = ",", ";"
    if glob.startswith("1H"):
        delim = glob[2]
        if glob[3:5] == "1H":
            end = glob[5]
    params = _iges_tokens(glob, delim, end)
    if len(params) < 15:
        raise ValueError("the IGES global section is incomplete")
    flag = int(params[13]) if isinstance(params[13], float) else 0
    units = IGES_UNITS.get(flag) if flag != 3 else str(params[14]).lower() or None
    version = {11: "5.3", 10: "5.2", 9: "5.1", 8: "5.0"}.get(
        int(params[22]) if len(params) > 22 and isinstance(params[22], float) else 0
    )
    entries = _iges_directory(sections["D"])
    parameters = _iges_parameters(sections.get("P", []), delim, end)
    counts: dict = {}
    for entry in entries.values():
        counts[entry["type"]] = counts.get(entry["type"], 0) + 1
    points = []
    for seq, entry in entries.items():
        if entry["use"] in ("01", "02", "05"):  # annotation, definition, parameter space
            continue
        for p in _iges_points(entry["type"], parameters.get(seq, [])):
            points.append(_iges_transform(p, entry["matrix"], entries, parameters))
    low, high = _box(points)
    surfaces = sum(n for t, n in counts.items() if t in IGES_SURFACES)
    return {
        "version": version,
        "units": units,
        "entities": counts,
        "surfaces": surfaces,
        "min": low,
        "max": high,
    }


def _iges_directory(lines: list) -> dict:
    entries = {}
    for i in range(0, len(lines) - 1, 2):
        first, second = lines[i], lines[i + 1]
        try:
            kind = int(first[0:8])
            matrix = int(first[48:56] or 0)
        except ValueError as error:
            raise ValueError(f"bad IGES directory entry: {first[:72]!r}") from error
        status = first[64:72].strip().zfill(8)
        seq = int(first[73:80])
        entries[seq] = {"type": kind, "matrix": matrix, "use": status[4:6], "form": second[32:40]}
    return entries


def _iges_parameters(lines: list, delim: str, end: str) -> dict:
    chunks: dict = {}
    for line in lines:
        try:
            pointer = int(line[64:72])
        except ValueError:
            continue
        chunks.setdefault(pointer, []).append(line[:64])
    return {seq: _iges_tokens("".join(parts), delim, end) for seq, parts in chunks.items()}


def _numbers(values) -> list:
    return [v for v in values if isinstance(v, float)]


def _iges_points(kind: int, params: list) -> list:
    v = _numbers(params[1:])  # params[0] is the entity type
    try:
        if kind == 110 and len(v) >= 6:  # line
            return [v[0:3], v[3:6]]
        if kind == 116 and len(v) >= 3:  # point
            return [v[0:3]]
        if kind == 100 and len(v) >= 7:  # circular arc in its plane z = v[0]
            z, cx, cy, sx, sy = v[0], v[1], v[2], v[3], v[4]
            r = ((sx - cx) ** 2 + (sy - cy) ** 2) ** 0.5
            return [[cx - r, cy - r, z], [cx + r, cy + r, z]]
        if kind == 126:  # rational B-spline curve: K, M, flags[4], knots, weights, points
            k, m = int(v[0]), int(v[1])
            start = 6 + (k + m + 2) + (k + 1)
            return [v[start + 3 * i : start + 3 * i + 3] for i in range(k + 1)]
        if kind == 128:  # rational B-spline surface
            k1, k2, m1, m2 = (int(x) for x in v[0:4])
            count = (k1 + 1) * (k2 + 1)
            start = 9 + (k1 + m1 + 2) + (k2 + m2 + 2) + count
            return [v[start + 3 * i : start + 3 * i + 3] for i in range(count)]
    except (IndexError, ValueError) as error:
        raise ValueError(f"bad IGES parameters for entity {kind}") from error
    return []


def _iges_transform(point, matrix: int, entries: dict, parameters: dict, depth: int = 0):
    if not matrix or depth > 8:
        return list(point)
    v = _numbers(parameters.get(matrix, [])[1:])
    if len(v) < 12:
        raise ValueError(f"bad IGES transformation matrix {matrix}")
    x, y, z = point
    moved = [v[4 * r] * x + v[4 * r + 1] * y + v[4 * r + 2] * z + v[4 * r + 3] for r in range(3)]
    parent = entries.get(matrix, {}).get("matrix", 0)
    return _iges_transform(moved, parent, entries, parameters, depth + 1)


# --- Parasolid -----------------------------------------------------------------------


def parasolid_facts(data: bytes) -> dict:
    """Format (text/binary) and schema from a Parasolid transmit file's header."""
    end = data.find(b"**END_OF_HEADER")
    if b"PARASOLID" not in data[:400] or end < 0:
        raise ValueError("not a Parasolid transmit file (no PARASOLID header)")
    header = data[:end].decode("latin-1")
    found = re.search(r"FORMAT=(\w+);", header)
    schema = re.search(r"SCH=(\w+);", header)
    body = data[end:].split(b"\n", 1)
    if len(body) < 2 or not body[1].strip():
        raise ValueError("the Parasolid file holds no data after its header")
    return {
        "format": found.group(1).lower() if found else None,
        "schema": schema.group(1) if schema else None,
        "units": "m",
    }


# --- DXF / DWG (3D model export) -----------------------------------------------------


DXF_UNITS = {1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m", 13: "um"}
DXF_POINTS = ("3DFACE", "VERTEX", "LINE", "POINT", "SOLID")
DXF_DEPTH = 8  # nested block references followed


def _dxf_records(data: bytes) -> tuple[dict, list, dict]:
    """``(header, entities, blocks)``: entities as ``(type, {code: [values]})``."""
    if data.startswith(b"AutoCAD Binary DXF"):
        raise ValueError("binary DXF is not read")
    lines = data.decode("latin-1").lstrip("\ufeff").splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) % 2:
        raise ValueError("odd number of lines (truncated group)")
    try:
        pairs = [(int(lines[i]), lines[i + 1].strip()) for i in range(0, len(lines), 2)]
    except ValueError:
        raise ValueError("a group code is not an integer") from None
    if not pairs or pairs[-1] != (0, "EOF"):
        raise ValueError("no EOF marker (truncated?)")
    header: dict[str, str] = {}
    entities: list = []
    blocks: dict[str, dict] = {}
    section = var = None
    record: tuple | None = None
    block: dict | None = None
    for code, value in pairs:
        if code == 0:
            record = None
            if value == "SECTION":
                section = "?"
            elif value == "ENDSEC":
                section = None
            elif section == "BLOCKS" and value == "BLOCK":
                block = {"name": None, "base": [0.0, 0.0, 0.0], "items": []}
                record = ("BLOCK", {})
            elif section == "BLOCKS" and value == "ENDBLK":
                if block is not None and block["name"]:
                    blocks[block["name"]] = block
                block = None
            elif section == "ENTITIES":
                record = (value, {})
                entities.append(record)
            elif section == "BLOCKS" and block is not None:
                record = (value, {})
                block["items"].append(record)
            continue
        if code == 2 and section == "?":
            section = value
        elif section == "HEADER":
            if code == 9:
                var = value
            elif var in ("$ACADVER", "$INSUNITS"):
                header[var] = value
        elif record is not None:
            if record[0] == "BLOCK" and block is not None:
                if code == 2:
                    block["name"] = value
                elif code in (10, 20, 30):
                    with contextlib.suppress(ValueError):
                        block["base"][code // 10 - 1] = float(value)
            else:
                record[1].setdefault(code, []).append(value)
    return header, entities, blocks


def _dxf_float(values: dict, code: int, default: float) -> float:
    try:
        return float(values.get(code, [default])[0])
    except ValueError:
        return default


def _dxf_expand(items, blocks, place, counts, points, depth=0) -> bool:
    """Count ``items`` (block references expanded) and collect their vertices through
    ``place``; False when a reference cannot be placed exactly (extruded, missing)."""
    exact = True
    for kind, values in items:
        if kind == "INSERT":
            block = blocks.get((values.get(2) or [""])[0])
            normal = [_dxf_float(values, c, d) for c, d in ((210, 0.0), (220, 0.0), (230, 1.0))]
            if block is None or depth >= DXF_DEPTH or normal != [0.0, 0.0, 1.0]:
                counts[kind] = counts.get(kind, 0) + 1
                exact = False
                continue
            at = [_dxf_float(values, c, 0.0) for c in (10, 20, 30)]
            scale = [_dxf_float(values, c, 1.0) for c in (41, 42, 43)]
            angle = math.radians(_dxf_float(values, 50, 0.0))
            cos, sin, base = math.cos(angle), math.sin(angle), block["base"]

            def inner(p, at=at, scale=scale, cos=cos, sin=sin, base=base):
                x, y, z = ((p[i] - base[i]) * scale[i] for i in range(3))
                return place([at[0] + cos * x - sin * y, at[1] + sin * x + cos * y, at[2] + z])

            exact &= _dxf_expand(block["items"], blocks, inner, counts, points, depth + 1)
            continue
        counts[kind] = counts.get(kind, 0) + 1
        if kind in DXF_POINTS:
            for base in (10, 11, 12, 13):
                if base in values:
                    points.append(place([_dxf_float(values, base + 10 * k, 0.0)
                                         for k in range(3)]))  # fmt: skip
    return exact


def dxf_model_facts(data: bytes) -> dict:
    """Version, units, entity counts (block references expanded), solids (3DSOLID/BODY),
    faces (3DFACE), meshes and vertex bounds of an ASCII DXF file."""
    header, entities, blocks = _dxf_records(data)
    if not entities:
        raise ValueError("no entities in the ENTITIES section")
    counts: dict[str, int] = {}
    points: list = []
    exact = _dxf_expand(entities, blocks, lambda p: p, counts, points)
    low, high = _box(points) if exact else (None, None)
    return {
        "version": header.get("$ACADVER", "AC1009"),
        "units": DXF_UNITS.get(int(float(header.get("$INSUNITS", "0") or 0))),
        "entities": counts,
        "solids": sum(counts.get(k, 0) for k in ("3DSOLID", "BODY")),
        "faces": counts.get("3DFACE", 0),
        "meshes": sum(counts.get(k, 0) for k in ("POLYLINE", "MESH")),
        "min": low,
        "max": high,
    }


def dwg_facts(data: bytes) -> dict:
    """The AC10xx version code of a DWG file (its content is not read)."""
    if not re.match(rb"AC10\d\d", data[:6]) or len(data) < 0x100:
        raise ValueError("not a DWG file (no AC10xx header)")
    return {"version": data[:6].decode("ascii"), "units": None}


# --- JT ------------------------------------------------------------------------------


def jt_facts(data: bytes) -> dict:
    """Version, units, shape segments and partition bounds of a monolithic JT file."""
    version = data[:80].decode("latin-1").strip()
    found = re.match(r"Version (\d+)\.(\d+) JT", version)
    if not found or len(data) < 120:
        raise ValueError("not a JT file (no 'Version n.n JT' header)")
    major = int(found.group(1))
    if data[80] != 0:
        raise ValueError("big-endian JT files are not read")
    if major >= 10:
        toc = struct.unpack_from("<Q", data, 85)[0]
        entry, size = "<16sQII", 32
    else:
        toc = struct.unpack_from("<i", data, 85)[0]
        entry, size = "<16siII", 28
    try:
        count = struct.unpack_from("<i", data, toc)[0]
        segments = [struct.unpack_from(entry, data, toc + 4 + size * i)[1:] for i in range(count)]
    except struct.error as error:
        raise ValueError("the JT table of contents is truncated") from error
    kinds = [attributes >> 24 for _, _, attributes in segments]
    facts = {
        "version": f"{found.group(1)}.{found.group(2)}",
        "units": None,
        "shapes": sum(1 for k in kinds if k in JT_SHAPE_SEGMENTS),
        "triangles": None,
        "min": None,
        "max": None,
    }
    for (offset, length, _), kind in zip(segments, kinds, strict=True):
        if offset + length > len(data):
            raise ValueError("a JT segment lies beyond the end of the file")
        raw = _jt_payload(data[offset + 24 : offset + length])
        if raw is None:
            continue
        if kind == JT_LSG and facts["min"] is None:
            facts.update(_jt_partition(raw))
        if kind == JT_METADATA and facts["units"] is None:
            facts["units"] = _jt_units(raw)
    return facts


def _jt_payload(body: bytes) -> bytes | None:
    """The data of a compressed segment (zlib or LZMA), or None when not compressed."""
    if len(body) < 9:
        return None
    flag, length, algorithm = struct.unpack_from("<iiB", body)
    if flag not in (2, 3) or algorithm not in (2, 3) or length - 1 > len(body) - 9:
        return None
    packed = body[9 : 9 + length - 1]
    try:
        return lzma.decompress(packed) if algorithm == 3 else zlib.decompress(packed)
    except (lzma.LZMAError, zlib.error) as error:
        raise ValueError("a JT segment does not decompress") from error


def _jt_partition(raw: bytes) -> dict:
    """Transformed bounds and polygon count of the top Partition Node element."""
    if raw[4:20] != JT_PARTITION:
        return {}
    end = 4 + struct.unpack_from("<i", raw, 0)[0]
    for k in range(21, end - 8):
        n = struct.unpack_from("<i", raw, k)[0]
        if not 4 <= n <= 260 or k + 4 + 2 * n + 48 > end:
            continue
        try:
            name = raw[k + 4 : k + 4 + 2 * n].decode("utf-16-le")
        except UnicodeDecodeError:
            continue
        if not name.lower().endswith(".jt") or not name.isprintable():
            continue
        at = k + 4 + 2 * n
        box = struct.unpack_from("<6f", raw, at)
        polygons = struct.unpack_from("<ii", raw, at + 24 + 4 + 16)
        return {"min": list(box[:3]), "max": list(box[3:]), "triangles": polygons[1]}
    return {}


def _jt_units(raw: bytes) -> str | None:
    texts = [t.decode("utf-16-le") for t in re.findall(rb"(?:[\x20-\x7e]\x00){2,}", raw)]
    for i, text in enumerate(texts[:-1]):
        if text == "JT_PROP_MEASUREMENT_UNITS::":
            name = texts[i + 1].strip().lower()
            return JT_UNITS.get(name, name)
    return None
