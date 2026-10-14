"""Sketch-based features: extrude, revolve, sweep (tool-surface 3.6; loft in ``lofts``).

Arguments take the short forms agents try first: an extrude ``end``/``start`` may be a
plain distance (``10``, ``"10 mm"``) and an axis or direction a bare principal name
(``"z"``, ``"-x"``); both are turned into the object forms before anything is read.
"""

from __future__ import annotations

from collections import Counter
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
from nx_mcp.kit import bodies as kbodies
from nx_mcp.kit import builders, curve, expr, geom, measure, readback, select, units
from nx_mcp.kit import sketch as ksketch

ZERO: LimitDistance = {"type": "distance", "value": 0.0}
COMBINE_CHANGE = common.COMBINE_CHANGE
# NX 2406 revolve refusal shared by several causes: a profile on both sides of the axis,
# an axis tilted out of the sketch plane, a combine whose body misses its targets.
REVOLVE_REFUSED = 671623
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
    # NX 2406 refuses a through-all extrude in a part that is not displayed ("Incorrect
    # object for this operation", live 2026-10-12); distance and until-face limits work.
    display_bound=("/end/type=through_all", "/start/type=through_all"),
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
    before = common.target_state(tx, targets)
    tags, known = common.body_tags(tx), kbodies.faulty(tx, targets)
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
        common.outside_hint(error, combine)
        raise
    common.sound(tx, common.feature_bodies(feature), known)
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


# Section checks of a revolve profile, in millimetres: curve ends closer than JOINED_MM
# are one point; free ends up to GAP_MM apart are a gap NX does not close (a section
# with a gap of 0.0007 mm was refused); each curve is sampled at SAMPLES + 1 points.
JOINED_MM = 1e-4
GAP_MM = 1.0
SAMPLES = 16


def _profile(tx, sk) -> list:
    """``[(curve, part points)]`` of the sketch's profile curves (construction curves and
    sketch points left out), each sampled at ``SAMPLES + 1`` points."""
    out = []
    for c in common.sketch_curves(sk):
        try:
            with curve.evaluator(tx, c) as e:
                points = [e.at(k / SAMPLES)[0] for k in range(SAMPLES + 1)]
        except Exception:
            continue  # a sketch point, not a curve
        out.append((c, points))
    return out


def _crosses_axis(tx, sk, profile, origin, direction) -> bool:
    """Whether the profile curves lie on both sides of an axis in the sketch plane
    (sampled); False when the axis leaves the plane."""
    f = ksketch.frame(sk)
    n = geom.unit(f["normal"])
    d = geom.unit(direction)
    tol = units.part_tolerance(tx)
    if abs(geom.dot(d, n)) > 1e-9 or abs(ksketch.height(f, origin)) > tol:
        return False
    side = geom.cross(d, n)  # in-plane normal of the axis line
    signs = set()
    for _c, points in profile:
        for p in points:
            s = geom.dot(geom.sub(p, origin), side)
            if abs(s) > tol:
                signs.add(s > 0)
    return len(signs) == 2


def _chains(tx, profile) -> dict:
    """``{"closed", "open", "branched", "gaps", "nodes"}``: closed loops, open chains and
    branched chains of the profile, the gaps (free ends that nearly meet another free end)
    and ``nodes`` ``[[point, {curve index}]]`` where curve ends meet."""
    joined, window = units.part_tolerance(tx, JOINED_MM), units.part_tolerance(tx, GAP_MM)
    nodes: list = []  # [point, {curve index}]
    ends = []
    for i, (_c, points) in enumerate(profile):
        pair = []
        for p in (points[0], points[-1]):
            k = next((k for k, n in enumerate(nodes) if geom.dist(p, n[0]) <= joined), None)
            if k is None:
                nodes.append([p, set()])
                k = len(nodes) - 1
            nodes[k][1].add(i)
            pair.append(k)
        ends.append(pair)
    degree: Counter = Counter(k for pair in ends for k in pair)
    parent = list(range(len(nodes)))

    def root(k):
        while parent[k] != k:
            k = parent[k]
        return k

    for a, b in ends:
        parent[root(a)] = root(b)
    groups: dict = {}
    for k in range(len(nodes)):
        groups.setdefault(root(k), []).append(degree[k])
    out: dict = {"closed": 0, "open": 0, "branched": 0, "gaps": [], "nodes": nodes}
    for degrees in groups.values():
        kind = "branched" if max(degrees) > 2 else "open" if min(degrees) < 2 else "closed"
        out[kind] += 1
    free = [k for k in range(len(nodes)) if degree[k] == 1]
    for x, a in enumerate(free):
        for b in free[x + 1 :]:
            gap = geom.dist(nodes[a][0], nodes[b][0])
            if gap <= window:
                out["gaps"].append({"point": [round(v, 6) for v in nodes[a][0]], "gap": gap})
    return out


