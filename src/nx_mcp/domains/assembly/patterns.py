"""Component patterns: associative NX patterns or independent copies (tool-surface 3.8).

NX 2406 findings encoded here:

- Counts include the seed; a circular pattern must not wrap onto the seed. A circular
  ``span`` becomes the step NX stores: span / (count - 1), or 360 / count for 360 (so a
  later count edit keeps the step, not the span).
- An edit can make NX recreate the pattern (new object, old one deleted), for
  example when the second direction is switched off: the object ``Commit`` returns
  is read back, and ``replaced`` says the old reference is stale.
- Every instance pose is read back against the layout (seed pose plus the step).
"""

from __future__ import annotations

import contextlib
from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    Angle,
    Axis,
    ComponentRef,
    Direction,
    Doc,
    Length,
    Range,
    RefTo,
    forbid_extra,
)
from nx_mcp.domains.assembly import common, structure
from nx_mcp.domains.assembly.common import MAX_INSTANCES, ComponentRow
from nx_mcp.kit import builders, geom, model, readback, result, select
from nx_mcp.kit import pose as kpose

Count = Annotated[int, Range(ge=2, le=MAX_INSTANCES), Doc("Instances including the seed")]
Count2 = Annotated[int, Range(ge=1, le=MAX_INSTANCES), Doc("Rows in the second direction")]
Spacing = Annotated[Length, Doc("Distance between instances")]
Pitch = Annotated[Angle, Doc("Angle between instances")]


class _LinearRequired(TypedDict):
    direction: Direction
    count: Count
    spacing: Spacing


class LinearLayout(_LinearRequired, total=False):
    direction2: Direction
    count2: Count2
    spacing2: Spacing


class _CircularRequired(TypedDict):
    axis: Axis
    count: Count
    angle: Annotated[Angle, Doc("Step, or the span if span")]


class CircularLayout(_CircularRequired, total=False):
    span: bool


class Linear(TypedDict):
    linear: LinearLayout


class Circular(TypedDict):
    circular: CircularLayout


forbid_extra(LinearLayout, CircularLayout, Linear, Circular)


class PatternData(TypedDict, total=False):
    pattern: result.Row
    replaced: bool
    associative: bool
    layout: str
    count: int
    spacing: float
    count2: int
    spacing2: float
    angle: float
    components: list[ComponentRow]


# ---------------------------------------------------------------------------
# Layout math


def _check_pitch(count, angle, field):
    if angle <= 0 or (count - 1) * angle >= 360.0 - 1e-9:
        fail("NX_INVALID_ARGUMENT", "Use a positive angle with (count - 1) * angle < 360",
             field=field)  # fmt: skip


def expected(seed, layout):
    """Instance poses of a layout from the seed pose (seed first)."""
    t0, r0 = seed
    if layout["kind"] == "linear":
        out = []
        for j in range(layout.get("count2") or 1):
            for i in range(layout["count"]):
                step = geom.scale(layout["direction"], i * layout["spacing"])
                if j:
                    step = geom.add(step, geom.scale(layout["direction2"], j * layout["spacing2"]))
                out.append((geom.add(t0, step), r0))
        return out
    return [
        kpose.rotate_about(seed, layout["origin"], layout["axis"], i * layout["angle"])
        for i in range(layout["count"])
    ]


def match(found, wanted, check="instances"):
    """Every wanted pose has its own found instance (any order); counts are equal."""
    readback.expect(
        len(found) == len(wanted), f"{check}.count", "NX made a different number of instances",
        count=len(found), wanted=len(wanted),
    )  # fmt: skip
    left = list(found)
    for i, pose in enumerate(wanted):
        tol = 1e-6 * max(1.0, *(abs(v) for v in pose[0]))
        hit = next((k for k, p in enumerate(left) if kpose.close(p, pose, tol)), None)
        readback.expect(
            hit is not None, f"{check}[{i}]", "No instance at an expected pose",
            wanted=pose[0],
        )  # fmt: skip
        left.pop(hit)


# ---------------------------------------------------------------------------
# Reading a native pattern


def _enum(tx):
    return tx.nx.GeometricUtilities.PatternDefinition.PatternEnum


def members(pattern) -> list:
    """Seed and instance occurrences of a native pattern, seed first, no repeats."""
    found = {int(c.Tag): c for c in pattern.GetComponentsToPattern()}
    for member in pattern.GetAllPatternMembers():
        for c in member.GetAllComponents():
            found.setdefault(int(c.Tag), c)
    return list(found.values())


