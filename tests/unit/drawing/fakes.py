"""Drawing extensions of the shared fake (``tests/kit``) for the drawing unit tests.

``drawing_nx(units)`` is a ``fake_nx`` whose NXOpen namespace auto-creates enum members,
whose sheets have scale, projection and units, whose drafting views keep a reference
point, scale and an optional locked (aligned) axis, and whose UF answers the drafting
queries the drawing tools make (MoveView, AskViewScale, AskViewBorders,
MapModelToDrawing, AskViewDependentStatus, handles, Eval of lines and arcs).
"""

from __future__ import annotations

import math
from types import SimpleNamespace as NS
from unittest.mock import Mock

from tests.kit import fake_nx
from tests.kit.fake import DrawingSheet, Factories, Node
from tests.kit.objects import Object, matrix, point

SHEET_UNITS = NS(Millimeters="mm", Inches="in")
ANGLES = NS(FirstAngle="first", ThirdAngle="third")


class AutoNS:
    """A namespace whose unknown attributes are stable auto-vivifying nodes."""

    def __init__(self, base, path="NXOpen"):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_path", path)
        object.__setattr__(self, "_made", {})

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        if hasattr(self._base, name):
            value = getattr(self._base, name)
            if isinstance(value, NS):
                return self._made.setdefault(name, AutoNS(value, f"{self._path}.{name}"))
            return value
        return self._made.setdefault(name, Node(f"{self._path}.{name}"))

    def __setattr__(self, name, value):
        setattr(self._base, name, value)


class Attributes:
    """String user attributes like NXOpen objects have them."""

    def _attrs(self):
        if not hasattr(self, "_attr_store"):
            self._attr_store = {}
        return self._attr_store

    def HasUserAttribute(self, name, *_):
        return name in self._attrs()

    def GetStringAttribute(self, name):
        return self._attrs()[name]

    def SetAttribute(self, name, value):
        self._attrs()[name] = value


def _view_class(native):
    return type(native, (Attributes, Object), {})


VIEW_CLASSES = {
    n: _view_class(n) for n in ("BaseView", "ProjectedView", "SectionView", "DetailView")
}


class Sheets(list):
    CurrentDrawingSheet = None


class Views(Factories):
    """``part.DraftingViews``: builder factories plus UpdateViews."""

    def __init__(self, fake):
        super().__init__(fake)
        self.updated = []

    def UpdateViews(self, views):
        self.updated.append(list(views))


def make_sheet(nx, name="SHT1", units="mm", size=(420.0, 297.0), scale=1.0, angle="third"):
    sheet = nx.sheet(name, units=units)
    sheet.Length, sheet.Height = size
    sheet.scale = [scale, 1.0]
    sheet.ProjectionAngle = angle
    sheet.GetScale = lambda: tuple(sheet.scale)

    def set_parameters(height, length, numerator, denominator, units_, angle_):
        sheet.scale = [numerator, denominator]
        sheet.ProjectionAngle = angle_

    sheet.SetParameters = set_parameters
    sheet.View = NS(Name=f"{name}@0")
    return sheet


def make_view(nx, sheet, name="Top@1", pos=(100.0, 100.0), scale=1.0, native="BaseView",
              axes=((1, 0, 0), (0, 1, 0), (0, 0, 1)), locked=None):  # fmt: skip
    view = VIEW_CLASSES[native](name)
    view.OwningPart, view.IsOccurrence = nx.part, False
    view.pos, view.scale, view.locked = list(pos), float(scale), locked
    view.Matrix = matrix(*axes)
    view.GetDrawingReferencePoint = lambda: point(view.pos[0], view.pos[1], 0.0)
    view.DependentDisplay = NS(Erase=Mock(), RemoveErasureOnObjectAndSubobjects=Mock())
    view.ref = nx.ref_of(view, "drawing_view")
    sheet.views.append(view)
    nx.views[view.Tag] = view
    return view


DEFAULT_STYLE = {
    "ViewStyleHiddenLines": {"HiddenLine": True, "Font": 2, "Width": "Normal", "SelfHidden": True},
    "ViewStyleVisibleLines": {"VisibleFont": 1, "VisibleWidth": "Normal"},
    "ViewStyleSmoothEdges": {"SmoothEdge": True, "Font": 1},
    "ViewStyleGeneral": {"Centerlines": True},
    "ViewStyleShading": {"RenderingStyle": "Wireframe"},
}


class StyleBuilder:
    """``CreateDrawingEditViewSettingsBuilder``: reads and commits a view's style."""

    def __init__(self, nx, views):
        self.nx = nx
        self.view = views[0]
        style = getattr(self.view, "style", None) or {g: dict(v) for g, v in DEFAULT_STYLE.items()}
        self.ViewStyle = NS(**{g: NS(**v) for g, v in style.items()})
        nx.style_builders.append(self)

    def InheritSettingsFromSelectedObjects(self, view):
        pass

    def Commit(self):
        self.view.style = {g: dict(vars(getattr(self.ViewStyle, g))) for g in DEFAULT_STYLE}

    def Destroy(self):
        self.destroyed = True


