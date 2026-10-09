"""Boundary/cleanup regressions; native geometry evidence is kept separately."""

import inspect
import math
import sys
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, Mock

import pytest

from nx_mcp import assembly_documentation_server, freeform_server, manufacturing_server
from nx_mcp.animation import interpolate_rotation
from nx_mcp.freeform import points3
from nx_mcp.hardened import IDENTITY, READ_ONLY
from nx_mcp.runtime import NXToolError
from tests.fakes import Body, Edge, Face, Feature, Object, point


@pytest.fixture
def ff(rig, monkeypatch):
    r = rig
    r.body = Body()
    r.body.IsOccurrence = False
    r.body.OwningPart = r.part
    r.part.Bodies.append(r.body)
    r.feature = Feature(bodies=[r.body])
    r.feature.FeatureType = "STUDIO_SPLINE"
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
        StudioSplineBuilderEx=NS(Types=NS(ThroughPoints=1, ByPoles=2)),
        StudioSpline=Feature,
        ThroughCurveMeshBuilder=NS(BodyPreferenceTypes=NS(Sheet=1)),
        BridgeSurfaceBuilder=NS(EndObjectType=NS(Edge=1)),
        SewBuilder=NS(Types=NS(Sheet=1), BodyPreferenceTypes=NS(Solid=2, Sheet=1)),
        DeleteFaceBuilder=NS(SelectTypes=NS(Face=1)),
        TrimSheetBuilder=NS(KeepDiscardOption=NS(Keep=1, Discard=2)),
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
    r.b = MagicMock()
    r.b.Validate.return_value = True
    r.b.CommitFeature.return_value = r.feature
    r.b.Curve = Object()
    r.b.Curve.Order = 4
    r.b.Curve.Periodic = False
    r.b.GetUnsewnBodies.return_value = []
    r.e._freeform_builder = Mock(return_value=r.b)
    r.e._engineering_direction = Mock(return_value=Object())
    r.e._update_model = Mock()
    r.e._model_health = Mock(return_value={"healthy": True, "issues": []})
    r.ref = lambda o, kind: r.e._reference(o, kind, r.part, kind)["id"]
    return r


def test_new_contracts_match_native_handlers(rig):
    for module in [freeform_server, manufacturing_server, assembly_documentation_server]:
        for name, function in vars(module).items():
            if name.startswith("nx_") and inspect.isfunction(function):
                assert name in rig.e._handlers
                assert (
                    inspect.signature(function).parameters.keys()
                    == inspect.signature(rig.e._handlers[name]).parameters.keys()
                )
        assert module.READ_ONLY <= READ_ONLY


@pytest.mark.parametrize("value", [[], [[0, 1]], [[0, 0, math.inf]], [[True, 0, 0]], "points"])
def test_invalid_point_sets(value):
    with pytest.raises(NXToolError):
        points3(value)


@pytest.mark.parametrize("axis", range(3))
def test_rotation_interpolation_stays_orthonormal_at_half_turn(axis):
    end = [[float(i == j) * (1 if i == axis else -1) for j in range(3)] for i in range(3)]
    for fraction in [0, 0.25, 0.5, 0.75, 1]:
        matrix = interpolate_rotation(IDENTITY, end, fraction)
        for i in range(3):
            for j in range(3):
                assert sum(matrix[i][k] * matrix[j][k] for k in range(3)) == pytest.approx(
                    float(i == j)
                )
    assert interpolate_rotation(IDENTITY, IDENTITY, 0.5) == IDENTITY
    for row, expected in zip(interpolate_rotation(IDENTITY, end, 1), end, strict=True):
        assert row == pytest.approx(expected)


@pytest.mark.parametrize("method", ["through_points", "poles"])
def test_spline_edit_replaces_constraints_and_returns_curve(ff, method):
    result = ff.e._spline(
        [[0, 0, 0], [1, 0, 1], [2, 1, 2], [3, 0, 4]],
        method=method,
        feature=ff.ref(ff.feature, "feature"),
    )
    assert result["curve"]["kind"] == "curve"
    ff.b.ConstraintManager.Clear.assert_called_once()
    assert ff.b.ConstraintManager.Append.call_count == 4
    assert ff.b.HasPlaneConstraint is False
    ff.b.Destroy.assert_called_once()


