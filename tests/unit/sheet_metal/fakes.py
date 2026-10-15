"""Sheet-metal extensions of the shared fake (``tests/kit``) for the sheet-metal unit tests.

``sm_nx(units, application)`` is a ``fake_nx`` whose work part has a
``SheetmetalManager`` (sheet-metal bodies, thickness, inner bend faces and their
parameters), ``Preferences.SheetMetalPreferences`` (creation defaults and their
builder) and whose ``NXOpen.Features.SheetMetal`` / ``Preferences`` / ``Annotations``
namespaces answer every enum as the string ``"<Enum>.<Member>"``. The kernel sees the
application ``application`` (``sheet_metal`` by default).

``nx.tab(x, y, t)`` makes a sheet-metal tab body, ``nx.add_bend(body, ...)`` adds an
inner bend face, and ``nx.made(kind, body)`` is the feature a builder commits.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from tests.kit import fake_nx
from tests.kit.kernel import services
from tests.kit.objects import Face, Feature, Object


class Enum(str):
    """``Enum.Member`` -> the string ``"Enum.Member"`` (stable, comparable, nestable:
    ``Builder.FileType.Dxf`` is ``"Builder.FileType.Dxf"``)."""

    def __getattr__(self, member):
        if member.startswith("__"):
            raise AttributeError(member)
        return Enum(f"{self}.{member}")


class Enums:
    """A namespace whose unknown members are :class:`Enum`s (explicit ones win)."""

    def __init__(self, **known):
        self.__dict__.update(known)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = Enum(name)
        self.__dict__[name] = value
        return value


BENT, FLAT = "SheetmetalBendState.Bent", "SheetmetalBendState.Flat"


class Manager:
    """``SheetmetalManager``: answers from the fake bodies; builders from ``nx.builder``."""

    def __init__(self, fake):
        self.fake = fake

    def IsSheetmetalBody(self, body):
        return bool(getattr(body, "sheet_metal", False))

    def GetBodyThickness(self, body):
        return body.thickness

    def GetInnerBendFaces(self, body):
        return list(body.bends), [f.bend.state for f in body.bends]

    def GetBendParameters(self, face):
        b = face.bend
        return NS(InnerRadius=b.radius, BendAngle=b.angle, NeutralFactor=b.nf)

    def GetFaceType(self, face):
        return getattr(face, "sm_type", "SheetmetalFaceType.Web")

    def GetFaceLayer(self, face):
        return getattr(face, "layer", "SheetmetalFaceLayer.Unknown")

    def GetOppositeFace(self, face):
        return face.opposite

    def __getattr__(self, name):
        if name.startswith("Create") and name.endswith("Builder"):
            kind = name[len("Create") :]
            return lambda *args: self.fake.builder(kind, *args)
        raise AttributeError(name)


NUMBERS = {
    "MaterialThickness": 3.0,
    "BendRadius": 3.0,
    "NeutralFactor": 0.33,
    "BendReliefWidth": 3.0,
    "BendReliefDepth": 3.0,
}
# NX's names of the default expressions features refer to.
DEFAULT_NAMES = {
    "MaterialThickness": "Sheet_Metal_Material_Thickness",
    "BendRadius": "Sheet_Metal_Bend_Radius",
    "NeutralFactor": "Sheet_Metal_Neutral_Factor",
    "BendReliefWidth": "Sheet_Metal_Relief_Width",
    "BendReliefDepth": "Sheet_Metal_Relief_Depth",
}


class Preferences:
    """``SheetMetalPreferences``: the defaults; its builder's commit hook stores them."""

    def __init__(self, fake):
        self.fake = fake
        self.numbers = dict(NUMBERS)
        self.entry = "ParameterEntryTypes.Value"
        self.method = "BendDefinitionMethodOptions.NeutralFactorValue"
        self.table = "BEND_TABLE"
        self.formulas = {"BendAllowanceFormula": "(Radius+(Thickness*0.44))*rad(Angle)",
                         "BendDeductionFormula": "2*Thickness"}  # fmt: skip
        self.material = ""
        self.tool = ""
        self.ignore = set()  # numbers NX silently keeps (read-back test)
        # The part's default expressions (``Sheet_Metal_Bend_Radius``...), one per number.
        self.expressions = {
            n: fake.expression(DEFAULT_NAMES[n], repr(v), v) for n, v in self.numbers.items()
        }
        fake.on_commit("SheetMetalPreferencesBuilder", self._commit)

    def __getattr__(self, name):
        if name.startswith("Get") and name[3:] in NUMBERS:
            exp = self.expressions[name[3:]]
            exp.Value = self.numbers[name[3:]]
            return lambda: exp
        raise AttributeError(name)

    def GetParameterEntryType(self):
        return self.entry

    def GetBendDefinitionMethod(self):
        return self.method

    def GetBendTable(self):
        return self.table

    def GetBendAllowanceFormula(self):
        return self.formulas["BendAllowanceFormula"]

    def GetBendDeductionFormula(self):
        return self.formulas["BendDeductionFormula"]

    def GetMaterialName(self):
        return self.material

    def GetToolName(self):
        return self.tool

    def GetMaterialNames(self):  # NX 2406: native memory-access fault
        raise AssertionError("GetMaterialNames must never be called")

    def CreateSheetMetalPreferencesBuilder(self):
        return self.fake.builder("SheetMetalPreferencesBuilder")

    def _commit(self, b):
        for name in NUMBERS:
            text = b.__dict__.get(name) and b.__dict__[name].__dict__.get("RightHandSide")
            if isinstance(text, str) and name not in self.ignore:
                self.numbers[name] = float(text)
        if isinstance(b.__dict__.get("ParameterEntryType"), str):
            self.entry = b.ParameterEntryType
        for attr in ("BendAllowanceFormula", "BendDeductionFormula"):
            if isinstance(b.__dict__.get(attr), str):
                self.formulas[attr] = b.__dict__[attr]
        for method, field in (("SetMaterial", "material"), ("SetToolName", "tool"),
                              ("SetBendDefinitionMethod", "method"), ("SetBendTable", "table")):  # fmt: skip
            node = b.__dict__.get(method)
            if node is not None and node.calls:
                setattr(self, field, node.calls[-1][0][0])
        return None


