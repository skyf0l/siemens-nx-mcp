"""``nx_create_hole``: the NX Hole feature (HolePackage) on a planar face (tool-surface 3.6).

NX 2406 lessons kept here: hole centres must lie inside the face (UF point
containment, 1 = inside); points within 1e-4 x the coordinate scale of the
plane are projected onto it; setting a thread size does not refresh the tap
drill, so it is set from the table row; start/end chamfers and relief are off;
the depth is measured to the cylinder bottom (the drill tip comes on top); the
committed parameters are read back through an edit builder that is destroyed
without committing.
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
    Text,
    forbid_extra,
)
from nx_mcp.domains.modeling import common, threads
from nx_mcp.kit import builders, geom, measure, readback, result, select, units

FORMS = ("simple", "counterbore", "countersink", "threaded")


class Counterbore(TypedDict):
    diameter: Length
    depth: Length


class _CountersinkRequired(TypedDict):
    diameter: Length


class Countersink(_CountersinkRequired, total=False):
    angle: Angle


class _ThreadRequired(TypedDict):
    standard: Annotated[str, Text(64)]
    size: Annotated[str, Text(64)]


class HoleThread(_ThreadRequired, total=False):
    depth: Length


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


def check_spec(form, diameter, depth, tip_angle, counterbore, countersink, thread):
    """A normalised hole spec, or ``invalid_argument`` (no NX needed)."""
    if diameter <= 0:
        fail("NX_INVALID_ARGUMENT", "diameter must be positive", field="/diameter")
    if not 0 < tip_angle < 180:
        fail("NX_INVALID_ARGUMENT", "tip_angle must be between 0 and 180", field="/tip_angle")
    through = depth["type"] == "through_all"
    spec = {
        "form": form,
        "diameter": float(diameter),
        "tip_angle": float(tip_angle),
        "through_all": through,
        "depth": None if through else float(depth["value"]),
    }
    if spec["depth"] is not None and spec["depth"] <= 0:
        fail("NX_INVALID_ARGUMENT", "depth must be positive", field="/depth/value")
    given = {"counterbore": counterbore, "countersink": countersink, "thread": thread}
    wanted = {"counterbore": "counterbore", "countersink": "countersink", "threaded": "thread"}
    for key, value in given.items():
        if value is not None and wanted.get(form) != key:
            fail("NX_INVALID_ARGUMENT", f"{key} does not apply to a {form} hole", field="/" + key)
    if form in wanted and given[wanted[form]] is None:
        fail("NX_INVALID_ARGUMENT", f"A {form} hole needs {wanted[form]}", field="/" + wanted[form])
    if form == "counterbore":
        cb = {"diameter": float(counterbore["diameter"]), "depth": float(counterbore["depth"])}
        if cb["diameter"] <= spec["diameter"]:
            fail("NX_INVALID_ARGUMENT", "The counterbore must be wider", field="/counterbore")
        if cb["depth"] <= 0 or (spec["depth"] is not None and cb["depth"] >= spec["depth"]):
            fail("NX_INVALID_ARGUMENT", "counterbore depth must be 0..depth", field="/counterbore")
        spec["counterbore"] = cb
    elif form == "countersink":
        cs = {
            "diameter": float(countersink["diameter"]),
            "angle": float(countersink.get("angle", 90.0)),
        }
        if cs["diameter"] <= spec["diameter"]:
            fail("NX_INVALID_ARGUMENT", "The countersink must be wider", field="/countersink")
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
        spec["thread"] = {**thread, "depth": None if td is None else float(td)}
    return spec


def check_positions(positions):
    scale = max([1.0] + [abs(c) for p in positions for c in p])
    for i, a in enumerate(positions):
        for j in range(i):
            if geom.dist(a, positions[j]) <= 1e-6 * scale:
                fail(
                    "NX_INVALID_ARGUMENT",
                    f"positions {i} and {j} coincide",
                    field=f"/positions/{i}",
                )
    return [list(p) for p in positions]


def tap_row(rows, spec, part_units):
    """The table row of the thread whose tap drill equals the hole diameter."""
    t = spec["thread"]
    candidates = threads.select_rows(rows, t["standard"], t["size"], "/thread/standard")
    if (candidates[0].get("Unit") == "Metric") != (part_units == "mm"):
        fail(
            "NX_UNSUPPORTED_OPTION",
            f"{t['standard']} is {candidates[0].get('Unit')}; the work part is in {part_units}",
            field="/thread/standard",
        )
    drills = sorted({float(r["TapDrillDia"]) for r in candidates if r.get("TapDrillDia")})
    tol = 1e-6 * max(1.0, spec["diameter"])
    for r in candidates:
        if (
            r.get("TapDrillDia")
            and abs(float(r["TapDrillDia"]) - spec["diameter"]) <= tol
            and r.get("RadialEngage") != "Custom"
        ):
            return r
    fail(
        "NX_INVALID_ARGUMENT",
        f"diameter must be a tap-drill diameter of {t['size']}: {drills}",
        field="/diameter",
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


def _on_face(tx, placement, points, origin, normal):
    """Project points within tolerance onto the plane; each must lie inside the face."""
    tol = 1e-4 * max([1.0, *(abs(c) for p in [*points, origin] for c in p)])
    out = []
    for i, p in enumerate(points):
        distance = geom.dot(geom.sub(p, origin), normal)
        if abs(distance) > tol:
            fail(
                "NX_INVALID_ARGUMENT",
                f"position {i} is {distance:.6g} off the face plane",
                field=f"/positions/{i}",
                details={"tolerance": tol},
            )
        q = geom.sub(p, geom.scale(normal, distance))
        state = tx.uf.Modeling.AskPointContainment([float(c) for c in q], placement.Tag)
        if state != 1:  # 1 inside, 2 outside, 3 on the boundary
            where = "on the face boundary" if state == 3 else "outside the face"
            fail("NX_INVALID_ARGUMENT", f"position {i} is {where}", field=f"/positions/{i}")
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
        expression.RightHandSide = repr(float(value))


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
    positions: list[Point3]
    direction: Point3


@tool(
    effect="model",
    label="Create hole",
    summary=lambda a, say: f"{a['form'].capitalize()} hole x{len(a['positions'])} on {say('face')}",
    keywords=("hole", "drill", "counterbore", "countersink", "tapped hole", "threaded hole"),
    requires=("work_part", "no_active_sketch"),
    variants="form",
    cost="heavy",
    previewable=True,
    example={
        "face": "fc:7",
        "positions": [[10, 10, 20]],
        "form": "simple",
        "diameter": 6,
        "depth": {"type": "distance", "value": 10},
    },
)
def nx_create_hole(
    tx: Tx,
    *,
    face: FaceRef,
    positions: Annotated[list[Point3], Items(1, 100), Doc("Hole centres on the face")],
    form: Literal["simple", "counterbore", "countersink", "threaded"],
    diameter: Annotated[Length, Doc("Drill diameter; for threaded, a tap drill of the size")],
    depth: Annotated[LimitDistance | LimitThroughAll, Doc("To the cylinder bottom")],
    tip_angle: Angle = 118.0,
    counterbore: Counterbore | None = None,
    countersink: Countersink | None = None,
    thread: HoleThread | None = None,
    target: BodyRef | None = None,
) -> HoleData:
    """Create one NX Hole feature with 1-100 holes on a planar face, drilled into the material.
    nominal is the analytic removed volume of blind holes fully inside the body."""
    spec = check_spec(form, diameter, depth, tip_angle, counterbore, countersink, thread)
    points = check_positions(positions)
    placement, body, origin, outward = _placement(tx, face, target)
    centres = _on_face(tx, placement, points, origin, outward)
    row = None
    if form == "threaded":
        row = tap_row(threads.thread_rows(), spec, units.part_units(tx))
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
    return {
        "feature": tx.ref(feature, kind="feature"),
        "bodies": common.body_rows(tx, [body]),
        "volume": after,
        "removed": before - after,
        "nominal": nominal,
        "positions": origins,
        "direction": drill,
    }