@pytest.mark.parametrize(
    "kwargs", [{"degree": True}, {"degree": 8}, {"degree": 4}, {"method": "unknown"}]
)
def test_spline_rejects_invalid_parameters_before_builder(ff, kwargs):
    with pytest.raises(NXToolError):
        ff.e._spline([[0, 0, 0], [1, 0, 1], [2, 1, 2], [3, 0, 4]], **kwargs)
    ff.e._freeform_builder.assert_not_called()


def test_spline_edit_refuses_other_feature_types_before_builder(ff):
    # Passing an extrude to the Studio Spline builder crashes NX 2406 natively.
    sys.modules["NXOpen.Features"].StudioSpline = type("StudioSpline", (Feature,), {})
    with pytest.raises(NXToolError) as error:
        ff.e._spline([[0, 0, 0], [1, 1, 1]], degree=1, feature=ff.ref(ff.feature, "feature"))
    assert error.value.code == "NX_OBJECT_TYPE_MISMATCH"
    ff.e._freeform_builder.assert_not_called()


def test_builder_rejection_always_destroys_and_does_not_commit(ff):
    ff.b.Validate.return_value = False
    with pytest.raises(NXToolError):
        ff.e._thicken([ff.ref(ff.faces[0], "face")], 2)
    ff.b.CommitFeature.assert_not_called()
    ff.b.Destroy.assert_called_once()


def test_thicken_uses_explicit_native_tolerance(ff):
    ff.e._thicken([ff.ref(ff.faces[0], "face")], 2, -1)
    assert ff.b.Tolerance == 0.001
    assert ff.b.FirstOffset.RightHandSide == "2.0"
    assert ff.b.SecondOffset.RightHandSide == "-1.0"
    with pytest.raises(NXToolError):
        ff.e._thicken([ff.ref(ff.faces[0], "face")], 2, 2)


def test_sampled_thickness_retains_unresolved_samples(ff):
    source = ff.faces[0]
    data = ([0.0, 0.0, 5.0], None, None, None, None, [0.0, 0.0, 1.0], [1e30, 1e30])
    ff.e._face_samples = Mock(return_value=([(source, [0, 0], data)] * 2, 1))
    ff.uf.Modeling.AskPointContainment.side_effect = [1, 2]
    ff.uf.Modeling.TraceARay.return_value = (1, [NS(HitPoint=[0.0, 0.0, 0.0], HitFace=source.Tag)])
    result = ff.e._wall_thickness(ff.ref(ff.body, "body"))
    assert result["minimum_sampled_thickness"] == 5
    assert result["measured_count"] == 1
    assert result["sample_count"] == 2
    assert result["samples"][1]["status"] == "unresolved"
    assert result["skipped_outside_trim"] == 1


def test_face_sampling_reports_signed_draft_and_ignores_trimmed_outside(ff):
    ff.uf.Modeling.AskFaceUvMinmax.return_value = [0.0, 1.0, 0.0, 1.0]
    ff.uf.Modeling.AskFaceProps.return_value = (
        [0.0, 0.0, 0.0],
        None,
        None,
        None,
        None,
        [0.0, 0.0, -1.0],
        [1e30, 2.0],
    )
    ff.uf.Modeling.AskPointContainment.side_effect = [1, 2, 1, 1]
    result = ff.e._face_analysis(
        [ff.ref(ff.faces[0], "face")], samples_per_axis=2, pull_direction=[0, 0, 1]
    )
    assert result["sample_count"] == 3 and result["skipped_outside_trim"] == 1
    assert result["samples"][0]["signed_draft_degrees"] == -90
    assert result["samples"][0]["principal_curvatures"] == [0, 0.5]


def test_shape_operator_is_parameterization_invariant():
    from nx_mcp.surface_math import continuity_difference, shape_operator

    plane = shape_operator([1, 0, 0], [0, 1, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0])
    reversed_plane = shape_operator([0, 3, 0], [2, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0])
    assert continuity_difference(plane, reversed_plane) == (0, 0)
    cylinder = shape_operator([0, 2, 0], [0, 0, 1], [-2, 0, 0], [0, 0, 0], [0, 0, 0])
    scaled = shape_operator([0, 4, 0], [0, 0, 3], [-8, 0, 0], [0, 0, 0], [0, 0, 0])
    assert continuity_difference(cylinder, scaled) == pytest.approx((0, 0))
    assert abs(cylinder[1][1][1]) == 0.5
    with pytest.raises(NXToolError):
        shape_operator([0, 0, 0], [1, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0])


