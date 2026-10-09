"""Assembly constraints: create and edit, solved by the NX positioner (tool-surface 3.8).

NX 2406 findings encoded here:

- A constraint counts only when the solver reports ``Solved``; anything else rolls
  the change back as ``NX_CONSTRAINT_UNSATISFIED`` with the solver status.
- Concentric needs circular edges (cylindrical faces leave it unsolved), and the
  solver may move either component unless one is fixed: ``moved`` lists every
  direct component whose pose changed.
- Align is a Touch constraint with co-alignment (the legacy mate "align").
"""

from __future__ import annotations

import contextlib
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Range, RefTo
from nx_mcp.domains.assembly import common
from nx_mcp.domains.assembly.common import ComponentRef, ComponentRow
from nx_mcp.kit import assembly, model, readback, result, select
from nx_mcp.kit import pose as kpose

TYPES = {
    "fix": "Fix",
    "touch": "Touch",
    "align": "Touch",
    "distance": "Distance",
    "parallel": "Parallel",
    "perpendicular": "Perpendicular",
    "angle": "Angle",
    "concentric": "Concentric",
}
ALIGNMENTS = {"infer": "InferAlign", "same": "CoAlign", "opposite": "ContraAlign"}
VALUED = ("distance", "angle")
Geometry = RefTo("face", "edge")
Alignment = Literal["infer", "same", "opposite"]
Value = Annotated[float, Range(ge=0, le=1e6), Doc("distance in part units, or angle in degrees")]


class ConstraintReference(TypedDict, total=False):
    component: result.Row
    geometry: result.Row


class ConstraintData(TypedDict, total=False):
    constraint: result.Row
    type: str
    alignment: str
    status: str
    suppressed: bool
    value: float
    references: list[ConstraintReference]
    moved: list[ComponentRow]


def _cls(tx):
    return tx.nx.Positioning.Constraint


@contextlib.contextmanager
def network(tx):
    """An established positioner network; always cleared and ended."""
    positioner = tx.part.ComponentAssembly.Positioner
    with native(default="NX_BUILD_FAILED"):
        positioner.BeginAssemblyConstraints()
    failed = False
    try:
        with native(default="NX_BUILD_FAILED"):
            net = positioner.EstablishNetwork()
            net.MoveObjectsState = True
        yield positioner, net
    except BaseException:
        failed = True
        raise
    finally:
        try:
            positioner.ClearNetwork()
        except Exception:
            if not failed:
                raise
        finally:
            try:
                positioner.EndAssemblyConstraints()
            except Exception:
                if not failed:
                    raise


def solve(tx, net):
    with native(codes=None, default="NX_CONSTRAINT_UNSATISFIED"):
        net.Solve()
        net.ApplyToModel()
    model.update(tx)


def all_constraints(part) -> list:
    positioner = getattr(getattr(part, "ComponentAssembly", None), "Positioner", None)
    return list(positioner.Constraints) if positioner is not None else []


def kind_name(tx, constraint) -> str:
    cls = _cls(tx)
    native_type = result.enum_name(constraint.ConstraintType, cls.Type)
    if native_type == "Touch":
        align = result.enum_name(constraint.ConstraintAlignment, cls.Alignment)
        return "align" if align == "CoAlign" else "touch"
    return native_type.lower()


def status_name(tx, constraint) -> str:
    cls = _cls(tx)
    return result.enum_name(constraint.GetConstraintStatus(), cls.SolverStatus)


def solved(tx, constraint) -> bool:
    return constraint.GetConstraintStatus() == _cls(tx).SolverStatus.Solved


def record(tx, constraint) -> dict:
    """The result row of one constraint: type, alignment, solver status, value, references."""
    cls = _cls(tx)
    alignment = result.enum_name(constraint.ConstraintAlignment, cls.Alignment)
    out = {
        "constraint": tx.ref(constraint, "assembly_constraint"),
        "type": kind_name(tx, constraint),
        "alignment": {v: k for k, v in ALIGNMENTS.items()}.get(alignment, alignment.lower()),
        "status": status_name(tx, constraint),
        "suppressed": bool(constraint.Suppressed),
        "references": [],
    }
    if constraint.ConstraintType in (cls.Type.Distance, cls.Type.Angle):
        with contextlib.suppress(Exception):
            out["value"] = float(constraint.Expression.Value)
    for ref in constraint.GetReferences():
        item = {}
        obj, geometry = ref.GetMovableObject(), ref.GetGeometry()
        with contextlib.suppress(Exception):
            if tx.kind_of(obj) == "component":
                item["component"] = tx.ref(obj, "component")
        with contextlib.suppress(Exception):
            kind = tx.kind_of(geometry)
            if kind in ("face", "edge", "component"):
                item["geometry"] = tx.ref(geometry, kind)
        out["references"].append(item)
    return out


