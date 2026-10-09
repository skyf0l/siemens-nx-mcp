import pytest

from tests.kit import run
from tests.unit.assembly.fakes import ALIGN, STATUS, TYPE, assembly_nx, place

RZ90 = [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]


@pytest.fixture
def nx():
    nx = assembly_nx()
    nx.a = place(nx, nx.blk, "A")
    nx.b = place(nx, nx.blk, "B", (60, 0, 0))
    return nx


def geometry(nx, c, kind="face", circular=False):
    obj = c.edge([0, 0, 0], [1, 0, 0], circular) if kind == "edge" else c.face()
    return nx.ref_of(obj, kind)


# ---- constraints ------------------------------------------------------------------------


def test_fix_solves_inside_one_positioner_session(nx):
    ok = run(nx, "nx_create_assembly_constraint", type="fix", component=nx.a.ref)
    assert ok.ok, ok
    c = nx.assembly.positioner.Constraints[0]
    assert c.ConstraintType == TYPE.Fix and [r.GetGeometry() for r in c.refs] == [nx.a]
    assert ok.data["type"] == "fix" and ok.data["status"] == "Solved" and ok.data["moved"] == []
    assert nx.assembly.positioner.calls == ["begin", "clear", "end"]


def test_touch_align_and_distance_set_type_alignment_and_value(nx):
    def move_b(_added):
        nx.b.t = [nx.b.t[0] - 50, 0.0, 0.0]

    nx.assembly.on_solve = move_b
    ok = run(nx, "nx_create_assembly_constraint", type="distance", component=nx.b.ref,
             geometry=geometry(nx, nx.b), target_component=nx.a.ref,
             target_geometry=geometry(nx, nx.a), value=7, alignment="opposite")  # fmt: skip
    assert ok.ok, ok
    c = nx.assembly.positioner.Constraints[-1]
    assert c.ConstraintType == TYPE.Distance and c.ConstraintAlignment == ALIGN.ContraAlign
    assert ok.data["value"] == 7 and ok.data["alignment"] == "opposite"
    assert [m["id"] for m in ok.data["moved"]] == [nx.b.ref]
    assert ok.data["moved"][0]["translation"] == [10, 0, 0]
    nx.assembly.on_solve = None
    align = run(nx, "nx_create_assembly_constraint", type="align", component=nx.b.ref,
                geometry=geometry(nx, nx.b), target_component=nx.a.ref,
                target_geometry=geometry(nx, nx.a))  # fmt: skip
    assert align.ok and align.data["type"] == "align" and align.data["alignment"] == "same"


def test_concentric_takes_circular_edges_only(nx):
    faces = run(nx, "nx_create_assembly_constraint", type="concentric", component=nx.b.ref,
                geometry=geometry(nx, nx.b), target_component=nx.a.ref,
                target_geometry=geometry(nx, nx.a))  # fmt: skip
    assert faces.error["code"] == "NX_WRONG_GEOMETRY" and faces.error["field"] == "/geometry"
    assert faces.mutation_outcome == "not_started" and not nx.assembly.positioner.Constraints
    ok = run(nx, "nx_create_assembly_constraint", type="concentric", component=nx.b.ref,
             geometry=geometry(nx, nx.b, "edge", True), target_component=nx.a.ref,
             target_geometry=geometry(nx, nx.a, "edge", True))  # fmt: skip
    assert ok.ok and ok.data["type"] == "concentric"


def test_unsolved_constraint_rolls_back_and_reports_status(nx):
    nx.assembly.solver_status = STATUS.OverConstrained
    bad = run(nx, "nx_create_assembly_constraint", type="touch", component=nx.b.ref,
              geometry=geometry(nx, nx.b), target_component=nx.a.ref,
              target_geometry=geometry(nx, nx.a))  # fmt: skip
    assert (
        bad.error["code"] == "NX_CONSTRAINT_UNSATISFIED"
        and bad.error["category"] == "solver_failed"
    )
    assert bad.error["details"]["status"] == "OverConstrained"
    assert bad.mutation_outcome == "rolled_back" and not nx.assembly.positioner.Constraints
    assert nx.assembly.positioner.calls[-2:] == ["clear", "end"]


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"type": "fix", "value": 1}, "/value"),
        ({"type": "fix", "alignment": "same"}, "/alignment"),
        ({"type": "touch"}, "/geometry"),
        ({"type": "distance", "geometry": "G", "target_component": "T", "target_geometry": "G"},
         "/value"),
        ({"type": "touch", "geometry": "G", "target_component": "T", "target_geometry": "G",
          "value": 2}, "/value"),
        ({"type": "angle", "geometry": "G", "target_component": "T", "target_geometry": "G",
          "value": 200}, "/value"),
        ({"type": "align", "geometry": "G", "target_component": "T", "target_geometry": "G",
          "alignment": "opposite"}, "/alignment"),
    ],
)  # fmt: skip
def test_constraint_argument_rules(nx, args, field):
    g = geometry(nx, nx.b)
    args = {k: (g if v == "G" else nx.a.ref if v == "T" else v) for k, v in args.items()}
    bad = run(nx, "nx_create_assembly_constraint", component=nx.b.ref, **args)
    assert bad.error["field"] == field and bad.mutation_outcome == "not_started"


