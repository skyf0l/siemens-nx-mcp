"""nx_create_spline and the Studio Spline edit definition on the shared fake."""

import pytest

from nx_mcp.core import catalogue
from nx_mcp.core.errors import NXError
from tests.kit import NXException, run
from tests.unit.freeform.fakes import Spline, freeform_nx, studio_spline
from tests.unit.modeling.fakes import feature

POINTS = [[0, 0, 0], [10, 5, 0], [20, 0, 3], [30, 2, 1]]


@pytest.mark.parametrize("method", ["through_points", "poles"])
def test_spline_configures_the_builder_and_reads_back(method):
    nx = freeform_nx()
    ok = run(nx, "nx_create_spline", points=POINTS, method=method, degree=3)
    assert ok.ok and ok.mutation_outcome == "committed", ok
    b = nx.last("StudioSplineBuilderEx")
    assert b.HasPlaneConstraint is False and b.IsAssociative is True and b.Degree == 3
    types = nx.nx.Features.StudioSplineBuilderEx.Types
    assert b.Type is (types.ThroughPoints if method == "through_points" else types.ByPoles)
    assert len(b.ConstraintManager.Append.calls) == 4 and b.ConstraintManager.Clear.calls
    assert ok.data["degree"] == 3 and ok.data["periodic"] is False
    assert ok.data["start"] == [0, 0, 0] and ok.data["end"] == [30, 2, 1]
    assert ok.data["curve"]["kind"] == "curve" and ok.data["feature"]["kind"] == "feature"
    assert nx.live_builders == 0


def test_spline_converts_nothing_and_rejects_bad_degrees_before_nx():
    nx = freeform_nx()
    few = run(nx, "nx_create_spline", points=POINTS[:3], method="poles", degree=3)
    assert few.error["field"] == "/degree" and few.mutation_outcome == "not_started"
    schema = run(nx, "nx_create_spline", points=POINTS, method="poles", degree=8)
    assert schema.error["category"] == "invalid_argument"
    same = run(nx, "nx_create_spline", points=[[0, 0, 0], [0, 0, 0], [1, 1, 1]], method="poles",
               degree=1)  # fmt: skip
    assert same.error["field"] == "/points/1" and not nx.builders


def test_spline_readback_failures_roll_back():
    nx = freeform_nx()
    base = nx.hooks["StudioSplineBuilderEx"]

    def low_degree(b):
        f = base(b)
        b.Curve.Order = 3
        return f

    nx.on_commit("StudioSplineBuilderEx", low_degree)
    bad = run(nx, "nx_create_spline", points=POINTS, method="through_points", degree=3)
    assert bad.error["details"]["check"] == "degree" and bad.mutation_outcome == "rolled_back"

    def moved_pole(b):
        f = base(b)
        b.Curve.poles[1].X = 99
        return f

    nx.on_commit("StudioSplineBuilderEx", moved_pole)
    bad = run(nx, "nx_create_spline", points=POINTS, method="poles", degree=3)
    assert bad.error["details"]["check"] == "pole[1]" and nx.live_builders == 0

    def missed(b):
        f = base(b)
        b.Curve.shape.points[2] = [20, 0, 9]
        return f

    nx.on_commit("StudioSplineBuilderEx", missed)
    bad = run(nx, "nx_create_spline", points=POINTS, method="through_points", degree=3)
    assert bad.error["details"]["check"] == "point[2]"


def test_native_refusal_is_build_failed_with_nx_code():
    nx = freeform_nx()
    nx.refuse("StudioSplineBuilderEx", 670015, "Points too close")
    bad = run(nx, "nx_create_spline", points=POINTS, method="poles", degree=3)
    assert bad.error["code"] == "NX_BUILD_FAILED" and bad.error["nx_code"] == 670015
    assert nx.live_builders == 0


def test_periodic_poles_may_wrap():
    nx = freeform_nx()
    base = nx.hooks["StudioSplineBuilderEx"]

    def wrapped(b):
        f = base(b)
        b.Curve.poles = b.Curve.poles + b.Curve.poles[:3]
        return f

    nx.on_commit("StudioSplineBuilderEx", wrapped)
    ok = run(nx, "nx_create_spline", points=POINTS, method="poles", degree=3, periodic=True)
    assert ok.ok and ok.data["periodic"] is True, ok


def edit(nx, f, value):
    """Call the definition hook as nx_edit_feature does, with a real Tx over the fake."""
    from nx_mcp.core.tx import Tx
    from tests.kit.kernel import services

    hook = catalogue.hooks("definition")["SPLINE"]
    return hook(Tx(services(nx), None, operation_id=None), f, value)