def sm_nx(units="mm", application="sheet_metal"):
    nx = fake_nx(units=units)
    nx.application = application
    kit = services(nx)
    base_state = kit.state
    kit.state = lambda: {**base_state(), "application": nx.application}
    nx.nx.Features.SheetMetal = Enums(SheetmetalBendState=NS(Bent=BENT, Flat=FLAT))
    nx.nx.Preferences = Enums(SheetMetalPreferencesBuilder=Enums())
    nx.nx.Annotations = Enums(SheetMetalPMIBuilder=Enum("SheetMetalPMIBuilder"))
    nx.nx.ObjectList = Enums()
    nx.nx.NXObject = NS(AttributeType=NS(String="string"))
    nx.part.Features.SheetmetalManager = Manager(nx)
    nx.prefs = Preferences(nx)
    nx.part.Preferences = NS(SheetMetalPreferences=nx.prefs)
    nx.uf.Tag = NS(AskHandleFromTag=lambda tag: f"handle-{tag}", AskTagOfHandle=_tag_of)
    nx.tab = lambda *a, **k: tab(nx, *a, **k)
    nx.add_bend = lambda *a, **k: add_bend(nx, *a, **k)
    nx.made = lambda kind, body=None, **k: made(nx, kind, body, **k)
    nx.bend_edges = lambda *a, **k: bend_edges(nx, *a, **k)
    nx.flat_pattern = lambda *a, **k: flat_pattern(nx, *a, **k)
    return nx


def _tag_of(handle):
    if not str(handle).startswith("handle-"):
        raise RuntimeError("stale handle")
    return int(str(handle)[len("handle-") :])


