"""Part-file seams for ``fake_nx``: ``with_parts(nx)`` adds files, translators and clone.

It wires what the parts and files domain needs onto a ``FakeNX`` without changing
the shared fake for other domains:

- ``session.Parts``: ``FileNew`` (a builder), ``OpenBase``, ``SetDisplay``,
  ``SetWork``, ``LoadOptions``; parts it makes are :class:`FilePart` (``Save`` and
  ``SaveAs`` write the file, ``Close`` refuses a part a loaded assembly uses
  only when NX would).
- ``nx.assemble(top, [prototype, ...])``: a fake assembly whose occurrences use the
  given parts; ``OpenBase`` of a file in ``nx.structure`` loads its prototypes.
- ``session.DexManager``: ``CreateStepCreator`` writes a STEP file with one
  ``MANIFOLD_SOLID_BREP`` per exported solid (the display part, in memory) and a
  ``.log``; ``CreateStep214Importer`` adds ``nx.import_bodies`` bodies to the work part.
- ``uf.Clone``: copies files by the names set with ``SetNaming`` and records the
  copied assembly structure.
- ``uf.Eval``: lines and arcs from ``nx.curves`` (``nx.line``/``nx.arc``).

NX 2406 behaviour mirrored: ``FileNew`` leaves the display part empty when
``nx.file_new_keeps_display`` is False (the default here).
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path
from types import SimpleNamespace as NS

from tests.kit import neutral_files
from tests.kit.objects import Body, Collection, NXException, Object, Part, point

ENUMS = {
    "SaveComponents": NS(FalseValue=False, TrueValue=True),
    "CloseAfterSave": NS(FalseValue=False, TrueValue=True),
    "CloseWholeTree": NS(FalseValue=False, TrueValue=True),
    "CloseModified": NS(CloseModified=1, DontCloseModified=0, UseResponses=2),
}


def _status():
    return NS(
        Dispose=lambda: None, NumberUnsavedParts=0, NumberUnsavedObjects=0, NumberUnloadedParts=0
    )


class FilePart(Part):
    """A part with a file: ``Save``/``SaveAs`` write it; ``Close`` unloads it."""

    def __init__(self, fake, path, units="mm"):
        super().__init__(fake.session, Path(path), units)
        self.fake = fake
        self.SaveOptions = NS(DrawingCgmData=False)
        self.DrawingSheets = Collection()
        self.DrawingSheets.CurrentDrawingSheet = None
        self.Drafting = NS(ExitDraftingApplication=lambda: None)
        self.ComponentAssembly = NS(RootComponent=None, OpenOption=NS(ComponentOnly=1))
        self.ComponentAssembly.OpenComponents = lambda option, comps: (_status(), None)
        self.WCS.CoordinateSystem.Origin = point()

    def _write(self):
        Path(self.FullPath).parent.mkdir(parents=True, exist_ok=True)
        Path(self.FullPath).write_bytes(f"NX part {self.Name} {len(self.Bodies)}".encode())

    def Save(self, *_):
        if self.fake.save_error is not None:
            raise self.fake.save_error
        self._write()
        self.IsModified = False
        self.session.marks.clear()
        return _status()

    def SaveAs(self, path):
        if Path(path).exists():
            raise NXException(3, "File already exists")
        self.FullPath = str(path)
        self.Name = Path(path).stem
        self._write()
        self.IsModified = False
        return _status()

    def Close(self, *_):
        for other in list(self.session.Parts):
            if other is self:
                continue
            if any(c.Prototype is self for c in self.fake.occurrences(other)):
                raise NXException(4, "Part is used by a loaded assembly")
        super().Close()
        # NX unloads prototypes that no loaded part uses any more.
        for proto in [c.Prototype for c in self.fake.occurrences(self)]:
            used = any(
                c.Prototype is proto
                for other in self.session.Parts
                for c in self.fake.occurrences(other)
            )
            if proto in self.session.Parts and not used:
                proto.Close()


class Occurrence(Object):
    def __init__(self, prototype, name):
        super().__init__(name)
        self.Prototype = prototype
        self.IsSuppressed = False
        self.children = []

    def GetChildren(self):
        return self.children


class FileNew(NS):
    def __init__(self, fake):
        super().__init__(
            TemplateFileName=None, Units=None, NewFileName=None, DisplayPartOption=None
        )
        self._fake = fake
        self.destroyed = False

    def Commit(self):
        fake = self._fake
        if any(Path(p.FullPath).stem.casefold() == Path(self.NewFileName).stem.casefold()
               for p in fake.session.Parts):  # fmt: skip
            raise NXException(5, "File already exists")
        display = fake.session.Parts.Display
        part = FilePart(fake, self.NewFileName, "mm" if self.Units == "mm" else "in")
        part.template = self.TemplateFileName
        if not fake.file_new_keeps_display:
            fake.session.Parts.Display = display  # NX 2406: display part unchanged
        return part

    def Destroy(self):
        self.destroyed = True


class StepCreator(NS):
    def __init__(self, fake):
        super().__init__(
            SettingsFile=None, ExportFrom=None, LayerMask=None, OutputFile=None,
            FileSaveFlag=True, ProcessHoldFlag=False, InputFile=None, ExportAs=1,
            ObjectTypes=NS(Solids=False, Surfaces=False, Curves=True),
        )  # fmt: skip
        self._fake = fake
        self.selected = []
        self.ExportSelectionBlock = NS(
            SelectionScope=None, SelectionComp=NS(Add=lambda objs: self.selected.extend(objs) or True)
        )  # fmt: skip
        fake.step_creators.append(self)

    def Commit(self):
        fake = self._fake
        if fake.export_error is not None:
            raise fake.export_error
        part = fake.session.Parts.Display
        bodies = self.selected or [b for b in part.Bodies if b.IsSolidBody]
        out = Path(self.OutputFile)
        schema = "AUTOMOTIVE_DESIGN { 1 0 10303 214 3 1 1 1 }"
        if self.ExportAs == 2:  # Ap242
            if not fake.ap242_licensed:  # NX 2406 without the AP242 license
                with Path(fake.session.LogFile.FileName).open("a") as syslog:
                    syslog.write("STEP AP242 License not found\n")
                raise NXException(2, "Interrupt")
            schema = "AP242_MANAGED_MODEL_BASED_3D_ENGINEERING_MIM_LF { 1 0 10303 442 1 1 4 }"
        if fake.export_writes:
            out.write_bytes(neutral_files.step(len(bodies), _units(fake, part), schema))
        out.with_suffix(".log").write_text("translator log\n")
        if self.FileSaveFlag:
            part.Save()

    def Destroy(self):
        self.destroyed = True


def _units(fake, part):
    return "mm" if part.PartUnits == fake.nx.BasePart.Units.Millimeters else "in"


def _exact(box):
    corner = list(box[:3])
    lengths = [box[3 + i] - box[i] for i in range(3)]
    return corner, [[1, 0, 0], [0, 1, 0], [0, 0, 1]], lengths


class Translator(NS):
    """IGES, Parasolid, STL and JT exporters: write ``neutral_files`` samples of the
    display part's bodies (``body.box``) as NX would, in memory, without saving."""

    def __init__(self, fake, **props):
        super().__init__(**props)
        self._fake = fake
        self.selected = []
        fake.exporters.append(self)

    def _selector(self):
        return NS(SelectionScope=None, SelectionComp=NS(Add=self._add))

    def _add(self, objs):
        self.selected.extend(objs)
        return True

    def Commit(self):
        fake = self._fake
        if fake.export_error is not None:
            raise fake.export_error
        part = fake.session.Parts.Display
        boxes = [list(b.box) for b in (self.selected or list(part.Bodies))]
        out = Path(self._output())
        if fake.export_writes:
            name, data = self._file(out, boxes, _units(fake, part))
            Path(name).write_bytes(data)
        out.with_suffix(".log").write_text("translator log\n")
        if getattr(self, "FileSaveFlag", False):
            part.Save()

    def _output(self):
        return self.OutputFile

    def Destroy(self):
        self.destroyed = True


