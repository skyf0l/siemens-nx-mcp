"""Topology-optimization extensions of the modeling fake.

``topo_nx(units)`` is ``modeling_nx`` plus the license manager (``nx.licenses``), NX's
Topology Optimization task environment (``Enter`` makes a new feature or edits the one
set with ``SetFeatureToEdit``; ``SetCancelled`` + ``Exit`` removes a new one), studies whose
``Create*Builder`` factories are FakeBuilders committed into the study's lists, materials,
``EditWithUnits`` on expressions (``nx.edits``), UF minimum distance (``nx.gap``) and a
solve (``nx.solve_fraction``: the optimized body's share of the design volume).
The graphics window (``part.Views.CreateImageExportBuilder``) draws a plot like NX 2406:
the legend's colour bar always, the coloured result body only inside the task (and
``nx.body_drawn``); ``nx.window`` is its size, ``nx.exports`` what was captured.
"""

from __future__ import annotations

import struct
import zlib
from types import SimpleNamespace as NS

from tests.kit.objects import NXException, Object
from tests.unit.modeling.fakes import feature, modeling_nx, new_body

NONE_COMMITS = ("AnalysisConstraintBuilder", "OptimizationConstraintBuilder",
                "ShapeConstraintBuilder", "ConnectionBuilder", "EnvironmentLoadBuilder")  # fmt: skip
LISTS = {
    "DesignSpaceBuilder": "designs",
    "ConstructionBodyBuilder": "regions",
    "AnalysisConstraintBuilder": "supports",
    "AnalysisLoadBuilder": "loads",
    "OptimizationConstraintBuilder": "targets",
    "ShapeConstraintBuilder": "shapes",
    "SceneryBodyBuilder": "scenery",
    "ConnectionBuilder": "connections",
    "EnvironmentLoadBuilder": "environment",
}


def _set(builder, name, default=None):
    """A value the handler assigned on a FakeBuilder (unset members are Nodes)."""
    return builder.__dict__.get(name, default)


def _faces(builder, member="SelectFace"):
    collector = builder.__dict__.get(member)
    if collector is None or not collector.ReplaceRules.calls:
        return []
    (rules, _keep), _kw = collector.ReplaceRules.calls[-1]
    return [f for _kind, objs in rules for f in objs]


# Units of the builder expressions (mm part; INCH: an inch part's).
UNITS = {
    "StudyBuilder": {},
    "DesignSpaceBuilder": {},
    "ConstructionBodyBuilder": {"ThickenOffset": "MilliMeter"},
    "AnalysisConstraintBuilder": {},
    "AnalysisLoadBuilder": {"Force": "Newton", "Pressure": "PressureNewtonPerSquareMilliMeter"},
    "OptimizationConstraintBuilder": {"Mass": "Kilogram"},
    "ShapeConstraintBuilder": {"MinimumDiameter": "MilliMeter"},
    "SceneryBodyBuilder": {},
    "ConnectionBuilder": {},
    "EnvironmentLoadBuilder": {"GravityMagnitude": "MeterPerSquareSecond"},  # also inch parts
    "SubcaseManager": {},
}
INCH = {"MilliMeter": "Inch", "Newton": "PoundForce",
        "PressureNewtonPerSquareMilliMeter": "PressurePoundForcePerSquareInch"}  # fmt: skip


class Subcase:
    def __init__(self, name="Default Subcase", refs=()):
        self.Name = name
        self.refs = list(refs)
        self.Tag = Object().Tag

    def AddReference(self, item):
        self.refs.append(item)

    def GetReferences(self):
        return list(self.refs)

    def RemoveReference(self, item):
        self.refs.remove(item)


