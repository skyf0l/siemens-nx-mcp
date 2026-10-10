"""Assembly constraints: create and edit, solved by the NX positioner (tool-surface 3.8).

NX 2406 findings encoded here:

- A constraint counts only when the solver reports ``Solved``; anything else rolls
  the change back as ``NX_CONSTRAINT_UNSATISFIED`` with the solver status.
- Concentric needs circular edges (cylindrical faces leave it unsolved), and the
  solver may move either component unless one is fixed: ``moved`` lists every
  direct component whose pose changed.
- Align is a Touch constraint with co-alignment (the classic mate "align").
- Align/Lock aligns two axes and locks the rotation about them: a cylindrical face
  gives its axis (``usesAxis``), an edge its own line or circle axis.
- Fit takes two faces of equal radius; Bond welds the components (its references
  are the components themselves, like Fix).
- Center 1-to-2 centers one face or edge between two of the target; NX takes the
  first two references as the pair, so the target's go first (else CannotSolve).
  Center 2-to-2 centers a pair of the component between a pair of the target. A cylindrical or
  conical face there is referenced by its axis (``usesAxis``), else it stays unsolved.
- ``Constraint.Type`` has no distance-to-edge type (NX 2406); Gear, RackPinion and
  Screw are couplers between existing joint constraints, not geometry pairs.
"""

from __future__ import annotations

import contextlib
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import ComponentRef, Doc, Range, RefTo
from nx_mcp.domains.assembly import common, structure
from nx_mcp.domains.assembly.common import ComponentRow
from nx_mcp.kit import display, model, readback, result, select
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
    "align_lock": "AlignLock",
    "fit": "Fit",
    "bond": "Bond",
    "center_1_2": "Center12",
    "center_2_2": "Center22",
}
NAMES = {v: k for k, v in TYPES.items() if k != "align"}
Kind = Literal[
    "fix", "touch", "align", "distance", "parallel", "perpendicular", "angle", "concentric",
    "align_lock", "fit", "bond", "center_1_2", "center_2_2",
]  # fmt: skip
ALIGNMENTS = {"infer": "InferAlign", "same": "CoAlign", "opposite": "ContraAlign"}
VALUED = ("distance", "angle")
# Types whose references are the components themselves, and types without alignment.
WHOLE = ("fix", "bond")
NO_ALIGNMENT = ("fix", "bond", "fit", "center_1_2", "center_2_2")
Geometry = Annotated[str, RefTo("face", "edge")]
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
    return NAMES.get(native_type, native_type.lower())


def status_name(tx, constraint) -> str:
    cls = _cls(tx)
    return result.enum_name(constraint.GetConstraintStatus(), cls.SolverStatus)


def solved(tx, constraint) -> bool:
    return constraint.GetConstraintStatus() == _cls(tx).SolverStatus.Solved