class IgesCreator(Translator):
    def __init__(self, fake):
        super().__init__(
            fake, SettingsFile=None, ExportFrom=None, InputFile=None, LayerMask=None,
            OutputFile=None, FileSaveFlag=True, ProcessHoldFlag=False,
            ObjectTypes=NS(Solids=False, Surfaces=False, Curves=True),
        )  # fmt: skip
        self.ExportSelectionBlock = self._selector()

    def _file(self, out, boxes, units):
        return out, neutral_files.iges(boxes, units)


class ParasolidExporter(Translator):
    def __init__(self, fake):
        super().__init__(
            fake, ExportFrom=None, InputFile=None, OutputFile=None, ProcessHoldFlag=False,
            ObjectTypes=NS(Solids=False, Surfaces=False),
        )  # fmt: skip
        self.ExportSelectionBlock = self._selector()

    def _file(self, out, boxes, units):
        return out, neutral_files.parasolid(boxes, binary=out.suffix == ".x_b")


class StlCreator(Translator):
    def __init__(self, fake):
        super().__init__(fake, OutputType=0, ChordalTol=0.058, AngularTol=18.0, OutputFile=None)
        self.ExportSelectionBlock = NS(Add=self._add)

    def _file(self, out, boxes, units):
        binary = self.OutputType == 0
        name = out if binary else Path(str(out) + ".txt")  # NX appends .txt to ASCII STL
        return name, neutral_files.stl(boxes, binary=binary)


