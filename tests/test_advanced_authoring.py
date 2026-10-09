"""Failure boundaries and selection semantics; native fixtures test NX geometry."""

import copy
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from nx_mcp.runtime import NXToolError
from tests.fakes import Component, Curve, Face, Object, Sketch, point
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


def test_exact_distance_ranks_surface_not_center(author):
    a = author.body.faces[0]
    b = Face()
    b.box = [0, 0, 9, 1, 1, 9]
    b.native_type, b.normal, b.radius = 22, [0, 0, 1], 0
    a.box = [-100, -100, 10, 100, 100, 10]
    author.body.faces.append(b)
    author.uf.Modeling.AskMinimumDist3.side_effect = lambda level, tag, *args: (
        1 if tag == a.Tag else 5,
        [0, 0, 10],
        [0, 0, 11],
        0,
    )
    result = author.e._find_geometry(near=[0, 0, 11])
    assert result["items"][0]["object"]["id"] == author.ref(a, "face")
    assert result["items"][0]["distance"] == 1
    assert result["ranking"] == "native BREP minimum distance"
    author.uf.Modeling.AskMinimumDist3.assert_called_with(
        2, b.Tag, 0, 0, [0.0, 0.0, 0.0], 1, [0.0, 0.0, 11.0]
    )


def test_selection_rule_requeries_and_rejects_ties(author):
    result = author.e._find_geometry(near=[0, 0, 11])
    selector = result["selector"]
    assert author.e._resolve_geometry(selector)["match"]["distance"] == 2
    author.uf.Modeling.AskMinimumDist3.return_value = (8, [0, 0, 3], [0, 0, 11], 0)
    assert author.e._resolve_geometry(selector)["match"]["distance"] == 8
    author.body.faces.append(copy.copy(author.body.faces[0]))
    author.body.faces[-1].Tag += 100000
    with pytest.raises(NXToolError, match="tied"):
        author.e._resolve_geometry(selector)


@pytest.mark.parametrize("change", ["version", "extra", "query", "owner_part", "owner"])
def test_malformed_and_wrong_owner_rules_rejected(author, change):
    selector = author.e._find_geometry(near=[0, 0, 11])["selector"]
    if change == "extra":
        selector["extra"] = 1
    elif change == "version":
        selector["version"] = 2
    elif change == "query":
        selector["query"]["extra"] = 1
    elif change == "owner_part":
        selector["owner_part"] = "other.prt"
    else:
        selector["owner"] = {"kind": "face"}
    with pytest.raises(NXToolError):
        author.e._resolve_geometry(selector)


def test_saved_owner_resolution_and_no_match(author):
    owner = author.ref(author.body, "body")
    selector = author.e._find_geometry(owner=owner, near=[0, 0, 11])["selector"]
    assert selector["owner"]["kind"] == "body"
    assert author.e._resolve_geometry(selector)["match"]
    selector["query"]["geometry_type"] = "cylinder"
    with pytest.raises(NXToolError, match="No geometry"):
        author.e._resolve_geometry(selector)


def test_bore_groups_and_partial_faces(author):
    faces = [
        {
            "object": {"id": "a"},
            "cylindrical_role": "bore",
            "axis": [0, 0, 1],
            "axis_point": [0, 0, 0],
        },
        {
            "object": {"id": "b"},
            "cylindrical_role": "bore",
            "axis": [0, 0, -1],
            "axis_point": [0, 0, 10],
        },
        {
            "object": {"id": "c"},
            "cylindrical_role": "boss",
            "axis": [0, 0, 1],
            "axis_point": [10, 0, 0],
        },
    ]
    author.e._find_geometry = Mock(return_value={"items": faces, "next_offset": None})
    author.e._resolve = Mock(return_value=Object())
    author.uf.Modeling.AskFaceUvMinmax = Mock(
        side_effect=[[0, 6.283185307179586, 0, 10], [0, 3.14, 0, 10]]
    )
    r = author.e._recognize_holes()
    assert r["total"] == 2 and len(r["coaxial_groups"]) == 1
    assert r["items"][0]["full_circumference"] and not r["items"][1]["full_circumference"]


@pytest.mark.parametrize(
    "count,spacing", [(1, 1), (101, 1), (True, 1), (2.5, 1), (2, 0), (2, float("nan"))]
)
def test_native_pattern_invalid_inputs_precede_builder(author, count, spacing):
    with pytest.raises(NXToolError):
        author.e._native_component_pattern("missing", [1, 0, 0], spacing, count)


