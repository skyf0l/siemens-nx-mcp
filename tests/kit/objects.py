"""Fake NXOpen objects shared by every test rig (not a geometric kernel oracle).

The host ``rig`` fixture (``tests/kit/rig.py``), the other host test rigs and the
kit/domain unit tests use this one set of objects.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace as NS
from unittest.mock import Mock


def point(x=0, y=0, z=0):
    """An NX ``Point3d``/``Vector3d`` stand-in."""
    return NS(X=x, Y=y, Z=z)


def matrix(x_axis=(1, 0, 0), y_axis=(0, 1, 0), z_axis=(0, 0, 1)):
    """An NX ``Matrix3x3`` stand-in whose rows are the frame's X, Y and Z axes."""
    return NS(
        Xx=x_axis[0],
        Xy=x_axis[1],
        Xz=x_axis[2],
        Yx=y_axis[0],
        Yy=y_axis[1],
        Yz=y_axis[2],
        Zx=z_axis[0],
        Zy=z_axis[1],
        Zz=z_axis[2],
    )


class NXException(Exception):
    """A native NX error: carries ``ErrorCode`` like ``NXOpen.NXException``."""

    def __init__(self, code, message="NX error"):
        super().__init__(message)
        self.ErrorCode = code


class Object:
    ids = itertools.count(1)

    def __init__(self, name=""):
        self.Tag = next(self.ids)
        self.Name = name
        self.JournalIdentifier = f"OBJECT({self.Tag})"
        self.IsBlanked = False
        self.Color = 7
        self.transparency = 0
        self.highlighted = False

    def SetName(self, name):
        self.Name = name

    def Blank(self):
        self.IsBlanked = True

    def Unblank(self):
        self.IsBlanked = False

    def Highlight(self):
        self.highlighted = True

    def Unhighlight(self):
        self.highlighted = False


class Face(Object):
    pass


class Edge(Object):
    pass


class Curve(Object):
    pass


class Body(Object):
    def __init__(self, name="Body", box=None):
        super().__init__(name)
        self.box = box or [0, 0, 0, 10, 10, 10]
        self.IsSolidBody = True
        self.IsOccurrence = False
        self.faces = [Face("face")]
        self.edges = [Edge("edge")]
        self.volume = 1000

    def GetFaces(self):
        return self.faces

    def GetEdges(self):
        return self.edges


class Feature(Object):
    def __init__(self, name="Extrude", bodies=None):
        super().__init__(name)
        self.FeatureType = "EXTRUDE"
        self.bodies = bodies or []
        self.expressions = [NS(Name="p1", RightHandSide="10", Value=10)]
        # The feature's own faces and edges (NX Feature.GetFaces/GetEdges); None: every
        # face or edge of its bodies, as for a feature that made the whole body.
        self.own_faces = None
        self.own_edges = None

    def GetBodies(self):
        return self.bodies

    def GetFaces(self):
        if self.own_faces is not None:
            return list(self.own_faces)
        return [f for b in self.bodies for f in b.GetFaces()]

    def GetEdges(self):
        if self.own_edges is not None:
            return list(self.own_edges)
        return [e for b in self.bodies for e in b.GetEdges()]

    def GetExpressions(self):
        return self.expressions

    def GetParents(self):
        return []

    def GetChildren(self):
        return []


class Sketch(Object):
    Null = None
    ViewReorient = NS(FalseValue=False, TrueValue=True)
    UpdateLevel = NS(Model="model")
    InferConstraintsOption = NS(InferNoConstraints=False)
    Status = NS(UnderConstrained=1, WellConstrained=2, OverConstrained=3)
    ConstraintClass = NS(Any=0, Dimension=1)
    ConstraintType = NS(NoCon=0, Distance=1)

    def __init__(self, session, name="Sketch"):
        super().__init__(name)
        self.session = session
        self.Origin = point()
        self.Orientation = NS(Element=matrix())
        self.geometry = []
        self.constraints = []
        self.status = (1, 4)

    def Activate(self, *_):
        self.session.ActiveSketch = self

    def Deactivate(self, *_):
        self.session.ActiveSketch = None

    def AddGeometry(self, curve, *_):
        self.geometry.append(curve)

    def GetAllGeometry(self):
        return self.geometry

    def GetAllConstraintsOfType(self, *_):
        return self.constraints

    def GetConstraintsForGeometry(self, *_):
        return self.constraints

    def CalculateStatus(self):
        pass

    def GetStatus(self):
        return self.status


class Component(Object):
    def __init__(self, name, bodies=(), parent=None):
        super().__init__(name)
        self.Parent = parent
        self.IsSuppressed = False
        self.ReferenceSet = "Entire Part"
        self.children = []
        self.Prototype = NS(Bodies=list(bodies), FullPath=name + ".prt")
        self.position = point()
        self.rotation = matrix()
        self.occurrences = {}
        for b in bodies:
            o = Body(name + " occurrence", list(b.box))
            o.IsOccurrence = True
            o.OwningComponent = self
            self.occurrences[b.Tag] = o
        if parent:
            parent.children.append(self)

    def GetChildren(self):
        return self.children

    def FindOccurrence(self, body):
        return self.occurrences.get(body.Tag)

    def GetPosition(self):
        return self.position, self.rotation


