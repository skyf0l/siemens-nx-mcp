"""Assembly constraints: create and edit, solved by the NX positioner (tool-surface 3.8).

NX 2406 findings encoded here:

- A constraint counts only when the solver reports ``Solved``; anything else rolls
  the change back as ``NX_CONSTRAINT_UNSATISFIED`` with the solver status.
- Concentric needs circular edges (cylindrical faces leave it unsolved): a cylindrical
  or conical face given instead is replaced by its circular edge nearest the other
  geometry (edge pairs by bounding-box centre distance; a tie is refused), and the
  solver may move either component unless one is fixed: ``moved`` lists every
  direct component whose pose changed. It makes the circle centres coincide AND the
  circle planes coplanar (live: a pin's bottom edge concentric with another's top edge
  stacks it). A touch that agrees with that plane solves (redundant); a touch on another
  plane (shank end edge concentric with the hole's top edge, then head on the face)
  leaves the solver OverConstrained. For a bolt in a hole use align_lock (axes) + touch
  (head on the face): ``CONCENTRIC_HINT``.
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
- A distance between facing planar faces can solve with the faces overlapping (live
  tester, NX 2406): the result reports ``offset`` (the face's signed distance from the
  target face's plane along that face's outward normal) and warns about an overlap.
"""

from __future__ import annotations

import contextlib
import math
import re
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import MM_PER_IN, QUANTITY_MAX, Aliases, Doc, RefTo, Text
from nx_mcp.domains.assembly import common, structure
from nx_mcp.domains.assembly.common import ComponentId, ComponentRow
from nx_mcp.kit import display, geom, model, positioning, readback, result, select
from nx_mcp.kit import pose as kpose

TYPES, NAMES = positioning.TYPES, positioning.NAMES
all_constraints = positioning.all_constraints
kind_name = positioning.kind_name
status_name = positioning.status_name
solved = positioning.solved
Kind = Annotated[
    Literal[
        "fix", "touch", "align", "distance", "parallel", "perpendicular", "angle", "concentric",
        "align_lock", "fit", "bond", "center_1_2", "center_2_2",
    ],
    Aliases(mate="touch", fixed="fix"),  # other CAD systems' names (gate 1 maps them)
]  # fmt: skip
ALIGNMENTS = {"infer": "InferAlign", "same": "CoAlign", "opposite": "ContraAlign"}
VALUED = ("distance", "angle")
# Types whose references are the components themselves, and types without alignment.
WHOLE = ("fix", "bond")
NO_ALIGNMENT = ("fix", "bond", "fit", "center_1_2", "center_2_2")
Geometry = Annotated[str, RefTo("face", "edge")]
Alignment = Literal["infer", "same", "opposite"]
# A number, or a unit string checked against the constraint type by ``_value``.
Value = Annotated[
    float | Annotated[str, Text(QUANTITY_MAX)],
    Doc("Part units or '10 mm' (<0: other side); angle: deg or '0.5 rad'"),
]
_QUANTITY = re.compile(
    r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?)\s*(mm|in|deg|rad|\u00b0)?\s*$", re.IGNORECASE
)
VALUE_MAX = 1e6
CONCENTRIC_EDGES_HINT = (
    "Concentric takes circular edges: nx_find_geometry(owner=<component>, kind='edge', "
    "geometry_type='circle', near=[x,y,z] or radius=r), or nx_list_objects(kind='edge', "
    "owner=<face>); a cylindrical face works when one of its circular edges is nearest "
    "the other geometry"
)
CONCENTRIC_HINT = (
    "concentric makes the circular edges coaxial AND coplanar, so a touch on another plane "
    "over-constrains; for a bolt or pin in a hole use align_lock on the axes + touch (head "
    "on face) instead, or suppress one with nx_edit_assembly_constraint"
)


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
    offset: float
    references: list[ConstraintReference]
    moved: list[ComponentRow]


_cls = positioning.cls


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
    if constraint.ConstraintType in (cls.Type.Touch, cls.Type.Distance):
        with contextlib.suppress(Exception):
            placed = face_offset(tx, constraint)
            if placed is not None:
                out["offset"] = placed[0]
    return out