def _simplified(points, tol):
    """The two ends of a sampled curve that is straight within ``tol``, else all samples."""
    a, b = points[0], points[-1]
    chord = geom.sub(b, a)
    length = geom.norm(chord)
    if length > tol and all(
        abs(geom.cross2(chord, geom.sub(p, a))) / length <= tol for p in points[1:-1]
    ):
        return [a, b]
    return points


def _segment_cross(a, b, c, d):
    """The point where 2D segments ab and cd cross inside both (touching ends excluded)."""
    r, q = geom.sub(b, a), geom.sub(d, c)
    den = geom.cross2(r, q)
    if abs(den) <= 1e-12 * geom.norm(r) * geom.norm(q):
        return None  # parallel or degenerate
    w = geom.sub(c, a)
    t, u = geom.cross2(w, q) / den, geom.cross2(w, r) / den
    if 1e-9 < t < 1 - 1e-9 and 1e-9 < u < 1 - 1e-9:
        return [a[0] + t * r[0], a[1] + t * r[1]]
    return None


def _meeting(tx, a, b, start, tol):
    """The point where curves ``a`` and ``b`` meet near ``start`` (alternating closest
    points), or None when they stay apart: sampled polylines can cross where the curves
    themselves only come close."""
    x = start
    for _ in range(25):
        pa = curve.closest(tx, a, x)[0]
        pb = curve.closest(tx, b, pa)[0]
        if geom.dist(pa, pb) <= tol:
            return pb
        x = pb
    return None


def _crossings(tx, sk, profile, nodes) -> list:
    """Points (at most 5) where two profile curves cross or one crosses itself, away from
    the ends where curves join."""
    f = ksketch.frame(sk)
    tol = units.part_tolerance(tx)
    polys = [_simplified([ksketch.to_local(f, p) for p in points], tol) for _c, points in profile]
    boxes = [
        ([min(p[k] for p in poly) for k in (0, 1)], [max(p[k] for p in poly) for k in (0, 1)])
        for poly in polys
    ]
    found: list = []
    for i in range(len(polys)):
        for j in range(i, len(polys)):
            (lo_i, hi_i), (lo_j, hi_j) = boxes[i], boxes[j]
            if any(lo_i[k] > hi_j[k] + tol or lo_j[k] > hi_i[k] + tol for k in (0, 1)):
                continue
            hit = _pair_crossing(tx, f, profile, nodes, (i, j), (polys[i], polys[j]), tol)
            if hit is not None:
                found.append([round(v, 6) for v in hit])
                if len(found) >= 5:
                    return found
    return found


def _pair_crossing(tx, f, profile, nodes, pair, polys, tol):
    """The first crossing of profile curves ``pair`` (one index twice: the curve with
    itself), or None."""
    (i, j), (pi, pj) = pair, polys
    closed = len(pi) > 2 and geom.dist(pi[0], pi[-1]) <= tol
    for s in range(len(pi) - 1):
        for t in range(s + 2 if i == j else 0, len(pj) - 1):
            if i == j and closed and s == 0 and t == len(pj) - 2:
                continue  # a closed curve's first and last segments meet at its seam
            x = _segment_cross(pi[s], pi[s + 1], pj[t], pj[t + 1])
            if x is None:
                continue
            point = ksketch.to_part(f, x)
            if i != j:
                try:
                    point = _meeting(tx, profile[i][0], profile[j][0], point, 10 * tol)
                except Exception:
                    point = None  # UF cannot evaluate: no evidence of a crossing
                if point is None or any(
                    {i, j} <= n[1] and geom.dist(point, n[0]) <= 100 * tol for n in nodes
                ):
                    continue  # apart, or where the two curves join
            return point
    return None


