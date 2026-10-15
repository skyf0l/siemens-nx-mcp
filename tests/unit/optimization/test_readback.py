"""Topology studies on the fake: timeline guard for added bodies, kept faces measured face to
body, result locations in part units, the mass target after a solve, condition read-back."""

import pytest

from tests.kit import run
from tests.kit.objects import NXException
from tests.unit.modeling.fakes import feature
from tests.unit.optimization.fakes import bracket, topo_nx
from tests.unit.optimization.test_topology import add, create, ready


def made_by(nx, body, kind="BLOCK"):
    """A feature that makes ``body``, now (last in the timeline)."""
    f = feature(nx, kind, bodies=[body])
    body.GetFeatures = lambda: [f]
    return f


def test_a_body_made_after_the_study_is_refused_before_the_task():
    nx = topo_nx()
    plate, _holes, _end = bracket(nx)
    early = nx.block(10, 60, 30, origin=(-10, 0, -10))
    made_by(nx, early)
    study = create(nx, plate).data["study"]["id"]
    late = nx.block(5, 5, 5, origin=(40, 20, 10))
    made_by(nx, late)
    env = nx.session.TopologyOptimizationTaskEnvironment
    entered = list(env.log)
    bad = run(nx, "nx_create_topology_environment", study=study, kind="scenery",
              bodies=[early.ref, late.ref])  # fmt: skip
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/bodies/1"
    assert "after the study" in bad.error["message"] and "nx_set_feature_order" in bad.error["hint"]
    assert bad.error["details"]["feature"]["kind"] == "feature"
    assert bad.mutation_outcome == "not_started" and env.log == entered
    region = add(nx, study, "keep_out", bodies=[late.ref])
    assert region.error["field"] == "/bodies/0" and region.mutation_outcome == "not_started"
    assert env.log == entered
    ok = run(nx, "nx_create_topology_environment", study=study, kind="scenery",
             bodies=[early.ref])  # fmt: skip
    assert ok.ok, ok


def test_a_study_that_depends_on_itself_on_exit_is_an_argument_error():
    nx = topo_nx()
    plate, _holes, _end = bracket(nx)
    study = create(nx, plate).data["study"]["id"]
    other = nx.block(5, 5, 5, origin=(200, 0, 0))
    env = nx.session.TopologyOptimizationTaskEnvironment
    exit_ = env.Exit
    calls = []

    def failing_exit():
        calls.append(1)
        if len(calls) == 1:
            raise NXException(600008, "There is an object which depends on itself")
        exit_()

    env.Exit = failing_exit
    bad = run(nx, "nx_create_topology_environment", study=study, kind="scenery",
              bodies=[other.ref])  # fmt: skip
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/bodies"
    assert bad.error["nx_code"] == 600008 and bad.mutation_outcome == "rolled_back"
    assert env.ActiveFeature is None and env.log[-2:] == ["Cancel", "Exit"]


def test_kept_measures_the_face_not_its_centre():
    nx = topo_nx()
    study, _plate, holes, end = ready(nx)

    def distance(a, b, _f1, _p1, _f2, _p2):
        hollow = b == 0  # the face's points: the result was cut away there
        return (9.56 if hollow else 0.0), [0, 0, 0], [0, 0, 0]

    nx.uf.Modeling.AskMinimumDist = distance
    ok = run(nx, "nx_run_topology_study", study=study)
    assert ok.ok, ok
    rows = ok.data["kept"]
    assert len(rows) == 3 and all(r["kept"] and r["gap"] == 0.0 for r in rows)
    assert all(r["contact_share"] == 0.0 for r in rows) and ok.data["all_kept"] is True


def test_contact_share_counts_the_face_points_the_result_reaches():
    nx = topo_nx()
    study, *_ = ready(nx)
    ok = run(nx, "nx_run_topology_study", study=study)
    assert all(r["contact_share"] == 1.0 for r in ok.data["kept"]), ok.data["kept"]


def test_peak_locations_are_in_part_units_inside_the_design_space():
    nx = topo_nx()
    study, *_ = ready(nx)
    nx.peak_at = (20.0, 30.0, 5.0)
    ok = run(nx, "nx_analyze_topology_study", study=study)
    assert ok.ok, ok
    assert ok.data["stress_at"] == pytest.approx([20.0, 30.0, 5.0])
    assert ok.data["displacement_at"] == pytest.approx([20.0, 30.0, 5.0])
    nx.peak_at = (5000.0, 30.0, 5.0)  # at no scale inside the design space
    far = run(nx, "nx_analyze_topology_study", study=study)
    assert far.ok and far.data["displacement_at"] is None


def test_target_mass_stays_the_target_after_a_solve():
    nx = topo_nx()
    study, *_ = ready(nx)
    nx.solve_fraction = 0.35
    ok = run(nx, "nx_run_topology_study", study=study)
    assert ok.ok, ok
    data = ok.data
    assert data["mass_fraction"] == pytest.approx(0.35)
    assert data["target_mass"] == pytest.approx(0.4 * data["design_mass"])
    assert data["target_fraction"] == pytest.approx(0.4)
    info = run(nx, "nx_get_topology_study", study=study).data["items"][0]
    assert info["target_mass"] == pytest.approx(0.4 * info["design_mass"])
    assert info["target_fraction"] == pytest.approx(0.4)
    feature_ = nx.refs[study][0]
    assert len(feature_.expressions) == 2  # the target, then the result's mass
    again = run(nx, "nx_analyze_topology_study", study=study)
    assert again.ok
    target = nx.last("OptimizationConstraintBuilder").Mass
    assert float(target.RightHandSide) == pytest.approx(0.999 * data["design_mass"])


def test_condition_returns_its_row_and_duplicates_are_caught():
    nx = topo_nx()
    plate, holes, end = bracket(nx)
    study = create(nx, plate).data["study"]["id"]
    fixed = add(nx, study, "fixed", faces=[h.ref for h in holes])
    assert fixed.ok
    assert fixed.data["condition"]["type"] == "fixed"
    assert fixed.data["condition"]["face_refs"] == [h.ref for h in holes]
    again = add(nx, study, "fixed", faces=[h.ref for h in reversed(holes)])
    assert again.error["code"] == "NX_ALREADY_EXISTS" and again.error["field"] == "/faces"
    assert again.error["details"]["supports_index"] == 0
    assert again.mutation_outcome == "not_started"
    other = add(nx, study, "pinned", faces=[h.ref for h in holes])
    assert other.ok  # another type is not a duplicate
    force = add(nx, study, "pressure", faces=[end.ref], magnitude=0.5)
    assert force.ok
    row = force.data["condition"]
    assert (row["type"], row["magnitude"], row["unit"]) == ("pressure", 0.5, "MPa")
    assert row["direction"] is None
    more = add(nx, study, "pressure", faces=[end.ref], magnitude=0.5)
    assert more.ok and any("add up" in w["message"] for w in more.warnings)
    region = add(nx, study, "keep_in", faces=[end.ref])
    assert region.ok and region.data["condition"] is None


def test_acceleration_defaults_to_gravity():
    nx = topo_nx()
    study, *_ = ready(nx)
    ok = run(nx, "nx_create_topology_environment", study=study, kind="acceleration",
             magnitude=1)  # fmt: skip
    assert ok.ok, ok
    assert ok.data["acceleration"]["direction"] == [0, 0, -1]


def test_a_material_name_matches_the_library_in_any_case():
    nx = topo_nx()
    plate, _holes, _end = bracket(nx)
    ok = create(nx, plate, material="steel")
    assert ok.data["material"] == "Steel"
