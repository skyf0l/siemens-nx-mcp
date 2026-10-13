"""nx_analyze_topology_study on the fake: peaks, yield, safety factor, refusals."""

import sys
import types

import pytest

from nx_mcp.domains.optimization import common
from tests.kit import run
from tests.unit.optimization.fakes import bracket, topo_nx
from tests.unit.optimization.test_topology import add, create, ready


def test_analysis_reports_peaks_yield_and_safety_factor():
    nx = topo_nx()
    study, _plate, _holes, _end = ready(nx)  # Steel: yield 250 MPa
    ok = run(nx, "nx_analyze_topology_study", study=study)
    assert ok.ok, ok
    data = ok.data
    assert data["max_stress"] == pytest.approx(85.0) and data["max_displacement"] == 0.12
    assert data["yield_strength"] == 250.0 and data["material"] == "Steel"
    assert data["safety_factor"] == pytest.approx(250.0 / 85.0)
    assert data["stress_at"] == [0.0, 10.0, 10.0]
    assert [c["max_stress"] for c in data["load_cases"]] == [pytest.approx(85.0)]
    assert data["load_cases"][0]["safety_factor"] == pytest.approx(250.0 / 85.0)
    assert data["analyzed_fraction"] == pytest.approx(0.4)  # the fake's solve fraction
    feature = nx.refs[study][0]
    assert feature.ActiveStudy.runs == 1
    mass = nx.last("OptimizationConstraintBuilder").Mass  # the study's target expression
    assert float(mass.RightHandSide) == pytest.approx(0.999 * 100 * 60 * 10 * nx.density)
    env = nx.session.TopologyOptimizationTaskEnvironment
    assert env.log[-3:] == ["Enter", "Cancel", "Exit"] and env.ActiveFeature is None
    assert ok.mutation_outcome == "committed"


def test_analysis_without_yield_warns_and_gives_no_factor():
    nx = topo_nx()
    plate, holes, end = bracket(nx)
    study = create(nx, plate, material="Aluminum_6061").data["study"]["id"]
    assert add(nx, study, "fixed", faces=[holes[0].ref]).ok
    assert add(nx, study, "force", faces=[end.ref], magnitude=10, direction="-z").ok
    ok = run(nx, "nx_analyze_topology_study", study=study)
    assert ok.ok and ok.data["safety_factor"] is None and ok.data["yield_strength"] is None
    assert any("yield" in w["message"] for w in ok.warnings)


def test_analysis_refusals():
    nx = topo_nx()
    plate, holes, _end = bracket(nx)
    study = create(nx, plate).data["study"]["id"]
    bare = run(nx, "nx_analyze_topology_study", study=study)
    assert bare.error["code"] == "NX_INVALID_ARGUMENT" and "support" in bare.error["message"]
    assert bare.mutation_outcome == "not_started"
    assert add(nx, study, "fixed", faces=[holes[0].ref]).ok
    no_load = run(nx, "nx_analyze_topology_study", study=study)
    assert "load" in no_load.error["message"] and no_load.mutation_outcome == "not_started"
    unlicensed = topo_nx()
    study2, *_ = ready(unlicensed)
    unlicensed.licenses.clear()
    refused = run(unlicensed, "nx_analyze_topology_study", study=study2)
    assert refused.error["code"] == "NX_LICENSE_UNAVAILABLE"


def test_analysis_without_results_rolls_back():
    nx = topo_nx()
    study, *_ = ready(nx)
    nx.solve_fraction = None  # NX stores no results
    bad = run(nx, "nx_analyze_topology_study", study=study)
    assert bad.error["code"] == "NX_INCOMPLETE_RESULT" and bad.mutation_outcome == "rolled_back"


def test_design_simulation_is_imported_when_nx_has_not_yet(monkeypatch):
    """A fresh NX session lacks NXOpen.DesignSimulation until something imports it."""
    nxopen = types.ModuleType("fake_nxopen")
    namespace = types.ModuleType("fake_nxopen.DesignSimulation")
    monkeypatch.setitem(sys.modules, "fake_nxopen.DesignSimulation", namespace)
    assert common.ds(types.SimpleNamespace(nx=nxopen)) is namespace