def _section(tx, sk, profile) -> dict:
    """:func:`_chains` plus ``crossings`` of the profile."""
    section = _chains(tx, profile)
    section["crossings"] = _crossings(tx, sk, profile, section["nodes"])
    return section


def _tilt(sk, direction) -> float:
    """|cos| of the angle between the axis and the sketch normal: 0 for an axis in (or
    parallel to) the sketch plane, 1 for an axis normal to it."""
    return abs(geom.dot(geom.unit(direction), geom.unit(ksketch.frame(sk)["normal"])))


def _tilted(normal: bool, nx_code=None, nx_message=None) -> NoReturn:
    """An axis NX cannot revolve about: normal to the sketch plane (the profile would turn
    in its own plane) or tilted out of it (live NX 2406: refused, 671623)."""
    fail(
        "NX_INVALID_ARGUMENT",
        "The axis is normal to the sketch plane: the profile would turn in its own plane"
        if normal
        else "NX could not revolve the profile about an axis tilted out of the sketch plane",
        field="/axis",
        hint="Use an axis lying in (or parallel to) the sketch plane, e.g. x or y for a sketch "
        "on XY",
        nx_code=nx_code,
        details={"nx_message": nx_message} if nx_message else None,
    )


def _crosses():
    fail(
        "NX_INVALID_ARGUMENT",
        "The profile crosses the revolve axis; keep it on one side of the axis",
        field="/axis",
        hint="Move the sketch curves to one side of the axis (touching it is fine), "
        "or choose another axis",
    )


def _check_section(section) -> None:
    """Refuse, before NX, a section with gaps between curve ends or crossing curves."""
    gaps = section["gaps"]
    if gaps:
        worst = max(gaps, key=lambda g: g["gap"])
        fail(
            "NX_WRONG_GEOMETRY",
            f"The profile has {len(gaps)} gap(s) between curve ends (up to {worst['gap']:.3g} "
            f"at {worst['point']}): NX does not join ends that do not coincide",
            field="/sketch",
            hint="Make the ends coincide exactly: nx_get_sketch lists the end points; reshape "
            "a curve onto the other's end with nx_edit_sketch_curves",
            details={"gaps": gaps[:10]},
        )
    if section["crossings"]:
        at = section["crossings"]
        fail(
            "NX_WRONG_GEOMETRY",
            f"The profile curves cross each other at {at[0]} ({len(at)} crossing(s) found): NX "
            "cannot revolve a self-intersecting section",
            field="/sketch",
            hint="Trim the curves at the crossing (nx_edit_sketch_curves) and make leftover "
            "curves construction, so each region is one clean loop",
            details={"crossings": at},
        )


def _refused(error, combine, section) -> NoReturn:
    """NX's 671623 once the axis, crossings and gaps are ruled out: the causes left."""
    causes = []
    loose = section["open"] + section["branched"]
    if loose and section["closed"]:
        causes.append(
            f"the sketch has {loose} open or branched chain(s) next to {section['closed']} "
            "closed loop(s): make leftover curves construction"
        )
    if combine is not None:
        causes.append("the revolved body misses the combine targets")
    else:
        causes.append("the profile touches or crosses the axis between the sampled points")
    fail(
        "NX_BUILD_FAILED",
        f"NX refused the revolve ({error.message}); the profile stays on one side of the "
        "axis with no gap or crossing found. Likely: " + "; or ".join(causes),
        field="/combine/targets" if combine is not None and not loose else "/sketch",
        hint="Check the sketch loops with nx_get_sketch; or revolve without combine and "
        "use nx_create_boolean",
        nx_code=error.nx_code,
        details={k: section[k] for k in ("closed", "open", "branched")},
    )


class RevolveData(common.FeatureData, total=False):
    sheet: Annotated[bool, Doc("True: sheet bodies (surfaces); False: solids")]
    area: float  # the sheets' total area


