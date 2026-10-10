"""Mirror (bodies or features) and feature patterns in every layout (tool-surface 3.6, 4).

NX 2406 lessons kept here: ``MirrorBodyBuilder.Plane`` takes a datum plane (a face
or principal plane becomes a fixed datum plane) and has no delete-source option, so
``keep_original=false`` blanks the source. Feature mirror is ``CreateMirrorBuilder``
(a pattern service in its Mirror layout, ``PlaneOption`` Existing). Pattern layouts
come from one ``PatternFeatureBuilder.PatternService``: Linear (``RectangularDefinition``;
fill is a linear layout with a ``PatternFill`` face boundary and a simplified layout
type), Circular, Polygon, Spiral, AlongPath and General. NX computes the instance
count of spiral and fill layouts, so every layout reads the count back from the
committed pattern's instances, not from the requested numbers.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    Angle,
    Axis,
    ByVariant,
    Direction,
    Doc,
    Items,
    Json,
    Length,
    Plane,
    Point3,
    Range,
    RefTo,
)
from nx_mcp.domains.modeling import common
from nx_mcp.kit import args, builders, geom, measure, readback, result, select

BodyList = Annotated[list[RefTo("body")], Items(1, 50)]  # type: ignore[valid-type]
FeatureList = Annotated[list[RefTo("feature")], Items(1, 50)]  # type: ignore[valid-type]
MAX_INSTANCES = 1000


# ---------------------------------------------------------------------------
# Shared helpers


def _plane(tx, value, field):
    """``(datum plane, origin, unit normal)``: a datum plane ref is used as is (associative);
    a face, principal or explicit plane becomes a fixed datum plane."""
    obj, origin, normal = select.plane(tx, value, field)
    if obj is not None and hasattr(obj, "Normal"):
        return obj, origin, normal
    return builders.fixed_plane(tx, origin, normal), origin, normal


def _seeds(tx, features, field="/features"):
    args.distinct(field, features, "features")
    seeds = []
    for i, ref in enumerate(features):
        f = tx.resolve(ref, "feature", work_part=True)
        if getattr(f, "IsInternal", False):
            fail("NX_INVALID_ARGUMENT", "Use the owning feature", field=f"{field}/{i}")
        seeds.append(f)
    return seeds


def _hosts(seeds):
    return {int(b.Tag): b for f in seeds for b in common.feature_bodies(f)}


def _update(tx):
    return tx.nx.SmartObject.UpdateOption.WithinModeling


def _smart_direction(tx, value, field):
    """``(smart direction, unit vector)``: a linear edge or datum axis stays associative;
    a vector, principal axis or planar face/datum plane normal is a fixed direction."""
    vector = select.direction(tx, value, field)
    if isinstance(value, str):
        obj = select.one(tx, value, "edge", "datum_axis", "datum_plane", "face", field=field)
        is_axis = hasattr(obj, "Direction") and not hasattr(obj, "Normal")
        if hasattr(obj, "SolidEdgeType") or is_axis:
            with native():
                smart = tx.part.Directions.CreateDirection(obj, tx.nx.Sense.Forward, _update(tx))
            return smart, vector
    return builders.direction(tx, vector, field=field), vector


def _smart_axis(tx, value, field):
    """``(NX axis, origin, unit direction)`` of the Axis union (a datum axis or linear edge
    keeps its direction associative)."""
    obj, origin, want = select.axis(tx, value, field)
    if obj is not None and hasattr(obj, "Direction") and not hasattr(obj, "Normal"):
        smart = tx.part.Directions.CreateDirection(obj, tx.nx.Sense.Forward, _update(tx))
    else:
        smart = builders.direction(tx, want, origin, field)
    axis = tx.part.Axes.CreateAxis(builders.point(tx, origin), smart, _update(tx))
    return axis, origin, want


def _expression(expr, value):
    expr.RightHandSide = repr(float(value)) if isinstance(value, float) else str(value)


# ---------------------------------------------------------------------------
# Mirror


class MirrorData(common.FeatureData):
    target: str
    count: int


def _mirror_summary(a, say):
    if a.get("target") == "features":
        n = len(a.get("features") or [])
        return f"Mirror {n} feature" + ("" if n == 1 else "s")
    return "Mirror " + common.bodies_text(a)


def _mirror_bodies(tx, refs, plane, keep_original):
    sources = common.solids(tx, refs)
    datum, origin, normal = _plane(tx, plane, "/plane")
    masses = [measure.mass(tx, [b]) for b in sources]
    with builders.build(tx, tx.part.Features.CreateMirrorBodyBuilder, None) as b:
        b.MirrorBodyCollector.ReplaceRules(
            builders.collector(tx, sources, "Body").GetRules(), False
        )
        b.Plane.Value = datum
        b.option("DeleteSourceBody", False)
        feature = b.commit()
    made = common.feature_bodies(feature)
    readback.expect(len(made) == len(sources), "bodies", "NX mirrored a different body count")
    got = [measure.mass(tx, [m]) for m in made]
    for i, m in enumerate(masses):
        wanted = geom.reflect(m["centroid"], origin, normal) if m["centroid"] else None
        match = [
            g
            for g in got
            if geom.close(g["volume"], m["volume"], 1e-6)
            and (
                wanted is None
                or geom.dist(g["centroid"], wanted) <= 1e-5 * max(1.0, *map(abs, wanted))
            )
        ]
        readback.expect(bool(match), f"mirror[{i}]", "A mirrored body is not the reflection")
    if not keep_original:
        with native():
            tx.session.DisplayManager.BlankObjects(sources)
    return {**common.result_of(tx, feature), "target": "bodies", "count": len(made)}


def _mirror_features(tx, refs, plane):
    with tx.checks():
        seeds = _seeds(tx, refs)
        hosts = _hosts(seeds)
        if not hosts:
            fail("NX_WRONG_GEOMETRY", "The features own no body to mirror into",
                 field="/features")  # fmt: skip
    datum, _origin, _normal = _plane(tx, plane, "/plane")
    before = measure.volume(tx, list(hosts.values()))
    G = tx.nx.GeometricUtilities
    with builders.build(tx, tx.part.Features.CreateMirrorBuilder, None) as b:
        b.FeatureList.Add(seeds)
        service = b.PatternService
        service.PatternType = G.PatternDefinition.PatternEnum.Mirror
        mirror = service.MirrorDefinition
        mirror.PlaneOption = G.MirrorPattern.PlaneOptions.Existing
        mirror.ExistingPlane.Value = datum
        feature = b.commit()
    bodies = list(hosts.values())
    out = common.result_of(tx, feature, bodies)
    common.changed(before, out["volume"], "differs")
    return {**out, "target": "features", "count": len(seeds)}


@tool(
    effect="model",
    label="Mirror bodies or features",
    summary=_mirror_summary,
    keywords=("mirror", "mirror body", "mirror feature", "reflect", "symmetric copy"),
    requires=("work_part", "no_active_sketch"),
    variants="target",
    cost="heavy",
    previewable=True,
    example={"target": "bodies", "bodies": ["b:3"], "plane": {"principal": "yz"}},
)
def nx_create_mirror(
    tx: Tx,
    *,
    plane: Plane,
    target: Literal["bodies", "features"] = "bodies",
    bodies: BodyList | None = None,
    features: FeatureList | None = None,
    keep_original: Annotated[bool, Doc("bodies only: false hides the sources")] = True,
) -> MirrorData:
    """Mirror solid bodies into new bodies, or features into their own bodies, about a
    plane (associative to a datum plane). target selects which list is used."""
    wanted, other = ("bodies", "features") if target == "bodies" else ("features", "bodies")
    given = {"bodies": bodies, "features": features}
    if not given[wanted]:
        fail("NX_INVALID_ARGUMENT", f"target {target} needs {wanted}", field=f"/{wanted}")
    if given[other]:
        fail("NX_INVALID_ARGUMENT", f"target {target} takes no {other}", field=f"/{other}")
    if target == "features":
        if not keep_original:
            fail(
                "NX_INVALID_ARGUMENT",
                "A feature mirror keeps its seed features",
                field="/keep_original",
            )
        return _mirror_features(tx, features, plane)
    return _mirror_bodies(tx, bodies, plane, keep_original)


# ---------------------------------------------------------------------------
# Pattern layouts


Count = Annotated[int, Range(ge=2, le=MAX_INSTANCES), Doc("Instances including the seed")]


class _LinearRequired(TypedDict):
    direction: Direction
    count: Count
    spacing: Length


class LinearForm(_LinearRequired, total=False):
    direction2: Direction
    count2: Annotated[int, Range(ge=1, le=MAX_INSTANCES)]
    spacing2: Length


class CircularForm(TypedDict):
    axis: Axis
    count: Annotated[int, Range(ge=2, le=360), Doc("Instances including the seed")]
    angle: Annotated[Angle, Doc("Angle between instances")]


class AlongCurveForm(TypedDict):
    curve: Annotated[
        RefTo("edge", "curve"),  # type: ignore[valid-type]
        Doc("Path edge or curve; the seed should project inside it"),
    ]
    count: Count
    spacing: Annotated[Length, Doc("Arc length between instances")]


class _PolygonRequired(TypedDict):
    axis: Axis
    sides: Annotated[int, Range(ge=3, le=100)]
    count_per_side: Annotated[int, Range(ge=2, le=100), Doc("Instances per side, corners included")]


class PolygonForm(_PolygonRequired, total=False):
    size: Annotated[
        Literal["inscribed", "circumscribed"], Doc("Seed on a corner or on a side (inscribed)")
    ]


class _SpiralRequired(TypedDict):
    normal: Annotated[Direction, Doc("Spiral plane normal; the spiral starts at the seed")]
    reference: Annotated[Direction, Doc("In-plane start direction of the spiral")]
    angle: Annotated[Angle, Doc("Total spiral angle (360 = one turn)")]
    pitch: Annotated[Length, Doc("Radial growth per turn")]
    spacing: Annotated[Length, Doc("Distance between instances along the spiral")]


class SpiralForm(_SpiralRequired, total=False):
    hand: Literal["right", "left"]


class _FillRequired(TypedDict):
    boundary: Annotated[RefTo("face"), Doc("Planar face the grid through the seed fills")]  # type: ignore[valid-type]
    direction: Annotated[Direction, Doc("In-plane grid direction")]
    spacing: Length


class FillForm(_FillRequired, total=False):
    spacing2: Annotated[Length, Doc("Spacing across direction (default spacing)")]
    margin: Annotated[Length, Doc("Keep-out distance inside the boundary (default 0)")]


class _GeneralRequired(TypedDict):
    origin: Annotated[Point3, Doc("Reference point on the seed")]
    positions: Annotated[list[Point3], Items(1, MAX_INSTANCES - 1), Doc("Where origin goes")]


class GeneralForm(_GeneralRequired, total=False):
    pass


LAYOUT_FORMS = {
    "linear": LinearForm,
    "circular": CircularForm,
    "along_curve": AlongCurveForm,
    "polygon": PolygonForm,
    "spiral": SpiralForm,
    "fill": FillForm,
    "general": GeneralForm,
}
for _td in LAYOUT_FORMS.values():
    _td.__pydantic_config__ = {"extra": "forbid"}  # type: ignore[attr-defined]

Layout = Literal["linear", "circular", "along_curve", "polygon", "spiral", "fill", "general"]
Parameters = Annotated[
    dict,
    Json(max_keys=8),
    ByVariant("layout", LAYOUT_FORMS),
    Doc("Layout parameters: nx_get_tool_schemas(nx_create_pattern#<layout>)"),
]


class PatternData(TypedDict):
    feature: result.Row
    bodies: list[result.Row]
    volume: float
    layout: str
    count: int


def _pattern_summary(a, say):
    count = (a.get("parameters") or {}).get("count")
    return f"Pattern {say('features')} {a.get('layout')}" + (f" x{count}" if count else "")


def _check(layout, p):
    """Argument checks before NX; returns the requested instance count or None (NX decides)."""
    where = "/parameters"
    if layout == "linear":
        second = [p.get(k) is not None for k in ("direction2", "count2", "spacing2")]
        if any(second) and not all(second):
            fail(
                "NX_INVALID_ARGUMENT",
                "A second direction needs direction2, count2 and spacing2",
                field=where,
            )
        if p["spacing"] == 0 or (all(second) and p["spacing2"] == 0):
            fail("NX_INVALID_ARGUMENT", "Spacing must not be zero", field=where + "/spacing")
        total = p["count"] * (p.get("count2") or 1)
    elif layout == "circular":
        if p["angle"] <= 0 or (p["count"] - 1) * p["angle"] > 360.0 - 1e-9:
            fail(
                "NX_INVALID_ARGUMENT",
                "Use a positive angle with (count - 1) * angle < 360",
                field=where + "/angle",
            )
        total = p["count"]
    elif layout == "along_curve":
        if p["spacing"] <= 0:
            fail("NX_INVALID_ARGUMENT", "Spacing must be positive", field=where + "/spacing")
        total = p["count"]
    elif layout == "polygon":
        total = p["sides"] * (p["count_per_side"] - 1)
    elif layout == "spiral":
        for key in ("angle", "pitch", "spacing"):
            if p[key] <= 0:
                fail("NX_INVALID_ARGUMENT", f"{key} must be positive", field=f"{where}/{key}")
        if p["angle"] > 360.0 * 100:
            fail("NX_INVALID_ARGUMENT", "At most 100 turns", field=where + "/angle")
        total = None
    elif layout == "fill":
        for key in ("spacing", "spacing2"):
            if p.get(key) is not None and p[key] <= 0:
                fail("NX_INVALID_ARGUMENT", f"{key} must be positive", field=f"{where}/{key}")
        if p.get("margin") is not None and p["margin"] < 0:
            fail("NX_INVALID_ARGUMENT", "margin must not be negative", field=where + "/margin")
        total = None
    else:  # general
        points = [args.vec3(q, f"{where}/positions/{i}") for i, q in enumerate(p["positions"])]
        origin = args.vec3(p["origin"], where + "/origin")
        scale = max(1.0, *(abs(v) for q in [origin, *points] for v in q))
        for i, q in enumerate(points):
            if geom.dist(q, origin) <= 1e-9 * scale:
                fail("NX_INVALID_ARGUMENT", "A position repeats the seed",
                     field=f"{where}/positions/{i}")  # fmt: skip
            for j in range(i):
                if geom.dist(q, points[j]) <= 1e-9 * scale:
                    fail("NX_INVALID_ARGUMENT", "Positions must be distinct",
                         field=f"{where}/positions/{i}")  # fmt: skip
        total = len(points) + 1
    if total is not None and total > MAX_INSTANCES:
        fail("NX_TOO_MANY_OBJECTS", f"At most {MAX_INSTANCES} instances", field=where)
    return total


def _linear(tx, service, G, p):
    service.PatternType = G.PatternDefinition.PatternEnum.Linear
    d = service.RectangularDefinition
    where = "/parameters"
    d.XDirection, first = _smart_direction(tx, p["direction"], where + "/direction")
    _expression(d.XSpacing.NCopies, int(p["count"]))
    _expression(d.XSpacing.PitchDistance, float(p["spacing"]))
    if p.get("direction2") is not None:
        smart, other = _smart_direction(tx, p["direction2"], where + "/direction2")
        if geom.parallel(first, other, 1e-6):
            fail(
                "NX_INVALID_ARGUMENT",
                "The pattern directions must not be parallel",
                field=where + "/direction2",
            )
        d.UseYDirectionToggle = True
        d.YDirection = smart
        _expression(d.YSpacing.NCopies, int(p["count2"]))
        _expression(d.YSpacing.PitchDistance, float(p["spacing2"]))
    else:
        _expression(d.YSpacing.NCopies, 1)


def _circular(tx, service, G, p):
    service.PatternType = G.PatternDefinition.PatternEnum.Circular
    d = service.CircularDefinition
    d.RotationAxis = _smart_axis(tx, p["axis"], "/parameters/axis")[0]
    spacing = d.AngularSpacing
    spacing.SpaceType = G.PatternSpacing.SpacingType.Offset
    _expression(spacing.NCopies, int(p["count"]))
    _expression(spacing.PitchAngle, float(p["angle"]))
    _expression(d.RadialSpacing.NCopies, 1)


def _resolve(tx, layout, p):
    """Resolve and check the layout's own references before any builder exists."""
    if layout == "along_curve":
        path = select.one(tx, p["curve"], "edge", "curve", field="/parameters/curve")
        length = measure.length(tx, path)  # part units, like spacing
        if (p["count"] - 1) * p["spacing"] > length * (1 + 1e-9):
            fail(
                "NX_INVALID_ARGUMENT",
                f"(count - 1) * spacing exceeds the path length {length:.6g}",
                field="/parameters/spacing",
            )
        p["curve"] = path
    elif layout == "spiral":
        normal = select.direction(tx, p["normal"], "/parameters/normal")
        reference = select.direction(tx, p["reference"], "/parameters/reference")
        if abs(geom.dot(normal, reference)) > 1e-6:
            fail("NX_INVALID_ARGUMENT", "reference must lie in the spiral plane",
                 field="/parameters/reference")  # fmt: skip
    elif layout == "fill":
        where = "/parameters/boundary"
        face, _origin, normal = select.planar(tx, p["boundary"], where)
        if not hasattr(face, "GetEdges"):
            fail("NX_WRONG_KIND", "Select a planar face", field=where)
        first = select.direction(tx, p["direction"], "/parameters/direction")
        if abs(geom.dot(first, normal)) > 1e-6:
            fail("NX_INVALID_ARGUMENT", "direction must lie in the boundary face",
                 field="/parameters/direction")  # fmt: skip
        low, high = measure.bounds(tx, [face])
        diagonal = geom.dist(low, high)
        pitch = (float(p["spacing"]), float(p.get("spacing2") or p["spacing"]))
        counts = [int(math.ceil(diagonal / v)) + 1 for v in pitch]
        if (2 * counts[0] - 1) * (2 * counts[1] - 1) > 20 * MAX_INSTANCES:
            fail("NX_TOO_MANY_OBJECTS", "The spacing is too fine for this face",
                 field="/parameters/spacing")  # fmt: skip
        p.update(boundary=face, second=geom.cross(normal, first), counts=counts, pitch=pitch)


