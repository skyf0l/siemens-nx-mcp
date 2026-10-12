"""Topology-optimization extensions of the modeling fake.

``topo_nx(units)`` is ``modeling_nx`` plus the license manager (``nx.licenses``), NX's
Topology Optimization task environment (``Enter`` makes a new feature or edits the one
set with ``SetFeatureToEdit``; ``SetCancelled`` + ``Exit`` removes a new one), studies whose
``Create*Builder`` factories are FakeBuilders committed into the study's lists, materials,
``EditWithUnits`` on expressions (``nx.edits``), UF minimum distance (``nx.gap``) and a
solve (``nx.solve_fraction``: the optimized body's share of the design volume).
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from tests.kit.objects import NXException, Object
from tests.unit.modeling.fakes import feature, modeling_nx, new_body

NONE_COMMITS = ("AnalysisConstraintBuilder", "OptimizationConstraintBuilder",
                "ShapeConstraintBuilder")  # fmt: skip
LISTS = {
    "DesignSpaceBuilder": "designs",
    "ConstructionBodyBuilder": "regions",
    "AnalysisConstraintBuilder": "supports",
    "AnalysisLoadBuilder": "loads",
    "OptimizationConstraintBuilder": "targets",
    "ShapeConstraintBuilder": "shapes",
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
}
INCH = {"MilliMeter": "Inch", "Newton": "PoundForce",
        "PressureNewtonPerSquareMilliMeter": "PressurePoundForcePerSquareInch"}  # fmt: skip


class Subcase:
    def __init__(self):
        self.refs = []

    def AddReference(self, item):
        self.refs.append(item)

    def GetReferences(self):
        return list(self.refs)


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

    def _builder(self, kind):
        b = self.nx.builder(kind, None)
        object.__setattr__(b, "study", self)
        mm = self.nx.part.PartUnits == "mm"
        for member, unit in UNITS[kind].items():
            getattr(b, member).Units = NS(Name=unit if mm else INCH.get(unit, unit))
        return b

    def __getattr__(self, name):
        if name.startswith("Create") and name.endswith("Builder"):
            return lambda *_a: self._builder(name[len("Create") :])
        raise AttributeError(name)

    def GetAllDesignSpaces(self):
        return list(self.designs)

    def GetAllAnalysisBodies(self):
        return list(self.designs)

    def GetAllAnalysisConstraints(self):
        return list(self.supports)

    def GetAllAnalysisLoads(self):
        return list(self.loads)

    def GetAllOptimizationConstraints(self):
        return []  # NX 2406 lists no mass target (it is a feature expression)

    def GetAllSubcases(self):
        return [self.subcase]

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
        results = NS(
            GetSubcaseResults=lambda: [
                NS(
                    HasResults=lambda kind: True,
                    GetMaxMagnitudeAndLocation=lambda kind: (
                        NS(X=0, Y=0, Z=0),
                        self.nx.peaks[str(kind).rsplit(".", 1)[-1].rstrip(">")],
                    ),
                )
            ]
        )
        self.designs[0].Results = results

    def GetOptimizedBodies(self):
        return list(self.optimized)


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
        if self.edit is not None:
            self.ActiveFeature = self.edit
            return
        self.new = self.ActiveFeature = topology_feature(self.nx)

    def SetCancelled(self):
        self.cancelled = True

    def Exit(self):
        self.log.append("Exit")
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
                Type=_set(b, "Type"), GetFaces=lambda: list(faces), builder=b, Tag=Object().Tag
            )
        elif kind == "OptimizationConstraintBuilder":
            made = NS(Type=_set(b, "Type"), Mass=NS(Value=float(b.Mass.RightHandSide)))
        else:
            made = NS(Type=_set(b, "Type"), builder=b)
        getattr(study, LISTS[kind]).append(made)
        if kind == "OptimizationConstraintBuilder":
            study.owner.expressions.append(b.Mass)
        # NX 2406 returns None from these commits (the objects exist all the same).
        return None if kind in NONE_COMMITS else made

    return hook


def topo_nx(units="mm", licensed=True):
    nx = modeling_nx(units=units)
    nx.licenses = {"des_top_opt"} if licensed else set()
    nx.session.LicenseManager = NS(CheckPresence=lambda name: name in nx.licenses)
    nx.session.TopologyOptimizationTaskEnvironment = TaskEnvironment(nx)
    nx.density = 2.7e-6  # kg/mm^3 (aluminium)
    nx.solve_fraction = 0.4
    nx.solve_error = None
    nx.peaks = {"Displacement": 0.12, "StressVonMises": 85000.0}  # kPa (mm base units)
    nx.gap = 0.0
    for kind in ("StudyBuilder", *LISTS):
        nx.on_commit(kind, _commit(nx, kind))
        nx.lacks(kind, "GetCommittedObjects")
    nx.edits = []

    def edit_with_units(expression, unit, text):
        nx.edits.append((expression, unit, text))
        expression.RightHandSide = text

    nx.part.Expressions.EditWithUnits = edit_with_units
    nx.part.UnitCollection = NS(FindObject=lambda name: NS(Name=name))
    library = {"Aluminum_6061": NS(Name="Aluminum_6061"), "Steel": NS(Name="Steel")}
    nx.loaded_materials = {}
    nx.body_material = library["Steel"]  # every body's assigned material (None: none)

    def load(name):
        if name not in library:
            raise NXException(3940020, "Material not found in library")
        nx.loaded_materials[name] = library[name]
        return library[name]

    nx.part.MaterialManager = NS(
        PhysicalMaterials=NS(
            GetLoadedLibraryMaterial=lambda _lib, name: nx.loaded_materials.get(name),
            LoadFromNxmatmllibrary=load,
            AskMaterialOfObject=lambda body: nx.body_material,
        )
    )
    nx.part.Planes = NS(CreatePlane=lambda o, n, _u: NS(kind="plane", origin=o, normal=n))
    nx.uf.Modeling.AskMinimumDist = lambda a, b, *_r: (nx.gap, [0, 0, 0], [0, 0, 0])
    # facts.face_point: a face's point is its declared origin, inside the face.
    nx.uf.Modeling.AskFaceProps = lambda tag, uv: (list(nx.face_data[tag][1]),)
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
