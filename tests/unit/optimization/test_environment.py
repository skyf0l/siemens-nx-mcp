"""Topology study surroundings and load cases on the fake: acceleration, scenery bodies,
connections, supports on scenery faces, named load cases."""

import pytest

from tests.kit import run
from tests.unit.optimization.fakes import bracket, topo_nx
from tests.unit.optimization.test_topology import add, create, ready


def env(nx, study, kind, **args):
    return run(nx, "nx_create_topology_environment", study=study, kind=kind, **args)


def test_acceleration_is_written_in_m_per_s2_and_read_back_in_g():
    nx = topo_nx()
    study, *_ = ready(nx)
    ok = env(nx, study, "acceleration", magnitude=5, direction="-z")
    assert ok.ok, ok
    assert ok.data["acceleration"]["g"] == pytest.approx(5.0)
    assert ok.data["acceleration"]["direction"] == [0, 0, -1]
    b = nx.last("EnvironmentLoadBuilder")
    assert b.GravityEnabled is True
    assert float(b.GravityMagnitude.RightHandSide) == pytest.approx(5 * 9.80665)
    again = env(nx, study, "acceleration", magnitude=2, direction=[1, 0, 0])
    assert again.data["acceleration"]["g"] == pytest.approx(2.0)  # replaces the last one
    info = run(nx, "nx_get_topology_study", study=study).data["items"][0]
    assert info["acceleration"]["g"] == pytest.approx(2.0)
    off = env(nx, study, "acceleration", magnitude=0)
    assert off.ok and off.data["acceleration"] is None
    assert nx.last("EnvironmentLoadBuilder").GravityEnabled is False
    assert nx.live_builders == 0


def test_inch_part_acceleration_stays_in_m_per_s2():
    nx = topo_nx(units="in")
    study, *_ = ready(nx)
    ok = env(nx, study, "acceleration", magnitude=1, direction="-z")
    assert ok.ok and ok.data["acceleration"]["g"] == pytest.approx(1.0)
    assert float(nx.last("EnvironmentLoadBuilder").GravityMagnitude.RightHandSide) == (
        pytest.approx(9.80665)
    )


def test_scenery_connection_and_supports_on_scenery_faces():
    nx = topo_nx()
    plate, holes, end = bracket(nx)
    base = nx.block(10, 60, 30, origin=(-10, 0, -10))
    study = create(nx, plate).data["study"]["id"]
    early = env(nx, study, "connection", bodies=[plate.ref, base.ref])
    assert early.error["field"] == "/bodies/1" and early.mutation_outcome == "not_started"
    ok = env(nx, study, "scenery", bodies=[base.ref])
    assert ok.ok and [r["id"] for r in ok.data["scenery"]] == [base.ref], ok
    DS = nx.nx.DesignSimulation
    sb = nx.last("SceneryBodyBuilder")
    assert sb.MaterialDefinition is DS.SceneryBodyBuilder.MaterialDefinitionType.InheritedFromBody
    twice = env(nx, study, "scenery", bodies=[base.ref])
    assert twice.error["field"] == "/bodies/0"
    bond = env(nx, study, "connection", bodies=[plate.ref, base.ref])
    assert bond.ok and bond.data["connections"] == 1
    cb = nx.last("ConnectionBuilder")
    assert cb.Type is DS.ConnectionBuilder.Types.BodyToBody
    assert (cb.SelectBody1.Value, cb.SelectBody2.Value) == (plate, base)
    back = base.face((-1, 0, 0))
    assert add(nx, study, "fixed", faces=[back.ref]).ok  # a scenery face may hold a support
    region = add(nx, study, "keep_in", faces=[back.ref])
    assert region.error["code"] == "NX_WRONG_OWNER"  # regions stay on the design space
    assert add(nx, study, "force", faces=[end.ref], magnitude=10, direction="-z").ok
    info = run(nx, "nx_get_topology_study", study=study).data["items"][0]
    assert info["connections"] == 1 and [r["id"] for r in info["scenery"]] == [base.ref]
    solved = run(nx, "nx_run_topology_study", study=study)
    assert solved.ok and {r["role"] for r in solved.data["kept"]} == {"load"}  # design faces