def drawing_nx(units="mm"):
    nx = fake_nx(units=units)
    nx.nx = AutoNS(nx.nx)
    nx.nx.Drawings.DrawingSheet.Unit = SHEET_UNITS
    nx.nx.Drawings.DrawingSheet.ProjectionAngleType = ANGLES
    nx.nx.NXObject = NS(AttributeType=NS(String=1))
    nx.views = {}
    nx.dependent = {}  # tag -> (status, view name)
    part = nx.part
    part.DrawingSheets = Sheets()
    part.DraftingViews = Views(nx)
    part.DraftingDrawingSheets = Factories(nx)
    part.DraftingManager = NS(TitleBlocks=[])
    part.Dimensions = Factories(nx)
    part.Annotations = NS(PartsLists=[], BendTables=[])
    nx.style_builders = []
    part.SettingsManager = NS(
        CreateDrawingEditViewSettingsBuilder=lambda views: StyleBuilder(nx, views)
    )
    nx.nx.Preferences = NS(
        Font=NS(ValueOf=lambda v: v, Dashed=2, Solid=1),
        Width=NS(Thin="Thin", Normal="Normal", Thick="Thick", Original="Original"),
        ShadingRenderingStyleOption=NS(
            FullyShaded="FullyShaded", PartiallyShaded="PartiallyShaded", Wireframe="Wireframe"
        ),
    )
    part.ModelingViews._views.FindObject = lambda name: NS(Name=name)
    nx.make_sheet = lambda *a, **k: make_sheet(nx, *a, **k)
    nx.make_view = lambda *a, **k: make_view(nx, *a, **k)

    def move(tag, p):
        view = nx.views[int(tag)]
        for i in (0, 1):
            if view.locked != i:
                view.pos[i] = float(p[i])

    def set_scale(tag, s):
        nx.views[int(tag)].scale = float(s)

    def borders(tag):
        v = nx.views[int(tag)]
        return [v.pos[0] - 10 * v.scale, v.pos[1] - 5 * v.scale, v.pos[0] + 10 * v.scale,
                v.pos[1] + 5 * v.scale]  # fmt: skip

    def to_drawing(tag, p):
        v = nx.views[int(tag)]
        m = v.Matrix
        x = sum(a * b for a, b in zip(p, (m.Xx, m.Xy, m.Xz), strict=True))
        y = sum(a * b for a, b in zip(p, (m.Yx, m.Yy, m.Yz), strict=True))
        return [v.pos[0] + v.scale * x, v.pos[1] + v.scale * y]

    nx.uf.Draw.MoveView = move
    nx.uf.Draw.SetViewScale = set_scale
    nx.uf.Draw.AskViewScale = lambda tag: (0, nx.views[int(tag)].scale)
    nx.uf.Draw.AskViewBorders = borders
    nx.uf.View = NS(
        MapModelToDrawing=to_drawing,
        AskViewDependentStatus=lambda tag: nx.dependent.get(int(tag), (0, "")),
        AskVdeData=lambda tag: (0, []),
    )
    nx.uf.Tag = NS(AskHandleFromTag=lambda t: f"h{int(t)}", AskTagOfHandle=lambda h: int(h[1:]))
    nx.uf.Assem = NS(AskViewExplosion=lambda tag: 0, SetViewExplosion=Mock())
    nx.uf.Tabnot = NS(AskNmSections=lambda tag: 0)
    return nx


# ---- UF Eval of straight and circular edges -------------------------------------------------


def line_edge(nx, start, end):
    edge = nx.edge(start, end)
    edge.curve = ("line", list(map(float, start)), list(map(float, end)))
    return edge


def arc_edge(nx, center, radius, normal=(0, 0, 1), full=True):
    e = nx.edge(center, center, linear=False)
    n = [float(v) for v in normal]
    x_axis = [1.0, 0.0, 0.0] if abs(n[0]) < 0.9 else [0.0, 1.0, 0.0]
    dot = sum(a * b for a, b in zip(x_axis, n, strict=True))
    x_axis = [a - dot * b for a, b in zip(x_axis, n, strict=True)]
    length = math.sqrt(sum(a * a for a in x_axis))
    x_axis = [a / length for a in x_axis]
    y_axis = [n[1] * x_axis[2] - n[2] * x_axis[1], n[2] * x_axis[0] - n[0] * x_axis[2],
              n[0] * x_axis[1] - n[1] * x_axis[0]]  # fmt: skip
    e.curve = ("arc", list(map(float, center)), float(radius), x_axis, y_axis,
               2 * math.pi if full else math.pi / 2)  # fmt: skip
    return e


def install_eval(nx, edges):
    """UF Eval over ``edges`` (``line_edge``/``arc_edge``); parameters run 0..1."""
    by_tag = {int(e.Tag): e for e in edges}

    def at(handle, t):
        kind, *data = by_tag[handle].curve
        if kind == "line":
            a, b = data
            return [[a[i] + t * (b[i] - a[i]) for i in range(3)], [0, 0, 0]]
        center, radius, xa, ya, span = data
        angle = t * span
        return [
            [
                center[i] + radius * (math.cos(angle) * xa[i] + math.sin(angle) * ya[i])
                for i in range(3)
            ],  # fmt: skip
            [0, 0, 0],
        ]

    def arc(handle):
        _, center, radius, xa, _ya, _span = by_tag[handle].curve
        return NS(Center=center, Radius=radius, XAxis=xa)

    nx.uf.Eval = NS(
        Initialize=lambda tag: int(tag),
        Initialize2=lambda tag: int(tag),
        AskLimits=lambda h: (0.0, 1.0),
        EvaluateUnitVectors=at,
        IsLine=lambda h: by_tag[h].curve[0] == "line",
        IsArc=lambda h: by_tag[h].curve[0] == "arc",
        AskArc=arc,
        Free=lambda h: None,
    )


__all__ = ["DrawingSheet", "arc_edge", "drawing_nx", "install_eval", "line_edge"]
