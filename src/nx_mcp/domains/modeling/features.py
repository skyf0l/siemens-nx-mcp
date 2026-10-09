"""Sketch-based features: extrude, revolve, sweep, loft (tool-surface 3.6)."""

from __future__ import annotations

from typing import Annotated

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Angle, Axis, Combine, Direction, Doc, Items, Limit, RefTo
from nx_mcp.domains.modeling import common
from nx_mcp.kit import args, builders, geom, measure, readback, select, units
from nx_mcp.kit import sketch as ksketch

SketchRef = RefTo("sketch")
ZERO = {"type": "distance", "value": 0.0}
COMBINE_CHANGE = {"unite": "more", "subtract": "less", "intersect": "less"}


def _limit_text(limit, say) -> str:
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
        extend.Value.RightHandSide = repr(float(limit["value"]))
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
    example={"sketch": "s:3", "end": {"type": "distance", "value": "10 mm"}},
)
def nx_create_extrude(
    tx: Tx,
    *,
    sketch: SketchRef,  # type: ignore[valid-type]
    end: Limit,
    start: Limit | None = None,
    symmetric: Annotated[bool, Doc("end distance to both sides; omit start")] = False,
    direction: Direction | None = None,
    combine: Combine | None = None,
) -> common.FeatureData:
    """Extrude a sketch along its normal or a direction, as a new body or combined with bodies.
    Through all needs combine; volume is the result bodies' total."""
    start = start or ZERO
    if symmetric:
        if end["type"] != "distance" or start is not ZERO:
            fail(
                "NX_INVALID_ARGUMENT", "symmetric takes an end distance and no start", field="/end"
            )
        if end["value"] <= 0:
            fail("NX_INVALID_ARGUMENT", "symmetric needs a positive distance", field="/end/value")
        start = {"type": "distance", "value": -end["value"]}
    if start["type"] == end["type"] == "distance" and end["value"] <= start["value"]:
        fail("NX_INVALID_ARGUMENT", "end must lie beyond start", field="/end/value")
    if "through_all" in (start["type"], end["type"]) and combine is None:
        fail("NX_INVALID_ARGUMENT", "through_all needs combine targets", field="/combine")
    sk = ksketch.resolve(tx, sketch)
    origin = geom.xyz(sk.Origin)
    targets = (
        common.solids(tx, combine["targets"], "/combine/targets") if combine is not None else []
    )
    before = measure.volume(tx, targets) if targets else 0.0
    with builders.build(tx, tx.part.Features.CreateExtrudeBuilder, None) as b:
        b.Section = builders.section(tx, sk)
        b.Direction, want = _sketch_direction(tx, sk, direction, origin)
        _set_limit(tx, b.Limits.StartExtend, start, "/start")
        _set_limit(tx, b.Limits.EndExtend, end, "/end")
        builders.boolean(tx, b.BooleanOperation, combine)
        feature = b.commit()
    out = common.result_of(tx, feature)
    if combine is None:
        common.changed(0.0, out["volume"], "positive")
        axis = _principal(want)
        if axis is not None and start["type"] == end["type"] == "distance":
            # The body spans exactly [start, end] along the extrude direction.
            low, high = measure.bounds(tx, common.feature_bodies(feature))
            sign = want[axis]
            span = sorted(origin[axis] + sign * float(v["value"]) for v in (start, end))
            readback.expect_close([low[axis], high[axis]], span, "extent")
    else:
        common.changed(before, out["volume"], COMBINE_CHANGE[combine["operation"]])
    return out