def test_v2406_flat_direction_radius_reads_as_zero_curvature(ff):
    # NX 2406 reports a cylinder's straight direction as 1e10 inch (3.937e8 mm).
    ff.uf.Modeling.AskFaceUvMinmax.return_value = [0.0, 1.0, 0.0, 1.0]
    ff.uf.Modeling.AskFaceProps.return_value = (
        [1.0, 0.0, 0.0],
        None,
        None,
        None,
        None,
        [1.0, 0.0, 0.0],
        [1.0, 393700786.4015748],
    )
    ff.uf.Modeling.AskPointContainment.return_value = 1
    sample = ff.e._face_analysis([ff.ref(ff.faces[0], "face")], samples_per_axis=1)["samples"][0]
    assert sample["principal_radii"] == [1.0, None]
    assert sample["principal_curvatures"] == [1.0, 0.0]


class _NativeRefusal(Exception):
    ErrorCode = 670946


@pytest.mark.parametrize(
    "tool,code",
    [
        ("mesh", "NX_INVALID_GEOMETRY"),
        ("trim", "NX_INVALID_GEOMETRY"),
        ("sew", "NX_INCOMPLETE_SEW"),
    ],
)
def test_native_refusal_of_bad_inputs_is_a_stable_code(ff, tool, code):
    curve = Object("curve")
    curve.IsOccurrence, curve.OwningPart = False, ff.part
    ref = ff.ref(curve, "curve")
    other = Body("other")
    other.OwningPart = ff.part
    ff.part.Bodies.append(other)
    ff.part.CreateRegionPoint = Mock(return_value=Object())
    ff.b.CommitFeature.side_effect = _NativeRefusal("Strings do not intersect within tolerance.")
    with pytest.raises(NXToolError) as error:
        if tool == "mesh":
            ff.e._surface_mesh([[ref], [ref]], [[ref], [ref]])
        elif tool == "trim":
            ff.body.IsSolidBody = False
            ff.e._trim_sheet(ff.ref(ff.body, "body"), [ref], [0, 0, 0])
        else:
            ff.e._sew(ff.ref(ff.body, "body"), [ff.ref(other, "body")])
    assert error.value.code == code and error.value.nx_code == 670946
    assert "do not intersect" in error.value.message
    ff.b.Destroy.assert_called_once()


def test_sew_rejects_partial_or_sheet_fallback(ff):
    other = Body("other")
    other.OwningPart = ff.part
    ff.part.Bodies.append(other)
    target, tool = ff.ref(ff.body, "body"), ff.ref(other, "body")
    assert ff.e._sew(target, [tool])["body_count"] == 1
    ff.b.GetUnsewnBodies.return_value = [other]
    with pytest.raises(NXToolError, match="could not be sewn"):
        ff.e._sew(target, [tool])
    ff.b.GetUnsewnBodies.return_value = []
    ff.body.IsSolidBody = False
    with pytest.raises(NXToolError, match="instead of"):
        ff.e._sew(target, [tool], solid=True)
    with pytest.raises(NXToolError, match="also"):
        ff.e._sew(target, [target])


def test_surface_sections_and_trim_use_native_section_objects(ff):
    curve = Object("curve")
    curve.IsOccurrence = False
    curve.OwningPart = ff.part
    ref = ff.ref(curve, "curve")
    ff.e._surface_mesh([[ref], [ref]], [[ref], [ref]])
    assert ff.b.PrimaryCurvesList.Append.call_count == 2
    assert ff.b.CrossCurvesList.Append.call_count == 2
    with pytest.raises(NXToolError):
        ff.e._surface_mesh([[ref]], [[ref], [ref]])
    ff.body.IsSolidBody = False
    ff.part.CreateRegionPoint = Mock(return_value=Object())
    ff.e._trim_sheet(ff.ref(ff.body, "body"), [ref], [0, 0, 0], keep=False)
    ff.b.BoundaryObjects.Add.assert_called_with(ff.part.Sections.CreateSection.return_value)
    ff.b.Regions.Append.assert_called_once()


