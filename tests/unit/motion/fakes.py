"""A mechanism seam on ``fake_nx`` for the motion unit tests.

``motion_nx()`` returns a fake NX whose work part is an assembly (``nx.asm``) with:

- components (``nx.add(name, pose)``): occurrence poses read by ``GetPosition``, one
  solid occurrence body each; ``MoveComponent`` rotates about the assembly origin, then
  translates (NX 2406);
- ``session.Measurement.GetMinimumDistance(a, b)``: ``nx.gap(body_a, body_b)`` (a test
  function of the current poses) returning ``(distance, point_a, point_b)``;
- a ``Positioner`` whose network ``Solve`` calls ``nx.solve(constraint, value)``: the test
  places the components and returns the solver status of every constraint;
- undo marks that also restore component poses and constraint values (``nx.undos``
  counts ``UndoToMark`` calls).
"""

from __future__ import annotations

import math
from types import SimpleNamespace as NS

from nx_mcp.kit import geom
from tests.kit import fake_nx, matrix, point
from tests.kit.objects import Body, Object

STATUS = NS(Solved=0, CannotSolve=1, NotSolved=2, Suppressed=3)
TYPES = NS(Angle=10, Distance=11, Concentric=12, Fix=13)


def rz(deg, translation=(0.0, 0.0, 0.0)):
    """A pose rotated ``deg`` about +Z, then translated."""
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return [float(v) for v in translation], [[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]


class Component(Object):
    def __init__(self, nx, name, pose, parent):
        super().__init__(name)
        self.Parent = parent
        self.IsSuppressed = False
        self.IsOccurrence = True
        self.OwningPart = nx.asm
        self.children = []
        self.t, self.r = list(pose[0]), [list(row) for row in pose[1]]
        self.Prototype = NS(Bodies=[], Tag=next(Object.ids), FullPath=f"{name}.prt")
        self.body = None
        if parent is not None:
            parent.children.append(self)
            prototype_body = Body(name + "_BODY")
            self.Prototype.Bodies.append(prototype_body)
            self.body = Body(name + "_OCC")
            self.body.IsOccurrence = True
            self.body.OwningComponent = self
            self.body.OwningPart = nx.asm
            self._occurrences = {int(prototype_body.Tag): self.body}

    def GetChildren(self):
        return list(self.children)

    def GetPosition(self):
        return point(*self.t), matrix(
            *([self.r[0][j], self.r[1][j], self.r[2][j]] for j in range(3))
        )

    def FindOccurrence(self, obj):
        return self._occurrences.get(int(obj.Tag))


class Constraint(Object):
    def __init__(self, nx, kind, value=None):
        super().__init__(f"{kind} constraint")
        self.nx = nx
        self.ConstraintType = getattr(TYPES, kind)
        self.Suppressed = False
        self.Expression = NS(Value=float(value) if value is not None else 0.0)
        self.status = STATUS.Solved
        self.OwningPart = nx.asm

    def SetExpression(self, text):
        self.Expression.Value = float(text)

    def GetConstraintStatus(self):
        return STATUS.Suppressed if self.Suppressed else self.status


class Network:
    def __init__(self, nx):
        self.nx = nx
        self.added = []
        self.MoveObjectsState = False

    def AddConstraint(self, c):
        self.added.append(c)

    def Solve(self):
        for c in self.added:
            statuses = self.nx.solve(c, c.Expression.Value) or {}
            for constraint in self.nx.constraints:
                constraint.status = statuses.get(constraint, STATUS.Solved)

    def ApplyToModel(self):
        pass


class Positioner:
    def __init__(self, nx):
        self.nx = nx
        self.open = 0

    @property
    def Constraints(self):  # noqa: N802 - NX name
        return list(self.nx.constraints)

    def BeginAssemblyConstraints(self):
        self.open += 1

    def EndAssemblyConstraints(self):
        self.open -= 1

    def EstablishNetwork(self):
        return Network(self.nx)

    def ClearNetwork(self):
        pass


def motion_nx():
    nx = fake_nx()
    asm = nx.part
    nx.asm = asm
    nx.constraints = []
    nx.solve = lambda constraint, value: {}
    nx.gap = lambda a, b: (100.0, [0.0, 0.0, 0.0], [0.0, 0.0, 100.0])
    nx.undos = 0
    root = Component(nx, "ROOT", ([0.0, 0.0, 0.0], geom.IDENTITY), None)
    nx.root = root

    def move(c, shift, m):
        delta = geom.rows(m)
        c.t = geom.add(geom.matvec(delta, c.t), geom.xyz(shift))
        c.r = geom.matmul(delta, c.r)

    asm.ComponentAssembly = NS(RootComponent=root, MoveComponent=move, Positioner=Positioner(nx))
    nx.nx.Positioning = NS(Constraint=NS(Type=TYPES, SolverStatus=STATUS))
    session = nx.session
    session.UpdateManager = NS(DoUpdate=lambda mark: 0)

    def minimum_distance(a, b):
        d, pa, pb = nx.gap(a, b)
        return d, point(*pa), point(*pb), 0.0

    session.Measurement = NS(GetMinimumDistance=minimum_distance)
    marks = {}
    set_mark, undo = session.SetUndoMark, session.UndoToMark

    def state():
        return (
            {c: (list(c.t), [list(r) for r in c.r]) for c in components()},
            {c: (c.Expression.Value, c.status) for c in nx.constraints},
        )

    def set_undo_mark(*args):
        mark = set_mark(*args)
        marks[mark] = state()
        return mark

    def undo_to_mark(mark, *args):
        undo(mark, *args)
        nx.undos += 1
        poses, values = marks[mark]
        for c, (t, r) in poses.items():
            c.t, c.r = list(t), [list(row) for row in r]
        for c, (value, status) in values.items():
            c.Expression.Value, c.status = value, status

    session.SetUndoMark, session.UndoToMark = set_undo_mark, undo_to_mark

    def components():
        out = []

        def walk(node):
            for child in node.children:
                out.append(child)
                walk(child)

        walk(root)
        return out

    def add(name, pose=None):
        c = Component(nx, name, pose or ([0.0, 0.0, 0.0], geom.IDENTITY), root)
        c.ref = nx.ref_of(c, "component")
        c.body_ref = nx.ref_of(c.body, "body")
        return c

    def constraint(kind, value=None):
        c = Constraint(nx, kind, value)
        nx.constraints.append(c)
        c.ref = nx.ref_of(c, "assembly_constraint")
        return c

    nx.add, nx.constraint, nx.components = add, constraint, components
    return nx


def carried(component, local):
    """The absolute position of a point given in ``component``'s own coordinates."""
    return geom.add(geom.matvec(component.r, local), component.t)