def _axis(tx, value):
    """A smart axis for the Axis union (associative to a datum axis)."""
    obj, origin, want = select.axis(tx, value, "/axis")
    update = tx.nx.SmartObject.UpdateOption.WithinModeling
    part = tx.part
    if obj is not None and hasattr(obj, "Direction") and not hasattr(obj, "Normal"):
        smart = part.Directions.CreateDirection(obj, tx.nx.Sense.Forward, update)
    else:
        smart = builders.direction(tx, want, origin, "/axis")
    return part.Axes.CreateAxis(builders.point(tx, origin), smart, update)


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
    example={"sketch": "s:3", "axis": {"principal": "z"}},
)
def nx_create_revolve(
    tx: Tx,
    *,
    sketch: SketchRef,  # type: ignore[valid-type]
    axis: Axis,
    start_angle: Angle = 0.0,
    end_angle: Angle = 360.0,
    combine: Combine | None = None,
) -> common.FeatureData:
    """Revolve a sketch about an axis, as a new body or combined with bodies. Angles follow the
    right-hand rule about the axis direction."""
    start, end = float(start_angle), float(end_angle)
    if not start < end <= start + 360.0:
        fail("NX_INVALID_ARGUMENT", "Use start < end <= start + 360", field="/end_angle")
    sk = ksketch.resolve(tx, sketch)
    targets = (
        common.solids(tx, combine["targets"], "/combine/targets") if combine is not None else []
    )
    before = measure.volume(tx, targets) if targets else 0.0
    with builders.build(tx, tx.part.Features.CreateRevolveBuilder, None) as b:
        b.Section = builders.section(tx, sk)
        b.Axis = _axis(tx, axis)
        b.Limits.StartExtend.Value.RightHandSide = repr(start)
        b.Limits.EndExtend.Value.RightHandSide = repr(end)
        builders.boolean(tx, b.BooleanOperation, combine)
        feature = b.commit()
    out = common.result_of(tx, feature)
    if combine is None:
        common.changed(0.0, out["volume"], "positive")
    else:
        common.changed(before, out["volume"], COMBINE_CHANGE[combine["operation"]])
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
    section: SketchRef,  # type: ignore[valid-type]
    guide: SketchRef,  # type: ignore[valid-type]
    combine: Combine | None = None,
) -> common.FeatureData:
    """Sweep a closed section sketch along a guide sketch, as a new body or combined with
    bodies (a separate Boolean feature)."""
    if section == guide:
        fail("NX_INVALID_ARGUMENT", "section and guide must differ", field="/guide")
    if combine is not None and len(combine["targets"]) != 1:
        fail("NX_INVALID_ARGUMENT", "A sweep combines with one target", field="/combine/targets")
    a = ksketch.resolve(tx, section, "/section")
    g = ksketch.resolve(tx, guide, "/guide")
    if int(a.Tag) == int(g.Tag):
        fail("NX_INVALID_ARGUMENT", "section and guide must differ", field="/guide")
    targets = (
        common.solids(tx, combine["targets"], "/combine/targets") if combine is not None else []
    )
    before = measure.volume(tx, targets) if targets else 0.0

    with builders.build(tx, tx.part.Features.CreateSweptBuilder, None) as b:
        b.G0Tolerance = units.part_tolerance(tx)
        b.G1Tolerance = 0.1
        b.SectionList.Append(builders.section(tx, a))
        b.GuideList.Append(builders.section(tx, g))
        feature = b.commit()
    swept = common.feature_bodies(feature)
    readback.expect(bool(swept), "bodies", "The sweep built no body")
    if combine is None:
        out = common.result_of(tx, feature)
        common.changed(0.0, out["volume"], "positive")
        return out
    # NX 2406 Swept has no boolean option: combine with a Boolean feature.
    with builders.build(tx, tx.part.Features.CreateBooleanBuilder, None) as b:
        b.Operation = getattr(
            tx.nx.Features.FeatureBooleanType, builders.BOOLEAN_TYPES[combine["operation"]]
        )
        b.Target = targets[0]
        b.Tools.Add(swept)
        joined = b.commit()
    out = common.result_of(tx, joined, [targets[0]])
    common.changed(before, out["volume"], COMBINE_CHANGE[combine["operation"]])
    out["feature"] = tx.ref(feature, kind="feature")
    return out


@tool(
    effect="model",
    label="Loft through sections",
    summary="Loft through {sections}",
    keywords=("loft", "through curves", "blend sections", "transition", "ruled"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"sections": ["s:3", "s:4"]},
)
def nx_create_loft(
    tx: Tx,
    *,
    sections: Annotated[list[SketchRef], Items(2, 20), Doc("Section sketches in order")],  # type: ignore[valid-type]
    solid: bool = True,
) -> common.FeatureData:
    """Loft a body through 2-20 section sketches in order (NX Through Curves); solid=false
    builds a sheet."""
    args.distinct("/sections", sections, "sketches")
    sketches = [ksketch.resolve(tx, s, f"/sections/{i}") for i, s in enumerate(sections)]
    with builders.build(tx, tx.part.Features.CreateThroughCurvesBuilder, None) as b:
        prefer = b.BodyPreferenceTypes
        b.BodyPreference = prefer.Solid if solid else prefer.Sheet
        for sk in sketches:
            b.SectionsList.Append(builders.section(tx, sk))
            tx.checkpoint()
        feature = b.commit()
    bodies = common.feature_bodies(feature)
    readback.expect(bool(bodies), "bodies", "The loft built no body")
    readback.expect(
        all(bool(getattr(x, "IsSolidBody", solid)) == solid for x in bodies),
        "body_type",
        "NX built a different body type than requested",
    )
    out = common.result_of(tx, feature)
    if solid:
        common.changed(0.0, out["volume"], "positive")
    return out
