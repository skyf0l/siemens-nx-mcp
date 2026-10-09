"""Stateful contracts for authoring and review; native geometry is tested separately."""

from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from nx_mcp.authoring import page
from nx_mcp.hardened import IDENTITY
from nx_mcp.legacy_kit import finite
from nx_mcp.runtime import NXToolError
from tests.fakes import Body, Collection, Component, Feature
from tests.kit.objects import Expression

pytestmark = pytest.mark.fake_nx


@pytest.fixture
def author(rig):
    p, s = rig.part, rig.session
    rig.nx.Expression = NS(UnitsOption=NS(Expression="expression"))
    p.Expressions = Collection()
    p.Expressions.CreateNumberExpression = Mock(side_effect=lambda spec, unit: create(spec, unit))

    def create(spec, unit):
        name, formula = spec.split("=", 1)
        exp = Expression(name, formula, unit)
        p.Expressions.append(exp)
        return exp

    def edit(exp, formula):
        if formula == "invalid":
            raise RuntimeError("native formula error")
        exp.RightHandSide = formula

    p.Expressions.EditExpression = Mock(side_effect=edit)
    p.UnitCollection.FindObject = lambda n: NS(
        Name=n,
        Abbreviation={"MilliMeter": "mm", "Inch": "in", "Degrees": "deg", "Radian": "rad"}.get(
            n, n
        ),
    )
    rig.uf.Modeling = NS(
        AskBodyConsistency=Mock(return_value=(0, [], [])),
        AskFaceData=lambda tag: geom(tag),
        AskMinimumDist3=Mock(return_value=(2.0, [0, 0, 10], [0, 0, 12], 0.0)),
    )

    def geom(tag):
        face = next(f for b in p.Bodies for f in b.faces if f.Tag == tag)
        return face.native_type, [0, 0, 0], face.normal, face.box, face.radius, 0, 1

    rig.uf.Curve = NS(AskArcData=lambda tag: NS(Radius=5))
    b = Body()
    p.Bodies.append(b)
    for f in b.faces:
        f.box = [0, 0, 10, 10, 10, 10]
        f.native_type = 22
        f.normal = [0, 0, 1]
        f.radius = 5
    for edge in b.edges:
        edge.box = [0, 0, 0, 10, 10, 0]
        edge.SolidEdgeType = 1
    rig.nx.Edge.EdgeType = NS(Circular=1, Linear=2)
    f = Feature(bodies=[b])
    f.Suppressed = False
    f.GetFeatureErrorMessages = lambda: []
    f.GetFeatureWarningMessages = lambda: []
    p.Features.append(f)
    exp = Expression()
    p.Expressions.append(exp)
    f.expressions = [exp]
    target = Expression("p_limit")
    p.Expressions.append(target)
    builder = NS(
        Limits=NS(StartExtend=NS(Value=target), EndExtend=NS(Value=target)),
        CommitFeature=Mock(),
        Destroy=Mock(),
    )
    p.Features.CreateExtrudeBuilder = lambda _: builder
    view = p.ModelingViews.WorkView

    def camera(m, o, scale):
        view.Matrix = m
        view.Origin = o
        view.Scale = scale

    view.SetRotationTranslationScale = Mock(side_effect=camera)
    rig.ref(b)
    rig.ref(f, "feature")
    rig.ref(exp, "expression")
    for face in b.faces:
        rig.ref(face, "face")
    for edge in b.edges:
        rig.ref(edge, "edge")
    # Save/restore expression and sketch state in addition to existing model seam state.
    original_mark, original_undo = s.SetUndoMark, s.UndoToMark
    extra = {}

    def mark(*args):
        m = original_mark(*args)
        extra[m] = (
            [(x, x.RightHandSide, x.Value) for x in p.Expressions],
            [(sk, list(sk.geometry), list(sk.constraints)) for sk in p.Sketches],
        )
        return m

    def undo(m, *args):
        original_undo(m, *args)
        expressions, sketches = extra[m]
        p.Expressions[:] = [x for x, _, _ in expressions]
        for x, formula, value in expressions:
            x.RightHandSide = formula
            x.Value = value
        for sk, geometry, constraints in sketches:
            sk.geometry[:] = geometry
            sk.constraints[:] = constraints

    s.SetUndoMark = mark
    s.UndoToMark = undo
    rig.body, rig.feature, rig.expression, rig.builder = b, f, exp, builder
    return rig


@pytest.mark.parametrize("value", [None, True, float("nan"), float("inf"), "1", -1, 0])
def test_positive_number_validation(value):
    with pytest.raises(NXToolError):
        finite(value, "value", True)


