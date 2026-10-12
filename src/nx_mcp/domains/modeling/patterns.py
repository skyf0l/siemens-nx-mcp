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
from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    Angle,
    Axis,
    BodyRef,
    ByVariant,
    CountValue,
    Direction,
    Doc,
    FaceRef,
    FeatureRef,
    Items,
    Json,
    Length,
    Plane,
    Point3,
    Range,
    RefTo,
    forbid_extra,
)
from nx_mcp.domains.modeling import common
from nx_mcp.kit import args, builders, expr, geom, measure, readback, result, select

BodyList = Annotated[list[BodyRef], Items(1, 50)]
FeatureList = Annotated[list[FeatureRef], Items(1, 50), Doc("Feature ids f:.., not bodies b:..")]
MAX_INSTANCES = 1000


# ---------------------------------------------------------------------------
# Shared helpers


def _plane(tx, value, field):
    """``(datum plane, origin, unit normal)``: a datum plane ref is used as is (associative);
    a face, principal or explicit plane becomes a fixed datum plane. A bad plane is refused
    before anything is built (not_started)."""
    with tx.checks():
        obj, origin, normal = select.plane(tx, value, field)
    if obj is not None and hasattr(obj, "Normal"):
        return obj, origin, normal
    return builders.fixed_plane(tx, origin, normal), origin, normal


def _seeds(tx, features, field="/features"):
    args.distinct(field, features, "features")
    seeds = []
    for i, ref in enumerate(features):
        try:
            f = tx.resolve(ref, "feature", work_part=True)
        except NXError as error:
            if error.code == "NX_WRONG_KIND":
                _body_instead(tx, ref, error, f"{field}/{i}")
            raise
        if getattr(f, "IsInternal", False):
            fail("NX_INVALID_ARGUMENT", "Use the owning feature", field=f"{field}/{i}")
        seeds.append(f)
    return seeds


def _body_instead(tx, ref, error, field):
    """Point a body passed where a feature is expected at the feature that made it."""
    error.field = error.field or field
    try:
        body = tx.resolve(ref, "body")
        made = [tx.ref(f, kind="feature")["id"] for f in list(body.GetFeatures())[:1]]
    except Exception:  # not a body, or NX cannot tell its features: keep the error
        return
    error.hint = (
        f"Pass the feature that made the body ({made[0]}, see nx_list_objects kind feature)"
        if made
        else "Pass a feature id f:.. (nx_list_objects kind feature)"
    ) + "; to copy whole bodies use nx_create_mirror target bodies or nx_create_transform copy"


def _hosts(seeds):
    return {int(b.Tag): b for f in seeds for b in common.feature_bodies(f)}


UNITE_TEXT = "Unite copies made as separate bodies into the seed's body (they must touch it)"


class United(TypedDict, total=False):
    united: Annotated[int, Doc("Copies united into the seed's body (unite)")]
    unite_feature: result.Row


def _unite_host(hosts, unite, field="/unite"):
    """The one body ``unite`` merges into (checked before NX), else None."""
    if not unite:
        return None
    if len(hosts) != 1:
        fail(
            "NX_INVALID_ARGUMENT",
            "unite needs seed features of one body"
            + (f": they own {len(hosts)} bodies" if hosts else ": they own no body"),
            field=field,
            hint="Pattern or mirror the features of one body per call, or unite the bodies "
            "afterwards with nx_create_boolean",
        )
    return next(iter(hosts.values()))


def _unite_new(tx, host, new):
    """``(bodies, extra data)`` after uniting the ``new`` copy bodies into ``host``."""
    if host is None or not new:
        return None, {}
    feature, body = common.unite_into(tx, host, new)
    return [body], {"united": len(new), "unite_feature": tx.ref(feature, kind="feature")}


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


def _expression(target, value):
    """Set a builder expression: an int as a whole number, an expression formula as
    its formula (the pattern stays driven by the expression)."""
    expr.drive(target, value, integer=isinstance(value, int))


# ---------------------------------------------------------------------------
# Mirror


class MirrorData(common.FeatureData, United):
    target: str
    count: int
    new_bodies: Annotated[
        int,
        Doc("New bodies: every mirrored body, or mirrors of a feature with its own body "
            "(0 after unite)"),
    ]  # fmt: skip


def _mirror_summary(a, say):
    if a.get("target") == "features":
        n = len(a.get("features") or [])
        return f"Mirror {n} feature" + ("" if n == 1 else "s")
    return "Mirror " + common.bodies_text(a)


