"""``nx_create_hole``: the NX Hole feature (HolePackage) on a planar face (tool-surface 3.6).

NX 2406 lessons kept here: hole centres must lie inside the face (UF point
containment, 1 = inside); points within 1e-4 x the coordinate scale of the
plane are projected onto it; setting a thread size does not refresh the tap
drill, so it is set from the table row; start/end chamfers and relief are off;
the depth is measured to the cylinder bottom (the drill tip comes on top); the
committed parameters are read back through an edit builder that is destroyed
without committing.

Short forms: depth 10, '10 mm' or 'through_all'; a position [x, y] on a face normal to
z (y and z on a face normal to x); a threaded hole's thread {'size': 'M6'} (standard
Metric Coarse, see ``threads``) drills the 0.75-engagement tap drill when diameter is
omitted.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    Angle,
    BodyRef,
    Doc,
    FaceRef,
    Items,
    Length,
    LimitDistance,
    LimitThroughAll,
    Point3,
    forbid_extra,
)
from nx_mcp.domains.modeling import common, threads
from nx_mcp.kit import builders, expr, geom, measure, readback, result, select, units

FORMS = ("simple", "counterbore", "countersink", "threaded")
# [x, y, z], or [a, b]: the two coordinates in the face plane, in x, y, z order.
FacePoint = Annotated[list[float], Items(2, 3), Doc("[x, y, z], or [x, y] on a face normal to z")]
DepthArg = Annotated[
    LimitDistance | LimitThroughAll | Length | Literal["through_all", "through"],
    Doc("To the cylinder bottom: 10, '10 mm' or 'through_all'"),
]


class Counterbore(TypedDict):
    diameter: Length
    depth: Length


class _CountersinkRequired(TypedDict):
    diameter: Length


class Countersink(_CountersinkRequired, total=False):
    angle: Angle


class _ThreadRequired(TypedDict):
    size: threads.SizeText


class HoleThread(_ThreadRequired, total=False):
    standard: threads.StandardText
    depth: Annotated[Length, Doc("Thread depth; default the hole depth")]


forbid_extra(Counterbore, Countersink, HoleThread)


def tip_height(diameter, tip_angle):
    return diameter / 2 / math.tan(math.radians(tip_angle) / 2)


def countersink_height(diameter, sink_diameter, sink_angle):
    return (sink_diameter - diameter) / 2 / math.tan(math.radians(sink_angle) / 2)


def nominal_volume(spec):
    """Analytic volume of one blind hole (cylinders, countersink frustum, drill tip)."""
    r = spec["diameter"] / 2
    depth = spec["depth"]
    volume = math.pi * r * r * tip_height(spec["diameter"], spec["tip_angle"]) / 3
    if spec["form"] == "counterbore":
        big, h = spec["counterbore"]["diameter"] / 2, spec["counterbore"]["depth"]
        return volume + math.pi * big * big * h + math.pi * r * r * (depth - h)
    if spec["form"] == "countersink":
        big = spec["countersink"]["diameter"] / 2
        h = countersink_height(spec["diameter"], 2 * big, spec["countersink"]["angle"])
        return (
            volume + math.pi * h / 3 * (big * big + big * r + r * r) + math.pi * r * r * (depth - h)
        )
    return volume + math.pi * r * r * depth


def depth_limit(value) -> dict:
    """The Limit object of a depth given as a distance, 'through_all' or an object."""
    if isinstance(value, dict):
        return value
    if value in ("through_all", "through"):
        return {"type": "through_all"}
    if float(value) <= 0:
        fail("NX_INVALID_ARGUMENT", "depth must be positive", field="/depth")
    return {"type": "distance", "value": value}


def check_spec(form, diameter, depth, tip_angle, counterbore, countersink, thread):
    """A normalised hole spec, or ``invalid_argument`` (no NX needed). A threaded hole's
    diameter may be None (the tap drill of its thread, set by ``tap_row``)."""
    if diameter is None and form != "threaded":
        fail(
            "NX_INVALID_ARGUMENT",
            f"A {form} hole needs diameter",
            field="/diameter",
            hint="e.g. diameter 6 or '6 mm'",
        )
    if diameter is not None and diameter <= 0:
        fail("NX_INVALID_ARGUMENT", "diameter must be positive", field="/diameter")
    if not 0 < tip_angle < 180:
        fail("NX_INVALID_ARGUMENT", "tip_angle must be between 0 and 180", field="/tip_angle")
    through = depth["type"] == "through_all"
    spec = {
        "form": form,
        "diameter": None if diameter is None else expr.number(diameter),
        "tip_angle": expr.number(tip_angle),
        "through_all": through,
        "depth": None if through else expr.number(depth["value"]),
    }
    if spec["depth"] is not None and spec["depth"] <= 0:
        fail("NX_INVALID_ARGUMENT", "depth must be positive", field="/depth/value")
    given = {"counterbore": counterbore, "countersink": countersink, "thread": thread}
    wanted = {"counterbore": "counterbore", "countersink": "countersink", "threaded": "thread"}
    for key, value in given.items():
        if value is not None and wanted.get(form) != key:
            right = next(f for f, k in wanted.items() if k == key)
            fail(
                "NX_INVALID_ARGUMENT",
                f"{key} does not apply to a {form} hole",
                field="/" + key,
                hint=f"Pass form {right!r} with {key}, or drop {key}",
            )
    if form in wanted and given[wanted[form]] is None:
        fail(
            "NX_INVALID_ARGUMENT",
            f"A {form} hole needs {wanted[form]}",
            field="/" + wanted[form],
            hint={
                "counterbore": "e.g. counterbore {'diameter': 11, 'depth': 4}",
                "countersink": "e.g. countersink {'diameter': 12, 'angle': 90}",
                "threaded": "e.g. thread {'size': 'M6'} (Metric Coarse; diameter defaults to "
                "the tap drill)",
            }[form],
        )
    if form == "counterbore":
        cb = {
            "diameter": expr.number(counterbore["diameter"]),
            "depth": expr.number(counterbore["depth"]),
        }
        if cb["diameter"] <= spec["diameter"]:
            fail("NX_INVALID_ARGUMENT",
                 f"The counterbore ({cb['diameter']:g}) must be wider than the hole "
                 f"({spec['diameter']:g})", field="/counterbore")  # fmt: skip
        if cb["depth"] <= 0 or (spec["depth"] is not None and cb["depth"] >= spec["depth"]):
            fail("NX_INVALID_ARGUMENT", "counterbore depth must be 0..depth", field="/counterbore")
        spec["counterbore"] = cb
    elif form == "countersink":
        cs = {
            "diameter": expr.number(countersink["diameter"]),
            "angle": expr.number(countersink.get("angle", 90.0)),
        }
        if cs["diameter"] <= spec["diameter"]:
            fail("NX_INVALID_ARGUMENT",
                 f"The countersink ({cs['diameter']:g}) must be wider than the hole "
                 f"({spec['diameter']:g})", field="/countersink")  # fmt: skip
        if not 0 < cs["angle"] < 180:
            fail("NX_INVALID_ARGUMENT", "countersink angle must be 0..180", field="/countersink")
        h = countersink_height(spec["diameter"], cs["diameter"], cs["angle"])
        if spec["depth"] is not None and h >= spec["depth"]:
            fail(
                "NX_INVALID_ARGUMENT",
                f"The countersink cone ({h:.6g} deep) must be shallower than depth",
                field="/countersink",
            )
        spec["countersink"] = cs
    elif form == "threaded":
        td = thread.get("depth")
        if td is not None and (
            td <= 0 or (spec["depth"] is not None and td > spec["depth"] * (1 + 1e-9))
        ):
            fail("NX_INVALID_ARGUMENT", "thread depth must be 0..depth", field="/thread/depth")
        spec["thread"] = {**thread, "depth": None if td is None else expr.number(td)}
    return spec


def check_positions(positions):
    """The positions as lists; two equal positions (of the same form) are refused."""
    scale = max([1.0] + [abs(c) for p in positions for c in p])
    for i, a in enumerate(positions):
        for j in range(i):
            if len(a) == len(positions[j]) and geom.dist(a, positions[j]) <= 1e-6 * scale:
                fail(
                    "NX_INVALID_ARGUMENT",
                    f"positions {i} and {j} coincide",
                    field=f"/positions/{i}",
                )
    return [list(p) for p in positions]


def tap_row(rows, spec):
    """The table row of the hole's thread whose tap drill equals the hole diameter; with no
    diameter, the default engagement's row (its tap drill becomes the diameter). The
    thread's standard and size become the table's names. Needs no NX."""
    t = spec["thread"]
    candidates = threads.size_rows(rows, t.get("standard"), t["size"], "/thread/size")
    t["standard"], t["size"] = candidates[0]["Standard"], candidates[0]["Size"]
    usable = [r for r in candidates if r.get("TapDrillDia") and r.get("RadialEngage") != "Custom"]
    if spec["diameter"] is None:
        row = threads.pick_row(usable or candidates, field="/thread")
        spec["diameter"] = float(row["TapDrillDia"])
        return row
    tol = 1e-6 * max(1.0, spec["diameter"])
    for r in usable:
        if abs(float(r["TapDrillDia"]) - spec["diameter"]) <= tol:
            return r
    drills = sorted({float(r["TapDrillDia"]) for r in usable})
    major = common.float_or_none(candidates[0].get("MajorDiameter"))
    clearance = major is not None and spec["diameter"] >= major
    fail(
        "NX_INVALID_ARGUMENT",
        f"A threaded {t['size']} hole drills a tap drill {drills}, not {spec['diameter']:g}",
        field="/diameter",
        hint="Omit diameter to drill the 0.75-engagement tap drill"
        + ("; a clearance hole for the screw is form 'simple'" if clearance else ""),
        details={"tap_drill_diameters": drills},
    )


def _placement(tx, face, target):
    """(face, body, plane point, outward unit normal) of a planar face of a solid."""
    placement = select.one(tx, face, "face", field="/face")
    modeling = tx.uf.Modeling
    kind, point, _ = select.face_data(tx, placement)
    if kind != select.FACE_PLANE:
        fail("NX_WRONG_GEOMETRY", "The placement face must be planar", field="/face")
    u0, u1, v0, v1 = modeling.AskFaceUvMinmax(placement.Tag)
    props = modeling.AskFaceProps(placement.Tag, [(u0 + u1) / 2, (v0 + v1) / 2])
    outward = geom.unit(list(props[5]), "/face")
    body = placement.GetBody()
    if not getattr(body, "IsSolidBody", True):
        fail("NX_WRONG_GEOMETRY", "The face must belong to a solid body", field="/face")
    if target is not None:
        chosen = select.one(tx, target, "body", field="/target")
        if int(chosen.Tag) != int(body.Tag):
            fail("NX_WRONG_OWNER", "target must be the body that owns the face", field="/target")
    return placement, body, point, outward


def _principal(normal):
    """Index of the axis a unit normal lies along, or None."""
    return next((i for i in range(3) if abs(abs(normal[i]) - 1.0) <= 1e-9), None)


def _complete(points, origin, normal):
    """3D points: an [a, b] position takes the face plane's coordinate along its normal."""
    axis = _principal(normal)
    out = []
    for i, p in enumerate(points):
        if len(p) == 3:
            out.append(list(p))
            continue
        if axis is None:
            fail(
                "NX_INVALID_ARGUMENT",
                f"position {i} has 2 coordinates but the face is not normal to x, y or z",
                field=f"/positions/{i}",
                hint="Give [x, y, z] on the face",
            )
        q = list(p)
        q.insert(axis, origin[axis])
        out.append(q)
    return out


