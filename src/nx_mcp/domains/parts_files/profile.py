"""1:1 profile DXF of a sketch or planar face (tool-surface 3.2), without model changes.

Lines, arcs and circles are written analytically in millimetres (or inches); any other
curve is refused unless ``tolerance`` is given, which writes it as lines whose chord
error stays within it (subdivided until each midpoint lies within the tolerance). The
file is DXF R12 (no handles), which NX imports back. The default frame is the sketch's,
or for a face its plane through the point NX gives for it (often its centre) with x
along the global axis projected into it; ``origin`` ``min_corner`` moves the profile so
its lowest x and y are 0. The result states the frame used. Existing files are replaced
only with ``overwrite``.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import re
from collections import Counter
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Length, PathTo, Point3, Range, RefKind, RefTo, Text, Vec3
from nx_mcp.kit import args, files, geom, select
from nx_mcp.kit import sketch as kit_sketch
from nx_mcp.kit import units as kit_units

FACE_PLANE = 22
MAX_CURVES = 20_000
MAX_SEGMENTS = 4096  # lines written for one approximated curve, at most
LAYER_PATTERN = r"^[A-Za-z0-9_.\-]{1,64}$"
Layer = Annotated[str, Text(64), Doc("DXF layer name")]
SOURCE_DOC = "Sketch s:N or planar face fc:N ref (nx_list_objects); not a name"
LAYERS_DOC = "[ref, layer] pairs: sketch curves c:N or face edges e:N"
MAX_LISTED = 50
CRLF = "\r\n"  # DXF line ends as AutoCAD writes them
XYZ = tuple[float, float, float]
INSUNITS = {"mm": 4, "in": 1}  # DXF $INSUNITS
FRAME_HINT = (
    "Give frame as [origin, x_axis, y_axis]: an origin in the source plane and two "
    "perpendicular directions in it, or omit frame to use the source's"
)


# [origin, x_axis, y_axis] in work-part coordinates; [curve or edge ref, layer name].
Frame = Annotated[
    tuple[XYZ, XYZ, XYZ],
    Doc(
        "[origin, x_axis, y_axis] in the source plane; default: the sketch's, or the face's "
        "point (often its centre)"
    ),
]
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
    approximated: Annotated[int, Doc("Curves written as lines within tolerance")]
    replaced: bool


def dxf_text(entities, units: str = "mm") -> str:
    """An ASCII DXF R12 (AC1009) in ``units`` (mm or in) with the layers the entities use.

    R12 needs no entity handles, OBJECTS section or subclass markers, so NX (which reads
    R13+ DXF only as AutoCAD writes it) imports it with nx_import_model, as do laser and
    CAM programs. ``$INSUNITS`` (4 millimetres, 1 inches) states the units for readers
    that honour it.
    """
    rows: list[str] = []

    def put(*pairs):
        for code, value in pairs:
            rows.extend(
                (str(code), format(value, ".15g") if isinstance(value, float) else str(value))
            )

    put((0, "SECTION"), (2, "HEADER"), (9, "$ACADVER"), (1, "AC1009"))
    put((9, "$INSUNITS"), (70, INSUNITS[units]), (0, "ENDSEC"))
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


def _frame_axes(frame) -> tuple:
    """Unit x and y axes of ``frame`` and their normal: any length, perpendicular."""
    x, y = args.vec3(frame[1], "/frame"), args.vec3(frame[2], "/frame")
    lengths = geom.norm(x), geom.norm(y)
    if min(lengths) < 1e-12:
        fail("NX_INVALID_ARGUMENT", "x_axis and y_axis must not be zero", field="/frame",
             hint=FRAME_HINT)  # fmt: skip
    x, y = geom.scale(x, 1 / lengths[0]), geom.scale(y, 1 / lengths[1])
    cosine = geom.dot(x, y)
    if abs(cosine) > 1e-6:
        angle = math.degrees(math.acos(max(-1.0, min(1.0, cosine))))
        fail("NX_INVALID_ARGUMENT",
             f"x_axis and y_axis must be perpendicular; they are {angle:.6g} deg apart",
             field="/frame", hint=FRAME_HINT)  # fmt: skip
    return x, y, geom.cross(x, y)


def _check_frame(origin, normal, plane, units: str) -> None:
    """The frame lies in the source plane: same normal (either sense), origin on it."""
    where = (
        f"the source plane passes through {[round(v, 6) for v in plane['origin']]} with "
        f"normal {[round(v, 6) for v in plane['normal']]}"
    )
    if not geom.parallel(normal, plane["normal"], 1e-8):
        fail("NX_WRONG_GEOMETRY",
             f"The frame axes span a plane with normal {[round(v, 6) for v in normal]}; "
             f"{where}", field="/frame", hint=FRAME_HINT)  # fmt: skip
    height = geom.dot(geom.sub(origin, plane["origin"]), plane["normal"])
    if abs(height) > kit_units.tolerance(units):
        fail("NX_WRONG_GEOMETRY",
             f"The frame origin is {height:.6g} {units} off the source plane; {where}",
             field="/frame", hint=FRAME_HINT)  # fmt: skip


def _layers(tx, overrides) -> dict:
    out = {}
    for i, (ref, name) in enumerate(overrides or []):
        if not re.match(LAYER_PATTERN, name):
            fail("NX_INVALID_ARGUMENT", "Layer names use 1-64 of A-Z a-z 0-9 _ . -",
                 field=f"/layers/{i}/1")  # fmt: skip
        try:
            obj = tx.resolve(ref, "curve", "edge")
        except NXError as error:
            error.field = f"/layers/{i}/0"
            raise
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
    path: Annotated[str, PathTo("replace", ".dxf")],
    overwrite: Annotated[bool, Doc("Replace an existing file at path")] = False,
    frame: Frame | None = None,
    origin: Annotated[
        Literal["frame", "min_corner"], Doc("min_corner: move the profile so it starts at 0,0")
    ] = "frame",
    units: Annotated[Literal["mm", "in"], Doc("Units of the DXF")] = "mm",
    tolerance: Annotated[
        Length, Range(gt=0), Doc("Write other curves (splines, ellipses) as lines within this")
    ]
    | None = None,
    layer: Layer = "0",
    layers: Annotated[list[LayerOverride], Items(1, 1000), Doc(LAYERS_DOC)] | None = None,
) -> ProfileData:
    """Export a sketch or planar face as a 1:1 DXF R12 of lines, arcs and circles
    (nx_import_model reads it back). Other curves are refused unless tolerance writes them
    as lines. Drawings: nx_export_drawing; flat patterns: nx_export_flat_pattern."""
    if not re.match(LAYER_PATTERN, layer):
        fail("NX_INVALID_ARGUMENT", "Layer names use 1-64 of A-Z a-z 0-9 _ . -", field="/layer")
    if frame is not None:
        _frame_axes(frame)
    target = Path(path)
    replacing = target.exists()
    if replacing and not overwrite:
        free = _free_name(target)
        fail(
            "NX_ALREADY_EXISTS",
            "/path: a file already exists there; set overwrite=true to replace it",
            field="/path",
            hint=f"Choose a new path such as '{files.relative(free, tx.workspace.root)}', or "
            "set overwrite=true",
            details={
                "path": files.relative(target, tx.workspace.root),
                "suggested_path": files.relative(free, tx.workspace.root),
            },
        )
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
        part_units = kit_units.part_units(tx)
        if frame is not None:
            start = args.vec3(frame[0], "/frame")
            x, y, normal = _frame_axes(frame)
            _check_frame(start, normal, plane, part_units)
        else:
            start, x, y = plane["origin"], plane["x_axis"], plane["y_axis"]
            normal = geom.cross(x, y)
        scale = kit_units.factor(part_units, units)
        entities = _entities(
            tx, curves, kind, start, x, y, normal, scale, layer, overrides, tolerance
        )
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
    if origin == "min_corner":
        low = _low_corner(entities)
        _shift(entities, low)
        start = geom.add(
            start, geom.add(geom.scale(x, low[0] / scale), geom.scale(y, low[1] / scale))
        )
    approximated = len({e["tag"] for e in entities if e.get("approximated")})
    if approximated:
        tx.warn("NX_APPROXIMATED", f"{approximated} curve(s) written as lines within the tolerance")
    raw = dxf_text(entities, units).encode("ascii")
    temp = tx.files.stage(target, replace=overwrite)
    temp.write_bytes(raw)
    tx.files.publish()
    return {
        "path": files.relative(target, tx.workspace.root),
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "units": units,
        "source_units": part_units,
        "frame": {"origin": start, "x_axis": x, "y_axis": y, "normal": normal},
        "entity_count": len(entities),
        "entity_counts": dict(Counter(e["type"] for e in entities)),
        "approximated": approximated,
        "replaced": replacing,
    }


def _free_name(target: Path) -> Path:
    for n in range(2, 1000):
        candidate = target.with_name(f"{target.stem}-{n}{target.suffix}")
        if not candidate.exists():
            return candidate
    return target


def _arc_points(e) -> list:
    """Points that bound an arc or circle entity: ends and the quadrant points it sweeps."""
    c, r = e["center"], e["radius"]
    if e["type"] == "CIRCLE":
        return [[c[0] - r, c[1] - r], [c[0] + r, c[1] + r]]
    a0, a1 = e["start_angle"], e["end_angle"]
    sweep = (a1 - a0) % 360
    angles = [a0, a1] + [q for q in (0, 90, 180, 270) if (q - a0) % 360 <= sweep]
    return [[c[0] + r * math.cos(math.radians(a)), c[1] + r * math.sin(math.radians(a))]
            for a in angles]  # fmt: skip


def _low_corner(entities) -> list[float]:
    points = []
    for e in entities:
        points += [e["start"], e["end"]] if e["type"] == "LINE" else _arc_points(e)
    return [min(p[0] for p in points), min(p[1] for p in points)]


def _shift(entities, low) -> None:
    for e in entities:
        for key in ("start", "end", "center"):
            if key in e:
                e[key] = [e[key][0] - low[0], e[key][1] - low[1]]


def _polyline(uf, evaluator, limits, local, tolerance) -> list:
    """Points of a curve whose chords stay within ``tolerance`` (part units) of it."""

    def at(t):
        return [float(v) for v in uf.Eval.EvaluateUnitVectors(evaluator, t)[0]]

    def chord_error(a, b, m):
        ab = geom.sub(b, a)
        length = geom.norm(ab)
        if length < 1e-12:
            return geom.dist(a, m)
        return geom.norm(geom.cross(ab, geom.sub(m, a))) / length

    t0, t1 = float(limits[0]), float(limits[1])
    params = [t0 + (t1 - t0) * k / 8 for k in range(9)]
    out = [(params[0], at(params[0]))]
    stack = [(params[k + 1], params[k]) for k in reversed(range(8))]
    while stack:
        b, a = stack.pop()
        pa, pb, mid = out[-1][1], at(b), (a + b) / 2
        if chord_error(pa, pb, at(mid)) > tolerance and len(out) + len(stack) < MAX_SEGMENTS:
            stack += [(b, mid), (mid, a)]
            continue
        out.append((b, pb))
    return [local(p) for _t, p in out]


def _entities(tx, curves, kind, origin, x, y, normal, scale, layer, overrides, tolerance) -> list:
    uf = tx.uf

    def local(p):
        delta = geom.sub([float(v) for v in p], origin)
        if abs(geom.dot(delta, normal)) * scale > 1e-6:
            fail(
                "NX_WRONG_GEOMETRY",
                "Source geometry is off the output plane (the source is not planar)",
                field="/source",
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
            elif tolerance is not None:
                points = _polyline(uf, evaluator, limits, local, float(tolerance))
                for a, b in zip(points, points[1:], strict=False):
                    out.append({**e, "type": "LINE", "start": a, "end": b, "approximated": True})
                tx.checkpoint()
                continue
            else:
                fail(
                    "NX_UNSUPPORTED_GEOMETRY",
                    "Only lines, arcs and circles are exported exactly; this curve needs "
                    "approximation",
                    field="/source",
                    hint="Give tolerance (e.g. 0.01) to write it as lines within that distance",
                    retry="after_fix",
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
