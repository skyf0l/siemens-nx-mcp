"""Sketch-based features: extrude, revolve, sweep (tool-surface 3.6; loft in ``lofts``).

Arguments take the short forms agents try first: an extrude ``end``/``start`` may be a
plain distance (``10``, ``"10 mm"``) and an axis or direction a bare principal name
(``"z"``, ``"-x"``); both are turned into the object forms before anything is read.
"""

from __future__ import annotations

from typing import Annotated, NoReturn, cast

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    Angle,
    Axis,
    Combine,
    Direction,
    Doc,
    Length,
    Limit,
    LimitDistance,
    SketchRef,
)
from nx_mcp.domains.modeling import common
from nx_mcp.kit import builders, curve, expr, geom, measure, readback, select, units
from nx_mcp.kit import sketch as ksketch

ZERO: LimitDistance = {"type": "distance", "value": 0.0}
COMBINE_CHANGE = common.COMBINE_CHANGE
# NX 2406 revolve refusal for a profile on both sides of the axis.
REVOLVE_CROSSES_AXIS = 671623
# NX 2406 extrude refusal "Limit face not found in the limit direction": the extruded
# profile misses the target body (through all) or the until-face limit.
LIMIT_FACE_MISSED = 671473

# Short forms first: a plain distance for a limit, a bare principal name for an axis.
ExtrudeLimit = Limit | Length


def _as_limit(value):
    """The Limit object of an extrude limit given as a plain distance or an object."""
    if value is None or isinstance(value, dict):
        return value
    return {"type": "distance", "value": value}


def _limit_text(limit, say) -> str:
    limit = _as_limit(limit)
    if limit["type"] == "distance":
        value = limit["value"]
        unit = "" if isinstance(value, str) else " " + str(say.units.get("length") or "")
        return f"{value}{unit}".strip() if isinstance(value, str) else f"{value:.6g}{unit}"
    return limit["type"].replace("_", " ")


def _extrude_summary(a, say) -> str:
    text = f"Extrude {say('sketch')} {_limit_text(a['end'], say)}"
    if a.get("combine"):
        text += " " + a["combine"]["operation"]
    return text


def _set_limit(tx, extend, limit, field):
    types = tx.nx.GeometricUtilities.Extend.ExtendType
    if limit["type"] == "distance":
        extend.TrimType = types.Value
        expr.drive(extend.Value, limit["value"])  # an expression keeps the limit driven
    elif limit["type"] == "through_all":
        extend.TrimType = types.ThroughAll
    else:
        extend.TrimType = types.UntilSelected
        extend.Target = select.one(tx, limit["face"], "face", field=field + "/face")


def _sketch_direction(tx, sk, value, origin):
    """``(smart direction, unit vector)``: the sketch normal by default."""
    if value is None:
        want = geom.unit(ksketch.frame(sk)["normal"])
        smart = tx.part.Directions.CreateDirection(
            sk, tx.nx.Sense.Forward, tx.nx.SmartObject.UpdateOption.WithinModeling
        )
        return smart, want
    want = select.direction(tx, value, "/direction")
    return builders.direction(tx, want, origin, "/direction"), want


class ExtrudeData(common.FeatureData, total=False):
    sheet: bool  # an open profile made sheet bodies (volume 0)
    area: float  # the sheets' total area


def _limit_missed(error: NXError, through: bool) -> NoReturn:
    what = "the target body" if through else "the limit face"
    fail(
        "NX_BUILD_FAILED",
        f"The extruded profile misses {what}: NX found no limit face in the extrude "
        f"direction ({error.message})",
        hint="Check direction (or a negative start) and that the profile lies over the "
        "target or the limit face",
        nx_code=error.nx_code,
    )


def _sheet(tx, feature, out):
    """``out`` for an open profile's sheet bodies (``sheet``, ``area``), else None."""
    bodies = common.feature_bodies(feature)
    if not bodies or not all(getattr(b, "IsSheetBody", False) for b in bodies):
        return None
    faces = [f for b in bodies for f in b.GetFaces()]
    area = measure.area(tx, faces) if faces else 0.0
    readback.expect(area > 0, "area", f"NX built sheets of area {area:.6g}", area=area)
    return {**out, "sheet": True, "area": area}


def _principal(vector):
    """Index of the axis ``vector`` is parallel to, or None."""
    for i in range(3):
        axis = [0.0, 0.0, 0.0]
        axis[i] = 1.0
        if geom.parallel(vector, axis, 1e-9):
            return i
    return None