def poses(part) -> dict:
    return {int(c.Tag): (c, assembly.pose(c)) for c in assembly.direct_children(part)}


def moved(tx, before) -> list:
    """Rows of the direct components whose pose changed since ``before``."""
    return [
        common.component_row(tx, c)
        for c, was in before.values()
        if not kpose.close(assembly.pose(c), was, 1e-9)
    ]


def _unsatisfied(tx, constraint, message):
    details = {}
    with contextlib.suppress(Exception):
        details = {"status": status_name(tx, constraint), "type": kind_name(tx, constraint)}
    fail(
        "NX_CONSTRAINT_UNSATISFIED",
        message,
        hint="Check the geometry pair and the fixed components; it was rolled back",
        details=details,
    )


def _geometry(tx, ref, owner, field, concentric):
    obj = select.resolve(tx, ref, "face", "edge", field=field)
    if not getattr(obj, "IsOccurrence", False) or not assembly.same(
        getattr(obj, "OwningComponent", None), owner
    ):
        fail(
            "NX_WRONG_OWNER",
            "Select a face or edge of that component (occurrence geometry)",
            field=field,
        )
    if concentric and (
        tx.kind_of(obj) != "edge" or obj.SolidEdgeType != tx.nx.Edge.EdgeType.Circular
    ):
        fail("NX_WRONG_GEOMETRY", "Concentric needs circular edges", field=field)
    return obj


@tool(
    effect="model",
    label="Create assembly constraint",
    summary={
        "fix": "Fix {component}",
        "touch": "Touch {component} to {target_component}",
        "align": "Align {component} with {target_component}",
        "distance": "Distance {value} {component} to {target_component}",
        "parallel": "Parallel {component} to {target_component}",
        "perpendicular": "Perpendicular {component} to {target_component}",
        "angle": "Angle {value} deg {component} to {target_component}",
        "concentric": "Concentric {component} with {target_component}",
    },
    keywords=("mate", "assembly constraint", "touch align", "concentric", "fix component"),
    requires=("work_part", "no_active_sketch", "assembly"),
    variants="type",
    cost="heavy",
    previewable=True,
    invalidates="deleted",
    example={
        "type": "touch",
        "component": "o:5",
        "geometry": "fc:21",
        "target_component": "o:4",
        "target_geometry": "fc:30",
    },
)
def nx_create_assembly_constraint(
    tx: Tx,
    *,
    type: Literal[
        "fix", "touch", "align", "distance", "parallel", "perpendicular", "angle", "concentric"
    ],
    component: ComponentRef,  # type: ignore[valid-type]
    geometry: Geometry | None = None,  # type: ignore[valid-type]
    target_component: ComponentRef | None = None,  # type: ignore[valid-type]
    target_geometry: Geometry | None = None,  # type: ignore[valid-type]
    value: Value | None = None,
    alignment: Alignment = "infer",
) -> ConstraintData:
    """Constrain a direct component: fix it, or relate a face or edge of it to one of a sibling.
    It must solve. Concentric takes circular edges; unfixed, either component may move."""
    if type == "fix":
        for name, given in (("geometry", geometry), ("target_component", target_component),
                            ("target_geometry", target_geometry), ("value", value)):  # fmt: skip
            if given is not None:
                fail("NX_INVALID_ARGUMENT", f"fix takes no {name}", field="/" + name)
        if alignment != "infer":
            fail("NX_INVALID_ARGUMENT", "fix takes no alignment", field="/alignment")
    else:
        for name, given in (("geometry", geometry), ("target_component", target_component),
                            ("target_geometry", target_geometry)):  # fmt: skip
            if given is None:
                fail("NX_INVALID_ARGUMENT", f"{type} needs {name}", field="/" + name)
        if type in VALUED and value is None:
            fail("NX_INVALID_ARGUMENT", f"{type} needs value", field="/value")
        if type not in VALUED and value is not None:
            fail("NX_INVALID_ARGUMENT", "value is only for distance and angle", field="/value")
        if type == "angle" and value > 180:
            fail("NX_INVALID_ARGUMENT", "An angle is 0 to 180 degrees", field="/value")
        if type == "align" and alignment == "opposite":
            fail("NX_INVALID_ARGUMENT", "align faces the same way; use touch", field="/alignment")
    with tx.checks():
        moving = common.component(tx, component, "/component", unsuppressed=True)
        refs = [(moving, moving)]
        if type != "fix":
            target = common.component(tx, target_component, "/target_component", unsuppressed=True)
            if assembly.same(target, moving):
                fail(
                    "NX_INVALID_ARGUMENT", "Use two different components", field="/target_component"
                )
            concentric = type == "concentric"
            refs = [
                (moving, _geometry(tx, geometry, moving, "/geometry", concentric)),
                (target, _geometry(tx, target_geometry, target, "/target_geometry", concentric)),
            ]
    if type == "align" and alignment == "infer":
        alignment = "same"
    cls = _cls(tx)
    part = tx.part
    before = poses(part)
    with network(tx) as (positioner, net):
        with native(default="NX_BUILD_FAILED"):
            constraint = positioner.CreateConstraint(True)
            constraint.ConstraintType = getattr(cls.Type, TYPES[type])
            constraint.ConstraintAlignment = getattr(cls.Alignment, ALIGNMENTS[alignment])
            for owner, obj in refs:
                constraint.CreateConstraintReference(owner, obj, False, False)
            if value is not None:
                constraint.SetExpression(repr(float(value)))
            net.AddConstraint(constraint)
        solve(tx, net)
        if not solved(tx, constraint):
            _unsatisfied(tx, constraint, "The assembly solver did not satisfy the constraint")
    out = record(tx, constraint)
    readback.expect(out["type"] == type, "type", actual=out["type"])
    if value is not None:
        readback.expect_close(out.get("value", float("nan")), value, "value")
    out["moved"] = moved(tx, before)
    return out