def describe(tx, pattern) -> dict:
    """Layout parameters of a native pattern (read through its builder)."""
    out = {"pattern": tx.ref(pattern, "component_pattern")}
    with builders.build(tx, tx.part.ComponentAssembly.CreateComponentPatternBuilder, pattern) as b:
        service = b.PatternService
        out["associative"] = bool(b.Associative)
        kinds = _enum(tx)
        if service.PatternType == kinds.Linear:
            d = service.RectangularDefinition
            out.update(
                layout="linear",
                count=int(round(d.XSpacing.NCopies.Value)),
                spacing=float(d.XSpacing.PitchDistance.Value),
            )
            with contextlib.suppress(Exception):
                out["_direction"] = geom.unit(geom.xyz(d.XDirection.Vector))
            if d.UseYDirectionToggle:
                out.update(
                    count2=int(round(d.YSpacing.NCopies.Value)),
                    spacing2=float(d.YSpacing.PitchDistance.Value),
                )
                with contextlib.suppress(Exception):
                    out["_direction2"] = geom.unit(geom.xyz(d.YDirection.Vector))
        elif service.PatternType == kinds.Circular:
            d = service.CircularDefinition
            out.update(
                layout="circular",
                count=int(round(d.AngularSpacing.NCopies.Value)),
                angle=float(d.AngularSpacing.PitchAngle.Value),
            )
        else:
            out["layout"] = result.enum_name(service.PatternType, kinds).lower()
    return out


def _total(info) -> int:
    return int(info.get("count", 0)) * int(info.get("count2") or 1)


def _component_rows(tx, comps) -> list:
    names = structure.paths(tx.part)
    return [common.component_row(tx, c, names.get(int(c.Tag))) for c in comps]


