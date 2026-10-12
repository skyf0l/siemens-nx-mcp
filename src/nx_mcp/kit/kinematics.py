"""Rigid kinematic sweeps of assembly components (no NX Motion), in assembly coordinates.

    p = kinematics.pose(component)                    # (t, R): p_asm = R p_local + t
    kinematics.place(tx, component, target)           # MoveComponent to an absolute pose
    bad = kinematics.drive(tx, constraint, value, mark)  # unsolved constraints after solving
    d, pa, pb = kinematics.gap(tx, bodies_a, bodies_b)  # minimum distance and witness points
    q = kinematics.carry(point, before, after)        # a point fixed to a moving component
    a = kinematics.angle_about(before, after, axis)   # signed rotation about axis, degrees

NX 2406 findings encoded here:

- The Simcenter 3D Motion solver (license feature ``sc_motion_solver``) is not licensed
  on the reference seat, and a new motion simulation takes NX's customer default solver
  (``CAE_DefaultMotionSolverType`` = ``LmsSolver``): creating one opens the blocking
  "Change Solver" license dialog. Sweeps therefore move the assembly itself: a rigid
  rotation or translation of components, or an assembly constraint's angle or distance
  driven through the positioner (its solver closes linkage loops).
- ``ComponentAssembly.MoveComponent`` rotates about the assembly origin
  (``kit.pose.move_args``). Moves and constraint solves are undone by an undo mark, so a
  sweep inside ``tx.scratch()`` leaves the assembly as it found it.
- ``Measurement.GetMinimumDistance`` on occurrence bodies answers at the current
  (moved) occurrence pose: 0 for touching or overlapping bodies.
"""

from __future__ import annotations

import contextlib
import math

from nx_mcp.core.errors import NXError, native
from nx_mcp.kit import builders, geom, model, positioning
from nx_mcp.kit import pose as kpose


def pose(component):
    """``(translation, row-major rotation)`` of an occurrence in assembly coordinates."""
    position, matrix = component.GetPosition()
    return geom.xyz(position), [list(map(float, row)) for row in geom.rows(matrix)]


def place(tx, component, target) -> None:
    """Move ``component`` (a component of the work part's assembly) to the absolute pose
    ``target``."""
    shift, delta = kpose.move_args(pose(component), target)
    with native(default="NX_BUILD_FAILED"):
        tx.part.ComponentAssembly.MoveComponent(
            component, tx.nx.Vector3d(*shift), builders.matrix(tx, delta)
        )


def drive(tx, constraint, value: float, mark=None) -> list:
    """Set an angle or distance constraint to ``value`` (degrees or part units), solve the
    assembly and update. Returns the unsuppressed constraints of the work part that do
    not solve afterwards (empty: the mechanism reached ``value``); an NX refusal returns
    ``[constraint]``. NX 2406 solves the driven constraint past a linkage's reach by
    opening the loop: another constraint of the loop then reports an unsolved status."""
    positioner = tx.part.ComponentAssembly.Positioner
    try:
        positioner.BeginAssemblyConstraints()
        try:
            net = positioner.EstablishNetwork()
            net.MoveObjectsState = True
            constraint.SetExpression(repr(float(value)))
            net.AddConstraint(constraint)
            net.Solve()
            net.ApplyToModel()
        finally:
            with contextlib.suppress(Exception):
                positioner.ClearNetwork()
            with contextlib.suppress(Exception):
                positioner.EndAssemblyConstraints()
        model.update(tx, mark)
    except Exception:
        return [constraint]
    failed = []
    for other in positioning.all_constraints(tx.part):
        try:
            if not other.Suppressed and not positioning.solved(tx, other):
                failed.append(other)
        except Exception:
            failed.append(other)
    return failed


def constraint_value(constraint) -> float | None:
    """The current angle or distance of a constraint, else None."""
    with contextlib.suppress(Exception):
        return float(constraint.Expression.Value)
    return None


def gap(tx, first, second):
    """``(distance, point_on_first, point_on_second)``: the minimum distance between two
    groups of bodies (0 when any pair touches or overlaps)."""
    best = None
    for a in first:
        for b in second:
            with native(default="NX_ERROR"):
                distance, pa, pb, _accuracy = tx.session.Measurement.GetMinimumDistance(a, b)
            distance = max(0.0, float(distance))
            if best is None or distance < best[0]:
                best = (distance, geom.xyz(pa), geom.xyz(pb))
    if best is None:
        raise NXError("NX_WRONG_GEOMETRY", "A clearance pair has no solid body to measure")
    return best


def carry(point, before, after):
    """Where ``point`` (absolute, fixed to a component at pose ``before``) is at ``after``."""
    t0, r0 = before
    t1, r1 = after
    local = geom.matvec(geom.transpose(r0), geom.sub(point, t0))
    return geom.add(geom.matvec(r1, local), t1)


def angle_about(before, after, axis) -> float:
    """Signed rotation in degrees from pose ``before`` to ``after`` about unit ``axis``
    (right hand). Exact for a rotation about ``axis``; for any other rotation it is the
    rotation's component about ``axis``."""
    relative = geom.matmul(after[1], geom.transpose(before[1]))
    vee = [
        relative[2][1] - relative[1][2],
        relative[0][2] - relative[2][0],
        relative[1][0] - relative[0][1],
    ]
    sine = geom.dot(vee, axis) / 2.0
    cosine = (relative[0][0] + relative[1][1] + relative[2][2] - 1.0) / 2.0
    return math.degrees(math.atan2(sine, cosine))


def unwrap(previous: float | None, angle: float) -> float:
    """``angle`` shifted by whole turns to the value closest to ``previous`` (continuous
    angle histories past +-180 degrees)."""
    if previous is None:
        return angle
    while angle - previous > 180.0:
        angle -= 360.0
    while angle - previous < -180.0:
        angle += 360.0
    return angle


__all__ = [
    "angle_about",
    "carry",
    "constraint_value",
    "drive",
    "gap",
    "place",
    "pose",
    "unwrap",
]
