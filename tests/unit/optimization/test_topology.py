"""Topology optimization tools on the fake: create, add conditions, run, get."""

import pytest

from tests.kit import run
from tests.unit.modeling.fakes import feature
from tests.unit.optimization.fakes import bracket, topo_nx


def create(nx, plate, **extra):
    ok = run(nx, "nx_create_topology_study", design_space=plate.ref, mass_fraction=0.4, **extra)
    assert ok.ok, ok
    return ok


def add(nx, study, kind, **args):
    return run(nx, "nx_create_topology_condition", study=study, kind=kind, **args)


def ready(nx):
    """A bracket study with both bolt holes fixed and a force on the end face."""
    plate, holes, end = bracket(nx)
    study = create(nx, plate).data["study"]["id"]
    assert add(nx, study, "fixed", faces=[f.ref for f in holes]).ok
    assert add(nx, study, "force", faces=[end.ref], magnitude=1000, direction="-z").ok
    return study, plate, holes, end


def test_create_study_sets_objective_material_and_mass_target():
    nx = topo_nx()
    plate, _holes, _end = bracket(nx)
    ok = create(nx, plate, material="Aluminum_6061", quality=8, name="bracket")
    data = ok.data
    design_mass = 100 * 60 * 10 * nx.density
    assert data["design_mass"] == pytest.approx(design_mass)
    assert data["target_mass"] == pytest.approx(0.4 * design_mass)
    assert data["mass_fraction"] == 0.4 and data["quality"] == 8
    assert data["material"] == "Aluminum_6061" and data["name"] == "bracket"
    assert data["study"]["kind"] == "feature" and data["design_space"]["id"] == plate.ref
    DS = nx.nx.DesignSimulation
    sb = nx.last("StudyBuilder")
    assert sb.OptimizationObjectiveType is (
        DS.StudyBuilder.OptimizationObjectiveOption.MaximumStiffness
    )
    target = nx.last("OptimizationConstraintBuilder")
    assert target.Type is DS.OptimizationConstraintBuilder.ConstraintType.MassTarget
    assert float(target.Mass.RightHandSide) == pytest.approx(0.4 * design_mass)
    assert nx.edits == []  # every expression is written in its own unit
    assert nx.session.TopologyOptimizationTaskEnvironment.log == ["Enter", "Exit"]
    assert nx.live_builders == 0 and ok.mutation_outcome == "committed"


def test_create_refusals():
    nx = topo_nx()
    plate, _holes, _end = bracket(nx)
    whole = run(nx, "nx_create_topology_study", design_space=plate.ref, mass_fraction=1.0)
    assert whole.error["field"] == "/mass_fraction" and whole.mutation_outcome == "not_started"
    unknown = run(
        nx, "nx_create_topology_study", design_space=plate.ref, mass_fraction=0.3,
        material="Unobtainium",
    )  # fmt: skip
    assert unknown.error["code"] == "NX_NOT_FOUND" and unknown.error["field"] == "/material"
    assert unknown.mutation_outcome == "not_started"  # checked against the library first
    near = run(nx, "nx_create_topology_study", design_space=plate.ref, mass_fraction=0.3,
               material="Stel")  # fmt: skip
    assert near.error["details"]["candidates"] == ["Steel"]
    env = nx.session.TopologyOptimizationTaskEnvironment
    assert env.log == [] and env.ActiveFeature is None
    assert not [f for f in nx.part.Features if hasattr(f, "GetAllStudies")]


def test_the_default_material_is_the_bodys_and_a_body_without_one_is_refused():
    nx = topo_nx()
    plate, _holes, _end = bracket(nx)
    assert create(nx, plate).data["material"] == "Steel"  # AskMaterialOfObject
    other = topo_nx()
    plate, _holes, _end = bracket(other)
    other.body_material = None
    bad = run(other, "nx_create_topology_study", design_space=plate.ref, mass_fraction=0.4)
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/material"
    assert bad.mutation_outcome == "not_started"
    assert other.session.TopologyOptimizationTaskEnvironment.log == []


def test_unlicensed_seat_is_refused_without_entering_the_task():
    nx = topo_nx(licensed=False)
    plate, _holes, _end = bracket(nx)
    bad = run(nx, "nx_create_topology_study", design_space=plate.ref, mass_fraction=0.4)
    assert bad.error["code"] == "NX_LICENSE_UNAVAILABLE" and "des_top_opt" in bad.error["message"]
    assert bad.mutation_outcome == "not_started"
    assert nx.session.TopologyOptimizationTaskEnvironment.log == []


