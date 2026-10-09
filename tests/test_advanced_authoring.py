"""Failure boundaries and selection semantics; native fixtures test NX geometry."""

from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from tests.fakes import Curve, Object, Sketch, point
from tests.test_authoring_review import Expression
from tests.test_authoring_review import author as author_fixture

pytestmark = pytest.mark.fake_nx


@pytest.fixture
def author(rig, monkeypatch):
    import sys
    from types import ModuleType

    r = author_fixture.__wrapped__(rig)
    r.e._active_mark = None
    module = ModuleType("NXOpen.GeometricUtilities")
    module.PatternDefinition = NS(PatternEnum=NS(Linear="linear"))
    monkeypatch.setitem(sys.modules, "NXOpen.GeometricUtilities", module)
    r.nx.GeometricUtilities = module
    return r


@pytest.fixture
def constrained(author):
    r = author

    class Line(Curve):
        pass

    class Arc(Curve):
        pass

    r.nx.Line, r.nx.Arc = Line, Arc
    sk = Sketch(r.session)
    r.part.Sketches.append(sk)
    sk.Update = Mock()
    a, b, arc = Line(), Line(), Arc()
    for c in (a, b):
        c.StartPoint, c.EndPoint = point(), point(10, 0, 0)
    sk.geometry = [a, b, arc]
    r.nx.Sketch.DimensionGeometry = lambda: NS(Geometry=None, AssocType=None)
    r.nx.Sketch.ConstraintGeometry = lambda: NS(Geometry=None, PointType=None)
    r.nx.Sketch.DimensionOption = NS(CreateAsReference="reference", CreateAsDriving="driving")
    r.nx.Sketch.AssocType = NS(StartPoint="start", EndPoint="end")
    r.nx.Sketch.ConstraintPointType = NS(StartVertex="start", EndVertex="end", ArcCenter="center")
    for name in ["ParallelDim", "HorizontalDim", "VerticalDim"]:
        setattr(r.nx.Sketch.ConstraintType, name, name)
    constraint = Object()
    constraint.AssociatedExpression = Expression("dim")
    for name in [
        "CreateDimension",
        "CreateRadialDimension",
        "CreateDiameterDimension",
        "CreateParallelConstraint",
        "CreatePerpendicularConstraint",
        "CreateEqualLengthConstraint",
        "CreateEqualRadiusConstraint",
        "CreateConcentricConstraint",
        "CreateCoincidentConstraint",
    ]:
        setattr(sk, name, Mock(return_value=constraint))
    r.e._relation_residual = Mock(return_value=0.0)
    r.e._sketch_diagnostics = Mock(
        return_value={"solver_status": "UnderConstrained", "constraints": [], "constraint_count": 0}
    )
    for name in [
        "CreateParallelConstraint",
        "CreatePerpendicularConstraint",
        "CreateEqualLengthConstraint",
        "CreateEqualRadiusConstraint",
        "CreateConcentricConstraint",
        "CreateCoincidentConstraint",
    ]:

        def create(*args):
            sk.constraints.append(constraint)
            return constraint

        getattr(sk, name).side_effect = create
    constraint.ConstraintType = 99
    r.sk, r.lines, r.arc = sk, [a, b], arc
    return r