def _outside(tx, face, q) -> str:
    """Why a point of the face plane is off the face: inside its outline (an opening, such as
    an existing hole) or beyond it."""
    try:
        low, high = measure.bounds(tx, [face])
    except Exception:  # noqa: BLE001 - no box: say it plainly
        return "outside the face"
    slack = 1e-6 * max(1.0, *(abs(v) for v in (*low, *high)))
    if all(low[k] - slack <= q[k] <= high[k] + slack for k in range(3)):
        return "in an opening of the face (e.g. an existing hole), not on its material"
    return "outside the face"


def _on_face(tx, placement, points, origin, normal):
    """Project points within tolerance onto the plane; each must lie inside the face."""
    points = check_positions(_complete(points, origin, normal))
    tol = 1e-4 * max([1.0, *(abs(c) for p in [*points, origin] for c in p)])
    axis = _principal(normal)
    out = []
    for i, p in enumerate(points):
        distance = geom.dot(geom.sub(p, origin), normal)
        if abs(distance) > tol:
            at = f"{'xyz'[axis]} = {origin[axis]:.6g}" if axis is not None else "its plane"
            fail(
                "NX_INVALID_ARGUMENT",
                f"position {i} is {distance:.6g} off the face plane ({at})",
                field=f"/positions/{i}",
                hint="Give a point on the face"
                + (f" ({at}) or its 2 in-plane coordinates" if axis is not None else ""),
                details={"tolerance": tol},
            )
        q = geom.sub(p, geom.scale(normal, distance))
        state = tx.uf.Modeling.AskPointContainment([float(c) for c in q], placement.Tag)
        if state != 1:  # 1 inside, 2 outside, 3 on the boundary
            where = "on the face boundary" if state == 3 else _outside(tx, placement, q)
            fail("NX_INVALID_ARGUMENT", f"position {i} {q} is {where}", field=f"/positions/{i}",
                 hint="Pick a point on the face material, away from its edges and openings")  # fmt: skip
        out.append(q)
    return out