def test_conditions_build_supports_loads_regions_and_shapes():
    nx = topo_nx()
    study, plate, holes, end = ready(nx)
    DS = nx.nx.DesignSimulation
    support = nx.last("AnalysisConstraintBuilder")
    assert support.Type is DS.AnalysisConstraintBuilder.ConstraintType.Fixed
    load = nx.last("AnalysisLoadBuilder")
    assert load.Type is DS.AnalysisLoadBuilder.AnalysisLoadType.Force
    assert load.Force.RightHandSide == "1000.0"
    vector = load.FaceVector.Vector
    assert [vector.X, vector.Y, vector.Z] == [0, 0, -1]
    keep = add(nx, study, "keep_in", faces=[f.ref for f in holes], thickness=4)
    assert keep.ok and keep.data["faces"] == 2, keep
    region = nx.last("ConstructionBodyBuilder")
    assert region.Method is DS.ConstructionBodyBuilder.MethodOption.KeepIn
    assert region.Type is DS.ConstructionBodyBuilder.ConstructionType.ByFace
    assert region.ThickenOffset.RightHandSide == "4.0"
    boss = nx.block(5, 5, 5, origin=(40, 20, 10))
    void = add(nx, study, "keep_out", bodies=[boss.ref])
    assert void.ok and nx.last("ConstructionBodyBuilder").Bodies.Add.calls == [((boss,), {})]
    mid = nx.datum_plane(origin=(0, 30, 0), normal=(0, 1, 0))
    assert add(nx, study, "symmetry", planes=[mid.ref]).ok
    plane = nx.last("ShapeConstraintBuilder").PlanarSymmetryPlane1
    assert (plane.origin.Y, plane.normal.Y) == (30.0, 1.0)
    assert add(nx, study, "member_size", size=3).ok
    assert nx.last("ShapeConstraintBuilder").MinimumDiameter.RightHandSide == "3.0"
    last = add(nx, study, "extrude", direction="z")
    assert last.ok and last.data["supports"] == 1 and last.data["loads"] == 1
    assert nx.last("ShapeConstraintBuilder").Type is (
        DS.ShapeConstraintBuilder.ConstraintType.ExtrudeAlongVector
    )
    log = nx.session.TopologyOptimizationTaskEnvironment.log
    assert log == ["Enter", "Exit"] * 8 and nx.live_builders == 0
    feature = nx.refs[study][0]
    refs = feature.ActiveStudy.subcase.refs  # every support and load joins the subcase
    assert len(refs) == 2 and nx.edits == []


def test_inch_part_converts_force_pressure_and_depth():
    nx = topo_nx(units="in")
    plate, holes, end = bracket(nx)
    study = create(nx, plate).data["study"]["id"]
    assert add(nx, study, "pinned", faces=[holes[0].ref]).ok
    assert add(nx, study, "force", faces=[end.ref], magnitude=444.82216152605,
               direction=[0, 0, -1]).ok  # fmt: skip
    assert add(nx, study, "pressure", faces=[end.ref], magnitude=0.689475729).ok
    assert add(nx, study, "keep_in", faces=[holes[1].ref]).ok
    force, pressure = [b for b in nx.builders if b.kind == "AnalysisLoadBuilder"]
    assert float(force.Force.RightHandSide) == pytest.approx(100.0)  # lbf
    assert float(pressure.Pressure.RightHandSide) == pytest.approx(100.0)  # psi
    region = nx.last("ConstructionBodyBuilder")
    assert float(region.ThickenOffset.RightHandSide) == pytest.approx(5 / 25.4)  # 5 mm in inches
    DS = nx.nx.DesignSimulation
    pinned = [b for b in nx.builders if b.kind == "AnalysisConstraintBuilder"][0]
    assert pinned.Type is DS.AnalysisConstraintBuilder.ConstraintType.Pinned


def test_condition_refusals_before_nx():
    nx = topo_nx()
    plate, holes, end = bracket(nx)
    study = create(nx, plate).data["study"]["id"]
    env = nx.session.TopologyOptimizationTaskEnvironment
    entered = list(env.log)
    no_dir = add(nx, study, "force", faces=[end.ref], magnitude=5)
    assert no_dir.error["field"] == "/direction" and no_dir.mutation_outcome == "not_started"
    both = add(nx, study, "keep_out", faces=[end.ref], bodies=[plate.ref])
    assert both.error["field"] == "/faces"
    extra = add(nx, study, "fixed", faces=[end.ref], magnitude=3)
    assert extra.error["field"] == "/magnitude"
    itself = add(nx, study, "keep_in", bodies=[plate.ref])
    assert itself.error["field"] == "/bodies/0" and itself.mutation_outcome == "not_started"
    other = nx.block(5, 5, 5, origin=(200, 0, 0))
    foreign = add(nx, study, "fixed", faces=[other.face((0, 0, 1)).ref])
    assert foreign.error["code"] == "NX_WRONG_OWNER" and foreign.error["field"] == "/faces/0"
    plain = feature(nx, "EXTRUDE")
    wrong = add(nx, nx.ref_of(plain, "feature"), "fixed", faces=[end.ref])
    assert wrong.error["code"] == "NX_WRONG_KIND" and wrong.mutation_outcome == "not_started"
    assert env.log == entered