class Study(Object):
    def __init__(self, nx, name):
        super().__init__(name)
        self.nx = nx
        self.StudyQuality = 5
        self.Active = True
        self.designs, self.regions, self.supports = [], [], []
        self.loads, self.targets, self.shapes = [], [], []
        self.optimized = []
        self.runs = 0
        self.subcase = Subcase()
        self.subcases = [self.subcase]
        self.scenery, self.connections, self.environment = [], [], []

    def _builder(self, kind):
        b = self.nx.builder(kind, None)
        object.__setattr__(b, "study", self)
        if kind == "SubcaseManager":
            items = SubcaseList(Subcase(c.Name, c.refs) for c in self.subcases)
            object.__setattr__(b, "SubcaseList", items)
            object.__setattr__(b, "CreateSubcase", lambda: Subcase("Subcase"))
        mm = self.nx.part.PartUnits == "mm"
        for member, unit in UNITS[kind].items():
            getattr(b, member).Units = NS(Name=unit if mm else INCH.get(unit, unit))
        return b

    def __getattr__(self, name):
        if name == "CreateSubcaseManager":
            return lambda: self._builder("SubcaseManager")
        if name.startswith("Create") and name.endswith("Builder"):
            return lambda *_a: self._builder(name[len("Create") :])
        raise AttributeError(name)

    def GetAllDesignSpaces(self):
        return list(self.designs)

    def GetAllAnalysisBodies(self):
        return list(self.designs) + list(self.scenery)

    def GetAllSceneryBodies(self):
        return list(self.scenery)

    def GetAllConnections(self):
        return list(self.connections)

    def GetAllEnvironmentLoads(self):
        return list(self.environment)

    def GetAllAnalysisConstraints(self):
        return list(self.supports)

    def GetAllAnalysisLoads(self):
        return list(self.loads)

    def GetAllOptimizationConstraints(self):
        return []  # NX 2406 lists no mass target (it is a feature expression)

    def GetAllSubcases(self):
        return list(self.subcases)

    def RunOptimization(self):
        self.runs += 1
        if self.nx.solve_error is not None:
            raise self.nx.solve_error

    def RetrieveOptimizationResults(self):
        design = self.designs[0].Body
        if self.nx.solve_fraction is None:
            return
        body = new_body(self.nx, list(design.box), volume=design.volume * self.nx.solve_fraction)
        self.optimized = [body]
        self.owner.bodies = [b for b in self.owner.bodies if b is not self.copy] + [body]
        self.designs[0].Results = results(self.nx, len(self.GetAllSubcases()))
        # NX 2406 adds the result's mass as a feature expression after the target's.
        mass = repr(body.volume * self.nx.density)
        self.owner.expressions.append(NS(RightHandSide=mass, Units=NS(Name="Kilogram")))

    def RunAnalysis(self):  # NX 2406 refuses it on a topology study
        raise NXException(3520201, "Operation cannot be performed while in a task environment")

    def GetOptimizedBodies(self):
        return list(self.optimized)


class SubcaseList(list):
    """NX's SubcaseItemList: the manager starts with the study's subcases."""

    @property
    def Length(self):
        return len(self)

    def FindItem(self, i):
        return self[i]

    def Append(self, item):
        self.append(item)

    def Erase(self, item, *_option):
        self.remove(item)


def results(nx, count):
    """Analysis body results: one subcase result per load case; ``nx.case_peaks[i]`` (else
    ``nx.peaks``) maps a result type name to its max magnitude, at ``nx.peak_at`` (part
    units; the stress peak's location 1e-6 of them, as NX 2406 gives it)."""

    def peak(peaks, kind):
        name = str(kind).rsplit(".", 1)[-1].rstrip(">")
        scale = 1e-6 if name == "StressVonMises" else 1.0
        at = NS(**{k: v * scale for k, v in zip("XYZ", nx.peak_at, strict=True)})
        return at, peaks[name]

    def case(i):
        peaks = nx.case_peaks[i] if i < len(nx.case_peaks) else nx.peaks
        return NS(
            HasResults=lambda kind: True,
            GetMaxMagnitudeAndLocation=lambda kind: peak(peaks, kind),
        )

    return NS(GetSubcaseResults=lambda: [case(i) for i in range(count)])


class TaskEnvironment:
    def __init__(self, nx):
        self.nx = nx
        self.ActiveFeature = None
        self.edit = None
        self.cancelled = False
        self.new = None
        self.log = []

    def SetFeatureToEdit(self, f):
        self.edit = f

    def Enter(self):
        if not self.nx.licenses:
            raise NXException(949895, "The requested license feature is not available")
        self.log.append("Enter")
        self.nx.part.PostManager.plot = None  # NX 2406: entering the task clears the plot
        if self.edit is not None:
            self.ActiveFeature = self.edit
            return
        self.new = self.ActiveFeature = topology_feature(self.nx)

    def SetCancelled(self):
        self.cancelled = True
        self.log.append("Cancel")

    def Exit(self):
        self.log.append("Exit")
        if self.cancelled:
            self.nx.part.PostManager.plot = None  # NX 2406: a cancelled task takes its plot
        if self.cancelled and self.new is not None and self.new in self.nx.part.Features:
            self.nx.part.Features.remove(self.new)
        self.ActiveFeature = self.edit = self.new = None
        self.cancelled = False