class JtCreator(Translator):
    def __init__(self, fake):
        super().__init__(fake, JtfileStructure=None, OutputJtFile="")
        self.SelectBody = NS(Add=self._add)

    def _output(self):
        return self.OutputJtFile

    def _file(self, out, boxes, units):
        return out, neutral_files.jt(boxes, {"mm": "millimeters", "in": "inches"}[units])


class StepImporter(NS):
    def __init__(self, fake):
        super().__init__(
            SettingsFile=None, InputFile=None, OutputFile=None, ImportTo=None,
            FileOpenFlag=True, FlattenAssembly=None, SimplifyGeometry=True,
            ProcessHoldFlag=False, ObjectTypes=NS(Solids=False, Surfaces=False, Curves=False),
        )  # fmt: skip
        self._fake = fake
        fake.step_importers.append(self)

    def Commit(self):
        fake = self._fake
        if self.OutputFile is None:  # NX 2406: refuses a work-part import without it
            raise NXException(4625016, "Unable to import selected file to work part")
        Path(self.OutputFile).with_suffix(".log").write_text("import log\n")
        part = fake.session.Parts.Work
        for _ in range(fake.import_bodies):
            part.Bodies.append(Body("IMPORTED"))
        part.IsModified = True

    def Destroy(self):
        self.destroyed = True


class Clone:
    OperationClass = NS(CLONE_OPERATION=1)
    Action = NS(CLONE=1)
    NamingTechnique = NS(USER_NAME=1)

    def __init__(self, fake):
        self.fake = fake
        self.names = {}
        self.calls = []

    def Initialise(self, _op):
        self.names = {}
        self.calls.append("Initialise")

    def SetDefAction(self, _action):
        self.calls.append("SetDefAction")

    def AddAssembly(self, path):
        self.calls.append(
            ("AddAssembly", path, self.fake.session.Parts.LoadOptions.ComponentLoadMethod)
        )
        return (NS(Failed=False, FileNames=[], Statuses=[]),)  # fmt: skip

    def SetNaming(self, source, _technique, target):
        self.names[str(source)] = str(target)

    def InitNamingFailures(self):
        return None

    def PerformClone(self, _failures):
        for source, target in self.names.items():
            shutil.copyfile(source, target)
        structure = self.fake.structure
        for source, target in self.names.items():
            key = str(Path(source).resolve()).casefold()
            if key in structure:
                structure[str(Path(target).resolve()).casefold()] = [
                    self.names.get(child, child) for child in structure[key]
                ]

    def Terminate(self):
        self.calls.append("Terminate")


class Evaluator:
    """UF Eval over ``nx.curves``: {tag: ("line", a, b) | ("arc", center, r, x, y, a0, a1)}."""

    def __init__(self, fake):
        self.fake = fake

    def Initialize2(self, tag):
        return tag

    def AskLimits(self, tag):
        kind = self.fake.curves[tag]
        return [kind[5], kind[6]] if kind[0] == "arc" else [0.0, 1.0]

    def EvaluateUnitVectors(self, tag, t):
        kind = self.fake.curves[tag]
        if kind[0] != "arc":  # lines, and splines through two points
            a, b = kind[1], kind[2]
            return ([a[i] + t * (b[i] - a[i]) for i in range(3)],)
        _, c, r, x, y, _a0, _a1 = kind
        return ([c[i] + r * (math.cos(t) * x[i] + math.sin(t) * y[i]) for i in range(3)],)

    def IsLine(self, tag):
        return self.fake.curves[tag][0] == "line"

    def IsArc(self, tag):
        return self.fake.curves[tag][0] == "arc"

    def AskArc(self, tag):
        _, c, r, x, y, _a0, _a1 = self.fake.curves[tag]
        return NS(Center=c, Radius=r, XAxis=x, YAxis=y)

    def Free(self, tag):
        self.fake.freed.append(tag)