@tool(
    effect="model",
    label="Extrude sketch",
    summary=_extrude_summary,
    keywords=("pad", "boss", "protrude", "extrusion", "cut", "pocket"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"sketch": "s:3", "end": "10 mm", "direction": "-z"},
)
def nx_create_extrude(
    tx: Tx,
    *,
    sketch: SketchRef,
    end: ExtrudeLimit,
    start: ExtrudeLimit | None = None,
    symmetric: Annotated[bool, Doc("end distance to both sides; omit start")] = False,
    direction: Direction | None = None,
    combine: Combine | None = None,
) -> ExtrudeData:
    """Extrude a sketch along its normal or a direction, as a new body or combined with bodies.
    Through all needs combine; an open profile makes a sheet. volume: result bodies' total."""
    value_field = "/end" if not isinstance(end, dict) else "/end/value"
    end_limit: dict = _as_limit(end)
    start_limit: dict = _as_limit(start) or dict(ZERO)
    direction = common.principal(direction)
    if symmetric:
        if end_limit["type"] != "distance" or start is not None:
            fail(
                "NX_INVALID_ARGUMENT", "symmetric takes an end distance and no start", field="/end"
            )
        if float(end_limit["value"]) <= 0:
            fail("NX_INVALID_ARGUMENT", "symmetric needs a positive distance", field=value_field)
        start_limit = {"type": "distance", "value": -end_limit["value"]}
    if (
        start_limit["type"] == "distance"
        and end_limit["type"] == "distance"
        and float(end_limit["value"]) <= float(start_limit["value"])
    ):
        fail(
            "NX_INVALID_ARGUMENT",
            "end must lie beyond start (both are distances along the extrude direction)",
            field=value_field,
            hint="Use a larger end, or a negative start to extrude behind the sketch",
        )
    if "through_all" in (start_limit["type"], end_limit["type"]) and combine is None:
        fail("NX_INVALID_ARGUMENT", "through_all needs combine targets", field="/combine")
    common.check_combine(combine)
    sk = ksketch.resolve(tx, sketch)
    origin = geom.xyz(sk.Origin)
    targets = common.combine_targets(tx, combine)
    before = measure.volume(tx, targets) if targets else 0.0
    tags = common.body_tags(tx)
    try:
        with builders.build(tx, tx.part.Features.CreateExtrudeBuilder, None) as b:
            b.Section = builders.section(tx, sk)
            b.Direction, want = _sketch_direction(tx, sk, direction, origin)
            _set_limit(tx, b.Limits.StartExtend, start_limit, "/start")
            _set_limit(tx, b.Limits.EndExtend, end_limit, "/end")
            builders.boolean(tx, b.BooleanOperation, combine)
            feature = b.commit()
    except NXError as error:
        if error.nx_code == LIMIT_FACE_MISSED:
            _limit_missed(error, "through_all" in (start_limit["type"], end_limit["type"]))
        raise
    if combine is not None:
        return cast(ExtrudeData, common.combined(tx, feature, combine, targets, before, tags))
    out = cast(ExtrudeData, common.result_of(tx, feature))
    sheet = _sheet(tx, feature, out)
    if sheet is not None:
        out = sheet
    else:
        common.changed(0.0, out["volume"], "positive")
    axis = _principal(want)
    if axis is not None and start_limit["type"] == "distance" and end_limit["type"] == "distance":
        # The body spans exactly [start, end] along the extrude direction.
        low, high = measure.bounds(tx, common.feature_bodies(feature))
        sign = want[axis]
        span = sorted(origin[axis] + sign * float(v["value"]) for v in (start_limit, end_limit))
        readback.expect_close([low[axis], high[axis]], span, "extent")
    return out


def _axis(tx, value, resolved=None):
    """A smart axis for the Axis union (associative to a datum axis)."""
    obj, origin, want = resolved or select.axis(tx, value, "/axis")
    update = tx.nx.SmartObject.UpdateOption.WithinModeling
    part = tx.part
    if obj is not None and hasattr(obj, "Direction") and not hasattr(obj, "Normal"):
        smart = part.Directions.CreateDirection(obj, tx.nx.Sense.Forward, update)
    else:
        smart = builders.direction(tx, want, origin, "/axis")
    return part.Axes.CreateAxis(builders.point(tx, origin), smart, update)


def _crosses_axis(tx, sk, origin, direction) -> bool:
    """Whether the sketch's profile curves lie on both sides of an axis in the sketch
    plane (sampled at five points per curve); False when the axis leaves the plane."""
    f = ksketch.frame(sk)
    n = geom.unit(f["normal"])
    d = geom.unit(direction)
    tol = units.part_tolerance(tx)
    if abs(geom.dot(d, n)) > 1e-9 or abs(ksketch.height(f, origin)) > tol:
        return False
    side = geom.cross(d, n)  # in-plane normal of the axis line
    signs = set()
    for c in ksketch.geometry(sk):
        if getattr(c, "IsReference", False):
            continue
        try:
            points = [curve.point_at(tx, c, t) for t in (0.0, 0.25, 0.5, 0.75, 1.0)]
        except Exception:
            continue  # a sketch point, not a curve
        for p in points:
            s = geom.dot(geom.sub(geom.xyz(p) if hasattr(p, "X") else list(p), origin), side)
            if abs(s) > tol:
                signs.add(s > 0)
    return len(signs) == 2