def record(tx, constraint) -> ConstraintData:
    """The result row of one constraint: type, alignment, solver status, value, references."""
    cls = _cls(tx)
    alignment = result.enum_name(constraint.ConstraintAlignment, cls.Alignment)
    out: ConstraintData = {
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
        item: ConstraintReference = {}
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
    return {int(c.Tag): (c, structure.pose(c)) for c in structure.direct_children(part)}


def moved(tx, before) -> list[ComponentRow]:
    """Rows of the direct components whose pose changed since ``before``."""
    return [
        common.component_row(tx, c)
        for c, was in before.values()
        if not kpose.close(structure.pose(c), was, 1e-9)
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


def _geometry(tx, ref, owner, field, type):
    obj = select.resolve(tx, ref, "face", "edge", field=field)
    if not getattr(obj, "IsOccurrence", False) or not display.same(
        getattr(obj, "OwningComponent", None), owner
    ):
        fail(
            "NX_WRONG_OWNER",
            "Select a face or edge of that component (occurrence geometry)",
            field=field,
        )
    if type == "concentric" and (
        tx.kind_of(obj) != "edge" or obj.SolidEdgeType != tx.nx.Edge.EdgeType.Circular
    ):
        fail("NX_WRONG_GEOMETRY", "Concentric needs circular edges", field=field)
    if type == "fit" and tx.kind_of(obj) != "face":
        fail("NX_WRONG_GEOMETRY", "Fit needs cylindrical or spherical faces", field=field)
    return obj


def _uses_axis(tx, type, obj) -> bool:
    """Align/Lock takes the axis of any face; the center types that of a cylinder or cone."""
    if tx.kind_of(obj) != "face":
        return False
    if type == "align_lock":
        return True
    if type in ("center_1_2", "center_2_2"):
        with contextlib.suppress(Exception):
            return select.face_data(tx, obj)[0] in (select.FACE_CYLINDER, select.FACE_CONE)
    return False


def _arguments(type, given, alignment):
    """Refuse missing and surplus arguments of ``type`` (``given``: name -> value)."""
    needed = {
        "geometry": type not in WHOLE,
        "target_component": type != "fix",
        "target_geometry": type not in WHOLE,
        "geometry2": type == "center_2_2",
        "target_geometry2": type in ("center_1_2", "center_2_2"),
        "value": type in VALUED,
    }
    for name, need in needed.items():
        if need and given[name] is None:
            fail("NX_INVALID_ARGUMENT", f"{type} needs {name}", field="/" + name)
        if not need and given[name] is not None:
            fail("NX_INVALID_ARGUMENT", f"{type} takes no {name}", field="/" + name)
    if type in NO_ALIGNMENT and alignment != "infer":
        fail("NX_INVALID_ARGUMENT", f"{type} takes no alignment", field="/alignment")
    if type == "angle" and given["value"] > 180:
        fail("NX_INVALID_ARGUMENT", "An angle is 0 to 180 degrees", field="/value")
    if type == "align" and alignment == "opposite":
        fail("NX_INVALID_ARGUMENT", "align faces the same way; use touch", field="/alignment")


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
        "align_lock": "Align/lock {component} with {target_component}",
        "fit": "Fit {component} into {target_component}",
        "bond": "Bond {component} to {target_component}",
        "center_1_2": "Center {component} between faces of {target_component}",
        "center_2_2": "Center {component} pair between {target_component}",
    },
    keywords=(
        "mate",
        "assembly constraint",
        "touch align",
        "concentric",
        "fix component",
        "align lock",
        "fit bond",
        "center",
    ),  # fmt: skip
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
    type: Kind,
    component: ComponentRef,
    geometry: Geometry | None = None,
    target_component: ComponentRef | None = None,
    target_geometry: Geometry | None = None,
    geometry2: Annotated[Geometry, Doc("center_2_2: the component's second face or edge")]
    | None = None,
    target_geometry2: Annotated[Geometry, Doc("center types: the target's second face or edge")]
    | None = None,
    value: Value | None = None,
    alignment: Alignment = "infer",
) -> ConstraintData:
    """Constrain a direct component: fix it, bond it to a sibling, or relate faces or edges of
    it to a sibling's. It must solve. Concentric takes circular edges, fit equal-radius faces,
    align_lock axes; center_1_2 centers geometry between target_geometry and target_geometry2."""
    given = {
        "geometry": geometry, "target_component": target_component,
        "target_geometry": target_geometry, "geometry2": geometry2,
        "target_geometry2": target_geometry2, "value": value,
    }  # fmt: skip
    _arguments(type, given, alignment)
    with tx.checks():
        moving = common.component(tx, component, "/component", unsuppressed=True)
        refs = [(moving, moving)]
        if type != "fix":
            target = common.component(tx, target_component, "/target_component", unsuppressed=True)
            if display.same(target, moving):
                fail(
                    "NX_INVALID_ARGUMENT", "Use two different components", field="/target_component"
                )
            if type == "bond":
                refs.append((target, target))
            else:
                fields: tuple[str, ...] = (
                    "geometry", "geometry2", "target_geometry", "target_geometry2"
                )  # fmt: skip
                if type == "center_1_2":  # NX pairs the first two references
                    fields = ("target_geometry", "target_geometry2", "geometry")
                refs = []
                for name in fields:
                    if given[name] is not None:
                        owner = moving if name.startswith("geometry") else target
                        refs.append((owner, _geometry(tx, given[name], owner, "/" + name, type)))
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
                constraint.CreateConstraintReference(owner, obj, _uses_axis(tx, type, obj), False)
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
    constraint: Annotated[str, RefTo("assembly_constraint")],
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