def topology_feature(nx):
    f = feature(
        nx, "TOPOLOGY_OPTIMIZATION", name=f"Topology Optimization({len(nx.part.Features) + 1})"
    )
    f.studies = []
    f.expressions = []
    f.GetExpressions = lambda: list(f.expressions)
    f.ActiveStudy = None

    def create_study_builder(_study):
        b = nx.builder("StudyBuilder", None)
        object.__setattr__(b, "owner", f)
        return b

    f.CreateStudyBuilder = create_study_builder
    f.GetAllStudies = lambda: list(f.studies)
    return f


def _commit(nx, kind):
    def hook(b):
        if kind == "StudyBuilder":
            study = Study(nx, _set(b, "StudyName", "Study 1"))
            study.StudyQuality = _set(b, "StudyQuality", 5)
            b.owner.studies.append(study)
            study.owner = b.owner
            b.owner.ActiveStudy = study
            return study
        study = b.study
        if kind == "DesignSpaceBuilder":
            body = b.SelectBody.Value
            study.owner.bodies.append(body)  # NX lists the design body under the feature
            copy = new_body(nx, list(body.box), volume=0.0)  # the result body, empty until solved
            study.owner.bodies.append(copy)
            study.copy = copy
            made = NS(
                Body=body,
                Material=_set(b, "Material"),
                Results=None,
                Tag=Object().Tag,
                GetMass=lambda: (body.volume * nx.density, NS(Abbreviation="kg")),
            )
        elif kind == "ConstructionBodyBuilder":
            made = NS(
                Method=_set(b, "Method"), Type=_set(b, "Type"), faces=_faces(b, "FaceCollector")
            )
        elif kind == "AnalysisConstraintBuilder":
            faces = _faces(b)
            made = NS(Type=_set(b, "Type"), GetFaces=lambda: list(faces), Tag=Object().Tag)
        elif kind == "AnalysisLoadBuilder":
            faces = _faces(b)
            made = NS(
                Type=_set(b, "Type"), GetFaces=lambda: list(faces), builder=b, Tag=Object().Tag,
                Force=b.Force, Pressure=b.Pressure, FaceVector=_set(b, "FaceVector"),
            )  # fmt: skip
        elif kind == "OptimizationConstraintBuilder":
            made = NS(Type=_set(b, "Type"), Mass=NS(Value=float(b.Mass.RightHandSide)))
        elif kind == "SceneryBodyBuilder":
            (body,), _kw = b.Bodies.Add.calls[-1]
            made = NS(Body=body, Material=nx.body_material, Results=None, Tag=Object().Tag,
                      MaterialDefinition=_set(b, "MaterialDefinition"))  # fmt: skip
        elif kind == "ConnectionBuilder":
            made = NS(Type=_set(b, "Type"), bodies=(b.SelectBody1.Value, b.SelectBody2.Value))
        elif kind == "EnvironmentLoadBuilder":
            study.environment.clear()  # one gravity per study; disabled = none
            if _set(b, "GravityEnabled"):
                study.environment.append(
                    NS(Magnitude=b.GravityMagnitude, Direction=_set(b, "GravityDirection"))
                )
            return None
        elif kind == "SubcaseManager":
            study.subcases = list(b.SubcaseList)
            study.subcase = study.subcases[0] if study.subcases else None
            return study
        else:
            made = NS(Type=_set(b, "Type"), builder=b)
        getattr(study, LISTS[kind]).append(made)
        if kind in ("AnalysisConstraintBuilder", "AnalysisLoadBuilder") and study.subcases:
            study.subcases[0].refs.append(made)  # NX 2406 fills the active case itself
        if kind == "OptimizationConstraintBuilder":
            study.owner.expressions.append(b.Mass)
        # NX 2406 returns None from these commits (the objects exist all the same).
        return None if kind in NONE_COMMITS else made

    return hook


def _name(member) -> str:
    """The member name of a fake enum value (a Node path)."""
    return str(member).rsplit(".", 1)[-1].rstrip(">")