def _along_curve(tx, service, G, p):
    path = p["curve"]
    service.PatternType = G.PatternDefinition.PatternEnum.AlongPath
    d = service.AlongPathDefinition
    builders.curve_section(tx, [path], into=d.XPath)
    d.XPathOption = G.AlongPathPattern.PathOptions.Rigid
    spacing = d.XOnPathSpacing
    spacing.SpaceType = G.PatternSpacing.SpacingType.Offset
    pitch = spacing.OnPathPitchDistance
    reason = G.OnPathDimensionBuilder.UpdateReason.Path
    pitch.Update(reason)
    spacing.OnPathSpanDistance.Update(reason)
    pitch.IsPercentUsed = False
    pitch.IsParameterUsed = False
    _expression(spacing.NCopies, int(p["count"]))
    _expression(pitch.Expression, float(p["spacing"]))
    d.UseYDirectionToggle = False


def _polygon(tx, service, G, p):
    service.PatternType = G.PatternDefinition.PatternEnum.Polygon
    d = service.PolygonDefinition
    d.RotationAxis = _smart_axis(tx, p["axis"], "/parameters/axis")[0]
    d.PolygonSizeOption = getattr(
        G.PolygonPattern.SizeOptions, (p.get("size") or "inscribed").capitalize()
    )
    _expression(d.NumberOfSides, int(p["sides"]))
    spacing = d.PolygonSpacing
    spacing.SpaceType = G.PatternSpacing.SpacingType.PolygonCountPerSide
    _expression(spacing.NCopies, int(p["count_per_side"]))
    _expression(spacing.SpanAngle, 360.0)
    _expression(d.RadialSpacing.NCopies, 1)


