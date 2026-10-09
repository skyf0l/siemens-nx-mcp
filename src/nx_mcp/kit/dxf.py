"""Facts about exported 2D files: ASCII DXF entities and extents, Trumpf GEO bounds.

    facts = dxf.dxf_facts(data)   # {"version", "entities": {"LINE": 5}, "layers", "min", "max",
                                  #  "extents"}; extents [dx, dy] of the ENTITIES section
    facts = dxf.geo_facts(data)   # {"points", "entities", "area", "thickness", "min", "max",
                                  #  "extents"} of a Trumpf GEO file

Coordinates are the file's own (NX writes flat patterns in part units). Arcs and
circles are bounded exactly (their axis extremes inside the swept angle), polylines by
their vertices, splines by their control points (an upper bound). A binary DXF has no
readable entities: ``version`` is ``"binary"`` and ``extents`` None.
"""

from __future__ import annotations

import math

BINARY_DXF = b"AutoCAD Binary DXF"
POINT_CODES = {10: 0, 20: 1, 11: 0, 21: 1}


def _pairs(text):
    lines = text.splitlines()
    for i in range(0, len(lines) - 1, 2):
        try:
            code = int(lines[i].strip())
        except ValueError:
            continue
        yield code, lines[i + 1].strip()


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def arc_points(center, radius, start=0.0, end=360.0):
    """The extreme points of an arc (degrees, counter-clockwise from ``start``)."""
    cx, cy = center
    span = (end - start) % 360.0 or 360.0
    angles = [start, start + span]
    first = math.ceil(start / 90.0) * 90.0
    angles += [a for a in (first + 90.0 * k for k in range(5)) if a < start + span]
    return [
        (cx + radius * math.cos(math.radians(a)), cy + radius * math.sin(math.radians(a)))
        for a in angles
    ]


class _Box:
    def __init__(self):
        self.low = [math.inf, math.inf]
        self.high = [-math.inf, -math.inf]

    def add(self, x, y):
        if x is None or y is None:
            return
        self.low = [min(self.low[0], x), min(self.low[1], y)]
        self.high = [max(self.high[0], x), max(self.high[1], y)]

    def facts(self):
        if not math.isfinite(self.low[0]):
            return {"min": None, "max": None, "extents": None}
        return {
            "min": list(self.low),
            "max": list(self.high),
            "extents": [self.high[0] - self.low[0], self.high[1] - self.low[1]],
        }


def _bound_entity(box, kind, record):
    points = record.get("points", [])
    if kind in ("ARC", "CIRCLE"):
        center = record.get("center")
        radius = record.get(40)
        if center is None or radius is None:
            return
        start, end = (record.get(50, 0.0), record.get(51, 360.0)) if kind == "ARC" else (0, 360)
        for x, y in arc_points(center, radius, start, end):
            box.add(x, y)
        return
    for x, y in points:
        box.add(x, y)


def dxf_facts(data: bytes) -> dict:
    """Version, entity counts by type, layers and 2D extents of an ASCII DXF."""
    if data.startswith(BINARY_DXF):
        return {"version": "binary", "entities": {}, "layers": [], "min": None, "max": None,
                "extents": None}  # fmt: skip
    text = data.decode("latin-1")
    version = None
    entities: dict[str, int] = {}
    layers: set[str] = set()
    box = _Box()
    section = previous = None
    kind, record, pending = None, {}, [None, None]
    want_version = False

    def flush():
        if kind is not None and section == "ENTITIES" and kind not in ("SEQEND",):
            if kind != "VERTEX":
                entities[kind] = entities.get(kind, 0) + 1
            _bound_entity(box, kind, record)

    for code, value in _pairs(text):
        if code == 0:
            flush()
            if value in ("SECTION", "ENDSEC"):
                section = None
            kind = value if section == "ENTITIES" else None
            record, pending = {"points": []}, [None, None]
            previous = value
            continue
        if code == 2 and previous == "SECTION" and section is None:
            section = value
            continue
        if section == "HEADER":
            if code == 9:
                want_version = value == "$ACADVER"
            elif code == 1 and want_version:
                version, want_version = value, False
            continue
        if kind is None:
            continue
        if code == 8:
            layers.add(value)
        elif code in POINT_CODES:
            number = _number(value)
            pending[POINT_CODES[code]] = number
            if code in (20, 21):
                if kind in ("ARC", "CIRCLE") and code == 20:
                    record["center"] = (pending[0], pending[1])
                else:
                    record["points"].append((pending[0], pending[1]))
                pending = [None, None]
        elif code in (40, 50, 51):
            number = _number(value)
            if number is not None:
                record[code] = number
    flush()
    return {"version": version, "entities": entities, "layers": sorted(layers), **box.facts()}


def _block(lines, name):
    """The lines of a GEO ``#~<name>`` block (up to its ``##~~``), or []."""
    try:
        start = lines.index("#~" + name) + 1
    except ValueError:
        return []
    end = lines.index("##~~", start) if "##~~" in lines[start:] else len(lines)
    return lines[start:end]


def geo_facts(data: bytes) -> dict:
    """Entity counts, 2D bounds, area and thickness of a Trumpf GEO file.

    GEO lists points in a ``#~31`` block as ``P``, an index line and an ``x y z`` line;
    lines (``LIN``) and arcs (``ARC``) refer to them, so the points bound the outline.
    The ``#~1`` header holds the bounding box and the area (6th line), the ``#~11``
    block the sheet thickness (its first value).
    """
    lines = [line.strip() for line in data.decode("latin-1").splitlines()]
    box = _Box()
    points: dict[int, tuple[float, float]] = {}
    entities: dict[str, int] = {}
    header = _block(lines, "1")
    area = _number(header[5]) if len(header) > 5 else None
    thickness = next((_number(v) for v in _block(lines, "11") if v), None)
    i = 0
    while i < len(lines):
        word = lines[i]
        if word == "P" and i + 2 < len(lines):
            try:
                index = int(lines[i + 1].split()[0])
                coords = [float(v) for v in lines[i + 2].split()[:2]]
            except (ValueError, IndexError):
                i += 1
                continue
            points[index] = (coords[0], coords[1])
            box.add(*coords)
            i += 3
            continue
        if word in ("LIN", "ARC", "CIR"):
            entities[word] = entities.get(word, 0) + 1
        i += 1
    return {
        "points": len(points),
        "entities": entities,
        "area": area,
        "thickness": thickness,
        **box.facts(),
    }