class PostManager:
    """``BasePart.PostManager``: one contour plot at a time (``plot``: component name and
    load case index). NX 2406's ``HideContour`` leaves the plot shown; entering the task
    clears it. ``GetContourLimits(0)`` derives the range from the fake's peaks."""

    def __init__(self, nx):
        self.nx = nx
        self.plot = None
        self.hides = 0
        nx.on_commit("ViewContourBuilder", self._commit)
        nx.lacks("ViewContourBuilder", "GetCommittedObjects")

    def CreateViewContourBuilder(self):
        return self.nx.builder("ViewContourBuilder")

    def _commit(self, b):
        study, index = b.FirstStudy, b.SubcaseIndexForFirstStudy
        solved = study.designs and study.designs[0].Results is not None
        if not solved or not 0 <= index < len(study.subcases):
            raise NXException(3520300, "No solved result for the specified subcase.")
        self.plot = (_name(b.ResultComponentType), index)
        return None

    def IsContourShown(self):
        return self.plot is not None

    def HideContour(self):
        self.hides += 1  # NX 2406: no effect outside the task

    def GetContourLimits(self, index):
        if index != 0 or self.plot is None:
            raise NXException(3520301, "No contour is displayed at the specified index.")
        nx, (component, case) = self.nx, self.plot
        peaks = nx.case_peaks[case] if case < len(nx.case_peaks) else nx.peaks
        length = NS(Name="MilliMeter" if nx.part.PartUnits == "mm" else "Inch")
        stress = peaks["StressVonMises"] * (1e-3 if nx.part.PartUnits == "mm" else 1.7858e-5)
        if component == "StressVonMises":
            return 0.0, stress, NS(Name="StressNewtonPerSquareMilliMeter")
        if component == "StressWorstPrincipal":
            return -1.2 * stress, 1.1 * stress, NS(Name="StressNewtonPerSquareMilliMeter")
        if component == "SafetyFactor":
            return 250.0 / stress, 1e6, None
        if component == "DisplacementMagnitude":
            return 0.0, peaks["Displacement"], length
        return -0.9 * peaks["Displacement"], 1e-4, length


def _predicted(kind, line, prev, i):
    """The PNG filter predictor of byte ``i`` (RGB rows, 3 bytes per pixel)."""
    a = line[i - 3] if i >= 3 else 0
    b = prev[i]
    c = prev[i - 3] if i >= 3 else 0
    if kind == 0:
        return 0
    if kind == 1:
        return a
    if kind == 2:
        return b
    if kind == 3:
        return (a + b) >> 1
    p = a + b - c  # Paeth
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else (b if pb <= pc else c)


def _chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png(width, height, pixel, filters=(0, 1, 2, 3, 4)) -> bytes:
    """An RGB PNG of ``pixel(x, y) -> (r, g, b)``; row y uses filter ``filters[y % n]``."""
    raw, prev = bytearray(), bytes(width * 3)
    for y in range(height):
        line = bytes(v for x in range(width) for v in pixel(x, y))
        kind = filters[y % len(filters)]
        raw.append(kind)
        raw += bytes((line[i] - _predicted(kind, line, prev, i)) & 255 for i in range(len(line)))
        prev = line
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = _chunk(b"IDAT", zlib.compress(bytes(raw)))
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + idat + _chunk(b"IEND", b"")


