"""An assembly seam on ``fake_nx`` + ``with_parts`` for the assembly unit tests.

``assembly_nx(root)`` returns a fake NX whose work part ``nx.asm`` is an assembly part
with a ``ComponentAssembly`` that mirrors NX 2406 behaviour the domain relies on:

- ``AddComponent`` loads (or reuses) the prototype file and adds an occurrence under
  the root at the given pose; ``MoveComponent`` rotates about the *assembly origin*,
  then translates (``nx.pivot_at_origin = False`` simulates a component-origin pivot).
- undo marks also restore occurrences, poses, suppression, names and reference sets.
- ``Positioner``: constraints solve with the status a test sets (``nx.solver_status``)
  and may move components (``nx.on_solve``).
- ``CreateComponentPatternBuilder``: linear/circular patterns whose members sit at the
  layout poses (``nx.recreate_on_edit`` returns a new pattern object from an edit).
- explosions: per-component post-transforms relative to the exploded parent, the UF
  ``Assem`` explode/unexplode/view calls, trace lines and smart points on occurrence
  edges (percent of a straight edge).
- ``CreateReferenceSet`` seeds every component when ``nx.seed_reference_sets``.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace as NS

from nx_mcp.kit import geom
from nx_mcp.kit import pose as kpose
from tests.kit import fake_nx
from tests.kit.kernel import services
from tests.kit.objects import Body, Collection, Edge, Face, Object, matrix, point
from tests.kit.parts import FilePart, with_parts

ENUM = itertools.count(100)


def nx_matrix(r):
    """An NX Matrix3x3 for row-major R (its X, Y, Z axes are R's columns)."""
    return matrix(*([r[0][j], r[1][j], r[2][j]] for j in range(3)))


class Expr:
    """An NX expression whose RightHandSide sets Value."""

    def __init__(self, value=0.0):
        self.Value = float(value)
        self._rhs = str(value)

    @property
    def RightHandSide(self):  # noqa: N802 - NX name
        return self._rhs

    @RightHandSide.setter
    def RightHandSide(self, text):  # noqa: N802 - NX name
        self._rhs = str(text)
        self.Value = float(text)


class Component(Object):
    def __init__(self, fake, prototype, name, pose, parent):
        super().__init__(name.upper())
        self.fake = fake
        self.Prototype = prototype
        self.Parent = parent
        self.IsSuppressed = False
        self.IsOccurrence = True
        self.OwningPart = fake.asm
        self.ReferenceSet = "Entire Part"
        self.children = []
        self.t, self.r = [float(v) for v in pose[0]], [list(map(float, row)) for row in pose[1]]
        self.occurrences = {}
        if parent is not None:
            parent.children.append(self)

    def SetName(self, name):
        self.Name = str(name).upper()

    def GetChildren(self):
        return list(self.children)

    def GetPosition(self):
        return point(*self.t), nx_matrix(self.r)

    def FindOccurrence(self, obj):
        return self.occurrences.get(int(obj.Tag))

    def edge(self, start, end, circular=False):
        """A prototype edge and its occurrence (assembled coordinates follow the pose)."""
        proto = Edge("EDGE")
        proto.IsOccurrence = False
        body = next(iter(getattr(self.Prototype, "Bodies", []) or []), None)
        if body is None:
            body = Body("PROTO")
            self.Prototype.Bodies.append(body)
        body.edges.append(proto)
        occ = Edge("EDGE")
        occ.IsOccurrence = True
        occ.OwningComponent = self
        occ.OwningPart = self.fake.asm
        occ.Prototype = proto
        occ.local = (list(start), list(end))
        occ.SolidEdgeType = 2 if circular else 1
        self.occurrences[int(proto.Tag)] = occ
        return occ

    def face(self):
        occ = Face("FACE")
        occ.IsOccurrence = True
        occ.OwningComponent = self
        occ.OwningPart = self.fake.asm
        return occ


class ErrorList:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.Length = len(self.messages)
        self.disposed = False

    def GetErrorInfo(self, i):
        return self.messages[i]

    def Dispose(self):
        self.disposed = True


# ---- constraints ------------------------------------------------------------------------
TYPE = NS(Fix=1, Touch=2, Distance=3, Parallel=4, Perpendicular=5, Angle=6, Concentric=7)
ALIGN = NS(InferAlign=10, CoAlign=11, ContraAlign=12)
STATUS = NS(Solved=20, NotSolved=21, OverConstrained=22, Suppressed=23)


class Reference:
    def __init__(self, movable, geometry):
        self._movable, self._geometry = movable, geometry

    def GetMovableObject(self):
        return self._movable

    def GetGeometry(self):
        return self._geometry


class Constraint(Object):
    def __init__(self, fake):
        super().__init__("CONSTRAINT")
        self.fake = fake
        self.ConstraintType = None
        self.ConstraintAlignment = ALIGN.InferAlign
        self.Suppressed = False
        self.Expression = Expr(0.0)
        self.refs = []
        self.status = STATUS.NotSolved
        self.OwningPart = fake.asm

    def CreateConstraintReference(self, movable, geometry, *_):
        self.refs.append(Reference(movable, geometry))

    def SetExpression(self, text):
        self.Expression.RightHandSide = text

    def GetReferences(self):
        return list(self.refs)

    def GetConstraintStatus(self):
        return STATUS.Suppressed if self.Suppressed else self.status


class Network:
    def __init__(self, positioner):
        self.positioner = positioner
        self.added = []
        self.MoveObjectsState = False

    def AddConstraint(self, c):
        self.added.append(c)
        if c not in self.positioner.Constraints:
            self.positioner.Constraints.append(c)

    def Solve(self):
        fake = self.positioner.fake
        for c in self.positioner.Constraints:
            c.status = fake.solver_status
        if fake.on_solve:
            fake.on_solve(self.added)

    def ApplyToModel(self):
        self.positioner.applied += 1


class Positioner:
    def __init__(self, fake):
        self.fake = fake
        self.Constraints = Collection()
        self.calls = []
        self.applied = 0

    def BeginAssemblyConstraints(self):
        self.calls.append("begin")

    def EndAssemblyConstraints(self):
        self.calls.append("end")

    def ClearNetwork(self):
        self.calls.append("clear")

    def EstablishNetwork(self):
        return Network(self)

    def CreateConstraint(self, _persistent):
        return Constraint(self.fake)


# ---- patterns -----------------------------------------------------------------------------
PATTERN = NS(Linear=31, Circular=32, Polygon=33)


class Pattern(Object):
    def __init__(self, fake, seeds):
        super().__init__("COMPONENT_PATTERN")
        self.seeds = list(seeds)
        self.members = []
        self.OwningPart = fake.asm
        self.params = None

    def GetComponentsToPattern(self):
        return list(self.seeds)

    def GetAllPatternMembers(self):
        return [NS(GetAllComponents=lambda c=c: [c]) for c in self.members]


class PatternBuilder:
    def __init__(self, fake, pattern):
        self.fake = fake
        self.pattern = pattern
        self.Associative = True
        self.seeds = []
        self.ComponentPatternSet = NS(Add=self.seeds.append)
        rect = NS(
            XDirection=None, YDirection=None, UseYDirectionToggle=False,
            XSpacing=NS(NCopies=Expr(1), PitchDistance=Expr(0)),
            YSpacing=NS(NCopies=Expr(1), PitchDistance=Expr(0)),
        )  # fmt: skip
        circ = NS(
            RotationAxis=None,
            AngularSpacing=NS(NCopies=Expr(1), PitchAngle=Expr(0)),
            RadialSpacing=NS(NCopies=Expr(1)),
        )
        self.PatternService = NS(
            PatternType=PATTERN.Linear, PatternEnum=PATTERN,
            RectangularDefinition=rect, CircularDefinition=circ,
        )  # fmt: skip
        self.destroyed = False
        if pattern is not None:
            self._load(pattern.params)
        fake.pattern_builders.append(self)

    def _load(self, params):
        s = self.PatternService
        s.PatternType = params["type"]
        self.Associative = params["associative"]
        d, c = s.RectangularDefinition, s.CircularDefinition
        d.XDirection, d.YDirection = params["x"], params["y"]
        d.UseYDirectionToggle = params["toggle"]
        d.XSpacing.NCopies.RightHandSide = params["n"]
        d.XSpacing.PitchDistance.RightHandSide = params["d"]
        d.YSpacing.NCopies.RightHandSide = params["n2"]
        d.YSpacing.PitchDistance.RightHandSide = params["d2"]
        c.RotationAxis = params["axis"]
        c.AngularSpacing.NCopies.RightHandSide = params["cn"]
        c.AngularSpacing.PitchAngle.RightHandSide = params["angle"]

    def _params(self):
        s = self.PatternService
        d, c = s.RectangularDefinition, s.CircularDefinition
        return {
            "type": s.PatternType, "associative": self.Associative,
            "x": d.XDirection, "y": d.YDirection, "toggle": d.UseYDirectionToggle,
            "n": int(d.XSpacing.NCopies.Value), "d": d.XSpacing.PitchDistance.Value,
            "n2": int(d.YSpacing.NCopies.Value), "d2": d.YSpacing.PitchDistance.Value,
            "axis": c.RotationAxis, "cn": int(c.AngularSpacing.NCopies.Value),
            "angle": c.AngularSpacing.PitchAngle.Value,
        }  # fmt: skip

    def Commit(self):
        fake = self.fake
        params = self._params()
        if self.pattern is None:
            pattern = Pattern(fake, self.seeds)
            fake.patterns.append(pattern)
        elif fake.recreate_on_edit:
            old = self.pattern
            fake.patterns.remove(old)
            for m in old.members:
                fake.remove_component(m)
            pattern = Pattern(fake, old.seeds)
            fake.patterns.append(pattern)
        else:
            pattern = self.pattern
            for m in pattern.members:
                fake.remove_component(m)
        pattern.params = params
        pattern.members = []
        seed = pattern.seeds[0]
        poses = fake.pattern_poses(seed, params)
        for i, p in enumerate(poses[1:], start=2):
            pattern.members.append(fake.add(seed.Prototype, f"{seed.Name}_P{i}", p))
        return pattern

    def Destroy(self):
        self.destroyed = True


# ---- explosions -----------------------------------------------------------------------------
class ExplodedNode:
    def __init__(self, explosion, component):
        self.explosion = explosion
        self.component = component

    def GetComponent(self):
        return self.component

    def GetChildren(self):
        return [ExplodedNode(self.explosion, c) for c in self.component.GetChildren()]

    def GetPosition(self):
        t, r = self.explosion.pose(self.component)
        return point(*t), nx_matrix(r)


class Explosion(Object):
    def __init__(self, fake, name):
        super().__init__(name)
        self.fake = fake
        self.OwningPart = fake.asm
        self.deltas = {}

    @property
    def RootComponent(self):  # noqa: N802 - NX name
        return ExplodedNode(self, self.fake.root)

    def pose(self, component):
        """Exploded pose: exploded parent o relative assembled pose o local delta."""
        assembled = (component.t, component.r)
        parent = component.Parent
        if parent is None or parent is self.fake.root:
            base = assembled
        else:
            rel = kpose.compose(kpose.inverse((parent.t, parent.r)), assembled)
            base = kpose.compose(self.pose(parent), rel)
        m = self.deltas.get(int(component.Tag))
        if m is None:
            return base
        delta = ([m[0][3], m[1][3], m[2][3]], [row[:3] for row in m[:3]])
        return kpose.compose(base, delta)

    def Delete(self):
        self.fake.explosions.remove(self)


class Explosions(Collection):
    def __init__(self, fake):
        super().__init__()
        self.fake = fake

    def Create(self, name):
        ex = Explosion(self.fake, name)
        self.append(ex)
        return ex


class TracePoint:
    def __init__(self, coordinates):
        self.Coordinates = point(*coordinates)

    def SetCoordinates(self, p):
        self.Coordinates = point(p.X, p.Y, p.Z)

    def Blank(self):
        self.blanked = True


class Traceline(Object):
    def __init__(self, fake, explosion, start, end):
        super().__init__("TRACELINE")
        self.explosion = explosion
        self.OwningPart = fake.asm
        self.StartPoint = TracePoint(geom.xyz(start.Coordinates))
        self.EndPoint = TracePoint(geom.xyz(end.Coordinates))
        self.StartOffset = self.EndOffset = 0.0
        self.attributes = {}

    def AskExplosion(self):
        return self.explosion

    def SetAttribute(self, name, value):
        self.attributes[name] = value

    def GetStringAttribute(self, name):
        return self.attributes[name]

    def HasUserAttribute(self, name, *_):
        return name in self.attributes


# ---- the fake ------------------------------------------------------------------------------
class AssemblyFake:
    """Assembly state attached to a fake NX (``nx.assembly``)."""

    def __init__(self, nx, asm):
        self.nx = nx
        self.asm = asm
        self.root = None
        self.pivot_at_origin = True
        self.solver_status = STATUS.Solved
        self.on_solve = None
        self.recreate_on_edit = False
        self.patterns = Collection()
        self.pattern_builders = []
        self.explosions = Explosions(self)
        self.view_explosion = {}
        self.seed_reference_sets = True
        self.positioner = Positioner(self)
        self.deleted = []
        self.root = Component(self, asm, "ROOT", ([0.0, 0.0, 0.0], geom.IDENTITY), None)

    # occurrences
    def components(self):
        out = []

        def walk(node):
            for c in node.children:
                out.append(c)
                walk(c)

        walk(self.root)
        return out

    def add(self, prototype, name, pose, parent=None):
        self.asm.ComponentAssembly.RootComponent = self.root
        return Component(self, prototype, name, pose, parent or self.root)

    def remove_component(self, c):
        if c.Parent is not None and c in c.Parent.children:
            c.Parent.children.remove(c)
        self.deleted.append(c)

    def pattern_poses(self, seed, params):
        base = (seed.t, seed.r)
        if params["type"] == PATTERN.Linear:
            d1 = geom.unit(geom.xyz(params["x"].Vector))
            d2 = geom.unit(geom.xyz(params["y"].Vector)) if params["toggle"] else None
            out = []
            for j in range(params["n2"] if params["toggle"] else 1):
                for i in range(params["n"]):
                    step = geom.scale(d1, i * params["d"])
                    if j:
                        step = geom.add(step, geom.scale(d2, j * params["d2"]))
                    out.append((geom.add(seed.t, step), seed.r))
            return out
        axis = params["axis"]
        return [
            kpose.rotate_about(base, axis.origin, axis.direction, i * params["angle"])
            for i in range(params["cn"])
        ]

    # undo
    def state(self):
        comps = self.components()
        return {
            "root_children": {id(c): list(c.children) for c in [self.root, *comps] if c},
            "poses": {
                id(c): (
                    list(c.t),
                    [r[:] for r in c.r],
                    c.IsSuppressed,
                    c.Name,
                    c.ReferenceSet,
                    c.Prototype,
                )
                for c in comps
            },  # fmt: skip
            "comps": comps,
            "constraints": list(self.positioner.Constraints),
            "patterns": list(self.patterns),
            "explosions": list(self.explosions),
            "deltas": {id(e): dict(e.deltas) for e in self.explosions},
            "views": dict(self.view_explosion),
        }

    def restore(self, state):
        for c in state["comps"]:
            c.t, c.r, c.IsSuppressed, c.Name, c.ReferenceSet, c.Prototype = state["poses"][id(c)]
        for c in [self.root, *state["comps"]]:
            if c is not None and id(c) in state["root_children"]:
                c.children[:] = state["root_children"][id(c)]
        self.positioner.Constraints[:] = state["constraints"]
        self.patterns[:] = state["patterns"]
        self.explosions[:] = state["explosions"]
        for e in self.explosions:
            e.deltas = dict(state["deltas"].get(id(e), {}))
        self.view_explosion = dict(state["views"])
        if not self.root.children:
            self.asm.ComponentAssembly.RootComponent = None


def _wire(nx, fake):
    asm = fake.asm
    session = nx.session
    marks = {}
    set_mark, undo = session.SetUndoMark, session.UndoToMark

    def set_undo_mark(*args):
        mark = set_mark(*args)
        marks[mark] = fake.state()
        return mark

    def undo_to_mark(mark, *args):
        undo(mark, *args)
        fake.restore(marks[mark])

    session.SetUndoMark, session.UndoToMark = set_undo_mark, undo_to_mark
    session.UpdateManager = NS(DoUpdate=lambda mark: 0, AddToDeleteList=fake.deleted.extend)

    def add_component(path, reference_set, name, origin, m, _layer):
        proto = next((p for p in session.Parts if str(p.FullPath) == str(path)), None)
        if proto is None:
            proto = FilePart(nx, path)
            proto.ReferenceSets = []
        if proto in (session.Parts.Work,):
            session.Parts.Work = asm
        session.Parts.Work = session.Parts.Display = asm
        c = fake.add(proto, name, (geom.xyz(origin), geom.rows(m)))
        c.ReferenceSet = reference_set
        return c, NS(Dispose=lambda: None)

    def move_component(c, shift, m):
        delta = geom.rows(m)
        if fake.pivot_at_origin:
            c.t = geom.add(geom.matvec(delta, c.t), geom.xyz(shift))
        else:
            c.t = geom.add(c.t, geom.xyz(shift))
        c.r = geom.matmul(delta, c.r)

    def suppress(values, on):
        for c in values:
            c.IsSuppressed = on
        return ErrorList()

    def replace_reference_set(c, name):
        c.ReferenceSet = name

    asm.ComponentAssembly = NS(
        RootComponent=None,
        AddComponent=add_component,
        MoveComponent=move_component,
        SuppressComponents=lambda values: suppress(values, True),
        UnsuppressComponents=lambda values: suppress(values, False),
        ReplaceReferenceSet=replace_reference_set,
        Positioner=fake.positioner,
        CreateComponentPatternBuilder=lambda pattern: PatternBuilder(fake, pattern),
        ComponentPatterns=NS(GetAllComponentPatterns=lambda: list(fake.patterns)),
        Explosions=fake.explosions,
        OpenOption=NS(ComponentOnly=1),
    )

    class Replace:
        def __init__(self):
            self.targets = []
            self.ComponentsToReplace = NS(Add=self.targets.append)
            self.ReplacementPart = None
            self.ReplaceAllOccurrences = True
            self.MaintainRelationships = False
            self.destroyed = False
            fake.replace_builders.append(self)

        def Commit(self):
            proto = next(
                (p for p in session.Parts if str(p.FullPath) == str(self.ReplacementPart)), None
            ) or FilePart(nx, self.ReplacementPart)
            for c in self.targets:
                c.Prototype = proto
            return None

        def GetErrorList(self):
            return ErrorList()

        def Destroy(self):
            self.destroyed = True

    fake.replace_builders = []
    asm.AssemblyManager = NS(CreateReplaceComponentBuilder=Replace)

    # reference sets
    asm.reference_sets = []

    def create_reference_set():
        members = list(fake.components()) if fake.seed_reference_sets else []
        rs = Object("REFERENCE_SET")
        rs.OwningPart = asm
        rs.members = members
        rs.AskAllDirectMembers = lambda: list(rs.members)
        rs.SetAddComponentsAutomatically = lambda *a: None
        rs.RemoveObjectsFromReferenceSet = lambda objs: [rs.members.remove(o) for o in objs]
        rs.AddObjectsToReferenceSet = lambda objs: rs.members.extend(
            o for o in objs if o not in rs.members
        )
        asm.reference_sets.append(rs)
        return rs

    asm.CreateReferenceSet = create_reference_set
    asm.GetAllReferenceSets = lambda: list(asm.reference_sets)

    # explosions, traces, points
    def ask_view_explosion(tag):
        return fake.view_explosion.get(int(tag), 0)

    def set_view_explosion(tag, ex_tag):
        fake.view_explosion[int(tag)] = int(ex_tag)

    def explosion_of(tag):
        return next(e for e in fake.explosions if int(e.Tag) == int(tag))

    def explode(ex_tag, comp_tag, m):
        explosion_of(ex_tag).deltas[int(comp_tag)] = [list(map(float, row)) for row in m]

    def unexplode(ex_tag, comp_tag):
        explosion_of(ex_tag).deltas.pop(int(comp_tag), None)

    nx.uf.Assem = NS(
        AskViewExplosion=ask_view_explosion, SetViewExplosion=set_view_explosion,
        ExplodeComponent=explode, UnexplodeComponent=unexplode,
        AskComponentData=lambda tag: ("D:/missing/part.prt", "", "", [], [], []),
    )  # fmt: skip
    handles = {}

    def handle_of(tag):
        handles[f"H{tag}"] = tag
        return f"H{tag}"

    nx.uf.Tag = NS(AskHandleFromTag=handle_of, AskTagOfHandle=lambda h: handles[h])
    nx.regenerated = []
    nx.uf.Disp = NS(RegenerateDisplay=lambda: nx.regenerated.append(1))
    asm.Tracelines = Collection()

    def create_traceline(ex, p0, _d0, p1, _d1, *_rest):
        line = Traceline(fake, ex, p0, p1)
        asm.Tracelines.append(line)
        return line

    asm.Tracelines.CreateAutomaticTraceline = create_traceline
    asm.Scalars = NS(CreateScalar=lambda value, *_: NS(Value=value))

    def create_point(*args):
        if len(args) == 1:
            return TracePoint(geom.xyz(args[0]))
        edge, scalar = args[0], args[1]
        c = edge.OwningComponent
        a, b = (geom.add(geom.matvec(c.r, p), c.t) for p in edge.local)
        f = scalar.Value / 100.0
        return TracePoint([x + f * (y - x) for x, y in zip(a, b, strict=True)])

    asm.Points = NS(CreatePoint=create_point)
    asm.Directions = NS(
        CreateDirection=lambda origin, vector, option: NS(Origin=origin, Vector=vector)
    )
    asm.Axes = NS(
        CreateAxis=lambda p, d, _u: NS(
            origin=geom.xyz(p.Coordinates), direction=geom.unit(geom.xyz(d.Vector))
        )
    )
    view = Object("Trimetric")
    view.OwningPart = asm
    view.Fit = lambda: nx.fitted.append(1)
    view.UpdateDisplay = lambda: None
    view.RenderingStyle = "shaded"
    nx.fitted = []
    asm.ModelingViews = Collection([view])
    asm.ModelingViews.WorkView = view
    asm.Annotations = NS(PartsLists=[], BendTables=[], TableSections=[])
    asm.DraftingViews = Collection()
    asm.DraftingViews.UpdateViews = lambda vs: nx.updated_views.append(list(vs))
    nx.updated_views = []
    asm.Drafting = NS(ExitDraftingApplication=lambda: None)
    nx.nx.Positioning = NS(Constraint=NS(Type=TYPE, Alignment=ALIGN, SolverStatus=STATUS))
    nx.nx.GeometricUtilities = NS(PatternDefinition=NS(PatternEnum=PATTERN))
    nx.nx.Scalar = NS(DimensionalityType=NS(NotSet=0))
    nx.nx.PointCollection = NS(PointOnCurveLocationOption=NS(PercentArcLength=1))
    nx.nx.AutomaticTraceline = NS(ModeOption=NS(Infer=0))
    nx.nx.NXObject = NS(AttributeType=NS(String=3))
    nx.nx.Edge = NS(EdgeType=NS(Linear=1, Circular=2))


def assembly_nx():
    """A fake NX with saved parts ``blk.prt``/``pin.prt`` and an empty assembly ``asm.prt``."""
    nx = fake_nx()
    root = services(nx).root
    with_parts(nx, root)
    blk = nx.new_part("parts/blk.prt")
    blk.Save()
    blk.ReferenceSets = [NS(Name="SOLID")]
    blk.GetAllReferenceSets = lambda: list(blk.ReferenceSets)
    pin = nx.new_part("parts/pin.prt")
    pin.Save()
    pin.GetAllReferenceSets = lambda: []
    asm = nx.new_part("asm.prt")
    fake = AssemblyFake(nx, asm)
    nx.assembly = fake
    nx.asm, nx.blk, nx.pin = asm, blk, pin
    nx.part = asm  # nx.tx (hooks called directly) works on the assembly
    nx.tx.mark = "hook-mark"  # deleters update under the caller's mark
    nx.tx.kind_of = services(nx).infer_kind
    _wire(nx, fake)
    return nx


def place(nx, prototype, name, translation=(0, 0, 0), rotation=None):
    """An occurrence added directly (no tool), with a reference for tool calls."""
    c = nx.assembly.add(prototype, name, (list(translation), rotation or geom.IDENTITY))
    c.ref = nx.ref_of(c, "component")
    return c