def face_offset(tx, constraint) -> tuple[float, bool] | None:
    """``(offset, facing)`` of a constraint between two parallel planar faces: the first
    (the component's) face's signed distance from the second (the target's) face plane
    along that face's outward normal, and whether the faces look at each other (then a
    negative offset is an overlap). None for any other geometry."""
    faces = [ref.GetGeometry() for ref in constraint.GetReferences()]
    if len(faces) != 2 or any(tx.kind_of(f) != "face" for f in faces):
        return None
    (k1, p1, n1), (k2, p2, n2) = (select.face_data(tx, f) for f in faces)
    if k1 != select.FACE_PLANE or k2 != select.FACE_PLANE:
        return None
    n1, n2 = geom.unit(n1), geom.unit(n2)
    if not geom.parallel(n1, n2, 1e-6):
        return None
    offset = geom.dot(geom.sub(p1, p2), n2)
    return (0.0 if abs(offset) < 1e-9 else offset), geom.dot(n1, n2) < 0


def _check_overlap(tx, constraint):
    """Warn when a distance leaves two facing planar faces overlapping."""
    if constraint.ConstraintType != _cls(tx).Type.Distance:
        return
    placed = None
    with contextlib.suppress(Exception):
        placed = face_offset(tx, constraint)
    if placed is not None and placed[1] and placed[0] < -1e-6:
        tx.warn(
            "NX_UPDATE_WARNING",
            f"The faces overlap by {-placed[0]:.6g} (offset {placed[0]:.6g}); for a gap, "
            "edit the constraint with the opposite sign of value",
        )


def poses(part) -> dict:
    return {int(c.Tag): (c, structure.pose(c)) for c in structure.direct_children(part)}


def moved(tx, before) -> list[ComponentRow]:
    """Rows of the direct components whose pose changed since ``before``."""
    return [
        common.component_row(tx, c)
        for c, was in before.values()
        if not kpose.close(structure.pose(c), was, 1e-9)
    ]


def _components(constraint) -> set[int]:
    tags = set()
    for ref in constraint.GetReferences():
        with contextlib.suppress(Exception):
            tags.add(int(ref.GetMovableObject().Tag))
    return tags


def _others(tx, constraint, moving=None) -> list[dict]:
    """The other unsuppressed constraints on ``moving`` (else on any component of
    ``constraint``), at most 10."""
    mine = {int(moving.Tag)} if moving is not None else _components(constraint)
    rows = []
    for other in all_constraints(tx.part):
        if int(other.Tag) == int(constraint.Tag) or other.Suppressed:
            continue
        if _components(other) & mine:
            rows.append({"constraint": tx.ref(other, "assembly_constraint"),
                         "type": kind_name(tx, other)})  # fmt: skip
        if len(rows) == 10:
            break
    return rows


def _unsatisfied(tx, constraint, message, moving=None):
    details: dict = {}
    hints = ["Check the geometry pair and the fixed components; it was rolled back"]
    with contextlib.suppress(Exception):
        details = {"status": status_name(tx, constraint), "type": kind_name(tx, constraint)}
        alignment = result.enum_name(constraint.ConstraintAlignment, _cls(tx).Alignment)
        if alignment in ("CoAlign", "ContraAlign") and details["type"] in ("touch", "distance"):
            hints.append(
                "alignment 'same' points the face normals the same way and 'opposite' makes "
                "the faces look at each other; 'infer' lets NX choose"
            )
    others: list = []
    with contextlib.suppress(Exception):
        others = _others(tx, constraint, moving)
        if others:
            details["others"] = others
            hints.append(
                "other constraints on these components (details.others) may conflict: "
                "suppress them with nx_edit_assembly_constraint(suppressed=true)"
            )
    if _concentric_conflict(details.get("type"), others):
        hints.append(CONCENTRIC_HINT)
    fail("NX_CONSTRAINT_UNSATISFIED", message, hint="; ".join(hints), details=details)


def _concentric_conflict(kind, others) -> bool:
    """A concentric among the failing constraint and the others on its component."""
    return bool(others) and "concentric" in {kind, *(o.get("type") for o in others)}


