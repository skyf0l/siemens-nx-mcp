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
from tests.kit.objects import Expression, Face, Feature, Object


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
        fake.on_commit("SheetMetalPreferencesBuilder", self._commit)

    def __getattr__(self, name):
        if name.startswith("Get") and name[3:] in NUMBERS:
            value = self.numbers[name[3:]]
            return lambda: _number(value)
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


def _number(value):
    e = Expression("p", repr(value))
    e.Value = value
    return e


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