@pytest.fixture
def patterned(author):
    r = author
    root = Component("root")
    r.part.ComponentAssembly.RootComponent = root
    seed = Component("seed", parent=root)
    r.nx.GeometricUtilities.PatternDefinition = NS(PatternEnum=NS(Linear="linear"))
    r.nx.Vector3d = point
    r.nx.SmartObject = NS(UpdateOption=NS(WithinModeling="model"))
    r.part.Directions = NS(CreateDirection=Mock(return_value="direction"))
    count = Expression("count")
    count.Value = 4
    pitch = Expression("pitch")
    pitch.Value = 20
    pattern = Object("pattern")
    pattern.GetComponentsToPattern = lambda: [seed]
    others = [Component("copy" + str(i), parent=root) for i in range(3)]
    for i, c in enumerate(others):
        c.position = point((i + 1) * 20, 0, 0)
    pattern.GetAllPatternMembers = lambda: [NS(GetAllComponents=lambda: [seed, *others])]
    b = NS(
        Associative=True,
        ComponentPatternSet=NS(Add=Mock()),
        PatternService=NS(
            PatternType="linear",
            PatternEnum=NS(Linear="linear", Circular="circular"),
            RectangularDefinition=NS(
                UseYDirectionToggle=False,
                XSpacing=NS(NCopies=count, PitchDistance=pitch),
                YSpacing=NS(NCopies=Expression("y")),
            ),
        ),
        Commit=Mock(return_value=pattern),
        Destroy=Mock(),
    )
    r.part.ComponentAssembly.CreateComponentPatternBuilder = Mock(return_value=b)
    r.part.ComponentAssembly.ComponentPatterns = NS(GetAllComponentPatterns=lambda: [pattern])
    r.seed, r.pattern, r.pattern_builder = seed, pattern, b
    return r


def test_native_pattern_creation_readback_edit_and_cleanup(patterned):
    r = patterned
    result = r.e._native_component_pattern(r.ref(r.seed, "component"), [1, 0, 0], 20, 4)
    assert result["associative"] and result["total_instances"] == 4
    assert result["native_type"] == "NXOpen.Assemblies.ComponentPattern"
    assert r.e._list_component_patterns()["count"] == 1
    r.e._edit_component_pattern(result["object"]["id"], spacing=22, count=4)
    assert (
        r.pattern_builder.PatternService.RectangularDefinition.XSpacing.PitchDistance.RightHandSide
        == "22.0"
    )
    assert r.pattern_builder.Destroy.call_count == 5


def test_pattern_edit_reads_back_the_pattern_nx_recreated(patterned):
    # NX 2406 deletes and recreates a pattern when an edit turns its second
    # direction off; the old object is dead, so read back what Commit returned.
    r = patterned
    old = r.pattern
    reference = r.ref(old, "component_pattern")
    recreated = Object("recreated")
    recreated.GetComponentsToPattern = old.GetComponentsToPattern
    recreated.GetAllPatternMembers = old.GetAllPatternMembers
    r.pattern_builder.Commit.return_value = recreated
    builders = []
    original = r.part.ComponentAssembly.CreateComponentPatternBuilder

    def create(target):
        builders.append(target)
        return original(target)

    r.part.ComponentAssembly.CreateComponentPatternBuilder = create
    result = r.e._edit_component_pattern(reference, count=4)
    assert builders == [old, recreated]
    assert result["object"]["id"] != reference
    assert "stale" in result["warnings"][0]


def test_pattern_failure_count_and_unsupported_edits(patterned):
    r = patterned
    with pytest.raises(NXToolError, match="count differs"):
        r.e._native_component_pattern(r.ref(r.seed, "component"), [1, 0, 0], 20, 5)
    with pytest.raises(NXToolError, match="Supply"):
        r.e._edit_component_pattern(r.ref(r.pattern, "component_pattern"))
    r.pattern_builder.Associative = False
    with pytest.raises(NXToolError, match="associative"):
        r.e._edit_component_pattern(r.ref(r.pattern, "component_pattern"), spacing=20)
    r.seed.IsSuppressed = True
    with pytest.raises(NXToolError, match="unsuppressed"):
        r.e._native_component_pattern(r.ref(r.seed, "component"), [1, 0, 0], 20, 4)


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