def test_geometry_must_belong_to_its_component_and_target_must_differ(nx):
    wrong = run(nx, "nx_create_assembly_constraint", type="touch", component=nx.b.ref,
                geometry=geometry(nx, nx.a), target_component=nx.a.ref,
                target_geometry=geometry(nx, nx.a))  # fmt: skip
    assert wrong.error["code"] == "NX_WRONG_OWNER" and wrong.error["field"] == "/geometry"
    same = run(nx, "nx_create_assembly_constraint", type="touch", component=nx.b.ref,
               geometry=geometry(nx, nx.b), target_component=nx.b.ref,
               target_geometry=geometry(nx, nx.b))  # fmt: skip
    assert same.error["field"] == "/target_component"


def test_edit_constraint_value_alignment_suppression(nx):
    ok = run(nx, "nx_create_assembly_constraint", type="distance", component=nx.b.ref,
             geometry=geometry(nx, nx.b), target_component=nx.a.ref,
             target_geometry=geometry(nx, nx.a), value=7)  # fmt: skip
    ref = ok.data["constraint"]["id"]
    edited = run(nx, "nx_edit_assembly_constraint", constraint=ref, value=12, alignment="same")
    assert edited.ok and edited.data["value"] == 12 and edited.data["alignment"] == "same"
    off = run(nx, "nx_edit_assembly_constraint", constraint=ref, suppressed=True)
    assert off.ok and off.data["suppressed"] and off.data["status"] == "Suppressed"
    nx.assembly.solver_status = STATUS.NotSolved
    still = run(nx, "nx_edit_assembly_constraint", constraint=ref, value=20)
    assert still.ok, "a suppressed constraint need not solve"
    bad = run(nx, "nx_edit_assembly_constraint", constraint=ref, suppressed=False)
    assert (
        bad.error["code"] == "NX_CONSTRAINT_UNSATISFIED" and bad.mutation_outcome == "rolled_back"
    )
    nothing = run(nx, "nx_edit_assembly_constraint", constraint=ref)
    assert nothing.error["code"] == "NX_INVALID_ARGUMENT"
    nx.assembly.solver_status = STATUS.Solved
    fix = run(nx, "nx_create_assembly_constraint", type="fix", component=nx.a.ref)
    value_on_fix = run(nx, "nx_edit_assembly_constraint", constraint=fix.data["constraint"]["id"],
                       value=3)  # fmt: skip
    assert (
        value_on_fix.error["field"] == "/value" and value_on_fix.mutation_outcome == "not_started"
    )


# ---- patterns -----------------------------------------------------------------------------


def linear(**kw):
    return {"linear": {"direction": [1, 0, 0], "count": 3, "spacing": 20, **kw}}


def test_associative_linear_pattern_reads_back_every_instance(nx):
    ok = run(nx, "nx_create_component_pattern", component=nx.a.ref,
             layout=linear(direction2=[0, 2, 0], count2=2, spacing2=30))  # fmt: skip
    assert ok.ok, ok
    assert ok.data["associative"] and ok.data["count"] == 3 and ok.data["count2"] == 2
    xs = sorted((c["translation"][0], c["translation"][1]) for c in ok.data["components"])
    assert xs == [(0, 0), (0, 30), (20, 0), (20, 30), (40, 0), (40, 30)]
    assert ok.data["pattern"]["kind"] == "component_pattern"
    assert all(b.destroyed for b in nx.assembly.pattern_builders)


