"""Regression contracts for observed NX failures; native receipts validate geometry.

These seams verify validation, result cardinality, transform semantics, and cleanup.
They do not claim to emulate the NX geometric kernel.
"""

import contextlib
import sys
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from nx_mcp.runtime import NXToolError
from tests.fakes import Body, Feature, Object, Sketch
from tests.kit.objects import Collector

pytestmark = pytest.mark.fake_nx


@pytest.fixture
def eng(rig, monkeypatch):
    r = rig
    r.body = Body()
    r.feature = Feature(bodies=[r.body])
    r.sketch = Sketch(r.session)
    monkeypatch.setattr(Sketch, "Feature", r.feature, raising=False)
    r.part.Bodies.append(r.body)
    r.part.Features.append(r.feature)
    r.part.Sketches.append(r.sketch)
    for o in [r.body, r.feature, r.sketch, *r.body.faces, *r.body.edges]:
        o.OwningPart = r.part
        o.IsOccurrence = False
    for o in [*r.body.faces, *r.body.edges]:
        o.GetBody = lambda: r.body
    r.nx.SmartObject = NS(UpdateOption=NS(WithinModeling=1))
    r.nx.Features.MoveObject = Feature
    r.part.Directions = NS(CreateDirection=lambda p, v, _: NS(origin=p, vector=v))
    r.part.CoordinateSystems = NS(
        CreateCoordinateSystem=lambda p, m, _: NS(Origin=p, Orientation=m)
    )
    r.part.ScRuleFactory = NS(CreateRuleOptions=lambda: NS(Dispose=Mock()))
    for kind in ["Edge", "Body", "Face"]:
        setattr(r.part.ScRuleFactory, "CreateRule" + kind + "Dumb", lambda objects: objects)
    r.part.ScRuleFactory.CreateRuleCurveFeature = lambda objects, *_: objects

    r.part.ScCollectors = NS(CreateCollector=Collector)
    r.part.Sections = NS(CreateSection=lambda: NS(AddToSection=Mock()))
    r.nx.Section = NS(Mode=NS(Create=1))
    g = NS(
        Extend=NS(ExtendType=NS(Value="value", ThroughAll="all", UntilSelected="face")),
        BooleanOperation=NS(
            BooleanType=NS(
                Create="create", Unite="unite", Subtract="subtract", Intersect="intersect"
            )
        ),
    )
    monkeypatch.setitem(sys.modules, "NXOpen.GeometricUtilities", g)
    r.nx.GeometricUtilities = g
    r.e._update_model = Mock()
    r.e._check_sketch_result = Mock(return_value={"valid": True})
    r.e._editing_sketch = lambda _: contextlib.nullcontext()
    r.builders = []

    def builder(*_):
        def expression():
            return NS(RightHandSide="0")

        b = NS(
            Destroy=Mock(),
            CommitFeature=Mock(return_value=r.feature),
            Commit=Mock(return_value=r.feature),
            DefaultThickness=expression(),
            StationaryReference=Collector(),
            FaceSetAngleExpressionList=NS(Append=Mock()),
            Type=NS(Face=1),
            SectionsList=NS(Append=Mock()),
            BodyPreferenceTypes=NS(Solid=1, Sheet=2),
            Limits=NS(StartExtend=NS(Value=expression()), EndExtend=NS(Value=expression())),
            BooleanOperation=NS(SetTargetBodies=Mock()),
            BooleanOption=NS(SetTargetBodies=Mock()),
            Diameter=expression(),
            Height=expression(),
            FirstOffsetExp=expression(),
            ChamferOption=NS(SymmetricOffsets=1),
            AddChainset=Mock(),
            ExtractType=NS(Body=1),
            ExtractBodyCollector=Collector(),
            ObjectToMoveObject=NS(Add=Mock()),
            MoveObjectResultOptions=NS(MoveOriginal=1),
            TransformMotion=NS(Options=NS(CsysToCsys=1)),
            MirrorBodyCollector=Collector(),
            Plane=NS(),
            SectionList=NS(Append=Mock()),
            GuideList=NS(Append=Mock()),
        )
        r.builders.append(b)
        return b

    r.builder = builder
    for name in [
        "Extrude",
        "Shell",
        "ThroughCurves",
        "Draft",
        "EdgeBlend",
        "Chamfer",
        "Cylinder",
        "ExtractFace",
        "MirrorBody",
        "Swept",
    ]:
        setattr(r.part.Features, "Create" + name + "Builder", builder)
    r.part.BaseFeatures = NS(CreateMoveObjectBuilder=builder)
    r.part.Datums = NS(CreateFixedDatumPlane=Mock(return_value=Object("plane")))
    r.part.CreateExpressionCollectorSet = lambda *args: args
    return r


def test_owned_geometry_rejects_occurrences_and_other_parts(eng):
    ref = eng.ref(eng.body)
    eng.body.IsOccurrence = True
    with pytest.raises(NXToolError, match="owned"):
        eng.e._engineering_owned(ref, "body")
    eng.body.IsOccurrence = False
    eng.body.OwningPart = object()
    with pytest.raises(NXToolError, match="owned"):
        eng.e._engineering_owned(ref, "body")


@pytest.fixture
def material(eng):
    r = eng
    r.nx.PhysicalMaterial = NS(Type=NS(Isotropic=1))
    materials = []

    class Materials(list):
        def CreatePhysicalMaterialBuilder(self, _):
            b = NS(
                PropertyTable=NS(
                    SetBaseScalarWithDataPropertyValue=lambda key, value, unit: setattr(
                        r, "density", value
                    )
                ),
                Destroy=Mock(),
            )

            def commit():
                m = NS(Name=b.Name, AssignObjects=Mock())
                self.append(m)
                return m

            b.Commit = commit
            r.material_builder = b
            return b

        def AskMaterialOfObject(self, _):
            return self[0] if self else None

    r.materials = Materials(materials)
    r.part.MaterialManager = NS(PhysicalMaterials=r.materials)
    r.part.UnitCollection = NS(FindObject=lambda name: name)
    r.nx.UF.Modl = NS(DensityUnits=NS(KILOGRAMS_METERS=1))
    r.density = 1000
    r.uf.Modeling = NS(AskBodyDensity=lambda *_: r.density)
    return r