def _mirror_bodies(tx, refs, plane, keep_original):
    with tx.checks():
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
    out = common.result_of(tx, feature)
    return {**out, "target": "bodies", "count": len(made), "new_bodies": len(made)}


def _mirror_features(tx, refs, plane, unite):
    with tx.checks():
        seeds = _seeds(tx, refs)
        hosts = _hosts(seeds)
        if not hosts:
            fail("NX_WRONG_GEOMETRY", "The features own no body to mirror into",
                 field="/features")  # fmt: skip
        host = _unite_host(hosts, unite)
    datum, _origin, _normal = _plane(tx, plane, "/plane")
    before = measure.volume(tx, list(hosts.values()))
    existing = {int(b.Tag) for b in tx.part.Bodies}
    G = tx.nx.GeometricUtilities
    with builders.build(tx, tx.part.Features.CreateMirrorBuilder, None) as b:
        b.FeatureList.Add(seeds)
        service = b.PatternService
        service.PatternType = G.PatternDefinition.PatternEnum.Mirror
        mirror = service.MirrorDefinition
        mirror.PlaneOption = G.MirrorPattern.PlaneOptions.Existing
        mirror.ExistingPlane.Value = datum
        feature = b.commit()
    # As for patterns: NX mirrors a seed that made its own body (an extrude without
    # combine) as a new body and leaves the seed body unchanged: count it in.
    new = [b for b in tx.part.Bodies if int(b.Tag) not in existing]
    out = common.result_of(tx, feature, [*hosts.values(), *new])
    common.changed(before, out["volume"], "differs")
    united, extra = _unite_new(tx, host, new)
    if united is not None:
        out = {**common.result_of(tx, feature, united), **extra}
        new = []
    return {**out, "target": "features", "count": len(seeds), "new_bodies": len(new)}


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
    unite: Annotated[
        bool, Doc("features only: unite separate-body mirrors into the seed's body")
    ] = False,
) -> MirrorData:
    """Mirror solid bodies, or features into their own bodies, about a plane ('yz', a datum
    plane or a face). target bodies: bodies and volume are the new mirrors only. Mirrors of
    a feature that made its own body are new bodies (new_bodies) unless unite merges them
    into the seed's body."""
    plane = common.principal_plane(plane)
    wanted, other = ("bodies", "features") if target == "bodies" else ("features", "bodies")
    given: dict[str, list[str] | None] = {"bodies": bodies, "features": features}
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
        return _mirror_features(tx, features, plane, unite)
    if unite:
        fail(
            "NX_INVALID_ARGUMENT",
            "unite applies to target features",
            field="/unite",
            hint="Unite mirrored bodies with nx_create_boolean",
        )
    return _mirror_bodies(tx, bodies, plane, keep_original)


# ---------------------------------------------------------------------------
# Pattern layouts


# Counts may be an NX expression name or formula too (``"n_bolts"``): the pattern follows it.
Count = Annotated[CountValue, Range(ge=2, le=MAX_INSTANCES), Doc("Instances including the seed")]


class _LinearRequired(TypedDict):
    direction: Direction
    count: Count
    spacing: Length


class LinearForm(_LinearRequired, total=False):
    direction2: Direction
    count2: Annotated[CountValue, Range(ge=1, le=MAX_INSTANCES)]
    spacing2: Length


class CircularForm(TypedDict):
    axis: Axis
    count: Annotated[CountValue, Range(ge=2, le=360), Doc("Instances including the seed")]
    angle: Annotated[
        Angle,
        Doc("Step between neighbours, not the total span: 6 evenly on a circle use 60"),
    ]