def test_circular_pattern_rotates_about_the_axis(nx):
    ok = run(nx, "nx_create_component_pattern", component=nx.b.ref,
             layout={"circular": {"axis": {"origin": [0, 0, 0], "direction": [0, 0, 1]},
                                  "count": 4, "angle": 90}})  # fmt: skip
    assert ok.ok, ok
    got = sorted(tuple(round(v, 6) for v in c["translation"]) for c in ok.data["components"])
    assert got == [(-60, 0, 0), (0, -60, 0), (0, 60, 0), (60, 0, 0)]


def test_independent_copies_are_plain_components(nx):
    ok = run(nx, "nx_create_component_pattern", component=nx.a.ref, layout=linear(),
             associative=False)  # fmt: skip
    assert ok.ok and not ok.data["associative"] and "pattern" not in ok.data
    assert [c.Name for c in nx.assembly.components()][-2:] == ["A_2", "A_3"]
    assert not nx.assembly.patterns


@pytest.mark.parametrize(
    ("layout", "field"),
    [
        (linear(direction2=[1, 0, 0], count2=2, spacing2=5), "/layout/linear/direction2"),
        (linear(count2=2), "/layout/linear"),
        (linear(spacing=0), "/layout/linear"),
        ({"circular": {"axis": {"principal": "z"}, "count": 5, "angle": 90}},
         "/layout/circular/angle"),
        (linear(count=20, direction2=[0, 1, 0], count2=6, spacing2=5), "/layout"),
    ],
)  # fmt: skip
def test_pattern_layout_rules(nx, layout, field):
    bad = run(nx, "nx_create_component_pattern", component=nx.a.ref, layout=layout)
    assert bad.error["field"] == field and bad.mutation_outcome == "not_started"


def test_pattern_count_mismatch_rolls_back(nx, monkeypatch):
    from tests.unit.assembly import fakes

    original = fakes.AssemblyFake.pattern_poses
    monkeypatch.setattr(fakes.AssemblyFake, "pattern_poses",
                        lambda self, seed, params: original(self, seed, params)[:-1])  # fmt: skip
    bad = run(nx, "nx_create_component_pattern", component=nx.a.ref, layout=linear())
    assert bad.error["code"] == "NX_VERIFICATION_FAILED" and bad.mutation_outcome == "rolled_back"
    assert not nx.assembly.patterns and len(nx.assembly.components()) == 2


def test_edit_pattern_reads_back_the_recreated_pattern(nx):
    made = run(nx, "nx_create_component_pattern", component=nx.a.ref,
               layout=linear(direction2=[0, 1, 0], count2=2, spacing2=30))  # fmt: skip
    ref = made.data["pattern"]["id"]
    ok = run(nx, "nx_edit_component_pattern", pattern=ref, count=4, spacing=25)
    assert ok.ok and not ok.data["replaced"] and len(ok.data["components"]) == 8
    nx.assembly.recreate_on_edit = True
    flat = run(nx, "nx_edit_component_pattern", pattern=ref, count2=1)
    assert flat.ok and flat.data["replaced"] and flat.data["pattern"]["id"] != ref
    assert flat.warnings and len(flat.data["components"]) == 4
    xs = sorted(c["translation"][0] for c in flat.data["components"])
    assert xs == [0, 25, 50, 75]


def test_edit_pattern_rules(nx):
    made = run(nx, "nx_create_component_pattern", component=nx.a.ref, layout=linear())
    ref = made.data["pattern"]["id"]
    for args, field in (({"angle": 30}, "/angle"), ({"count2": 2}, "/count2"),
                        ({"spacing2": 3}, "/spacing2"), ({"spacing": 0}, "/spacing")):  # fmt: skip
        bad = run(nx, "nx_edit_component_pattern", pattern=ref, **args)
        assert bad.error["field"] == field and bad.mutation_outcome == "not_started", args
    circ = run(nx, "nx_create_component_pattern", component=nx.b.ref,
               layout={"circular": {"axis": {"principal": "z"}, "count": 3, "angle": 60}})  # fmt: skip
    wrap = run(nx, "nx_edit_component_pattern", pattern=circ.data["pattern"]["id"], count=7)
    assert wrap.error["field"] == "/angle"
    spacing = run(nx, "nx_edit_component_pattern", pattern=circ.data["pattern"]["id"], spacing=3)
    assert spacing.error["field"] == "/spacing"
    ok = run(nx, "nx_edit_component_pattern", pattern=circ.data["pattern"]["id"], count=5, angle=45)
    assert ok.ok and ok.data["count"] == 5 and ok.data["angle"] == 45