def tab(nx, x=100.0, y=80.0, t=2.0):
    """A sheet-metal tab body ``x`` by ``y``, ``t`` thick (volume x*y*t)."""
    body = nx.block(x, y, t)
    body.sheet_metal, body.thickness, body.bends = True, t, []
    body.volume = x * y * t
    return body


def add_bend(nx, body, radius=3.0, angle=90.0, nf=0.33, state=BENT):
    """Add an inner bend face to ``body``; returns the face."""
    face = nx.face(normal=(0, 0, 1), kind="cylinder", body=body)
    face.bend = NS(radius=radius, angle=angle, nf=nf, state=state)
    body.bends.append(face)
    body.faces.append(face)
    return face


def bend_edges(nx, face, length, width, layer="Top", flat=False):
    """Give an inner bend face its edges (two tangent lines ``length`` long; the ends border
    thickness faces, arcs when bent), its ``layer`` and an opposite (outer) face."""
    web = nx.face(body=face.GetBody())
    side = nx.face(body=face.GetBody())
    side.sm_type = "SheetmetalFaceType.Thickness"
    edges = []
    for y in (0.0, width):
        e = nx.edge((0.0, y, 0.0), (length, y, 0.0), body=face.GetBody())
        e.GetFaces = lambda e_face=face: [e_face, web]
        edges.append(e)
    for x in (0.0, length):
        e = nx.edge((x, 0.0, 0.0), (x, width, 0.0), body=face.GetBody(), linear=flat)
        e.GetFaces = lambda e_face=face: [e_face, side]
        edges.append(e)
    face.GetEdges = lambda: list(edges)
    face.layer = f"SheetmetalFaceLayer.{layer}"
    face.opposite = nx.face(body=face.GetBody())
    return face


def flat_pattern(nx, body, up=(), down=(), view="FLAT-PATTERN#1", sequence=()):
    """A Flat Pattern feature whose centre lines point at ``up``/``down`` formed faces, with
    the modeling view ``view`` whose bend list is ``sequence`` ([(id, name)])."""
    feature = nx.made("FLAT_PATTERN", body, feature_type="FLAT_PATTERN")
    feature.ref = nx.ref_of(feature, "feature")
    feature.GetBendUpCenterLines = lambda: [NS(FormedBodyObject=f) for f in up]
    feature.GetBendDownCenterLines = lambda: [NS(FormedBodyObject=f) for f in down]
    shown = NS(Name=view)
    nx.part.ModelingViews._views.FindObject = lambda name: shown if name == view else _no(name)

    def flat_builder(*args):
        b = nx.builder("FlatPatternBuilder", *args)
        object.__setattr__(b, "FlatPatternViewName", view)
        return b

    def list_builder():
        b = nx.builder("BendListBuilder")
        items = [NS(BendID=i, BendName=n) for i, n in sequence]
        object.__setattr__(b, "BendList", NS(Length=len(items), FindItem=items.__getitem__))
        return b

    manager = nx.part.Features.SheetmetalManager
    manager.CreateFlatPatternBuilder = flat_builder
    manager.CreateBendListBuilder = list_builder
    return feature


def _no(name):
    raise RuntimeError(f"No object found with this name: {name}")


def made(nx, kind, body=None, feature_type=None):
    f = Feature(kind, [body] if body is not None else [])
    f.FeatureType = feature_type or kind.upper()
    nx.part.Features.append(f)
    return f


class Annotation(Object):
    """A committed annotation with string user attributes and an origin."""

    def __init__(self, name="PMI", origin=(0.0, 0.0, 0.0)):
        super().__init__(name)
        from tests.kit.objects import point

        self.AnnotationOrigin = point(*origin)
        self.attributes = {}

    def HasUserAttribute(self, name, *_):
        return name in self.attributes

    def GetStringAttribute(self, name):
        return self.attributes[name]

    def SetAttribute(self, name, value):
        self.attributes[name] = value


__all__ = ["BENT", "FLAT", "Annotation", "Face", "sm_nx"]