def window_pixel(nx, legend, body):
    """The fake graphics window: white, a legend colour bar on the right, the result body
    (bands of hue) in the middle."""
    width, height = nx.window
    bands = [(0, 0, 255), (0, 200, 255), (0, 255, 0), (255, 255, 0), (255, 0, 0)]

    def pixel(x, y):
        if legend and x >= width - 6 and height // 5 <= y < height - height // 10:
            return bands[(y * len(bands)) // height] if body else (0, 0, 255)
        if body and width // 5 <= x < width // 2 and height // 3 <= y < height // 2:
            return bands[(x * len(bands)) // width]
        return (255, 255, 255)

    return pixel


class ImageExport:
    """``ImageExportBuilder``: writes the window (``nx.window``, any requested size) to
    ``FileName`` on Commit."""

    FileFormats = NS(Png="png")
    BackgroundOptions = NS(CustomColor="custom", Original="original", Transparent="clear")

    def __init__(self, nx):
        self.nx = nx

    def SetCustomBackgroundColor(self, color):
        self.color = list(color)

    def Commit(self):
        nx = self.nx
        task = nx.session.TopologyOptimizationTaskEnvironment.ActiveFeature is not None
        plotted = nx.part.PostManager.plot is not None
        body = plotted and task and nx.body_drawn
        nx.exports.append({"task": task, "plotted": plotted, "camera": dict(nx.camera)})
        data = png(*nx.window, window_pixel(nx, plotted, body))
        with open(self.FileName, "wb") as stream:
            stream.write(data)

    def Destroy(self):
        pass


class WorkView:
    """The model work view: ``Fit`` zooms (``nx.camera`` scale), like NX in the task."""

    def __init__(self, nx):
        self.nx = nx
        self.Name = "Trimetric"
        self.fits = 0

    def Fit(self):
        self.fits += 1
        self.nx.camera["scale"] = 4.4

    def UpdateDisplay(self):
        pass


def topo_nx(units="mm", licensed=True):
    nx = modeling_nx(units=units)
    nx.part.PostManager = PostManager(nx)
    nx.window, nx.exports, nx.body_drawn = (60, 40), [], True
    nx.part.Views = NS(CreateImageExportBuilder=lambda: ImageExport(nx))
    nx.part.ModelingViews._views = NS(WorkView=WorkView(nx))
    nx.licenses = {"des_top_opt"} if licensed else set()
    nx.session.LicenseManager = NS(CheckPresence=lambda name: name in nx.licenses)
    nx.session.TopologyOptimizationTaskEnvironment = TaskEnvironment(nx)
    nx.density = 2.7e-6  # kg/mm^3 (aluminium)
    nx.solve_fraction = 0.4
    nx.solve_error = None
    nx.peaks = {"Displacement": 0.12, "StressVonMises": 85000.0}  # kPa (mm base units)
    nx.case_peaks = []
    nx.peak_at = (0.0, 10.0, 10.0)
    nx.gap = 0.0
    for kind in ("StudyBuilder", "SubcaseManager", *LISTS):
        nx.on_commit(kind, _commit(nx, kind))
        nx.lacks(kind, "GetCommittedObjects")
    nx.edits = []

    def edit_with_units(expression, unit, text):
        nx.edits.append((expression, unit, text))
        expression.RightHandSide = text

    nx.part.Expressions.EditWithUnits = edit_with_units
    nx.part.UnitCollection = NS(FindObject=lambda name: NS(Name=name))

    def table(strength):
        def value(name):
            if name != "YieldStrength" or strength is None:
                raise NXException(3940050, "The material property is missing.")
            return strength, NS(Name="PressureNewtonPerSquareMilliMeter")

        return lambda: NS(GetBaseScalarWithDataPropertyValue=value)

    library = {"Aluminum_6061": NS(Name="Aluminum_6061", GetPropTable=table(None)),
               "Steel": NS(Name="Steel", GetPropTable=table(250.0))}  # fmt: skip
    nx.loaded_materials = {}
    nx.body_material = library["Steel"]  # every body's assigned material (None: none)

    def load(name):
        if name not in library:
            raise NXException(3940020, "Material not found in library")
        nx.loaded_materials[name] = library[name]
        return library[name]

    nx.part.MaterialManager = NS(
        PhysicalMaterials=NS(
            GetMaterialsFromLibrary=lambda _lib: list(library),
            GetLoadedLibraryMaterial=lambda _lib, name: nx.loaded_materials.get(name),
            LoadFromNxmatmllibrary=load,
            AskMaterialOfObject=lambda body: nx.body_material,
        )
    )
    nx.part.Planes = NS(CreatePlane=lambda o, n, _u: NS(kind="plane", origin=o, normal=n))
    nx.uf.Modeling.AskMinimumDist = lambda a, b, *_r: (nx.gap, [0, 0, 0], [0, 0, 0])
    # facts.face_point: a face's point is its declared origin, inside the face.
    nx.uf.Modeling.AskFaceProps = lambda tag, uv: (
        list(nx.face_data[tag][1]),
        [0] * 3,
        [0] * 3,
        [0] * 3,
        [0] * 3,
        list(nx.face_data[tag][2]),
    )
    nx.uf.Modeling.AskPointContainment = lambda p, tag: 1
    return nx


def bracket(nx):
    """A 100 x 60 x 10 plate with two bolt-hole faces and an end face, all faces remembered."""
    plate = nx.block(100, 60, 10)
    holes = [nx.remember(nx.face(normal=(1, 0, 0), origin=(15, y, 5), kind="cylinder", body=plate))
             for y in (15, 45)]  # fmt: skip
    for f in holes:
        f.contains = lambda p: 1
    plate.faces.extend(holes)
    end = plate.face((1, 0, 0))
    return plate, holes, end