def test_condition_failure_in_the_task_cancels_and_rolls_back():
    nx = topo_nx()
    plate, holes, end = bracket(nx)
    study = create(nx, plate).data["study"]["id"]
    nx.refuse("AnalysisLoadBuilder", 3520100, "Face is not on the design space")
    bad = add(nx, study, "force", faces=[end.ref], magnitude=10, direction="-z")
    assert bad.error["code"] == "NX_BUILD_FAILED" and bad.error["nx_code"] == 3520100
    assert bad.mutation_outcome == "rolled_back" and nx.live_builders == 0
    env = nx.session.TopologyOptimizationTaskEnvironment
    assert env.log[-3:] == ["Enter", "Cancel", "Exit"] and env.ActiveFeature is None


def test_run_reports_fraction_peaks_and_kept_faces():
    nx = topo_nx()
    study, plate, holes, end = ready(nx)
    ok = run(nx, "nx_run_topology_study", study=study)
    assert ok.ok, ok
    data = ok.data
    assert data["mass_fraction"] == pytest.approx(0.4)
    assert data["reduction_percent"] == pytest.approx(60.0)
    assert data["volume"] == pytest.approx(0.4 * data["design_volume"])
    assert data["mass"] == pytest.approx(0.4 * data["design_mass"])
    assert data["target_mass"] == pytest.approx(0.4 * data["design_mass"])
    assert data["max_stress"] == pytest.approx(85.0) and data["max_displacement"] == 0.12
    assert len(data["bodies"]) == 1 and len(data["kept"]) == 3
    assert all(row["kept"] for row in data["kept"]) and data["all_kept"] is True
    assert {row["role"] for row in data["kept"]} == {"support", "load"}
    env = nx.session.TopologyOptimizationTaskEnvironment
    assert env.log[-2:] == ["Enter", "Exit"]

    nx.gap = 2.0
    lost = run(nx, "nx_run_topology_study", study=study)
    assert lost.ok and not any(row["kept"] for row in lost.data["kept"])
    assert lost.data["all_kept"] is False


def test_run_refusals_and_empty_result():
    nx = topo_nx()
    plate, holes, end = bracket(nx)
    study = create(nx, plate).data["study"]["id"]
    bare = run(nx, "nx_run_topology_study", study=study)
    assert bare.error["code"] == "NX_INVALID_ARGUMENT" and "support" in bare.error["message"]
    assert bare.mutation_outcome == "not_started"
    assert add(nx, study, "fixed", faces=[holes[0].ref]).ok
    assert add(nx, study, "force", faces=[end.ref], magnitude=10, direction="-z").ok
    nx.solve_fraction = None
    empty = run(nx, "nx_run_topology_study", study=study)
    assert empty.error["code"] == "NX_INCOMPLETE_RESULT" and empty.mutation_outcome == "rolled_back"


def test_get_lists_and_describes_studies():
    nx = topo_nx()
    study, plate, _holes, _end = ready(nx)
    listed = run(nx, "nx_get_topology_study")
    assert listed.ok and len(listed.data["items"]) == 1, listed
    item = listed.data["items"][0]
    assert item["study"]["id"] == study and item["design_space"]["id"] == plate.ref
    holes = [f.ref for f in _holes]
    assert item["supports"] == [{"type": "fixed", "faces": 2, "face_refs": holes,
                                 "load_cases": ["Default Subcase"], "magnitude": None,
                                 "unit": None, "direction": None}]  # fmt: skip
    (load,) = item["loads"]
    assert load["type"] == "force" and load["faces"] == 1 and load["face_refs"] == [_end.ref]
    assert load["magnitude"] == pytest.approx(1000.0) and load["unit"] == "N"
    assert load["direction"] == [0, 0, -1] and load["load_cases"] == ["Default Subcase"]
    assert item["solved"] is False
    assert item["target_mass"] == pytest.approx(0.4 * item["design_mass"])
    run(nx, "nx_run_topology_study", study=study)
    one = run(nx, "nx_get_topology_study", study=study)
    assert one.data["items"][0]["solved"] is True and len(one.data["items"][0]["bodies"]) == 1
    assert one.data["items"][0]["mass_fraction"] == pytest.approx(0.4)
