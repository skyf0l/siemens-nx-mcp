"""Display extensions of the shared fake (``tests/kit``) for the display unit tests.

``display_nx()`` is a ``fake_nx`` whose work view orients, fits and takes explicit cameras,
whose session applies display modifications (colour, translucency; bodies pass them to
their faces), whose UF answers named colours, whose image builders write PNG headers
(the viewport export at the window's own size, Studio at the requested size), and whose
part keeps dynamic sections edited through section builders.
"""

from __future__ import annotations

import struct
from pathlib import Path
from types import SimpleNamespace as NS

from nx_mcp.kit import display
from tests.kit import fake_nx
from tests.kit.objects import Object, matrix, point

WINDOW = (1012, 1794)  # the NX 2406 graphics window the viewport export ignores sizes for
STYLES = NS(Shaded=2, ShadedWithEdges=0, StaticWireframe=9, Studio=6)
COLORS = {"RED_NAME": 186, "BLUE_NAME": 211, "MEDIUM_GRAY_NAME": 130}


def png(path, size):
    header = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", *size)
    Path(path).write_bytes(header + b"\0" * 16)


def nx_matrix(rotation):
    """The NX matrix of a row-major rotation (its rows are the rotation's columns)."""
    cols = [[rotation[r][c] for r in range(3)] for c in range(3)]
    return matrix(*cols)


class View:
    """A model work view: Orient, SetRotationTranslationScale, Fit, sections."""

    def __init__(self, name="Trimetric"):
        self.Name = name
        self.Matrix = nx_matrix(display.ORIENTATION_ROTATIONS["trimetric"])
        self.Origin = point(-1.0, -2.0, -3.0)
        self.AbsoluteOrigin = point(1.0, 2.0, 3.0)
        self.Scale = 1.5
        self.RenderingStyle = 0
        self.ActiveDynamicSection = None
        self.DisplaySectioningToggle = False
        self.fits = 0
        self.ignore_scale = False

    def Orient(self, canned, adjust):
        self.Matrix = nx_matrix(display.ORIENTATION_ROTATIONS[canned])
        if adjust == "fit":
            self.Fit()

    def SetRotationTranslationScale(self, m, p, s):
        self.Matrix, self.Origin = m, p
        if not self.ignore_scale:
            self.Scale = s

    def Fit(self):
        self.fits += 1
        self.Scale = 2.5
        self.Origin = point(-5.0, -5.0, -5.0)

    def UpdateDisplay(self):
        pass


class Modification:
    def __init__(self, fake):
        self.fake = fake
        self.NewColor = self.NewTranslucency = None
        self.disposed = False

    def Apply(self, objects):
        self.fake.modifications.append(list(objects))
        for obj in objects:
            targets = [obj, *getattr(obj, "faces", [])]
            for target in targets:
                if self.NewColor is not None:
                    target.Color = self.NewColor
                if self.NewTranslucency is not None:
                    target.transparency = self.NewTranslucency

    def Dispose(self):
        self.disposed = True


class Section(Object):
    def __init__(self, name):
        super().__init__(name)
        self.origin, self.normal, self.cap, self.clip = [0.0, 0.0, 0.0], [0.0, 0.0, 1.0], True, True


class SectionBuilder:
    def __init__(self, fake, section, view):
        self.fake, self.section, self.view = fake, section, view
        state = section or Section("")
        self.origin, self.normal = list(state.origin), list(state.normal)
        self.ShowCap, self.ShowClip, self.name = state.cap, state.clip, state.Name
        self.destroyed = False

    def GetOrigin(self):
        return point(*self.origin)

    def GetNormal(self):
        return point(*self.normal)

    def SetOrigin(self, p):
        self.origin = [p.X, p.Y, p.Z]

    def SetNormal(self, v):
        self.normal = [v.X, v.Y, v.Z]

    def SetName(self, name):
        self.name = name

    def Commit(self):
        if self.fake.section_error is not None:
            raise self.fake.section_error
        target = self.section or Section(self.name)
        target.origin = list(self.origin)
        target.normal = self.fake.section_normal or list(self.normal)
        target.cap, target.clip, target.Name = self.ShowCap, self.ShowClip, self.name
        sections = self.fake.part.DynamicSections
        if target not in sections:
            sections.append(target)
            self.view.ActiveDynamicSection = target  # NX 2406: a new section is active
            self.view.DisplaySectioningToggle = True
        return target

    def Destroy(self):
        self.destroyed = True