def _configure(tx, b, spec, row):
    B = tx.nx.Features.HolePackageBuilder
    values = []
    if spec["form"] == "threaded":
        b.Type = B.Types.ThreadedHole
        b.ThreadStandard = row["Standard"]
        b.ThreadSize = row["Size"]
        b.RadialEngageOption = row["RadialEngage"]
        values.append((b.TapDrillDiameter, float(row["TapDrillDia"])))
        b.ThreadedStartChamferEnabled = False
        b.ThreadedEndChamferEnabled = False
        b.ThreadedReliefEnabled = False
        b.RelateHoleDepthToThreadDepth = False
        b.ThreadRotation = B.ThreadRotationOptions.Right
        values.append((b.ThreadedTipAngle, spec["tip_angle"]))
        depth_t = spec["thread"]["depth"]
        if spec["through_all"] and depth_t is None:
            b.ThreadLengthOption = B.ThreadLengthOptions.Full
        else:
            b.ThreadLengthOption = B.ThreadLengthOptions.Custom
            values.append((b.ThreadDepth, depth_t or spec["depth"]))
        if not spec["through_all"]:
            values.append((b.ThreadedHoleDepth, spec["depth"]))
    else:
        b.Type = B.Types.GeneralHole
        values.append((b.GeneralTipAngle, spec["tip_angle"]))
        if spec["form"] == "simple":
            b.GeneralHoleForm = B.HoleForms.Simple
            values.append((b.GeneralSimpleHoleDiameter, spec["diameter"]))
            depth = b.GeneralSimpleHoleDepth
        elif spec["form"] == "counterbore":
            b.GeneralHoleForm = B.HoleForms.Counterbored
            values.append((b.GeneralCounterboreDiameter, spec["counterbore"]["diameter"]))
            values.append((b.GeneralCounterboreDepth, spec["counterbore"]["depth"]))
            values.append((b.GeneralCounterboreHoleDiameter, spec["diameter"]))
            depth = b.GeneralCounterboreHoleDepth
        else:
            b.GeneralHoleForm = B.HoleForms.Countersink
            values.append((b.GeneralCountersinkDiameter, spec["countersink"]["diameter"]))
            values.append((b.GeneralCountersinkAngle, spec["countersink"]["angle"]))
            values.append((b.GeneralCountersinkHoleDiameter, spec["diameter"]))
            depth = b.GeneralCountersinkHoleDepth
        if not spec["through_all"]:
            values.append((depth, spec["depth"]))
    b.HoleDepthLimitOption = (
        B.HoleDepthLimitOptions.ThroughBody
        if spec["through_all"]
        else B.HoleDepthLimitOptions.Value
    )
    b.DepthOption = B.HoleDepthOptions.ToCylinderBottom
    for expression, value in values:
        expr.drive(expression, value)  # an expression value keeps the hole driven