def test_bridge_explicit_continuity_and_reverse(ff):
    edge = ff.body.GetEdges()[0]
    result = ff.e._bridge_surface(
        ff.ref(edge, "edge"), ff.ref(edge, "edge"), continuity="G2", reverse_second=True
    )
    assert result["body_count"] == 1
    assert ff.b.FirstEdgeContinuity.ContinuityType == 2
    assert ff.b.IsSecondEdgeReversed is True
    with pytest.raises(NXToolError):
        ff.e._bridge_surface("x", "y", continuity="C9")


def _bridge_edges(ff):
    first, second = ff.body.GetEdges()[0], Edge("second")
    second.IsOccurrence, second.OwningPart = False, ff.part
    ff.body.edges.append(second)
    first.GetFaces = lambda: [ff.faces[0]]
    second.GetFaces = lambda: [ff.faces[1]]
    first.GetVertices = lambda: (point(20, 0, 0), point(20, 10, 0))
    second.GetVertices = lambda: (point(30, 10, 5), point(30, 0, 5))
    return ff.ref(first, "edge"), ff.ref(second, "edge")


class _LicenseError(Exception):
    ErrorCode = 949895


@pytest.mark.parametrize("reverse,start", [(False, (30, 0, 5)), (True, (30, 10, 5))])
def test_bridge_without_studio_license_uses_through_curves(ff, reverse, start):
    first, second = _bridge_edges(ff)
    sys.modules["NXOpen.Features"].ThroughCurvesBuilder = NS(BodyPreferenceTypes=NS(Sheet=1))
    ff.e._freeform_builder = Mock(side_effect=[_LicenseError("no license"), ff.b])
    result = ff.e._bridge_surface(first, second, continuity="G1", reverse_second=reverse)
    assert result["construction"] == "through_curves" and result["body_count"] == 1
    assert ff.e._freeform_builder.call_args_list[1].args == ("CreateThroughCurvesBuilder",)
    helps = [
        c.args[4] for c in ff.part.Sections.CreateSection.return_value.AddToSection.call_args_list
    ]
    # The second section starts at the end facing the first edge's start unless reversed.
    assert [(p.X, p.Y, p.Z) for p in helps] == [(20, 0, 0), start]
    for end in [ff.b.FirstSectionContinuity, ff.b.LastSectionContinuity]:
        assert end.ContinuityType == 1
        end.ConstraintFaces.ReplaceRules.assert_called_once()
    ff.b.Destroy.assert_called_once()


def test_bridge_fallback_failure_is_stable_and_other_errors_propagate(ff):
    first, second = _bridge_edges(ff)
    sys.modules["NXOpen.Features"].ThroughCurvesBuilder = NS(BodyPreferenceTypes=NS(Sheet=1))

    class Twisted(Exception):
        ErrorCode = 670929

    ff.b.CommitFeature.side_effect = Twisted("Cannot create B-surface by through curves.")
    ff.e._freeform_builder = Mock(side_effect=[_LicenseError("no license"), ff.b])
    with pytest.raises(NXToolError) as error:
        ff.e._bridge_surface(first, second, reverse_second=True)
    assert error.value.code == "NX_INVALID_GEOMETRY" and error.value.nx_code == 670929
    ff.b.Destroy.assert_called_once()
    ff.e._freeform_builder = Mock(side_effect=RuntimeError("other"))
    with pytest.raises(RuntimeError):
        ff.e._bridge_surface(first, second)


def test_curve_analysis_keeps_singular_samples(ff):
    edge = ff.body.GetEdges()[0]
    ff.nx.Edge = type(edge)
    ff.uf.ModlGeneral.EvaluateCurve.side_effect = [
        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        [1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 2.0, 0.0],
    ]
    result = ff.e._curve_analysis(ff.ref(edge, "curve"), samples=2)
    assert result["samples"][0]["singular"]
    assert result["samples"][1]["curvature"] == 2
    assert result["samples"][1]["radius"] == 0.5
    with pytest.raises(NXToolError):
        ff.e._curve_analysis("x", samples=1)


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


