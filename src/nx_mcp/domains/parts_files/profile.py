"""1:1 profile DXF of a sketch or planar face (tool-surface 3.2), without model changes.

Lines, arcs and circles are written analytically in millimetres; any other curve
is refused (never approximated). The file is DXF R12 (no handles), which NX
imports back. The default frame is the sketch's, or for a face its plane with x
along the global axis projected into it; the result states the frame used.
Existing files are never overwritten.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import re
from collections import Counter
from pathlib import Path
from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, PathTo, Point3, RefKind, RefTo, Text, Vec3
from nx_mcp.kit import args, files, geom, select
from nx_mcp.kit import sketch as kit_sketch
from nx_mcp.kit import units as kit_units

FACE_PLANE = 22
MAX_CURVES = 20_000
LAYER_PATTERN = r"^[A-Za-z0-9_.\-]{1,64}$"
Layer = Annotated[str, Text(64), Doc("DXF layer name")]
SOURCE_DOC = "Sketch s:N or planar face fc:N ref (nx_list_objects); not a name"
LAYERS_DOC = "[ref, layer] pairs: sketch curves c:N or face edges e:N"
MAX_LISTED = 50
CRLF = "\r\n"  # DXF line ends as AutoCAD writes them
XYZ = tuple[float, float, float]


# [origin, x_axis, y_axis] in work-part coordinates; [curve or edge ref, layer name].
Frame = Annotated[tuple[XYZ, XYZ, XYZ], Doc("[origin, x_axis, y_axis] in the source plane")]
LayerOverride = tuple[Annotated[str, Text(128), RefKind("curve", "edge")], Annotated[str, Text(64)]]


class ProfileFrame(TypedDict):
    origin: Point3
    x_axis: Vec3
    y_axis: Vec3
    normal: Vec3


class ProfileData(TypedDict):
    path: str
    size: int
    sha256: str
    units: str
    source_units: str
    frame: ProfileFrame
    entity_count: int
    entity_counts: dict


def dxf_text(entities) -> str:
    """An ASCII DXF R12 (AC1009) in millimetres with the layers the entities use.

    R12 needs no entity handles, OBJECTS section or subclass markers, so NX (which reads
    R13+ DXF only as AutoCAD writes it) imports it with nx_import_model, as do laser and
    CAM programs. ``$INSUNITS`` 4 states millimetres for readers that honour it.
    """
    rows: list[str] = []

    def put(*pairs):
        for code, value in pairs:
            rows.extend(
                (str(code), format(value, ".15g") if isinstance(value, float) else str(value))
            )

    put((0, "SECTION"), (2, "HEADER"), (9, "$ACADVER"), (1, "AC1009"))
    put((9, "$INSUNITS"), (70, 4), (0, "ENDSEC"))
    layers = sorted({e["layer"] for e in entities} | {"0"})
    put((0, "SECTION"), (2, "TABLES"), (0, "TABLE"), (2, "LTYPE"), (70, 1))
    put((0, "LTYPE"), (2, "CONTINUOUS"), (70, 0), (3, "Solid line"), (72, 65), (73, 0))
    put((40, 0.0), (0, "ENDTAB"), (0, "TABLE"), (2, "LAYER"), (70, len(layers)))
    for layer in layers:
        put((0, "LAYER"), (2, layer), (70, 0), (62, 7), (6, "CONTINUOUS"))
    put((0, "ENDTAB"), (0, "ENDSEC"), (0, "SECTION"), (2, "ENTITIES"))
    for e in entities:
        kind = e["type"]
        put((0, kind), (8, e["layer"]))
        p = e["start"] if kind == "LINE" else e["center"]
        put((10, p[0]), (20, p[1]), (30, 0.0))
        if kind == "LINE":
            put((11, e["end"][0]), (21, e["end"][1]), (31, 0.0))
        else:
            put((40, e["radius"]))
            if kind == "ARC":
                put((50, e["start_angle"]), (51, e["end_angle"]))
    put((0, "ENDSEC"), (0, "EOF"))
    return CRLF.join(rows) + CRLF


def _face_frame(tx, face) -> dict:
    kind, point, normal = select.face_data(tx, face)
    if kind != FACE_PLANE:
        fail("NX_WRONG_GEOMETRY", "Select a planar face or a sketch", field="/source")
    n = geom.unit(normal, "/source")
    seed = [1.0, 0.0, 0.0] if abs(n[0]) < 0.9 else [0.0, 1.0, 0.0]
    x = geom.unit(geom.sub(seed, geom.scale(n, geom.dot(seed, n))), "/source")
    return {"origin": point, "x_axis": x, "y_axis": geom.cross(n, x), "normal": n}


def _layers(tx, overrides) -> dict:
    out = {}
    for i, (ref, name) in enumerate(overrides or []):
        if not re.match(LAYER_PATTERN, name):
            fail("NX_INVALID_ARGUMENT", "Layer names use 1-64 of A-Z a-z 0-9 _ . -",
                 field=f"/layers/{i}/1")  # fmt: skip
        obj = tx.resolve(ref, "curve", "edge")
        tag = int(obj.Tag)
        if tag in out:
            fail("NX_INVALID_ARGUMENT", "An object is listed twice", field=f"/layers/{i}/0")
        out[tag] = (name, i)
    return out


@tool(
    effect="file",
    label="Export profile DXF",
    summary="Export profile of {source} to {path}",
    keywords=("dxf", "profile export", "laser cut", "outline", "flat dxf", "2d export"),
    requires=("work_part",),
    cost="light",
    example={"source": "s:3", "path": "exports/plate.dxf"},
)
def nx_export_profile(
    tx: Tx,
    *,
    source: Annotated[str, RefTo("sketch", "face"), Doc(SOURCE_DOC)],
    path: Annotated[str, PathTo("new", ".dxf")],
    frame: Frame | None = None,
    layer: Layer = "0",
    layers: Annotated[list[LayerOverride], Items(1, 1000), Doc(LAYERS_DOC)] | None = None,
) -> ProfileData:
    """Export a sketch or planar face as a 1:1 mm DXF R12 of lines, arcs and circles
    (nx_import_model reads it back). Other curves are refused."""
    if not re.match(LAYER_PATTERN, layer):
        fail("NX_INVALID_ARGUMENT", "Layer names use 1-64 of A-Z a-z 0-9 _ . -", field="/layer")
    if frame is not None:
        geom.orthonormal_frame(frame[1], frame[2], field="/frame")
    with tx.checks():
        obj = select.one(tx, source, "sketch", "face", field="/source")
        overrides = _layers(tx, layers)
        if tx.kind_of(obj) == "sketch":
            plane = kit_sketch.frame(obj)
            curves, kind = list(obj.GetAllGeometry()), "curve"
        else:
            plane = _face_frame(tx, obj)
            curves, kind = list(obj.GetEdges()), "edge"
        if not curves or len(curves) > MAX_CURVES:
            fail("NX_TOO_MANY_OBJECTS" if curves else "NX_WRONG_GEOMETRY",
                 f"The source must have 1..{MAX_CURVES} curves", field="/source")  # fmt: skip
        if frame is not None:
            origin = args.vec3(frame[0], "/frame")
            x, y, normal = geom.orthonormal_frame(frame[1], frame[2], field="/frame")
            if not geom.parallel(normal, plane["normal"], 1e-8):
                fail("NX_WRONG_GEOMETRY", "The frame must lie in the source plane", field="/frame")
        else:
            origin, x, y = plane["origin"], plane["x_axis"], plane["y_axis"]
            normal = geom.cross(x, y)
        part_units = kit_units.part_units(tx)
        scale = kit_units.factor(part_units, "mm")
        entities = _entities(tx, curves, kind, origin, x, y, normal, scale, layer, overrides)
        unused = [
            i for tag, (_l, i) in overrides.items() if tag not in {e["tag"] for e in entities}
        ]
        if unused:
            owned = "curves (c:N) of the sketch" if kind == "curve" else "edges (e:N) of the face"
            fail(
                "NX_INVALID_ARGUMENT",
                f"Layer overrides must name {owned} given as source",
                field=f"/layers/{unused[0]}/0",
                hint="Use one of the refs in details.valid",
                details={
                    "valid": [tx.ref(c, kind)["id"] for c in curves[:MAX_LISTED]],
                    "valid_count": len(curves),
                },
            )
    raw = dxf_text(entities).encode("ascii")
    target = Path(path)
    tx.touched = True
    files.write_new(target, raw)
    return {
        "path": files.relative(target, tx.workspace.root),
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "units": "mm",
        "source_units": part_units,
        "frame": {"origin": origin, "x_axis": x, "y_axis": y, "normal": normal},
        "entity_count": len(entities),
        "entity_counts": dict(Counter(e["type"] for e in entities)),
    }


def _entities(tx, curves, kind, origin, x, y, normal, scale, layer, overrides) -> list:
    uf = tx.uf

    def local(p):
        delta = geom.sub([float(v) for v in p], origin)
        if abs(geom.dot(delta, normal)) * scale > 1e-6:
            fail(
                "NX_WRONG_GEOMETRY",
                "Source geometry or the frame origin is off the output plane",
                field="/frame",
            )
        return [geom.dot(delta, x) * scale, geom.dot(delta, y) * scale]

    out = []
    for curve in curves:
        evaluator = uf.Eval.Initialize2(curve.Tag)
        try:
            limits = uf.Eval.AskLimits(evaluator)
            start = local(uf.Eval.EvaluateUnitVectors(evaluator, limits[0])[0])
            end = local(uf.Eval.EvaluateUnitVectors(evaluator, limits[1])[0])
            name, _ = overrides.get(int(curve.Tag), (layer, None))
            e = {"tag": int(curve.Tag), "layer": name}
            if uf.Eval.IsLine(evaluator):
                e.update(type="LINE", start=start, end=end)
            elif uf.Eval.IsArc(evaluator):
                arc = uf.Eval.AskArc(evaluator)
                center = local(arc.Center)
                turn = geom.dot(geom.cross(list(arc.XAxis), list(arc.YAxis)), normal)
                if abs(abs(turn) - 1) > 1e-8:
                    fail("NX_WRONG_GEOMETRY", "A circular curve is not in the output plane",
                         field="/source")  # fmt: skip
                full = abs(abs(limits[1] - limits[0]) - 2 * math.pi) < 1e-8
                e.update(type="CIRCLE" if full else "ARC", center=center, radius=arc.Radius * scale)
                if not full:
                    if turn < 0:
                        start, end = end, start
                    e.update(
                        start_angle=math.degrees(math.atan2(start[1] - center[1], start[0] - center[0])) % 360,
                        end_angle=math.degrees(math.atan2(end[1] - center[1], end[0] - center[0])) % 360,
                    )  # fmt: skip
            else:
                fail(
                    "NX_UNSUPPORTED_GEOMETRY",
                    "Only lines, arcs and circles are exported; this curve would need "
                    "approximation",
                    field="/source",
                    details={"object": tx.ref(curve, kind)},
                )
        finally:
            free = getattr(uf.Eval, "Free", None)
            if free is not None:
                with contextlib.suppress(Exception):
                    free(evaluator)
        out.append(e)
        tx.checkpoint()
    return out