def _read_back(tx, feature, form):
    """What NX stored, through an edit builder destroyed without committing."""
    B = tx.nx.Features.HolePackageBuilder
    with builders.build(tx, tx.part.Features.CreateHolePackageBuilder, feature) as b:
        through = b.HoleDepthLimitOption == B.HoleDepthLimitOptions.ThroughBody
        if form == "threaded":
            return {
                "form": "threaded" if b.Type == B.Types.ThreadedHole else str(b.Type),
                "diameter": b.TapDrillDiameter.Value,
                "depth": None if through else b.ThreadedHoleDepth.Value,
                "tip_angle": b.ThreadedTipAngle.Value,
                "through_all": through,
                "thread_size": str(b.ThreadSize),
            }
        names = {
            "simple": (B.HoleForms.Simple, b.GeneralSimpleHoleDiameter, b.GeneralSimpleHoleDepth),
            "counterbore": (
                B.HoleForms.Counterbored,
                b.GeneralCounterboreHoleDiameter,
                b.GeneralCounterboreHoleDepth,
            ),
            "countersink": (
                B.HoleForms.Countersink,
                b.GeneralCountersinkHoleDiameter,
                b.GeneralCountersinkHoleDepth,
            ),
        }
        found = next((n for n, v in names.items() if b.GeneralHoleForm == v[0]), "other")
        if b.Type != B.Types.GeneralHole:
            found = "other"
        _, diameter, depth = names.get(found, names["simple"])
        out = {
            "form": found,
            "diameter": diameter.Value,
            "depth": None if through else depth.Value,
            "tip_angle": b.GeneralTipAngle.Value,
            "through_all": through,
        }
        if found == "counterbore":
            out["counterbore"] = {
                "diameter": b.GeneralCounterboreDiameter.Value,
                "depth": b.GeneralCounterboreDepth.Value,
            }
        if found == "countersink":
            out["countersink"] = {
                "diameter": b.GeneralCountersinkDiameter.Value,
                "angle": b.GeneralCountersinkAngle.Value,
            }
        return out