@pytest.mark.parametrize("offset,limit", [(-1, 1), (0, 0), (0, 201), (True, 1), (0, False)])
def test_page_rejects_unbounded_requests(offset, limit):
    with pytest.raises(NXToolError):
        page([1], offset, limit)


def test_pagination_is_stable_and_complete():
    assert page([1, 2, 3], 0, 2) == {"items": [1, 2], "total": 3, "offset": 0, "next_offset": 2}
    assert page([1, 2, 3], 2, 2)["next_offset"] is None


def test_query_highlight_failure_clears_partial_selection(author):
    r = author
    e = r.e
    e._highlight_objects([r.ref(r.body)])
    assert r.body.highlighted
    r.body.faces[0].Highlight = Mock(side_effect=RuntimeError("highlight failed"))
    with pytest.raises(RuntimeError):
        e._highlight_objects([r.ref(r.body), r.ref(r.body.faces[0], "face")])
    assert not r.body.highlighted and not e._highlighted_objects


def test_health_reports_faults_suppression_and_paginates(author):
    r = author
    e = r.e
    assert e._model_health()["healthy"]
    r.feature.GetFeatureErrorMessages = lambda: ["broken reference"]
    r.feature.GetFeatureWarningMessages = lambda: ["out of date"]
    r.feature.Suppressed = True
    r.body.IsSolidBody = False
    r.uf.Modeling.AskBodyConsistency.return_value = (1, [100], [r.body.faces[0].Tag])
    report = e._model_health(limit=1)
    assert (
        report["error_count"] == 2 and report["warning_count"] == 1 and report["next_offset"] == 1
    )
    assert not report["healthy"] and report["bodies_checked"] == 1
    assert e._model_health(offset=99)["items"] == []


def test_health_assembly_unique_prototypes_and_unloaded(author):
    r = author
    root = Component("root")
    r.part.ComponentAssembly.RootComponent = root
    a = Component("a", parent=root)
    a.Prototype = r.part
    b = Component("b", parent=root)
    b.Prototype = r.part
    b.IsSuppressed = True
    Component("unloaded", parent=root).Prototype = None
    report = r.e._model_health("assembly")
    assert report["parts_checked"] == 1 and report["warning_count"] == 1
    assert {i["kind"] for i in report["items"]} == {"suppressed_component", "unloaded_component"}
    with pytest.raises(NXToolError):
        r.e._model_health("bad")


def test_health_missing_api_is_explicit(author):
    del author.uf.Modeling.AskBodyConsistency
    with pytest.raises(NXToolError, match="AskBodyConsistency"):
        author.e._model_health()


def test_camera_validation_and_readback(author):
    e = author.e
    with pytest.raises(NXToolError):
        e._set_camera([[1, 0, 0]] * 3, [0, 0, 0], 1)
    with pytest.raises(NXToolError):
        e._set_camera(IDENTITY, [1, 2], 1)
    assert e._set_camera(IDENTITY, [1, 2, 3], 3)["origin"] == [1, 2, 3]


def test_temporary_presentation_restores_on_exception(author):
    e = author.e
    before = e._view_info()
    with pytest.raises(RuntimeError), e._temporary_view():
        author.body.Blank()
        e._set_camera(IDENTITY, [5, 5, 5], 2)
        raise RuntimeError("capture")
    assert not author.body.IsBlanked and e._view_info()["origin"] == before["origin"]


def test_temporary_presentation_cleanup_failure_is_partial(author):
    author.session.UndoToMark = Mock(side_effect=RuntimeError("undo"))
    with pytest.raises(NXToolError) as err, author.e._temporary_view():
        pass
    assert err.value.details["mutation_outcome"] == "partial"


def test_expression_value_uses_its_declared_unit_not_part_unit(author):
    exp = author.expression
    exp.Units = NS(Abbreviation="in")
    exp.Value = 25.4
    exp.expression_value = 1.0
    record = author.e._expression_record(exp)
    assert record["value"] == 1 and record["units"] == "in"


@pytest.mark.asyncio
async def test_authoring_tool_schemas_are_explicit_and_path_scoped(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from nx_mcp.server import create_server
    from nx_mcp.workspace import Workspace

    monkeypatch.setenv("NX_MCP_DEV", "1")
    bridge = NS(call=AsyncMock(return_value={"ok": True, "data": {}}))
    server = create_server(bridge, Workspace(tmp_path), surface="full")
    tools = {t.name: t for t in await server.list_tools()}
    assert tools["nx_find_geometry"].annotations.readOnlyHint
    await server.call_tool("nx_export_inspection_report", {"path": "reports/review.zip"})
    args = bridge.call.call_args.args[1]
    assert args["path"] == str(tmp_path / "reports" / "review.zip") and args["operation_id"]