def test_environment_refusals_before_nx():
    nx = topo_nx()
    plate, _holes, _end = bracket(nx)
    other = nx.block(5, 5, 5, origin=(200, 0, 0))
    study = create(nx, plate).data["study"]["id"]
    entered = list(nx.session.TopologyOptimizationTaskEnvironment.log)
    no_dir = env(nx, study, "acceleration", magnitude=3)
    assert no_dir.error["field"] == "/direction" and no_dir.mutation_outcome == "not_started"
    extra = env(nx, study, "scenery", bodies=[other.ref], magnitude=2)
    assert extra.error["field"] == "/magnitude"
    one = env(nx, study, "connection", bodies=[plate.ref])
    assert one.error["field"] == "/bodies"
    itself = env(nx, study, "scenery", bodies=[plate.ref])
    assert itself.error["field"] == "/bodies/0"
    nx.body_material = None
    bare = env(nx, study, "scenery", bodies=[other.ref])
    assert bare.error["code"] == "NX_INVALID_ARGUMENT" and "material" in bare.error["message"]
    assert nx.session.TopologyOptimizationTaskEnvironment.log == entered


def test_load_cases_take_over_the_default_case_then_share_supports():
    nx = topo_nx()
    plate, holes, end = bracket(nx)
    study = create(nx, plate).data["study"]["id"]
    top = plate.face((0, 0, 1))
    assert add(nx, study, "fixed", faces=[h.ref for h in holes]).ok  # every case
    tip = add(nx, study, "force", faces=[end.ref], magnitude=10, direction="-z", load_case="tip")
    assert tip.ok and tip.data["load_case"] == "tip", tip
    feature = nx.refs[study][0]
    cases = feature.ActiveStudy.subcases
    assert [c.Name for c in cases] == ["tip"]  # NX's empty default case renamed
    slam = add(nx, study, "pressure", faces=[top.ref], magnitude=0.1, load_case="slam")
    assert slam.ok
    cases = feature.ActiveStudy.subcases
    assert [c.Name for c in cases] == ["tip", "slam"]
    support, force = feature.ActiveStudy.supports[0], feature.ActiveStudy.loads[0]
    pressure = feature.ActiveStudy.loads[1]
    assert cases[0].refs == [support, force] and cases[1].refs == [support, pressure]
    same = add(nx, study, "force", faces=[end.ref], magnitude=5, direction="z", load_case="TIP")
    assert same.ok and len(feature.ActiveStudy.subcases) == 2  # names ignore case
    info = run(nx, "nx_get_topology_study", study=study).data["items"][0]
    assert info["load_cases"] == [
        {"name": "tip", "supports": 1, "loads": 2},
        {"name": "slam", "supports": 1, "loads": 1},
    ]
    nx.case_peaks = [{"Displacement": 0.2, "StressVonMises": 50000.0},
                     {"Displacement": 0.1, "StressVonMises": 125000.0}]  # fmt: skip
    ok = run(nx, "nx_analyze_topology_study", study=study)
    assert ok.ok, ok
    rows = ok.data["load_cases"]
    assert [r["name"] for r in rows] == ["tip", "slam"]
    assert [r["max_stress"] for r in rows] == [pytest.approx(50.0), pytest.approx(125.0)]
    assert rows[1]["safety_factor"] == pytest.approx(2.0)
    assert ok.data["max_stress"] == pytest.approx(125.0) and ok.data["max_displacement"] == 0.2
    bad = add(nx, study, "keep_in", faces=[end.ref], load_case="tip")
    assert bad.error["field"] == "/load_case"