def _crosses(nx_code=None, nx_message=None):
    details = {"nx_message": nx_message} if nx_message else None
    fail(
        "NX_INVALID_ARGUMENT",
        "The profile crosses the revolve axis; keep it on one side of the axis",
        field="/axis",
        hint="Move the sketch curves to one side of the axis (touching it is fine), "
        "or choose another axis",
        nx_code=nx_code,
        details=details,
    )


@tool(
    effect="model",
    label="Revolve sketch",
    summary=lambda a, say: (
        f"Revolve {say('sketch')} " + f"{a.get('start_angle', 0)}..{a.get('end_angle', 360)} deg"
    ),
    keywords=("revolve", "revolution", "lathe", "turn", "rotate profile"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"sketch": "s:3", "axis": "z", "end_angle": 180},
)
def nx_create_revolve(
    tx: Tx,
    *,
    sketch: SketchRef,
    axis: Axis,
    start_angle: Angle = 0.0,
    end_angle: Angle = 360.0,
    combine: Combine | None = None,
) -> common.FeatureData:
    """Revolve a sketch about an absolute axis ('z' is world Z), as a new body or combined with
    bodies. Angles follow the right-hand rule about the axis direction."""
    axis = common.principal(axis)
    start, end = float(start_angle), float(end_angle)
    if not start < end <= start + 360.0:
        fail("NX_INVALID_ARGUMENT", "Use start < end <= start + 360", field="/end_angle")
    common.check_combine(combine)
    with tx.checks():
        sk = ksketch.resolve(tx, sketch)
        resolved = select.axis(tx, axis, "/axis")
        if _crosses_axis(tx, sk, resolved[1], resolved[2]):
            _crosses()
    targets = common.combine_targets(tx, combine)
    before = measure.volume(tx, targets) if targets else 0.0
    tags = common.body_tags(tx)
    try:
        with builders.build(tx, tx.part.Features.CreateRevolveBuilder, None) as b:
            b.Section = builders.section(tx, sk)
            b.Axis = _axis(tx, axis, resolved)
            expr.drive(b.Limits.StartExtend.Value, start_angle)
            expr.drive(b.Limits.EndExtend.Value, end_angle)
            builders.boolean(tx, b.BooleanOperation, combine)
            feature = b.commit()
    except NXError as error:
        if error.nx_code != REVOLVE_CROSSES_AXIS:
            raise
        _crosses(error.nx_code, error.message)  # a crossing the samples missed
    if combine is not None:
        return common.combined(tx, feature, combine, targets, before, tags)
    out = common.result_of(tx, feature)
    common.changed(0.0, out["volume"], "positive")
    return out


@tool(
    effect="model",
    label="Sweep section along guide",
    summary="Sweep {section} along {guide}",
    keywords=("sweep", "swept", "sweep along guide", "pipe path", "variational sweep"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"section": "s:3", "guide": "s:4"},
)
def nx_create_sweep(
    tx: Tx,
    *,
    section: SketchRef,
    guide: SketchRef,
    combine: Combine | None = None,
) -> common.FeatureData:
    """Sweep a closed section sketch along a guide sketch, as a new body or combined with
    bodies (a separate Boolean feature). The guide must be tangent-continuous (G1)."""
    if section == guide:
        fail("NX_INVALID_ARGUMENT", "section and guide must differ", field="/guide")
    common.check_one_target(combine, "sweep")
    with tx.checks():
        a = ksketch.resolve(tx, section, "/section")
        g = ksketch.resolve(tx, guide, "/guide")
        if int(a.Tag) == int(g.Tag):
            fail("NX_INVALID_ARGUMENT", "section and guide must differ", field="/guide")
        common.refuse_corners(tx, common.sketch_curves(g), "/guide", "guide")
    targets = common.one_target(tx, combine, "sweep")
    before = measure.volume(tx, targets) if targets else 0.0

    try:
        with builders.build(tx, tx.part.Features.CreateSweptBuilder, None) as b:
            b.G0Tolerance = units.part_tolerance(tx)
            b.G1Tolerance = 0.1
            b.SectionList.Append(builders.section(tx, a))
            b.GuideList.Append(builders.section(tx, g))
            feature = b.commit()
    except NXError as error:
        common.smooth_hint(error)
        raise
    swept = common.feature_bodies(feature)
    readback.expect(bool(swept), "bodies", "The sweep built no body")
    if combine is None:
        out = common.result_of(tx, feature)
        common.changed(0.0, out["volume"], "positive")
        return out
    # NX 2406 Swept has no boolean option: combine with a Boolean feature.
    return common.combine_new(tx, feature, swept, combine, targets[0], before)