def _verify(spec, got, centres, origins, directions, drill, removed):
    readback.expect(got["form"] == spec["form"], "form", form=got["form"])
    readback.expect(got["through_all"] == spec["through_all"], "depth", through=got["through_all"])
    for key in ("diameter", "depth", "tip_angle"):
        if spec[key] is not None:
            readback.expect_close(got[key], spec[key], key)
    for key in ("counterbore", "countersink"):
        for sub, value in (spec.get(key) or {}).items():
            readback.expect_close(got[key][sub], value, f"{key}.{sub}")
    if spec["form"] == "threaded":
        readback.expect(
            got["thread_size"] == spec["thread"]["size"], "thread", size=got["thread_size"]
        )
    readback.expect(len(origins) == len(centres), "count", origins=origins)
    tol = 1e-4 * max([1.0, *(abs(c) for p in centres for c in p)])
    for i, p in enumerate(centres):
        readback.expect(any(geom.dist(p, o) <= tol for o in origins), f"position[{i}]", at=p)
    for d in directions:
        readback.expect_same_direction(d, drill, "direction")
    readback.expect(removed > 0, "removed", removed=removed)


class HoleData(TypedDict):
    feature: result.Row
    bodies: list[result.Row]
    volume: float
    removed: float
    nominal: float | None
    diameter: Annotated[float, Doc("Drill diameter used (a threaded hole's tap drill)")]
    depth: Annotated[float | None, Doc("Blind depth used; null through_all")]
    positions: list[Point3]
    direction: Point3


# A blind hole removing less than this share of its analytic volume is not fully inside.
NOMINAL_REL = 1e-3


def _material(tx, body, origin, drill) -> float | None:
    """How deep the body reaches below the face plane along the drill (its box, so an
    upper bound); None when NX cannot box it."""
    try:
        low, high = measure.bounds(tx, [body])
    except Exception:  # a body NX cannot box: the volume check alone decides
        return None
    corners = [[x, y, z] for x in (low[0], high[0]) for y in (low[1], high[1])
               for z in (low[2], high[2])]  # fmt: skip
    return max(geom.dot(geom.sub(c, origin), drill) for c in corners)