def _spiral(tx, service, G, p):
    service.PatternType = G.PatternDefinition.PatternEnum.Spiral
    d = service.SpiralDefinition
    d.SpiralNormal = _smart_direction(tx, p["normal"], "/parameters/normal")[0]
    d.HorizontalRef.HorizontalRefVector = _smart_direction(
        tx, p["reference"], "/parameters/reference"
    )[0]
    d.DirectionType = getattr(
        G.SpiralPattern.OrientType, (p.get("hand") or "right").capitalize() + "hand"
    )
    d.SizeSpiralType = G.SpiralPattern.SpiralDefineSize.TotalAngle
    _expression(d.TotalAngle, float(p["angle"]))
    _expression(d.RadialPitch, float(p["pitch"]))
    pitch = d.PitchAlongSpiral.OnPathPitchDistance
    pitch.IsPercentUsed = False
    _expression(pitch.Expression, float(p["spacing"]))


def _fill(tx, service, G, p):
    """A symmetric grid through the seed that covers the face, clipped by its boundary
    (NX 2406: the simplified boundary fill builds no instance through NXOpen, and a
    face-boundary fill fails its update with "mixed selection intent rules")."""
    (n1, n2), (s1, s2) = p["counts"], p["pitch"]
    grid = {"direction": p["direction"], "count": n1, "spacing": s1, "direction2": p["second"],
            "count2": n2, "spacing2": s2}  # fmt: skip
    _linear(tx, service, G, grid)
    d = service.RectangularDefinition
    d.XSymmetryToggle = True
    d.YSymmetryToggle = True
    fill = service.PatternFill
    fill.FillOptions = G.PatternFill.PatternFillOptions.FillbyBoundary
    fill.SimplifiedBoundaryToggle = False
    face = p["boundary"]
    rule = tx.part.ScRuleFactory.CreateRuleOuterEdgesOfFaces([face])
    fill.FillBoundary.AddToSection(
        [rule], face, None, None, tx.nx.Point3d(0.0, 0.0, 0.0), tx.nx.Section.Mode.Create, False
    )
    _expression(fill.FillMargin, float(p.get("margin") or 0.0))


