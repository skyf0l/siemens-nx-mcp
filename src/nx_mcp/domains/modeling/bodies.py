"""Body operations: boolean, trim/split, scale, transform, face edits (tool-surface 3.6;
mirror and pattern live in ``patterns``).

NX 2406 lessons kept here: Trim/Split take a datum
plane through ``FaceOrPlane`` and a face through a coincident ``NewPlane``
(NX does not extend a selected face); a plane that misses the body is refused
before NX (a failed native commit leaves its message on the next feature); NX
removes the material on the side the tool normal points to; ``CreateTrimBody2Builder``
rejects ``Feature.Null`` (pass None); Delete Face lacks ``AllowPartialDelete``.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import definition, tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    Direction,
    Doc,
    Items,
    Length,
    Point3,
    Pose,
    Range,
    RefTo,
)
from nx_mcp.domains.modeling import common
from nx_mcp.kit import builders, geom, measure, readback, result, select

BodyList = Annotated[list[RefTo("body")], Items(1, 50)]  # type: ignore[valid-type]


# ---------------------------------------------------------------------------
# Boolean


@tool(
    effect="model",
    label="Combine bodies",
    summary="{operation} {tools} with {target}",
    keywords=("boolean", "unite", "subtract", "intersect", "union", "merge bodies"),
    requires=("work_part", "no_active_sketch"),
    variants="operation",
    cost="heavy",
    previewable=True,
    example={"operation": "unite", "target": "b:3", "tools": ["b:5"]},
)
def nx_create_boolean(
    tx: Tx,
    *,
    operation: Literal["unite", "subtract", "intersect"],
    target: RefTo("body"),  # type: ignore[valid-type]
    tools: BodyList,
) -> common.FeatureData:
    """Unite, subtract or intersect tool bodies with a target body; the tools are consumed."""
    if target in tools:
        fail("NX_INVALID_ARGUMENT", "target cannot also be a tool", field="/tools")
    body = common.solid(tx, target, "/target")
    others = common.solids(tx, tools, "/tools")
    if int(body.Tag) in {int(t.Tag) for t in others}:
        fail("NX_INVALID_ARGUMENT", "target cannot also be a tool", field="/tools")
    before = measure.volume(tx, body)
    with builders.build(tx, tx.part.Features.CreateBooleanBuilder, None) as b:
        b.Operation = getattr(tx.nx.Features.FeatureBooleanType, builders.BOOLEAN_TYPES[operation])
        b.Target = body
        b.Tools.Add(others)
        feature = b.commit()
    out = common.result_of(tx, feature)
    common.changed(before, out["volume"], "at_least" if operation == "unite" else "at_most")
    return out


# ---------------------------------------------------------------------------
# Trim / split


def plane_range(low, high, origin, normal):
    d = [
        geom.dot(geom.sub([x, y, z], origin), normal)
        for x in (low[0], high[0])
        for y in (low[1], high[1])
        for z in (low[2], high[2])
    ]
    return min(d), max(d)


def _tool_plane(tx, ref, field="/tool"):
    """(object, origin, unit normal, is_datum) of a datum plane or planar face."""
    obj, origin, normal = select.planar(tx, ref, field)
    return obj, origin, normal, hasattr(obj, "Normal")


def _set_tool(tx, builder, obj, is_datum, origin, normal):
    tool_builder = builder.BooleanTool
    types = tx.nx.GeometricUtilities.BooleanToolBuilder.BooleanToolType
    part = tx.part
    if is_datum:
        tool_builder.ToolOption = types.FaceOrPlane
        rule = part.ScRuleFactory.CreateRuleFaceDatum([obj])
        tool_builder.FacePlaneTool.ToolFaces.FaceCollector.ReplaceRules([rule], False)
    else:
        tool_builder.ToolOption = types.NewPlane
        plane = part.Planes.CreatePlane(
            tx.nx.Point3d(*origin),
            tx.nx.Vector3d(*normal),
            tx.nx.SmartObject.UpdateOption.WithinModeling,
        )
        plane.SetMethod(tx.nx.PlaneTypes.MethodType.Coincident)
        plane.SetGeometry([obj])
        plane.Evaluate()
        tool_builder.FacePlaneTool.ToolPlane = plane
    return tool_builder


class TrimData(TypedDict):
    feature: result.Row
    bodies: list[result.Row]
    volume: float
    volumes: list[float]


@tool(
    effect="model",
    label="Trim or split body",
    summary="Trim {body} by {tool}, keep {keep}",
    keywords=("trim body", "split body", "cut with plane", "slice", "section cut"),
    requires=("work_part", "no_active_sketch"),
    variants="keep",
    cost="heavy",
    previewable=True,
    example={"body": "b:3", "tool": "d:7", "keep": "back"},
)
def nx_create_trim_body(
    tx: Tx,
    *,
    body: RefTo("body"),  # type: ignore[valid-type]
    tool: RefTo("datum_plane", "face"),  # type: ignore[valid-type]
    keep: Annotated[
        Literal["back", "front", "both"], Doc("back: against the tool normal; both: split")
    ],
) -> TrimData:
    """Cut a solid body with a datum plane or planar face used as an infinite plane. Keep the
    side behind or in front of the tool normal (Trim Body), or both pieces (Split Body)."""
    target = common.solid(tx, body)
    obj, origin, normal, is_datum = _tool_plane(tx, tool)
    if not is_datum and int(obj.GetBody().Tag) == int(target.Tag):
        fail("NX_INVALID_ARGUMENT", "The tool face must not belong to the body", field="/tool")
    low, high = measure.bounds(tx, target)
    lo, hi = plane_range(low, high, origin, normal)
    if hi <= geom.tol(low, high) or lo >= -geom.tol(low, high):
        fail(
            "NX_INVALID_ARGUMENT",
            f"The tool plane does not cut the body (signed distances {lo:g}..{hi:g})",
            field="/tool",
            details={"plane_origin": origin, "plane_normal": normal},
        )
    volume = measure.volume(tx, target)
    part = tx.part
    if keep == "both":
        with builders.build(tx, part.Features.CreateSplitBodyBuilder, None) as b:
            b.TargetBodyCollector = builders.collector(tx, [target], "Body")
            _set_tool(tx, b, obj, is_datum, origin, normal)
            feature = b.commit()
        bodies = common.feature_bodies(feature)
        volumes = [measure.volume(tx, x) for x in bodies]
        readback.expect(
            len(bodies) >= 2 and geom.close(sum(volumes), volume, 1e-5),
            "split",
            "The pieces do not add up to the body",
            volumes=volumes,
            original=volume,
        )
    else:
        with builders.build(tx, part.Features.CreateTrimBody2Builder, None) as b:
            b.TargetBodyCollector = builders.collector(tx, [target], "Body")
            _set_tool(tx, b, obj, is_datum, origin, normal).ReverseDirection = keep == "front"
            feature = b.commit()
        bodies = common.feature_bodies(feature) or [target]
        masses = [measure.mass(tx, [x]) for x in bodies]
        volumes = [m["volume"] for m in masses]
        readback.expect(0 < sum(volumes) < volume * (1 - 1e-9), "trim", volume=sum(volumes))
        sides = [geom.dot(geom.sub(m["centroid"], origin), normal) for m in masses]
        readback.expect(
            all((s > 0) == (keep == "front") for s in sides),
            "kept_side",
            "NX kept the other side of the tool",
            centroid_offsets=sides,
        )
    out = common.result_of(tx, feature, bodies)
    return {**out, "volumes": volumes}


# ---------------------------------------------------------------------------
# Scale and transform


@tool(
    effect="model",
    label="Scale bodies",
    summary=lambda a, say: f"Scale {common.bodies_text(a)} x{say('factor')}",
    keywords=("scale", "resize", "uniform scale", "shrink", "enlarge"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"bodies": ["b:3"], "factor": 2},
)
def nx_create_scale(
    tx: Tx,
    *,
    bodies: BodyList,
    factor: Annotated[float, Range(gt=0, le=1e6), Doc("Uniform factor, not 1")],
    origin: Annotated[Point3, Doc("Fixed point; default the absolute origin")] | None = None,
) -> common.FeatureData:
    """Scale solid bodies uniformly about a point: p -> origin + factor * (p - origin)."""
    if abs(factor - 1.0) < 1e-12:
        fail("NX_INVALID_ARGUMENT", "factor 1 would not change the bodies", field="/factor")
    o = list(origin) if origin is not None else [0.0, 0.0, 0.0]
    targets = common.solids(tx, bodies)
    before = [measure.mass(tx, [t]) for t in targets]
    with builders.build(tx, tx.part.Features.CreateScaleBuilder, None) as b:
        b.Type = tx.nx.Features.ScaleBuilder.Types.Uniform
        b.BodyToScale.Add(targets)
        b.Point = builders.point(tx, o)
        b.UniformFactor.RightHandSide = repr(float(factor))
        feature = b.commit()
    for i, (t, m) in enumerate(zip(targets, before, strict=True)):
        got = measure.mass(tx, [t])
        readback.expect_close(
            got["volume"],
            m["volume"] * factor**3,
            f"volume[{i}]",
            tol=1e-6 * max(1.0, m["volume"] * factor**3),
        )
        if m["centroid"] and got["centroid"]:
            wanted = geom.add(o, geom.scale(geom.sub(m["centroid"], o), factor))
            readback.expect_close(
                got["centroid"], wanted, f"centroid[{i}]", tol=1e-5 * max(1.0, *map(abs, wanted))
            )
    return common.result_of(tx, feature, targets)


def _move(tx, existing, objects, translation, rotation):
    """Commit a MoveObject feature (CSYS to CSYS); returns the feature."""
    part = tx.part
    nx = tx.nx
    before = {int(f.Tag) for f in part.Features}
    with builders.build(tx, part.BaseFeatures.CreateMoveObjectBuilder, existing) as b:
        b.Associative = True
        b.MoveParents = False
        if existing is None:
            b.ObjectToMoveObject.Add(objects)
            b.MoveObjectResult = b.MoveObjectResultOptions.MoveOriginal
        motion = b.TransformMotion
        motion.Option = motion.Options.CsysToCsys
        motion.FromCsys = part.CoordinateSystems.CreateCoordinateSystem(
            nx.Point3d(0.0, 0.0, 0.0), builders.matrix(tx, geom.IDENTITY), False
        )
        motion.ToCsys = part.CoordinateSystems.CreateCoordinateSystem(
            nx.Point3d(*translation), builders.matrix(tx, rotation), False
        )
        made = b.commit()
    if made is None:
        created = [f for f in part.Features if int(f.Tag) not in before]
        made = existing or (created[0] if len(created) == 1 else None)
    readback.expect(made is not None, "feature", "NX built no Move Object feature")
    readback.expect(
        "MOVE" in str(getattr(made, "FeatureType", "")).upper(),
        "feature",
        "NX did not build an editable Move Object feature",
        type=str(getattr(made, "FeatureType", "")),
    )
    return made


def _pose(transform):
    translation = [float(v) for v in transform["translation"]]
    rotation = geom.rotation(transform.get("rotation", geom.IDENTITY), "/transform/rotation")
    return translation, rotation


@tool(
    effect="model",
    label="Move or copy bodies",
    summary=lambda a, say: ("Copy " if a.get("copy") else "Move ") + common.bodies_text(a),
    keywords=("move body", "copy body", "transform", "rotate body", "translate", "move object"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"bodies": ["b:3"], "transform": {"translation": [20, 0, 0]}},
)
def nx_create_transform(
    tx: Tx,
    *,
    bodies: BodyList,
    transform: Pose,
    copy: Annotated[bool, Doc("Move an associative copy")] = False,
) -> common.FeatureData:
    """Move or copy solid bodies by a rigid transform; edit it later with nx_edit_feature
    definition."""
    translation, rotation = _pose(transform)
    targets = common.solids(tx, bodies)
    before = [measure.mass(tx, [t]) for t in targets]
    moving = targets
    if copy:
        with builders.build(tx, tx.part.Features.CreateExtractFaceBuilder, None) as b:
            b.Type = b.ExtractType.Body
            b.Associative = True
            b.HideOriginal = False
            b.InheritDisplayProperties = True
            rules = builders.collector(tx, targets, "Body").GetRules()
            b.ExtractBodyCollector.ReplaceRules(rules, False)
            copied = b.commit()
        moving = common.feature_bodies(copied)
        readback.expect(len(moving) == len(targets), "copy", "NX copied a different body count")
    feature = _move(tx, None, moving, translation, rotation)
    for i, (body, m) in enumerate(zip(moving, before, strict=True)):
        got = measure.mass(tx, [body])
        readback.expect_close(
            got["volume"], m["volume"], f"volume[{i}]", tol=1e-6 * max(1.0, m["volume"])
        )
        if m["centroid"] and got["centroid"]:
            wanted = geom.add(geom.matvec(rotation, m["centroid"]), translation)
            readback.expect_close(
                got["centroid"], wanted, f"centroid[{i}]", tol=1e-5 * max(1.0, *map(abs, wanted))
            )
    return common.result_of(tx, feature, moving)


@definition("MOVE_OBJECT")
def edit_transform(tx, feature, value):
    """``nx_edit_feature(definition={"transform": Pose})`` of a Move Object feature."""
    if "transform" not in value:
        fail("NX_INVALID_ARGUMENT", "A Move Object takes {transform}", field="/definition")
    translation, rotation = _pose(value["transform"])
    _move(tx, feature, [], translation, rotation)


# ---------------------------------------------------------------------------
# Synchronous face edits


@tool(
    effect="model",
    label="Edit faces",
    summary="{action} {faces}",
    keywords=(
        "move face",
        "offset face",
        "replace face",
        "delete face",
        "synchronous",
        "push pull",
    ),
    requires=("work_part", "no_active_sketch"),
    variants="action",
    cost="heavy",
    previewable=True,
    example={"faces": ["fc:9"], "action": "offset", "distance": 2},
)
def nx_edit_faces(
    tx: Tx,
    *,
    faces: Annotated[list[RefTo("face")], Items(1, 200)],  # type: ignore[valid-type]
    action: Literal["move", "offset", "replace", "delete"],
    distance: Annotated[Length, Doc("move: along direction; offset: outward if > 0")] | None = None,
    direction: Direction | None = None,
    replacement: RefTo("face") | None = None,  # type: ignore[valid-type]
) -> common.FeatureData:
    """Edit faces of any solid, imported ones included (synchronous modeling). Move them along
    a direction, offset them, replace them by another face, or delete and heal."""
    needs = {
        "move": {"distance", "direction"},
        "offset": {"distance"},
        "replace": {"replacement"},
        "delete": set(),
    }[action]
    given = {"distance": distance, "direction": direction, "replacement": replacement}
    for name, value in given.items():
        if (value is not None) != (name in needs):
            verb = "needs" if name in needs else "does not take"
            fail("NX_INVALID_ARGUMENT", f"{action} {verb} {name}", field="/" + name)
    selected = select.faces(tx, faces, "/faces")
    body = common.one_body(selected, "/faces")
    vector = select.direction(tx, direction, "/direction") if direction is not None else None
    other = select.one(tx, replacement, "face", field="/replacement") if replacement else None
    before = measure.volume(tx, body)
    part = tx.part
    factory = {
        "move": part.Features.CreateAdmMoveFaceBuilder,
        "offset": part.Features.CreateOffsetFaceBuilder,
        "replace": part.Features.CreateReplaceFaceBuilder,
        "delete": part.Features.CreateDeleteFaceBuilder,
    }[action]
    with builders.build(tx, factory, None) as b:
        rule = part.ScRuleFactory.CreateRuleFaceDumb(selected)
        if action == "move":
            b.FaceToMove.FaceCollector.ReplaceRules([rule], False)
            b.Motion.Option = tx.nx.GeometricUtilities.ModlMotion.Options.Distance
            b.Motion.DistanceVector = builders.direction(tx, vector, field="/direction")
            b.Motion.DistanceValue.RightHandSide = repr(float(distance))
        elif action == "offset":
            b.FaceCollector.ReplaceRules([rule], False)
            b.Distance.RightHandSide = repr(abs(float(distance)))
            b.Direction = distance < 0
        elif action == "replace":
            b.ReplaceFaces.ReplaceRules([rule], False)
            b.ReplacementFaces.ReplaceRules([part.ScRuleFactory.CreateRuleFaceDumb([other])], False)
        else:
            b.Type = tx.nx.Features.DeleteFaceBuilder.SelectTypes.Face
            b.FaceCollector.ReplaceRules([rule], False)
            b.Heal = True
            b.option("AllowPartialDelete", False)  # absent on NX 2406
        feature = b.commit(validate=True)
    check = getattr(tx.uf.Modeling, "AskBodyConsistency", None)
    if check is not None:
        faults = check(body.Tag)[0]
        readback.expect(
            not faults, "consistency", "The face edit left an invalid body", faults=faults
        )
    out = common.result_of(tx, feature, [body])
    common.changed(before, out["volume"], "differs")
    return out