class Capture:
    """Image export or Studio capture builder: writes a PNG header on Commit."""

    def __init__(self, fake, kind):
        self.fake, self.kind = fake, kind
        self.SourceType = NS(WorkView=1)
        self.UnitsEnumType = NS(Pixels=1)
        self.FileFormats = NS(Png="png")
        self.BackgroundOptions = NS(CustomColor="custom", Original="original", Transparent="clear")
        self.background_color = None
        self.destroyed = False
        fake.captures.append(self)

    def SetImageDimensionsInteger(self, dims):
        self.dims = list(dims)

    def SetCustomBackgroundColor(self, color):
        self.background_color = list(color)

    def Commit(self):
        if self.fake.capture_error is not None:
            raise self.fake.capture_error
        self.styled = self.fake.work_view.RenderingStyle
        if self.kind == "studio":
            png(self.NativeFileBrowser, (self.dims[1], self.dims[0]))
        else:
            png(self.FileName, WINDOW)

    def Destroy(self):
        self.destroyed = True


class Lighting:
    def __init__(self):
        self.LightsShadedViewsLightingCollection = "user"
        self.LightingCollectionType = NS(**{f"Lighting{i}": f"L{i}" for i in range(1, 6)})
        self.committed = []
        self.destroyed = False

    def Commit(self):
        self.committed.append(self.LightsShadedViewsLightingCollection)

    def Destroy(self):
        self.destroyed = True


def display_nx(units="mm"):
    nx = fake_nx(units)
    view = View()
    nx.work_view = view
    nx.modifications, nx.captures, nx.lights = [], [], []
    nx.capture_error = nx.section_error = nx.section_normal = None
    nx.part.ModelingViews._views = NS(WorkView=view)
    sheet_view = NS(Name="SHT1@0", Matrix=matrix(), Origin=point(-210, -148.5, 0), Scale=0.6)
    sheet_view.Fit = lambda: setattr(sheet_view, "fitted", True)

    def lighting(_):
        lights = Lighting()
        nx.lights.append(lights)
        return lights

    nx.part.Views = NS(
        WorkView=sheet_view,
        CreateImageExportBuilder=lambda: Capture(nx, "viewport"),
        CreateStudioImageCaptureBuilder=lambda: Capture(nx, "studio"),
        CreateLighting=lighting,
    )
    nx.part.DynamicSections.CreateSectionBuilder = lambda *a: SectionBuilder(
        nx, a[0] if len(a) == 2 else None, a[-1]
    )

    def delete(_flag, targets):
        for t in targets:
            nx.part.DynamicSections.remove(t)
        if view.ActiveDynamicSection in targets:
            view.ActiveDynamicSection = None  # NX 2406 leaves the toggle on

    nx.part.DynamicSections.DeleteSections = delete
    nx.session.DisplayManager = NS(NewDisplayModification=lambda: Modification(nx))
    nx.nx.View = NS(
        Canned=NS(**{v: k for k, v in display.ORIENTATIONS.items()}),
        ScaleAdjustment=NS(Fit="fit", Current="current"),
        RenderingStyleType=STYLES,
    )
    nx.nx.Display = NS(
        DynamicSectionTypes=NS(
            Type=NS(OnePlane=1), CoordinateSystem=NS(Absolute=1), Clip=NS(Section=1)
        )
    )
    nx.uf.Disp = NS(
        ColorName=NS(**{k: k for k in display.COLOR_NAMES.values()}),
        AskClosestColorInDisplayedPart=lambda name: COLORS[name],
    )
    return nx


def show_sheet(nx, name="SHT1"):
    """Show a drawing sheet: the model view is gone while it is displayed."""
    sheet = nx.sheet(name)
    nx.part.DrawingSheets.CurrentDrawingSheet = sheet
    nx.drawing_work_view = True
    return sheet
