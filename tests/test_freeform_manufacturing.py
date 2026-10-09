"""Boundary/cleanup regressions; native geometry evidence is kept separately."""

import math
import sys
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, Mock

import pytest

from nx_mcp.legacy_kit import points3
from nx_mcp.runtime import NXToolError
from tests.fakes import Body, Face, Feature, Object, point


@pytest.fixture
def ff(rig, monkeypatch):
    r = rig
    r.body = Body()
    r.body.IsOccurrence = False
    r.body.OwningPart = r.part
    r.part.Bodies.append(r.body)
    r.feature = Feature(bodies=[r.body])
    r.feature.OwningPart = r.part
    r.feature.IsOccurrence = False
    r.part.Features.append(r.feature)
    r.body.faces.append(Face("other"))
    r.faces = list(r.body.GetFaces())
    for obj in [*r.faces, *r.body.GetEdges()]:
        obj.IsOccurrence = False
        obj.OwningPart = r.part
        obj.GetBody = lambda: r.body
    r.nx.SmartObject = NS(UpdateOption=NS(WithinModeling=1))
    r.nx.Point3d = lambda *coords: point(*coords)
    r.part.Points = NS(CreatePoint=Mock(return_value=Object()))
    r.part.ScRuleFactory = MagicMock()
    r.part.Sections = MagicMock()
    r.nx.Section = NS(AllowTypes=NS(OnlyCurves=1), Mode=NS(Create=1))
    f = NS(
        DeleteFaceBuilder=NS(SelectTypes=NS(Face=1)),
        ThreadBuilder=NS(
            Input=NS(Manual=1, ThreadTable=2),
            Type=NS(Detailed=2, Symbolic=1),
            LimitOption=NS(Value=1),
            Handedness=NS(LeftHand=1, RightHand=2),
        ),
    )
    g = NS(
        Continuity=NS(ContinuityTypes=NS(G0=0, G1=1, G2=2)), ModlMotion=NS(Options=NS(Distance=1))
    )
    monkeypatch.setitem(sys.modules, "NXOpen.Features", f)
    monkeypatch.setitem(sys.modules, "NXOpen.GeometricUtilities", g)
    r.nx.Features = f
    r.nx.GeometricUtilities = g
    r.uf = MagicMock()
    r.uf.Modeling.AskFaceData.return_value = [16, None, None, None, 1.0]
    u = NS(UFSession=NS(GetUFSession=lambda: r.uf))
    monkeypatch.setitem(sys.modules, "NXOpen.UF", u)
    r.nx.UF = u
    r.e._engineering_direction = Mock(return_value=Object())
    r.e._update_model = Mock()
    r.e._model_health = Mock(return_value={"healthy": True, "issues": []})
    r.ref = lambda o, kind: r.e._reference(o, kind, r.part, kind)["id"]
    return r


@pytest.mark.parametrize("value", [[], [[0, 1]], [[0, 0, math.inf]], [[True, 0, 0]], "points"])
def test_invalid_point_sets(value):
    with pytest.raises(NXToolError):
        points3(value)


class _Datum(Object):
    pass


class _Fcf(Object):
    pass


def _pmi_rig(ff, monkeypatch):
    from tests.fakes import point

    datum = _Datum("datum")
    datum.OwningPart = ff.part
    datum.IsOccurrence = False
    datum.AnnotationOrigin = point(10, 10, 0)
    fcf = _Fcf("fcf")
    fcf.OwningPart = ff.part
    fcf.IsOccurrence = False
    fcf.AnnotationOrigin = point(10, 10, 0)
    ff.part.Annotations = MagicMock()
    db, fb = MagicMock(), MagicMock()
    db.Letter = "A"
    db.Commit.return_value = datum
    fb.Commit.return_value = fcf
    ff.part.Annotations.Datums.CreatePmiDatumFeatureSymbolBuilder.return_value = db
    ff.part.Annotations.CreatePmiFeatureControlFrameBuilder.return_value = fb
    a = NS(
        FeatureControlFrameBuilder=NS(
            FcfCharacteristic=NS(Parallelism=1, Flatness=2, Position=3),
            FcfFrameStyle=NS(SingleFrame=1),
        ),
        FeatureControlFrameDataBuilder=NS(
            ToleranceMaterialModifier=NS(
                NotSet=0,
                MaximumMaterialCondition=1,
                LeastMaterialCondition=2,
                RegardlessOfFeatureSize=3,
            ),
            ToleranceZoneShape=NS(NotSet=0, Diameter=1, SphericalDiameter=2, Square=3),
        ),
        DatumReferenceBuilder=NS(
            DatumReferenceMaterialCondition=NS(
                NotSet=0,
                MaximumMaterialCondition=1,
                LeastMaterialCondition=2,
                RegardlessOfFeatureSize=3,
            )
        ),
    )
    a.Datum, a.Fcf = _Datum, _Fcf
    monkeypatch.setitem(sys.modules, "NXOpen.Annotations", a)
    ff.nx.Annotations = a
    return datum, fcf, db, fb