def test_animation_renders_absolute_poses_and_restores_after_failure(ff, tmp_path):
    from pathlib import Path

    ff.e._explosion_context = lambda: ff.part
    ff.e._explosion = lambda _: NS(Tag=99)
    ff.e._exploded_tree = lambda _: {1: (None, None, None, False)}
    ff.e._exploded_record = lambda _: {
        "component": {"id": "component"},
        "assembled_translation": [0, 0, 0],
        "translation": [20, 10, 0],
        "assembled_rotation_matrix": IDENTITY,
        "rotation_matrix": IDENTITY,
    }
    ff.e._explosion_uf = lambda: ff.uf.Assem
    ff.uf.Assem.AskViewExplosion.return_value = 0
    ff.session.IsBatch = False
    ff.part.DrawingSheets = NS(CurrentDrawingSheet=None)
    ff.part.ModelingViews = NS(WorkView=NS(Tag=2, UpdateDisplay=Mock()))
    ff.session.UndoToMark = Mock()
    ff.session.DeleteUndoMark = Mock()
    ff.e._edit_explosion = Mock()

    def render(path, **_):
        Path(path).write_bytes(b"native png fixture")
        return {"camera": {"scale": 1}}

    ff.e._render_view = Mock(side_effect=render)
    destination = ff.e.workspace.root / "animation.html"
    result = ff.e._export_explosion_animation("ex", str(destination), frames=3)
    assert result["model_restored"] and result["frame_count"] == 3
    assert "data:image/png;base64," in destination.read_text()
    placements = ff.e._edit_explosion.call_args_list
    assert placements[1].kwargs["placements"][0]["translation"] == [10, 5, 0]
    ff.session.UndoToMark.assert_called_once()
    ff.e._render_view.side_effect = RuntimeError("capture failed")
    failure_path = ff.e.workspace.root / "failed.html"
    with pytest.raises(RuntimeError, match="capture failed"):
        ff.e._export_explosion_animation("ex", str(failure_path), frames=2)
    assert not failure_path.exists()
    assert not list(ff.e.workspace.root.glob(".nx-animation-*"))
    assert ff.session.UndoToMark.call_count == 2


def test_trace_anchor_maps_assembled_to_exploded_coordinates(ff):
    source = NS(Coordinates=point(10, 25, 0))
    ff.part.Points.CreatePoint.return_value = source
    ff.part.Scalars = NS(CreateScalar=Mock(return_value=Object()))
    ff.nx.Scalar = NS(DimensionalityType=NS(NotSet=0))
    ff.nx.PointCollection = NS(PointOnCurveLocationOption=NS(PercentArcLength=1))
    ff.session.UpdateManager.DoUpdate = Mock(return_value=0)
    component = NS(
        Tag=123,
        JournalIdentifier="component",
        GetPosition=lambda: (point(10, 20, 0), ff.e._nx_matrix([[0, -1, 0], [1, 0, 0], [0, 0, 1]])),
    )
    edge = ff.body.GetEdges()[0]
    edge.JournalIdentifier = "edge"
    ff.uf.Tag.AskTagOfHandle.side_effect = lambda name: {
        "component": 123,
        "edge": int(edge.Tag),
    }.get(name, 0)
    component.Prototype = NS(Bodies=[ff.body])
    component.FindOccurrence = lambda _: edge
    exploded = NS(GetPosition=lambda: (point(100, 0, 0), ff.e._nx_matrix(IDENTITY)))
    ff.e._exploded_tree = lambda _: {1: (exploded, component, ["component"], False)}
    assert ff.e._trace_position(
        "ex", {"edge": "edge", "percent": 50, "component": "component"}
    ) == [105, 0, 0]
    with pytest.raises(NXToolError, match="missing"):
        ff.e._trace_position("ex", {"edge": "edge", "percent": 50, "component": "missing"})


def test_managed_trace_refresh_handles_collapsed_and_expanded_states(ff):
    import json

    line = MagicMock()
    line.AskExplosion.return_value = "ex"
    line.HasUserAttribute.return_value = True
    line.GetStringAttribute.return_value = json.dumps([{"point": "first"}, {"point": "second"}])
    ff.part.Tracelines = [line]
    ff.nx.NXObject = NS(AttributeType=NS(String=1))
    ff.session.UpdateManager.DoUpdate = Mock(return_value=0)
    ff.e._trace_position = Mock(side_effect=[[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]])
    ff.e._refresh_explosion_traces("ex")
    line.Blank.assert_called_once()
    line.StartPoint.SetCoordinates.assert_not_called()
    ff.e._trace_position.side_effect = [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]]
    ff.e._refresh_explosion_traces("ex")
    line.Unblank.assert_called_once()
    assert line.EndPoint.SetCoordinates.call_args.args[0].X == 10