def _public(info) -> dict:
    return {k: v for k, v in info.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Tools


def _summary(a, say):
    layout = a.get("layout") or {}
    kind = "linear" if "linear" in layout else "circular"
    count = (layout.get(kind) or {}).get("count")
    return f"Pattern {say('component')} {kind} x{count}"


def _layout(tx, layout):
    """A normalised layout: kind, unit directions or axis, counts and steps."""
    if "linear" in layout:
        lin = layout["linear"]
        second = [lin.get(k) is not None for k in ("direction2", "count2", "spacing2")]
        if any(second) and not all(second):
            fail("NX_INVALID_ARGUMENT", "A second direction needs direction2, count2 and spacing2",
                 field="/layout/linear")  # fmt: skip
        if lin["spacing"] <= 0 or (all(second) and lin["spacing2"] <= 0):
            fail("NX_INVALID_ARGUMENT", "Spacing must be positive", field="/layout/linear")
        out = {
            "kind": "linear",
            "count": int(lin["count"]),
            "spacing": float(lin["spacing"]),
            "direction": select.direction(tx, lin["direction"], "/layout/linear/direction"),
        }
        if all(second):
            other = select.direction(tx, lin["direction2"], "/layout/linear/direction2")
            if geom.parallel(out["direction"], other, 1e-6):
                fail("NX_INVALID_ARGUMENT", "The pattern directions must not be parallel",
                     field="/layout/linear/direction2")  # fmt: skip
            out.update(count2=int(lin["count2"]), spacing2=float(lin["spacing2"]), direction2=other)
        if out["count"] * (out.get("count2") or 1) > MAX_INSTANCES:
            fail("NX_TOO_MANY_OBJECTS", f"At most {MAX_INSTANCES} instances", field="/layout")
        return out
    circ = layout["circular"]
    step = float(circ["angle"])
    if circ.get("span"):
        if not 0 < step <= 360.0 + 1e-9:
            fail("NX_INVALID_ARGUMENT", "A span is above 0 and at most 360 degrees",
                 field="/layout/circular/angle")  # fmt: skip
        # 360 spreads the instances over the full circle; else first to last instance.
        full = abs(step - 360.0) <= 1e-9
        step = 360.0 / circ["count"] if full else step / (circ["count"] - 1)
    _check_pitch(circ["count"], step, "/layout/circular/angle")
    _obj, origin, axis = select.axis(tx, circ["axis"], "/layout/circular/axis")
    return {
        "kind": "circular",
        "count": int(circ["count"]),
        "angle": step,
        "origin": origin,
        "axis": axis,
    }


def _native(tx, seed, lay):
    part = tx.part
    update = tx.nx.SmartObject.UpdateOption.WithinModeling
    kinds = _enum(tx)
    with builders.build(tx, part.ComponentAssembly.CreateComponentPatternBuilder, None) as b:
        b.Associative = True
        b.ComponentPatternSet.Add(seed)
        service = b.PatternService
        if lay["kind"] == "linear":
            service.PatternType = kinds.Linear
            d = service.RectangularDefinition
            d.XDirection = builders.direction(
                tx, lay["direction"], field="/layout/linear/direction"
            )
            d.XSpacing.NCopies.RightHandSide = str(lay["count"])
            d.XSpacing.PitchDistance.RightHandSide = repr(lay["spacing"])
            d.UseYDirectionToggle = bool(lay.get("count2"))
            d.YSpacing.NCopies.RightHandSide = str(lay.get("count2") or 1)
            if lay.get("count2"):
                d.YDirection = builders.direction(tx, lay["direction2"], field="/layout/linear")
                d.YSpacing.PitchDistance.RightHandSide = repr(lay["spacing2"])
        else:
            service.PatternType = kinds.Circular
            d = service.CircularDefinition
            point = builders.point(tx, lay["origin"])
            smart = builders.direction(tx, lay["axis"], lay["origin"], "/layout/circular/axis")
            d.RotationAxis = part.Axes.CreateAxis(point, smart, update)
            d.AngularSpacing.NCopies.RightHandSide = str(lay["count"])
            d.AngularSpacing.PitchAngle.RightHandSide = repr(lay["angle"])
            d.RadialSpacing.NCopies.RightHandSide = "1"
        pattern = b.Commit()
    readback.expect(pattern is not None, "pattern", "NX created no pattern")
    model.update(tx)
    return pattern


def _copies(tx, seed, poses):
    part = tx.part
    path = seed.Prototype.FullPath
    reference_set = str(getattr(seed, "ReferenceSet", "") or "Entire Part")
    out = [seed]
    for i, (t, r) in enumerate(poses[1:], start=2):
        tx.checkpoint(done=i - 1, total=len(poses) - 1)
        with native(default="NX_BUILD_FAILED"):
            c, status = part.ComponentAssembly.AddComponent(
                path, reference_set, f"{seed.Name}_{i}", tx.nx.Point3d(*t),
                builders.matrix(tx, r), -1,
            )  # fmt: skip
        if status is not None and hasattr(status, "Dispose"):
            status.Dispose()
        out.append(c)
    return out


@tool(
    effect="model",
    label="Pattern component",
    summary=_summary,
    keywords=("component array", "component pattern", "repeat component", "assembly pattern"),
    requires=("work_part", "no_active_sketch", "assembly"),
    cost="heavy",
    previewable=True,
    invalidates="deleted",
    example={
        "component": "o:4",
        "layout": {"linear": {"direction": [1, 0, 0], "count": 4, "spacing": 20}},
    },
)
def nx_create_component_pattern(
    tx: Tx,
    *,
    component: ComponentRef,
    layout: Linear | Circular,
    associative: Annotated[
        bool, Doc("NX pattern (true) or plain copies, no pattern object")
    ] = True,
) -> PatternData:
    """Pattern a direct component linearly (one or two directions) or about an axis. Counts
    include the seed (at most 100). Circular angle is the step, or with span=true first to last
    (360: full circle); data.angle is the step. associative=false: plain copies, no pattern
    object."""
    lay = _layout(tx, layout)
    with tx.checks():
        seed = common.component(tx, component, "/component", unsuppressed=True)
    wanted = expected(structure.pose(seed), lay)
    if associative:
        pattern = _native(tx, seed, lay)
        info = describe(tx, pattern)
        comps = members(pattern)
        readback.expect(info["associative"], "associative", "NX made the pattern non-associative")
        readback.expect(_total(info) == len(wanted), "count", actual=_total(info))
    else:
        comps = _copies(tx, seed, wanted)
        info = {"associative": False, "layout": lay["kind"], "count": lay["count"]}
        for key in ("spacing", "count2", "spacing2", "angle"):
            if key in lay:
                info[key] = lay[key]
    match([structure.pose(c) for c in comps], wanted)
    return {**_public(info), "components": _component_rows(tx, comps)}


def _edit_summary(a, say):
    keys = ("count", "spacing", "count2", "spacing2", "angle")
    return f"Edit pattern {say('pattern')}: " + ", ".join(k for k in keys if a.get(k) is not None)


@tool(
    effect="model",
    label="Edit component pattern",
    summary=_edit_summary,
    keywords=("change array count", "pattern spacing", "pattern pitch", "edit array"),
    requires=("work_part", "no_active_sketch", "assembly"),
    cost="heavy",
    invalidates="deleted",
    example={"pattern": "cp:9", "count": 5},
)
def nx_edit_component_pattern(
    tx: Tx,
    *,
    pattern: Annotated[str, RefTo("component_pattern")],
    count: Count | None = None,
    spacing: Spacing | None = None,
    count2: Count2 | None = None,
    spacing2: Spacing | None = None,
    angle: Pitch | None = None,
) -> PatternData:
    """Change the count, spacing or angle of an associative single-seed component pattern.
    A lower count deletes the surplus components and their constraints. NX may recreate the
    pattern: then replaced is true and the old reference is stale."""
    given = {"count": count, "spacing": spacing, "count2": count2, "spacing2": spacing2,
             "angle": angle}  # fmt: skip
    if all(v is None for v in given.values()):
        fail("NX_INVALID_ARGUMENT", "Give count, spacing, count2, spacing2 or angle")
    for name, length in (("spacing", spacing), ("spacing2", spacing2)):
        if length is not None and float(length) <= 0:  # the kernel converted it to part units
            fail("NX_INVALID_ARGUMENT", f"{name} must be positive", field="/" + name)
    kinds = _enum(tx)
    with tx.checks():
        obj = select.resolve(tx, pattern, "component_pattern", field="/pattern")
        before = describe(tx, obj)
        seeds = list(obj.GetComponentsToPattern())
        if not before["associative"] or len(seeds) != 1:
            fail("NX_UNSUPPORTED_OPTION", "Only associative single-seed patterns can be edited",
                 field="/pattern")  # fmt: skip
        if before["layout"] == "linear":
            if angle is not None:
                fail("NX_INVALID_ARGUMENT", "angle needs a circular pattern", field="/angle")
            n2 = count2 if count2 is not None else before.get("count2") or 1
            if n2 > 1 and "count2" not in before:
                fail("NX_UNSUPPORTED_OPTION",
                     "This pattern has one direction; NX cannot add a second one by editing",
                     field="/count2", hint="Delete it and recreate it with nx_create_component_"
                     "pattern, layout.linear direction2, count2 and spacing2")  # fmt: skip
            if spacing2 is not None and n2 == 1:
                fail("NX_INVALID_ARGUMENT", "spacing2 needs a second direction", field="/spacing2")
            total = (count if count is not None else before["count"]) * n2
        elif before["layout"] == "circular":
            for name in ("spacing", "count2", "spacing2"):
                if given[name] is not None:
                    fail("NX_INVALID_ARGUMENT", f"A circular pattern takes count and angle, not "
                         f"{name}", field="/" + name)  # fmt: skip
            total = count if count is not None else before["count"]
            _check_pitch(total, angle if angle is not None else before["angle"], "/angle")
        else:
            fail("NX_UNSUPPORTED_OPTION", "Only linear and circular patterns can be edited",
                 field="/pattern")  # fmt: skip
        if total > MAX_INSTANCES:
            fail("NX_TOO_MANY_OBJECTS", f"At most {MAX_INSTANCES} instances")
    seed = seeds[0]
    with builders.build(tx, tx.part.ComponentAssembly.CreateComponentPatternBuilder, obj) as b:
        service = b.PatternService
        if service.PatternType == kinds.Linear:
            d = service.RectangularDefinition
            if count is not None:
                d.XSpacing.NCopies.RightHandSide = str(count)
            if spacing is not None:
                d.XSpacing.PitchDistance.RightHandSide = repr(float(spacing))
            if count2 is not None:
                d.YSpacing.NCopies.RightHandSide = str(count2)
                d.UseYDirectionToggle = count2 > 1
            if spacing2 is not None:
                d.YSpacing.PitchDistance.RightHandSide = repr(float(spacing2))
        else:
            d = service.CircularDefinition
            if count is not None:
                d.AngularSpacing.NCopies.RightHandSide = str(count)
            if angle is not None:
                d.AngularSpacing.PitchAngle.RightHandSide = repr(float(angle))
        committed = b.Commit()
    replaced = committed is not None and int(committed.Tag) != int(obj.Tag)
    if committed is not None:
        obj = committed
    model.update(tx)
    info = describe(tx, obj)
    readback.expect(_total(info) == total, "count", actual=_total(info), wanted=total)
    for name, value in given.items():
        if value is not None and not (name == "count2" and value == 1):
            readback.expect_close(info.get(name, float("nan")), value, name)
    comps = members(obj)
    if info["layout"] == "linear" and "_direction" in info:
        lay = {"kind": "linear", "count": info["count"], "spacing": info["spacing"],
               "direction": info["_direction"]}  # fmt: skip
        if info.get("count2", 1) > 1 and "_direction2" in info:
            lay.update(count2=info["count2"], spacing2=info["spacing2"],
                       direction2=info["_direction2"])  # fmt: skip
        match([structure.pose(c) for c in comps], expected(structure.pose(seed), lay))
    else:
        readback.expect(len(comps) == total, "instances.count", count=len(comps))
    if replaced:
        tx.warn("NX_UPDATE_WARNING", "NX recreated the pattern; use the returned pattern reference")
    return {**_public(info), "replaced": replaced, "components": _component_rows(tx, comps)}