def _general(tx, service, G, p):
    service.PatternType = G.PatternDefinition.PatternEnum.General
    d = service.GeneralDefinition
    d.FromLocationType = G.GeneralPattern.FromLocationOptions.Point
    d.FromLocationPoint = builders.point(tx, p["origin"])
    points = [builders.point(tx, q) for q in p["positions"]]
    rule = tx.part.ScRuleFactory.CreateRuleCurveDumbFromPoints(points)
    help_point = tx.nx.Point3d(*(float(v) for v in p["positions"][0]))
    d.ToPoints.AddToSection(
        [rule], points[0], None, None, help_point, tx.nx.Section.Mode.Create, False
    )


CONFIGURE = {
    "linear": _linear,
    "circular": _circular,
    "along_curve": _along_curve,
    "polygon": _polygon,
    "spiral": _spiral,
    "fill": _fill,
    "general": _general,
}


def instance_count(feature):
    """Instances of a committed pattern feature, the seed included."""
    with native():
        return 1 + len(list(feature.GetImmediateContainedFeatures()))


@tool(
    effect="model",
    label="Pattern features",
    summary=_pattern_summary,
    keywords=(
        "pattern",
        "array",
        "circular pattern",
        "polygon pattern",
        "spiral pattern",
        "fill pattern",
        "pattern along curve",
        "copies",
    ),
    requires=("work_part", "no_active_sketch"),
    variants="layout",
    cost="heavy",
    previewable=True,
    example={
        "features": ["f:8"],
        "layout": "linear",
        "parameters": {"direction": [1, 0, 0], "count": 3, "spacing": 10},
    },
)
def nx_create_pattern(
    tx: Tx,
    *,
    features: FeatureList,
    layout: Layout,
    parameters: Parameters,  # type: ignore[valid-type]
) -> PatternData:
    """Pattern features linearly, circularly, along a curve, on a polygon or spiral, filling a
    face, or at explicit points. Counts include the seed; NX computes spiral and fill counts."""
    p = dict(parameters)
    total = _check(layout, p)
    with tx.checks():
        seeds = _seeds(tx, features)
        _resolve(tx, layout, p)
    hosts = _hosts(seeds)
    before = measure.volume(tx, list(hosts.values())) if hosts else 0.0
    G = tx.nx.GeometricUtilities
    with builders.build(tx, tx.part.Features.CreatePatternFeatureBuilder, None) as b:
        b.FeatureList.Add(seeds)
        b.PatternMethod = tx.nx.Features.PatternFeatureBuilder.PatternMethodOptions.Simple
        CONFIGURE[layout](tx, b.PatternService, G, p)
        feature = b.commit()
    count = instance_count(feature)
    if total is not None:
        readback.expect(count == total, "count", "NX built a different instance count", count=count)
    else:
        readback.expect(count >= 2, "count", "NX built no instance", count=count)
        readback.expect(count <= MAX_INSTANCES, "count", f"More than {MAX_INSTANCES} instances",
                        count=count)  # fmt: skip
    bodies = list(hosts.values()) or common.feature_bodies(feature)
    out = common.result_of(tx, feature, bodies)
    if hosts:
        common.changed(before, out["volume"], "differs")
    return {**out, "layout": layout, "count": count}