def with_parts(nx, root):
    """Wire part files under ``root`` (the kit workspace) onto ``nx``; returns ``nx``."""
    nx.root = Path(root)
    nx.file_new_keeps_display = False
    nx.save_error = None
    nx.export_error = None
    nx.export_writes = True
    nx.import_bodies = 1
    nx.step_creators = []
    nx.exporters = []
    nx.ap242_licensed = False
    syslog = nx.root.parent / f"{nx.root.name}.syslog"  # NX's syslog, outside the workspace
    syslog.write_text("NX syslog\n")
    nx.session.LogFile = NS(FileName=str(syslog))
    nx.step_importers = []
    nx.structure = {}  # resolved casefold path of an assembly -> prototype paths
    nx.curves = {}
    nx.freed = []
    nx.BasePart = NS(Units=nx.nx.BasePart.Units, **ENUMS)
    nx.nx.BasePart = nx.BasePart
    nx.nx.Part = NS(Units=nx.nx.BasePart.Units)
    nx.nx.DisplayPartOption = NS(AllowAdditional=1)
    nx.nx.StepCreator = NS(
        ExportFromOption=NS(DisplayPart=0, ExistingPart=1),
        ExportAsOption=NS(Ap203=0, Ap214=1, Ap242=2, Ap242ED2=3),
    )
    nx.nx.IgesCreator = NS(ExportFromOption=NS(DisplayPart=0, ExistingPart=1))
    nx.nx.ParasolidExporter = NS(ExportFromOption=NS(DisplayedPart=0, ExistingPart=1))
    nx.nx.STLCreator = NS(OutputTypeEnum=NS(Binary=0, Text=1))
    nx.nx.JtCreatorFileStructure = NS(Monolithic=3, PerPart=0)
    nx.nx.ObjectSelector = NS(Scope=NS(SelectedObjects=2))
    nx.nx.Step214Importer = NS(ImportToOption=NS(WorkPart=0))
    nx.nx.LoadOptions = NS(LoadMethod=NS(AsSaved="as_saved"))
    parts = nx.session.Parts
    parts.LoadOptions = NS(UsePartialLoading=True, ComponentLoadMethod="search")
    parts.FileNew = lambda: FileNew(nx)

    def set_display(part, *_):
        parts.Display = parts.Work = part
        return None, _status()

    def set_work(part):
        parts.Work = part

    def open_base(path):
        if not Path(path).is_file():
            raise NXException(1020001, "File not found")
        part = FilePart(nx, path)
        nx.opened_files.append(str(path))
        for child in nx.structure.get(str(Path(path).resolve()).casefold(), []):
            proto = next((p for p in parts if Path(p.FullPath) == Path(child)), None)
            proto = proto or FilePart(nx, child)
            nx.attach(part, proto)
        parts.Work = parts.Display = part
        return part, _status()

    parts.SetDisplay = set_display
    parts.SetWork = set_work
    parts.OpenBase = open_base
    nx.opened_files = []
    nx.session.DexManager = NS(
        CreateStepCreator=lambda: StepCreator(nx),
        CreateStep214Importer=lambda: StepImporter(nx),
        CreateIgesCreator=lambda: IgesCreator(nx),
        CreateParasolidExporter=lambda: ParasolidExporter(nx),
        CreateStlCreator=lambda: StlCreator(nx),
    )
    nx.session.PvtransManager = NS(CreateJtCreator=lambda: JtCreator(nx))
    boxes = {}  # tag -> box, refreshed from the loaded parts' bodies

    def box_of(tag):
        for part in nx.session.Parts:
            for body in part.Bodies:
                boxes[body.Tag] = body.box
        return boxes[tag]

    nx.uf.ModlGeneral = NS(
        AskBoundingBox=lambda tag: list(box_of(tag)),
        AskBoundingBoxExact=lambda tag, _csys: _exact(box_of(tag)),
    )
    nx.uf.Clone = Clone(nx)
    nx.uf.Eval = Evaluator(nx)
    nx.occurrences = _occurrences
    nx.attach = _attach
    nx.new_part = lambda rel, units="mm": _new(nx, rel, units)
    nx.line = lambda a, b: _curve(nx, ("line", list(a), list(b)))
    nx.arc = lambda c, r, x=(1, 0, 0), y=(0, 1, 0), a0=0.0, a1=2 * math.pi: _curve(
        nx, ("arc", list(c), r, list(x), list(y), a0, a1)
    )
    return nx


def _new(nx, rel, units):
    part = FilePart(nx, nx.root / rel, units)
    nx.session.Parts.Work = nx.session.Parts.Display = part
    return part


def _curve(nx, kind):
    curve = Object("CURVE")
    nx.curves[curve.Tag] = kind
    return curve


def _occurrences(part):
    root = getattr(part.ComponentAssembly, "RootComponent", None)
    out = []

    def walk(node):
        for child in node.GetChildren():
            out.append(child)
            walk(child)

    if root is not None:
        walk(root)
    return out


def _attach(top, prototype, name=None):
    """Add an occurrence of ``prototype`` under ``top``'s root component."""
    assembly = top.ComponentAssembly
    if assembly.RootComponent is None:
        assembly.RootComponent = Occurrence(top, "ROOT")
    child = Occurrence(prototype, name or prototype.Name.upper())
    assembly.RootComponent.children.append(child)
    return child


__all__ = ["FilePart", "point", "with_parts"]