class Collection(list):
    pass


class Expression(Object):
    """A Number expression with dependency queries (NX 2406 lacks parents)."""

    def __init__(self, name="height", formula="10", unit=None):
        super().__init__(name)
        self.RightHandSide = formula
        self.Value = 10.0
        self.Type = "Number"
        self.Units = unit
        self.IsNoEdit = self.IsRightHandSideLockedFromEdit = self.IsInterpartExpression = False
        self.parents = []
        self.dependents = []

    def GetValueUsingUnits(self, option):
        return getattr(self, "expression_value", self.Value)

    def GetExpressionParents(self):
        return self.parents

    def GetReferencingExpressions(self):
        return self.dependents


class Collector:
    """A selection collector that keeps the rules it was given."""

    def __init__(self):
        self.rules = []

    def ReplaceRules(self, rules, *_):
        self.rules = rules

    def GetRules(self):
        return self.rules


class Session:
    """Session seam with undo marks that snapshot every part's object lists."""

    def __init__(self):
        self.Parts = Collection()
        self.Parts.Work = self.Parts.Display = None
        self.ActiveSketch = None
        self.IsBatch = False
        self.ApplicationName = "UG_APP_MODELING"
        self.marks = {}
        self.mark_ids = itertools.count(1)
        self.UpdateManager = NS(DoUpdate=Mock(return_value=0))

    def ApplicationSwitchImmediate(self, name):
        self.ApplicationName = name

    def SetUndoMark(self, *_):
        mark = next(self.mark_ids)
        states = []
        for p in self.Parts:
            groups = [
                p.Bodies,
                p.Features,
                p.Curves,
                p.Sketches,
                p.DynamicSections,
                p.Notes,
                p.Labels,
            ]
            objs = list(itertools.chain.from_iterable(groups))
            attrs = [(o, o.IsBlanked, o.Color, o.transparency) for o in objs]
            states.append((p, [list(g) for g in groups], attrs, p.IsModified))
        self.marks[mark] = (states, self.ActiveSketch)
        return mark

    def UndoToMark(self, mark, *_):
        states, active = self.marks[mark]
        for p, groups, attrs, modified in states:
            for dest, values in zip(
                [p.Bodies, p.Features, p.Curves, p.Sketches, p.DynamicSections, p.Notes, p.Labels],
                groups,
                strict=True,
            ):
                dest[:] = values
            for obj, blank, color, transparency in attrs:
                obj.IsBlanked, obj.Color, obj.transparency = blank, color, transparency
            p.IsModified = modified
        self.ActiveSketch = active

    def DeleteUndoMark(self, mark, *_):
        self.marks.pop(mark, None)

    def DoesUndoMarkExist(self, mark, *_):
        return mark in self.marks


class Part(Object):
    def __init__(self, session, path, units="mm"):
        super().__init__(path.stem)
        self.FullPath = str(path)
        self.PartUnits = units
        self.IsModified = False
        self.Bodies = Collection()
        self.Features = Collection()
        self.Curves = Collection()
        self.Datums = Collection()
        self.CoordinateSystems = Collection()
        self.Sketches = Collection()
        self.DynamicSections = Collection()
        self.Notes = Collection()
        self.Labels = Collection()
        self.ComponentAssembly = NS(RootComponent=None)
        self.WCS = NS(CoordinateSystem=NS(Orientation=NS(Element=matrix())))
        self.ModelingViews = NS(
            WorkView=NS(
                Name="Work",
                Matrix=matrix(),
                Origin=point(),
                AbsoluteOrigin=point(),
                Scale=1,
                RenderingStyle="shaded",
                ActiveDynamicSection=None,
                DisplaySectioningToggle=False,
                Fit=Mock(),
                UpdateDisplay=Mock(),
            )
        )
        self.UnitCollection = NS(FindObject=lambda name: name)
        self.MeasureManager = NS(
            NewMassProperties=lambda _u, _a, b: NS(Volume=sum(x.volume for x in b), Dispose=Mock())
        )
        self.session = session
        session.Parts.append(self)
        session.Parts.Work = session.Parts.Display = self

    def Save(self, *_):
        from pathlib import Path

        Path(self.FullPath).parent.mkdir(parents=True, exist_ok=True)
        Path(self.FullPath).write_bytes(b"native part fixture\n")
        self.IsModified = False
        self.session.marks.clear()
        return NS(Dispose=Mock(), NumberUnsavedParts=0, NumberUnsavedObjects=0)

    def SaveAs(self, path):
        self.FullPath = path
        return NS(Dispose=Mock(), NumberUnsavedParts=0, NumberUnsavedObjects=0)

    def Close(self, *_):
        self.session.Parts.remove(self)
        if self.session.Parts.Work is self:
            self.session.Parts.Work = None
        if self.session.Parts.Display is self:
            self.session.Parts.Display = None