def test_edit_definition_replaces_points_and_keeps_omitted_settings():
    nx = freeform_nx()
    f = studio_spline(nx)
    make = nx.builder

    def builder(kind, *args):
        b = make(kind, *args)
        if kind == "StudioSplineBuilderEx" and args and args[0] is not None:
            b.Degree, b.IsPeriodic = 2, False
            b.Type = nx.nx.Features.StudioSplineBuilderEx.Types.ByPoles
        return b

    nx.builder = builder
    edit(nx, f, {"spline": {"points": [[0, 0, 0], [5, 5, 0], [10, 0, 0]]}})
    b = nx.last("StudioSplineBuilderEx")
    assert b.args == (f,) and b.Degree == 2 and b.commits == 1 and b.destroyed
    assert b.Type is nx.nx.Features.StudioSplineBuilderEx.Types.ByPoles
    with pytest.raises(NXError) as error:
        edit(nx, f, {"spline": {"points": [[0, 0, 0], [5, 5, 0]], "degree": 3}})
    assert error.value.field == "/definition/spline/degree" and nx.live_builders == 0


@pytest.mark.parametrize(
    ("value", "field"),
    [
        ({"transform": {}}, "/definition"),
        ({"spline": {"points": [[0, 0, 0]]}}, "/definition/spline/points"),
        ({"spline": {"points": [[0, 0], [1, 1]]}}, "/definition/spline/points"),
        (
            {"spline": {"points": [[0, 0, 0], [1, 1, 1]], "method": "x"}},
            "/definition/spline/method",
        ),
        ({"spline": {"points": [[0, 0, 0], [1, 1, 1]], "degree": 9}}, "/definition/spline/degree"),
        (
            {"spline": {"points": [[0, 0, 0], [1, 1, 1]], "periodic": 1}},
            "/definition/spline/periodic",
        ),
        ({"spline": {"points": [[0, 0, 0], [1, 1, 1]], "bogus": 1}}, "/definition"),
    ],
)
def test_edit_definition_rejects_bad_values_before_the_builder(value, field):
    nx = freeform_nx()
    f = studio_spline(nx)
    with pytest.raises(NXError) as error:
        edit(nx, f, value)
    assert error.value.code == "NX_INVALID_ARGUMENT" and error.value.field == field
    assert not nx.builders


def test_edit_refuses_other_feature_types_before_the_builder():
    # NX 2406 crashed natively when the Studio Spline builder edited an extrude.
    nx = freeform_nx()
    extrude = feature(nx, "SPLINE")
    with pytest.raises(NXError) as error:
        edit(nx, extrude, {"spline": {"points": [[0, 0, 0], [1, 1, 1]], "degree": 1}})
    assert error.value.code == "NX_WRONG_KIND" and not nx.builders


def test_edit_native_refusal_destroys_the_builder():
    nx = freeform_nx()
    f = studio_spline(nx)
    nx.errors["StudioSplineBuilderEx"] = NXException(670015, "bad")
    with pytest.raises(NXError) as error:
        edit(nx, f, {"spline": {"points": [[0, 0, 0], [1, 1, 1]], "degree": 1}})
    assert error.value.code == "NX_BUILD_FAILED" and nx.live_builders == 0


def test_fake_spline_is_the_polyline():
    s = Spline([[0, 0, 0], [10, 0, 0]], 1, False)
    assert s.shape.closest([5, 3, 0])[0] == 3


def test_spline_defaults_fit_few_points_and_report_length():
    nx = freeform_nx(units="in")
    three = run(nx, "nx_create_spline", points=[[0, 0, 0], [10, 5, 0], [20, 0, 5]])
    assert three.ok and three.data["degree"] == 2, three  # default 3 capped at points - 1
    b = nx.last("StudioSplineBuilderEx")
    assert b.Degree == 2 and b.Type is nx.nx.Features.StudioSplineBuilderEx.Types.ThroughPoints
    two = run(nx, "nx_create_spline", points=[[0, 0, 0], [3, 4, 0]], method="poles")
    assert two.ok and two.data["degree"] == 1 and two.data["length"] == pytest.approx(5.0)
    four = run(nx, "nx_create_spline", points=POINTS)
    assert four.ok and four.data["degree"] == 3


def test_spline_explicit_degree_error_says_what_to_send():
    nx = freeform_nx()
    bad = run(nx, "nx_create_spline", points=POINTS[:3], degree=3)
    assert bad.error["field"] == "/degree" and "got 3" in bad.error["message"]
    assert "degree 2" in bad.error["hint"] and not nx.builders


def test_spline_ends_drop_round_off():
    nx = freeform_nx()
    ok = run(nx, "nx_create_spline", points=[[-6e-16, 1e-12, 0], [10, 5, 0], [20, 0, 3]])
    assert ok.ok and ok.data["start"] == [0.0, 0.0, 0.0], ok