@tool(
    effect="model",
    label="Edit assembly constraint",
    summary="Edit constraint {constraint}",
    keywords=("change mate", "constraint offset", "suppress constraint", "flip alignment"),
    requires=("work_part", "no_active_sketch", "assembly"),
    cost="heavy",
    invalidates="deleted",
    example={"constraint": "ac:7", "value": 12},
)
def nx_edit_assembly_constraint(
    tx: Tx,
    *,
    constraint: RefTo("assembly_constraint"),  # type: ignore[valid-type]
    value: Value | None = None,
    alignment: Alignment | None = None,
    suppressed: bool | None = None,
) -> ConstraintData:
    """Change a constraint's distance or angle, its alignment or its suppression and solve
    again. An unsuppressed constraint must solve, or the edit rolls back."""
    if value is None and alignment is None and suppressed is None:
        fail("NX_INVALID_ARGUMENT", "Give value, alignment or suppressed")
    cls = _cls(tx)
    with tx.checks():
        obj = select.resolve(tx, constraint, "assembly_constraint", field="/constraint")
        if value is not None:
            if obj.ConstraintType not in (cls.Type.Distance, cls.Type.Angle):
                fail("NX_INVALID_ARGUMENT", "value is only for distance and angle", field="/value")
            if obj.ConstraintType == cls.Type.Angle and value > 180:
                fail("NX_INVALID_ARGUMENT", "An angle is 0 to 180 degrees", field="/value")
    before = poses(tx.part)
    with network(tx) as (_positioner, net):
        with native(default="NX_BUILD_FAILED"):
            if value is not None:
                obj.SetExpression(repr(float(value)))
            if suppressed is not None:
                obj.Suppressed = suppressed
            if alignment is not None:
                obj.ConstraintAlignment = getattr(cls.Alignment, ALIGNMENTS[alignment])
            net.AddConstraint(obj)
        solve(tx, net)
        if not obj.Suppressed and not solved(tx, obj):
            _unsatisfied(tx, obj, "The edited constraint does not solve")
    out = record(tx, obj)
    if suppressed is not None:
        readback.expect(out["suppressed"] == suppressed, "suppressed")
    if value is not None:
        readback.expect_close(out.get("value", float("nan")), value, "value")
    out["moved"] = moved(tx, before)
    return out
