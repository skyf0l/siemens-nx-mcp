"""Display extensions of the shared fake (``tests/kit``) for the display unit tests.

``display_nx()`` is a ``fake_nx`` whose work view orients, fits and takes explicit cameras,
whose session applies display modifications (colour, translucency; bodies pass them to
their faces), whose UF answers named colours, whose part has a small colour table (``PALETTE``:
``Colors.Find(index)`` with ``Label`` and ``GetRgb()``), whose image builders write PNG
headers (the viewport export at the window's own size, Studio at the requested size), and
whose part keeps dynamic sections edited through section builders.

The work view keeps ``AbsoluteOrigin`` (the viewport centre) consistent with ``Origin``
(its negated view-space coordinates) as NX does. ``Fit()`` frames the shown bodies and
the absolute origin (NX's fit counts the datum CSYS there); ``FitToObjects`` frames the
objects given (``fit_to_error`` makes it raise). Both set the scale to 2.5.
"""

from __future__ import annotations

import struct
from pathlib import Path
from types import SimpleNamespace as NS

from nx_mcp.kit import display, geom
from tests.kit import fake_nx
from tests.kit.objects import NXException, Object, matrix, point

WINDOW = (1012, 1794)  # the NX 2406 graphics window the viewport export ignores sizes for
STYLES = NS(Shaded=2, ShadedWithEdges=0, StaticWireframe=9, Studio=6)
COLORS = {"RED_NAME": 186, "BLUE_NAME": 211, "MEDIUM_GRAY_NAME": 130}
PALETTE = {
    1: ("White", (1.0, 1.0, 1.0)),
    87: ("Light Gray", (0.6, 0.6, 0.6)),
    130: ("Medium Gray", (0.4, 0.4, 0.4)),
    123: ("Light Steel Blue", (0.6, 0.8, 1.0)),
    186: ("Red", (1.0, 0.0, 0.0)),
    211: ("Blue", (0.0, 0.0, 1.0)),
    216: ("Black", (0.0, 0.0, 0.0)),
}


class Colors:
    """``part.Colors``: ``Find(index)`` of ``PALETTE`` (an undefined index raises)."""

    def Find(self, index):
        if index not in PALETTE:
            raise NXException(3520016, "Invalid color index")
        label, (r, g, b) = PALETTE[index]
        return NS(Label=label, Id=index, GetRgb=lambda: NS(R=r, G=g, B=b))


def png(path, size):
    header = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", *size)
    Path(path).write_bytes(header + b"\0" * 16)


def nx_matrix(rotation):
    """The NX matrix of a row-major rotation (its rows are the rotation's columns)."""
    cols = [[rotation[r][c] for r in range(3)] for c in range(3)]
    return matrix(*cols)


class View:
    """A model work view: Orient, SetRotationTranslationScale, Fit, sections."""

    def __init__(self, name="Trimetric", part=None):
        self.Name = name
        self.part = part
        self.Matrix = nx_matrix(display.ORIENTATION_ROTATIONS["trimetric"])
        self.Origin = point(-1.0, -2.0, -3.0)
        self.Scale = 1.5
        self.RenderingStyle = 0
        self.ActiveDynamicSection = None
        self.DisplaySectioningToggle = False
        self.fits = 0
        self.ignore_scale = False
        self.fit_to = None
        self.fit_to_error = None

    def Orient(self, canned, adjust):
        self.Matrix = nx_matrix(display.ORIENTATION_ROTATIONS[canned])
        if adjust == "fit":
            self.Fit()

    def SetRotationTranslationScale(self, m, p, s):
        self.Matrix, self.Origin = m, p
        if not self.ignore_scale:
            self.Scale = s

    @property
    def AbsoluteOrigin(self):
        """The viewport centre: Origin is its negated view-space coordinates."""
        r, o = geom.rows(self.Matrix), geom.xyz(self.Origin)
        return point(*(-sum(r[i][j] * o[j] for j in range(3)) for i in range(3)))

    def _frame(self, boxes):
        self.fits += 1
        self.Scale = 2.5
        if not boxes:
            self.Origin = point(-5.0, -5.0, -5.0)
            return
        low = [min(b[i] for b in boxes) for i in range(3)]
        high = [max(b[i + 3] for b in boxes) for i in range(3)]
        centre = [(a + b) / 2 for a, b in zip(low, high, strict=True)]
        self.Origin = point(*display.origin_for(geom.rows(self.Matrix), centre))

    def Fit(self):
        """Everything shown, the datum CSYS at the absolute origin included."""
        shown = [b for b in getattr(self.part, "Bodies", []) if not b.IsBlanked]
        boxes = [list(b.box) for b in shown if hasattr(b, "box")]
        self._frame(boxes + [[0.0] * 6] if boxes else [])

    def FitToObjects(self, objects):
        if self.fit_to_error is not None:
            raise self.fit_to_error
        self.fit_to = list(objects)
        self._frame([list(b.box) for b in objects if hasattr(b, "box")])

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
        self.blanked = [bool(o.IsBlanked) for o in getattr(self.fake, "watch", [])]
        self.matrix = self.fake.work_view.Matrix
        self.modified = bool(self.fake.part.IsModified)
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
    view = View(part=nx.part)
    nx.work_view = view
    nx.part.Colors = Colors()
    if not hasattr(nx.uf, "ModlGeneral"):  # bodies' boxes for the fit's read-back
        boxes = lambda tag: list(next(b.box for b in nx.part.Bodies if b.Tag == tag))  # noqa: E731
        nx.uf.ModlGeneral = NS(AskBoundingBox=boxes)
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
        nx, a[0] if len(a) == 2 else a[-1].ActiveDynamicSection, a[-1]
    )  # NX 2406: (view) alone edits the view's active section, (Null, view) makes one

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
        DynamicSection=NS(Null=None),
        DynamicSectionTypes=NS(
            Type=NS(OnePlane=1), CoordinateSystem=NS(Absolute=1), Clip=NS(Section=1)
        ),
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