def _over_constrained(tx, constraint, moving):
    """After a solved constraint next to a concentric on the component: warn when other
    constraints there no longer solve (the new one solved, the network did not)."""
    bad = []
    with contextlib.suppress(Exception):
        others = [
            c for c in all_constraints(tx.part)
            if int(c.Tag) != int(constraint.Tag) and not c.Suppressed
            and int(moving.Tag) in _components(c)
        ]  # fmt: skip
        kinds = {kind_name(tx, constraint), *(kind_name(tx, c) for c in others)}
        if "concentric" in kinds:
            bad = [f"{tx.ref(c, 'assembly_constraint')['id']} ({kind_name(tx, c)}: "
                   f"{status_name(tx, c)})" for c in others if not solved(tx, c)]  # fmt: skip
    if bad:
        tx.warn("NX_UPDATE_WARNING", "Constraints left unsolved: " + ", ".join(bad) + "; "
                + CONCENTRIC_HINT)  # fmt: skip


def _value(tx, type, value, field="/value"):
    """``value`` as a float in part units (distance) or degrees (angle): a number, or a
    string such as '10 mm', '0.5 in', '45 deg' or '0.5 rad' (a degree sign works too)."""
    if value is None:
        return None
    angle = type == "angle"
    if isinstance(value, str):
        match = _QUANTITY.match(value)
        if match is None:
            fail("NX_INVALID_ARGUMENT", f"value {value!r} is not a number with a unit",
                 field=field, hint="Send a number, or '10 mm', '0.5 in', '45 deg', '0.5 rad'")  # fmt: skip
        number, unit = float(match.group(1)), (match.group(2) or "").lower()
        if unit and (unit in ("mm", "in")) == angle:
            want = "an angle (deg or rad)" if angle else "a length (mm or in)"
            fail("NX_INVALID_ARGUMENT", f"{type} takes {want}, not {value!r}", field=field)
        if unit == "rad":
            number = math.degrees(number)
        elif unit in ("mm", "in") and unit != (tx.units.get("length") or "mm"):
            number = number * MM_PER_IN if unit == "in" else number / MM_PER_IN
    else:
        number = float(value)
    if not math.isfinite(number) or abs(number) > VALUE_MAX:
        fail("NX_INVALID_ARGUMENT", f"value must be within +-{VALUE_MAX:g}", field=field)
    if angle and not 0 <= number <= 180:
        fail("NX_INVALID_ARGUMENT", "An angle is 0 to 180 degrees", field=field)
    return number


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
    if type == "fit" and tx.kind_of(obj) != "face":
        fail("NX_WRONG_GEOMETRY", "Fit needs cylindrical or spherical faces", field=field)
    return obj


def _circular(tx, edge) -> bool:
    return tx.kind_of(edge) == "edge" and edge.SolidEdgeType == tx.nx.Edge.EdgeType.Circular


def _centre(tx, obj) -> list[float]:
    """The bounding-box centre of an edge (a full circle's centre), assembly coordinates."""
    with native():
        box = [float(v) for v in tx.uf.ModlGeneral.AskBoundingBox(obj.Tag)]
    return [(box[i] + box[i + 3]) / 2 for i in range(3)]


def _circles_of(tx, obj, field) -> list:
    """``[obj]`` for a circular edge; the circular edges of a cylindrical or conical face.
    Anything else fails with the way to find circular edges (and a face's circular edges
    by ref in ``details.circular_edges``)."""
    if _circular(tx, obj):
        return [obj]
    circles: list = []
    axial = False
    if tx.kind_of(obj) == "face":
        with contextlib.suppress(Exception):
            circles = [e for e in obj.GetEdges() if _circular(tx, e)]
        with contextlib.suppress(Exception):
            axial = select.face_data(tx, obj)[0] in (select.FACE_CYLINDER, select.FACE_CONE)
    if circles and axial:
        return circles
    message = "Concentric needs circular edges"
    if circles:
        message += " (this face is not cylindrical; details.circular_edges lists its edges)"
    fail(
        "NX_WRONG_GEOMETRY",
        message,
        field=field,
        hint=CONCENTRIC_EDGES_HINT,
        details={"circular_edges": [tx.ref(e, "edge") for e in circles[:10]]} if circles else {},
    )