class AlongCurveForm(TypedDict):
    curve: Annotated[
        Annotated[str, RefTo("edge", "curve")],
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
    boundary: Annotated[FaceRef, Doc("Planar face the grid through the seed fills")]
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
forbid_extra(*LAYOUT_FORMS.values())

Layout = Literal["linear", "circular", "along_curve", "polygon", "spiral", "fill", "general"]
Parameters = Annotated[
    dict,
    Json(max_keys=8),
    ByVariant("layout", LAYOUT_FORMS),
    Doc("Layout parameters: nx_get_tool_schemas(nx_create_pattern#<layout>)"),
]


class PatternData(United):
    feature: result.Row
    bodies: Annotated[list[result.Row], Doc("The seed's body first, then the new bodies")]
    volume: float
    layout: str
    count: int
    new_bodies: Annotated[
        int,
        Doc("Instances NX built as separate bodies (a seed that made its own body; 0 after "
            "unite)"),
    ]  # fmt: skip


def _pattern_summary(a, say):
    count = (a.get("parameters") or {}).get("count")
    return f"Pattern {say('features')} {a.get('layout')}" + (f" x{count}" if count else "")


def _check(layout, p):
    """Argument checks before NX; returns the requested instance count or None (NX decides)."""
    where = "/parameters"
    for key, low in (("count", 2), ("count2", 1)):
        if p.get(key) is not None and not low <= p[key] <= MAX_INSTANCES:  # an expression's
            fail(
                "NX_INVALID_ARGUMENT",
                f"{key} must be {low}..{MAX_INSTANCES}, got {p[key]}",
                field=f"{where}/{key}",
            )
    if layout == "linear":
        second = [p.get(k) is not None for k in ("direction2", "count2", "spacing2")]
        if any(second) and not all(second):
            fail(
                "NX_INVALID_ARGUMENT",
                "A second direction needs direction2, count2 and spacing2",
                field=where,
            )
        for key in ("spacing", "spacing2") if all(second) else ("spacing",):
            if p[key] == 0:
                fail("NX_INVALID_ARGUMENT", f"{key} must not be zero", field=f"{where}/{key}")
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
    _expression(d.XSpacing.NCopies, p["count"])
    _expression(d.XSpacing.PitchDistance, expr.number(p["spacing"]))
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
        _expression(d.YSpacing.NCopies, p["count2"])
        _expression(d.YSpacing.PitchDistance, expr.number(p["spacing2"]))
    else:
        _expression(d.YSpacing.NCopies, 1)


def _circular(tx, service, G, p):
    service.PatternType = G.PatternDefinition.PatternEnum.Circular
    d = service.CircularDefinition
    d.RotationAxis = _smart_axis(tx, p["axis"], "/parameters/axis")[0]
    spacing = d.AngularSpacing
    spacing.SpaceType = G.PatternSpacing.SpacingType.Offset
    _expression(spacing.NCopies, p["count"])
    _expression(spacing.PitchAngle, expr.number(p["angle"]))
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
    _expression(spacing.NCopies, p["count"])
    _expression(pitch.Expression, expr.number(p["spacing"]))
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
    _expression(d.TotalAngle, expr.number(p["angle"]))
    _expression(d.RadialPitch, expr.number(p["pitch"]))
    pitch = d.PitchAlongSpiral.OnPathPitchDistance
    pitch.IsPercentUsed = False
    _expression(pitch.Expression, expr.number(p["spacing"]))


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
    _expression(fill.FillMargin, expr.number(p.get("margin") or 0.0))


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


# Layout fields that take a bare principal name ("x", "-z") for the axis or direction.
SHORT_AXES = frozenset({"direction", "direction2", "axis", "normal", "reference"})

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
    example={  # 6 instances evenly on a full circle: angle is the 60 deg step, not 360
        "features": ["f:8"],
        "layout": "circular",
        "parameters": {"axis": {"principal": "z"}, "count": 6, "angle": 60},
    },
)
def nx_create_pattern(
    tx: Tx,
    *,
    features: FeatureList,
    layout: Layout,
    parameters: Parameters,
    unite: Annotated[bool, Doc(UNITE_TEXT)] = False,
) -> PatternData:
    """Pattern features linearly, circularly, along a curve, on a polygon or spiral, filling a
    face, or at explicit points. Counts include the seed; NX computes spiral and fill counts.
    A seed with its own body makes new bodies (new_bodies) unless unite; a circular angle
    is the step between instances."""
    p = {k: common.principal(v) if k in SHORT_AXES else v for k, v in parameters.items()}
    total = _check(layout, p)
    with tx.checks():
        seeds = _seeds(tx, features)
        _resolve(tx, layout, p)
        hosts = _hosts(seeds)
        host = _unite_host(hosts, unite)
    before = measure.volume(tx, list(hosts.values())) if hosts else 0.0
    existing = {int(b.Tag) for b in tx.part.Bodies}
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
    # NX 2406 patterns a seed that made its own body (an extrude without combine) as new
    # bodies, one per instance, and leaves the seed body unchanged: count them in.
    new = [b for b in tx.part.Bodies if int(b.Tag) not in existing]
    bodies = [*hosts.values(), *new] or common.feature_bodies(feature)
    out = common.result_of(tx, feature, bodies)
    if hosts:
        common.changed(before, out["volume"], "differs")
    united, extra = _unite_new(tx, host, new)
    if united is not None:
        out = {**common.result_of(tx, feature, united), **extra}
        new = []
    return {**out, "layout": layout, "count": count, "new_bodies": len(new)}
