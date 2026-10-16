import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import builders
from tests.kit import NXException, fake_nx


def test_build_configures_commits_and_destroys():
    nx = fake_nx()
    with builders.build(nx.tx, nx.part.Features.CreateShellBuilder, None) as b:
        b.Tolerance = 0.001
        b.DefaultThickness.RightHandSide = "2"
        feature = b.commit()
    native = nx.last("ShellBuilder")
    assert native.args == (None,) and native.Tolerance == 0.001
    assert native.DefaultThickness.RightHandSide == "2"
    assert native.commits == 1 and b.committed is feature and native.result is feature
    assert native.destroyed and nx.live_builders == 0


def test_commit_uses_commit_without_commit_feature():
    nx = fake_nx()
    nx.on_commit("SketchInPlaceBuilder", lambda b: "sketch")
    nx.lacks("SketchInPlaceBuilder", "CommitFeature")
    with builders.build(nx.tx, nx.part.Sketches.CreateSketchInPlaceBuilder, None) as b:
        assert b.commit() == "sketch"
    assert nx.live_builders == 0


def test_option_is_skipped_where_this_nx_build_lacks_it():
    nx = fake_nx()
    nx.lacks("DeleteFaceBuilder", "AllowPartialDelete")
    with builders.build(nx.tx, nx.part.Features.CreateDeleteFaceBuilder, None) as b:
        assert b.has("Heal") and not b.has("AllowPartialDelete")
        assert b.option("AllowPartialDelete", False) is False
        assert b.option("Heal", True) is True
        b.commit()
    assert nx.last("DeleteFaceBuilder").Heal is True


def test_native_refusal_maps_and_still_destroys():
    nx = fake_nx()
    nx.refuse("EdgeBlendBuilder", 670015, "Blend radius too large")
    with (
        pytest.raises(NXError) as error,
        builders.build(nx.tx, nx.part.Features.CreateEdgeBlendBuilder, None) as b,
    ):
        b.commit()
    assert error.value.code == "NX_BUILD_FAILED" and error.value.nx_code == 670015
    assert error.value.category == "geometry_failed"
    assert nx.live_builders == 0


def test_codes_map_and_license_failures_stay_licenses():
    nx = fake_nx()
    nx.refuse("ExtrudeBuilder", 3680011)
    with (
        pytest.raises(NXError) as error,
        builders.build(
            nx.tx, nx.part.Features.CreateExtrudeBuilder, None, codes={3680011: "NX_WRONG_GEOMETRY"}
        ) as b,
    ):
        b.commit()
    assert error.value.code == "NX_WRONG_GEOMETRY"

    def unlicensed(*_):
        raise NXException(949895, "The requested license feature is not available")

    with pytest.raises(NXError) as error, builders.build(nx.tx, unlicensed):
        pass
    assert error.value.code == "NX_LICENSE_UNAVAILABLE" and error.value.nx_code == 949895


def test_validate_refusal_and_python_errors():
    nx = fake_nx()
    with (
        pytest.raises(NXError) as error,
        builders.build(nx.tx, nx.part.Features.CreateBridgeSurfaceBuilder, None) as b,
    ):
        b.native.valid = False
        b.commit(validate=True)
    assert error.value.code == "NX_BUILD_FAILED" and nx.last("BridgeSurfaceBuilder").commits == 0
    with (
        pytest.raises(NXError) as error,
        builders.build(nx.tx, nx.part.Features.CreateShellBuilder, None),
    ):
        raise KeyError("bug")
    assert error.value.code == "NX_ERROR"  # a Python bug is never a geometry failure
    assert nx.live_builders == 0


def test_destroy_failure_never_hides_the_original_error():
    nx = fake_nx()
    with (
        pytest.raises(NXError) as error,
        builders.build(nx.tx, nx.part.Features.CreateShellBuilder, None) as b,
    ):
        b.native.Destroy()  # a second Destroy() will fail
        raise NXException(670015, "refused")
    assert error.value.nx_code == 670015


def test_smart_objects_and_collectors():
    nx = fake_nx()
    p = builders.point(nx.tx, [1, 2, 3])
    assert (p.Coordinates.X, p.Coordinates.Y, p.Coordinates.Z) == (1.0, 2.0, 3.0)
    d = builders.direction(nx.tx, [0, 0, 5], origin=(1, 0, 0))
    assert (d.Vector.X, d.Vector.Y, d.Vector.Z) == (0, 0, 1) and d.Origin.X == 1.0
    with pytest.raises(NXError):
        builders.direction(nx.tx, [0, 0, 0])
    block = nx.block(10, 10, 10)
    c = builders.collector(nx.tx, [block.edges[0].ref, block.edges[1]], "Edge")
    assert c.GetRules() == [("EdgeDumb", block.edges[:2])]
    with pytest.raises(ValueError):
        builders.collector(nx.tx, [], "Curve")
    own = nx.part.ScCollectors.CreateCollector()  # a builder's own collector
    assert builders.fill(nx.tx, own, [block.ref], "Body") is own
    assert own.GetRules() == [("BodyDumb", [block])]
    calls = []
    nx.part.ScRuleFactory.CreateRuleBodyDumb = lambda objs, *flags: calls.append(flags) or (
        "BodyDumb", list(objs))  # fmt: skip
    builders.fill(nx.tx, own, [block], "Body", sheets=True)
    builders.fill(nx.tx, own, [block], "Body")
    assert calls == [(True,), ()]  # includeSheetBodies only when asked
    with pytest.raises(ValueError):
        builders.fill(nx.tx, own, [block.edges[0]], "Edge", sheets=True)
    s = nx.sketch()
    section = builders.section(nx.tx, s)
    assert section is not None
    # A builder's own section (rib, tube) is filled in place.
    from tests.kit.fake import FakeSection

    own = FakeSection()
    assert builders.section(nx.tx, s, into=own) is own and len(own.added) == 1
    edge = block.edges[0]
    assert builders.curve_section(nx.tx, [edge], into=own) is own
    assert own.added[-1][0] == [("EdgeDumb", [edge])] and own.allowed is not None
    assert own.DistanceTolerance == own.ChainingTolerance == pytest.approx(0.001)


def test_destroy_helper():
    nx = fake_nx()
    a, b = nx.builder("A"), nx.builder("B")
    builders.destroy(a, None, b)
    assert nx.live_builders == 0
    with pytest.raises(NXException):
        builders.destroy(a)


def test_first_takes_the_first_member_this_build_has():
    first = builders.first
    newer = type("AnalysisObjectsEx", (), {"__module__": "NXOpen.GeometricAnalysis"})
    owner = newer()
    owner.CreateOld = "old"
    assert first(owner, "CreateNew", "CreateOld") == "old"
    owner.CreateNew = "new"
    assert first(owner, "CreateNew", "CreateOld") == "new"
    with pytest.raises(NXError) as missing:
        first(newer(), "CreateNew", "CreateOld")
    error = missing.value
    assert error.code == "NX_VERSION_UNSUPPORTED" and error.retry == "never"
    member = "NXOpen.GeometricAnalysis.AnalysisObjectsEx.CreateNew|CreateOld"
    assert error.details["member"] == member
    assert "nothing changed" in error.message