def _concentric_edges(tx, refs, names):
    """Replace a cylindrical or conical face among the two concentric references by its
    circular edge nearest the other one: the closest pair of edge centres. A tie of the
    two closest pairs is refused with the candidates."""
    options = [
        _circles_of(tx, obj, "/" + name) for (_o, obj), name in zip(refs, names, strict=True)
    ]
    if all(len(found) == 1 for found in options):
        return [(owner, found[0]) for (owner, _obj), found in zip(refs, options, strict=True)]
    centres = {int(e.Tag): _centre(tx, e) for found in options for e in found}
    pairs = sorted(
        (math.dist(centres[int(a.Tag)], centres[int(b.Tag)]), i, a, b)
        for i, (a, b) in enumerate((a, b) for a in options[0] for b in options[1])
    )
    if len(pairs) > 1 and pairs[1][0] - pairs[0][0] <= 1e-6 * max(1.0, pairs[0][0]):
        candidates = [
            {name: tx.ref(e, "edge") for name, e in zip(names, pair[2:], strict=True)}
            for pair in pairs[:2]
        ]
        fail(
            "NX_AMBIGUOUS_REFERENCE",
            "Two circular-edge pairs of the faces are equally near each other",
            field="/" + names[0 if len(options[0]) > 1 else 1],
            hint=CONCENTRIC_EDGES_HINT,
            details={"candidates": candidates},
        )
    chosen = pairs[0][2:]
    for (_owner, obj), name, edge in zip(refs, names, chosen, strict=True):
        if edge is not obj:
            used = tx.ref(edge, "edge")["id"]
            tx.warn(
                "NX_NOTE",
                f"{name}: the face's circular edge {used}, nearest the other geometry, was "
                "used (concentric takes circular edges)",
            )
    return [(owner, edge) for (owner, _obj), edge in zip(refs, chosen, strict=True)]


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
    component: ComponentId,
    geometry: Geometry | None = None,
    target_component: ComponentId | None = None,
    target_geometry: Geometry | None = None,
    geometry2: Annotated[Geometry, Doc("center_2_2: the component's second face or edge")]
    | None = None,
    target_geometry2: Annotated[Geometry, Doc("center types: the target's second face or edge")]
    | None = None,
    value: Value | None = None,
    alignment: Alignment = "infer",
) -> ConstraintData:
    """Constrain a direct component to a sibling (touch is the mate) or fix it; it must solve.
    Concentric: circular edges (or a cylinder's edge nearest the other) made coaxial AND
    coplanar; bolt in hole: align_lock axes + touch. Fit: equal-radius faces; center_1_2:
    between two target geometries."""
    given = {
        "geometry": geometry, "target_component": target_component,
        "target_geometry": target_geometry, "geometry2": geometry2,
        "target_geometry2": target_geometry2, "value": value,
    }  # fmt: skip
    _arguments(type, given, alignment)
    number = _value(tx, type, value)
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
                names = [name for name in fields if given[name] is not None]
                for name in names:
                    owner = moving if name.startswith("geometry") else target
                    refs.append((owner, _geometry(tx, given[name], owner, "/" + name, type)))
                if type == "concentric":
                    refs = _concentric_edges(tx, refs, names)
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
            if number is not None:
                constraint.SetExpression(repr(number))
            net.AddConstraint(constraint)
        solve(tx, net)
        if not solved(tx, constraint):
            _unsatisfied(
                tx, constraint, "The assembly solver did not satisfy the constraint", moving
            )
    out = record(tx, constraint)
    readback.expect(out["type"] == type, "type", actual=out["type"])
    if number is not None:
        readback.expect_close(out.get("value", float("nan")), number, "value")
    _check_overlap(tx, constraint)
    _over_constrained(tx, constraint, moving)
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
        number = None
        if value is not None:
            if obj.ConstraintType not in (cls.Type.Distance, cls.Type.Angle):
                fail("NX_INVALID_ARGUMENT", "value is only for distance and angle", field="/value")
            angle = obj.ConstraintType == cls.Type.Angle
            number = _value(tx, "angle" if angle else "distance", value)
    before = poses(tx.part)
    with network(tx) as (_positioner, net):
        with native(default="NX_BUILD_FAILED"):
            if number is not None:
                obj.SetExpression(repr(number))
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
    if number is not None:
        readback.expect_close(out.get("value", float("nan")), number, "value")
    if not obj.Suppressed:
        _check_overlap(tx, obj)
    out["moved"] = moved(tx, before)
    return out