def _open_note(tx, section, sheet) -> None:
    """Warn about open chains: next to closed loops each becomes a body of its own; alone,
    they made a solid unless ``sheet`` (NX built sheet bodies)."""
    loose = section["open"] + section["branched"]
    if loose and section["closed"]:
        tx.warn(
            "NX_UPDATE_WARNING",
            f"The section has {loose} open chain(s) next to {section['closed']} closed "
            "loop(s): NX revolved each into a body of its own; make leftover curves "
            "construction (nx_edit_sketch_curves) and delete the extra body unless intended",
        )
    elif loose and not sheet:
        tx.warn(
            "NX_NOTE",
            "The profile is open: NX closed the revolved shape into a solid; pass sheet: true "
            "for a surface",
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
    sheet: Annotated[bool, Doc("Build sheet bodies (surfaces), not solids; no combine")] = False,
) -> RevolveData:
    """Revolve a sketch about an absolute axis ('z' is world Z), as a new body or combined with
    bodies. Angles follow the right-hand rule about the axis direction. NX closes an open
    profile into a solid unless sheet is true; the result's sheet says which was built."""
    axis = common.principal(axis)
    start, end = float(start_angle), float(end_angle)
    if not start < end <= start + 360.0:
        fail("NX_INVALID_ARGUMENT", "Use start < end <= start + 360", field="/end_angle")
    if sheet and combine is not None:
        fail("NX_INVALID_ARGUMENT", "A sheet revolve cannot combine with solid bodies",
             field="/combine", hint="Omit combine, or omit sheet to build a solid")  # fmt: skip
    common.check_combine(combine)
    with tx.checks():
        sk = ksketch.resolve(tx, sketch)
        resolved = select.axis(tx, axis, "/axis")
        tilt = _tilt(sk, resolved[2])
        if tilt > 1 - 1e-9:
            _tilted(True)
        profile = _profile(tx, sk)
        if _crosses_axis(tx, sk, profile, resolved[1], resolved[2]):
            _crosses()
        section = _section(tx, sk, profile)
        _check_section(section)
    targets = common.combine_targets(tx, combine)
    before = common.target_state(tx, targets)
    tags, known = common.body_tags(tx), kbodies.faulty(tx, targets)
    try:
        with builders.build(tx, tx.part.Features.CreateRevolveBuilder, None) as b:
            b.Section = builders.section(tx, sk)
            b.Axis = _axis(tx, axis, resolved)
            expr.drive(b.Limits.StartExtend.Value, start_angle)
            expr.drive(b.Limits.EndExtend.Value, end_angle)
            if sheet:
                style = tx.nx.GeometricUtilities.FeatureOptions.BodyStyle
                b.FeatureOptions.BodyType = style.Sheet
            builders.boolean(tx, b.BooleanOperation, combine)
            feature = b.commit()
    except NXError as error:
        if error.nx_code != REVOLVE_REFUSED:
            common.outside_hint(error, combine)
            raise
        if tilt > 1e-9:  # here the axis leaves the plane, not the profile
            _tilted(False, error.nx_code, error.message)
        _refused(error, combine, section)
    common.sound(tx, common.feature_bodies(feature), known)
    if combine is not None:
        _open_note(tx, section, False)
        out = cast(RevolveData, common.combined(tx, feature, combine, targets, before, tags))
        return {**out, "sheet": False}
    out = cast(RevolveData, common.result_of(tx, feature))
    made = _sheet(tx, feature, out)
    if sheet:
        readback.expect(made is not None, "sheet", "NX built solids, not sheet bodies")
    _open_note(tx, section, made is not None)
    if made is not None:
        return cast(RevolveData, made)
    common.changed(0.0, out["volume"], "positive")
    return {**out, "sheet": False}


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
    common.swept_sound(tx, swept)  # swapped section and guide self-intersect
    if combine is None:
        out = common.result_of(tx, feature)
        common.changed(0.0, out["volume"], "positive")
        return out
    # NX 2406 Swept has no boolean option: combine with a Boolean feature.
    return common.combine_new(tx, feature, swept, combine, targets[0], before)
