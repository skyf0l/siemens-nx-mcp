"""nx_set_topology_contour on the fake: plot, limits per result and load case, hide."""

from types import SimpleNamespace as NS

import pytest

from tests.kit import run
from tests.unit.optimization.fakes import topo_nx
from tests.unit.optimization.test_topology import add, ready


def solved(nx, **extra):
    study, plate, holes, end = ready(nx)
    if extra:
        assert add(nx, study, "force", faces=[end.ref], **extra).ok
    assert run(nx, "nx_run_topology_study", study=study).ok
    return study


def contour(nx, study, **args):
    return run(nx, "nx_set_topology_contour", study=study, **args)


def test_stress_contour_reads_the_plotted_range_in_mpa():
    nx = topo_nx()
    study = solved(nx)
    ok = contour(nx, study)
    assert ok.ok, ok
    data = ok.data
    assert data["result"] == "stress" and data["shown"] is True
    assert data["max"] == pytest.approx(85.0) and data["min"] == 0.0 and data["unit"] == "MPa"
    assert data["load_case"] == "Default Subcase" and data["study"]["id"] == study
    b = nx.last("ViewContourBuilder")
    DS = nx.nx.DesignSimulation
    assert b.ResultComponentType is DS.ViewContourBuilder.ResultComponentOption.StressVonMises
    assert b.DisplayType is DS.ViewContourBuilder.DisplayOption.Single
    assert b.ContourDisplayStyle is DS.ViewContourBuilder.ContourDisplayStyleOption.Banded
    assert b.SubcaseIndexForFirstStudy == 0 and b.FirstStudy is nx.refs[study][0].ActiveStudy
    assert nx.live_builders == 0 and ok.mutation_outcome == "committed"


@pytest.mark.parametrize(
    ("result", "low", "high", "unit"),
    [
        ("displacement", 0.0, 0.12, "mm"),
        ("displacement_z", -0.108, 1e-4, "mm"),
        ("principal_stress", -102.0, 93.5, "MPa"),
        ("safety_factor", 250.0 / 85.0, 1e6, None),
    ],
)
def test_other_results(result, low, high, unit):
    nx = topo_nx()
    ok = contour(nx, solved(nx), result=result, style="smooth")
    assert ok.ok, ok
    assert ok.data["min"] == pytest.approx(low) and ok.data["max"] == pytest.approx(high)
    assert ok.data["unit"] == unit
    DS = nx.nx.DesignSimulation
    style = nx.last("ViewContourBuilder").ContourDisplayStyle
    assert style is DS.ViewContourBuilder.ContourDisplayStyleOption.Smooth


def test_load_case_by_name():
    nx = topo_nx()
    nx.case_peaks = [{"Displacement": 0.12, "StressVonMises": 85000.0},
                     {"Displacement": 0.3, "StressVonMises": 120000.0}]  # fmt: skip
    study = solved(nx, magnitude=50, direction="y", load_case="Side")
    ok = contour(nx, study, load_case="side")
    assert ok.ok, ok
    assert ok.data["load_case"] == "Side" and ok.data["max"] == pytest.approx(120.0)
    assert nx.last("ViewContourBuilder").SubcaseIndexForFirstStudy == 1
    unknown = contour(nx, study, load_case="slam")
    assert unknown.error["code"] == "NX_NOT_FOUND" and unknown.error["field"] == "/load_case"
    assert unknown.error["details"]["load_cases"] == ["Default Subcase", "Side"]
    assert unknown.mutation_outcome == "not_started"


def test_none_hides_the_plot_through_the_task():
    nx = topo_nx()
    study = solved(nx)
    assert contour(nx, study).ok
    post = nx.part.PostManager
    assert post.plot is not None
    env = nx.session.TopologyOptimizationTaskEnvironment
    env.log.clear()
    ok = contour(nx, study, result="none")
    assert ok.ok, ok
    assert ok.data["shown"] is False and ok.data["max"] is None
    assert post.plot is None and post.hides == 1
    assert env.log == ["Enter", "Cancel", "Exit"] and env.ActiveFeature is None
    again = contour(nx, study)  # the results survive the cancelled task
    assert again.ok and again.data["max"] == pytest.approx(85.0)


def test_refusals():
    nx = topo_nx()
    study, *_ = ready(nx)
    unsolved = contour(nx, study)
    assert unsolved.error["code"] == "NX_NOT_FOUND" and unsolved.mutation_outcome == "not_started"
    assert "nx_run_topology_study" in unsolved.error["hint"]
    nx.licenses.clear()
    refused = contour(nx, study)
    assert refused.error["code"] == "NX_LICENSE_UNAVAILABLE"


def test_a_plot_that_misses_the_peak_fails_verification():
    nx = topo_nx()
    study = solved(nx)
    post = nx.part.PostManager
    post.GetContourLimits = lambda i: (0.0, 1.0, NS(Name="StressNewtonPerSquareMilliMeter"))
    bad = contour(nx, study)
    assert bad.error["code"] == "NX_VERIFICATION_FAILED"
    assert bad.error["details"]["check"] == "max"
