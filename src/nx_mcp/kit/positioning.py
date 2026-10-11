"""Assembly constraint state shared by the assembly and inspection domains.

NX 2406 keeps a solver status on every constraint (``GetConstraintStatus``): only
``Solved`` (and ``Suppressed`` for a suppressed one) means it holds. A constraint whose
geometry a prototype edit consumed (an edge merged away by a boolean or blend) reports
an unsolved status once the assembly updates. ``unsolved`` lists them for
``nx_check_model`` and the post-call check of model tools.
"""

from __future__ import annotations

import contextlib

from nx_mcp.kit import result

# Native types by tool name (Touch with co-alignment is reported as "align").
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
# Statuses that leave the components where the constraint cannot place them: errors.
BROKEN = ("CannotSolve", "OverConstrained", "Inconsistent", "Conflicting")


def cls(tx):
    return tx.nx.Positioning.Constraint


def all_constraints(part) -> list:
    """Every constraint of ``part`` (none for a part without a positioner)."""
    positioner = getattr(getattr(part, "ComponentAssembly", None), "Positioner", None)
    if positioner is None:
        return []
    try:
        return list(positioner.Constraints)
    except Exception:
        return []


def kind_name(tx, constraint) -> str:
    native_cls = cls(tx)
    native_type = result.enum_name(constraint.ConstraintType, native_cls.Type)
    if native_type == "Touch":
        align = result.enum_name(constraint.ConstraintAlignment, native_cls.Alignment)
        return "align" if align == "CoAlign" else "touch"
    return NAMES.get(native_type, native_type.lower())


def status_name(tx, constraint) -> str:
    return result.enum_name(constraint.GetConstraintStatus(), cls(tx).SolverStatus)


def solved(tx, constraint) -> bool:
    return constraint.GetConstraintStatus() == cls(tx).SolverStatus.Solved


def components(constraint) -> list:
    """The components a constraint positions (its references' movable objects)."""
    out, seen = [], set()
    for ref in constraint.GetReferences():
        with contextlib.suppress(Exception):
            obj = ref.GetMovableObject()
            if obj is not None and int(obj.Tag) not in seen:
                seen.add(int(obj.Tag))
                out.append(obj)
    return out


def unsolved(tx, part) -> list[dict]:
    """``[{constraint, type, status, severity, components}]`` of the unsuppressed
    constraints of ``part`` that do not report Solved (``_constraint``: the live object)."""
    rows = []
    for constraint in all_constraints(part):
        try:
            if constraint.Suppressed or solved(tx, constraint):
                continue
            status = status_name(tx, constraint)
        except Exception:
            continue
        if status == "Suppressed":
            continue
        row: dict = {
            "constraint": tx.ref(constraint, "assembly_constraint", part=part),
            "type": "",
            "status": status,
            "severity": "error" if status in BROKEN else "warning",
            "components": [],
            "_constraint": constraint,
        }
        with contextlib.suppress(Exception):
            row["type"] = kind_name(tx, constraint)
        for c in components(constraint):
            with contextlib.suppress(Exception):
                if tx.kind_of(c) == "component":
                    row["components"].append(tx.ref(c, "component", part=part)["id"])
        rows.append(row)
    return rows


def uses(constraint, prototype) -> bool:
    """Whether a constraint positions an occurrence of ``prototype``."""
    tag = int(prototype.Tag)
    for c in components(constraint):
        with contextlib.suppress(Exception):
            proto = getattr(c, "Prototype", None)
            if proto is not None and int(proto.Tag) == tag:
                return True
    return False