def _breaks_through(tx, spec, removed, nominal, material) -> bool:
    """Warn when a blind hole is not fully inside the material: deeper than the body
    reaches along the drill, or removing less than its analytic volume."""
    reach = spec["depth"] + tip_height(spec["diameter"], spec["tip_angle"])
    deep = material is not None and reach > material * (1 + 1e-9)
    short = removed < nominal * (1 - NOMINAL_REL)
    if not (deep or short):
        return False
    why = (
        f"the blind depth {spec['depth']:g} (tip included {reach:.6g}) exceeds the "
        f"{material:.6g} of material below the face"
        if deep and material is not None
        else f"it removed {removed:.6g}, less than its analytic {nominal:.6g}"
    )
    tx.warn(
        "NX_UPDATE_WARNING",
        f"A blind hole breaks through or meets a cavity: {why}; nominal is null. Use depth "
        "'through_all' for a through hole",
    )
    return True


@tool(
    effect="model",
    label="Create hole",
    summary=lambda a, say: (
        f"{a.get('form', 'simple').capitalize()} hole x{len(a['positions'])} on {say('face')}"
    ),
    keywords=("hole", "drill", "counterbore", "countersink", "tapped hole", "threaded hole"),
    requires=("work_part", "no_active_sketch"),
    variants="form",
    cost="heavy",
    previewable=True,
    example={"face": "fc:7", "positions": [[10, 10, 20]], "diameter": 6, "depth": 10},
)
def nx_create_hole(
    tx: Tx,
    *,
    face: FaceRef,
    positions: Annotated[list[FacePoint], Items(1, 100), Doc("Centres on the face")],
    depth: DepthArg,
    form: Literal["simple", "counterbore", "countersink", "threaded"] = "simple",
    diameter: Annotated[Length, Doc("Drill diameter; threaded: default the tap drill")]
    | None = None,
    tip_angle: Angle = 118.0,
    counterbore: Counterbore | None = None,
    countersink: Countersink | None = None,
    thread: HoleThread | None = None,
    target: BodyRef | None = None,
) -> HoleData:
    """Create one NX Hole feature with 1-100 holes on a planar face, drilled into the material.
    Threaded: thread {size: 'M6'}, diameter default its tap drill. nominal is the analytic
    removed volume of blind holes fully inside the body (null with a warning when one
    breaks through); diameter and depth echo the drill used."""
    limit = depth_limit(depth)
    spec = check_spec(form, diameter, limit, tip_angle, counterbore, countersink, thread)
    points = check_positions(positions)
    row = tap_row(threads.thread_rows(), spec) if form == "threaded" else None
    with tx.checks():  # lookups and refusals: nothing changed yet (not_started)
        placement, body, origin, outward = _placement(tx, face, target)
        centres = _on_face(tx, placement, points, origin, outward)
        if row is not None:
            threads.check_units(tx, row, "/thread/standard")
    drill = geom.scale(outward, -1.0)
    part = tx.part
    nx = tx.nx
    before = measure.volume(tx, body)
    with builders.build(tx, part.Features.CreateHolePackageBuilder, None) as b:
        b.Tolerance = units.part_tolerance(tx, mm=0.01)
        _configure(tx, b, spec, row)
        b.BooleanOperation.Type = nx.GeometricUtilities.BooleanOperation.BooleanType.Subtract
        b.BooleanOperation.SetTargetBodies([body])
        smart = [builders.point(tx, p) for p in centres]
        section = b.HolePosition
        section.SetAllowedEntityTypes(nx.Section.AllowTypes.OnlyPoints)
        rule = part.ScRuleFactory.CreateRuleCurveDumbFromPoints(smart)
        section.AddToSection(
            [rule], smart[0], None, None, nx.Point3d(*centres[0]), nx.Section.Mode.Create, False
        )
        feature = b.commit(validate=True)
    after = measure.volume(tx, body)
    got = _read_back(tx, feature, form)
    origins = [geom.xyz(o) for o in feature.GetOrigins()]
    directions = [geom.xyz(d) for d in feature.GetDirections()]
    _verify(spec, got, centres, origins, directions, drill, before - after)
    nominal = None if spec["through_all"] else nominal_volume(spec) * len(centres)
    if nominal is not None and _breaks_through(
        tx, spec, before - after, nominal, _material(tx, body, origin, drill)
    ):
        nominal = None
    return {
        "feature": tx.ref(feature, kind="feature"),
        "bodies": common.body_rows(tx, [body]),
        "volume": after,
        "removed": before - after,
        "nominal": nominal,
        "diameter": float(spec["diameter"]),
        "depth": spec["depth"],
        "positions": origins,
        "direction": drill,
    }
