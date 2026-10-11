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


def test_concentric_with_a_touch_hints_align_lock(nx):
    """Concentric is coaxial AND coplanar: a touch on the same component over-constrains."""
    edge = geometry(nx, nx.b, "edge", True)
    target = geometry(nx, nx.a, "edge", True)
    ok = run(nx, "nx_create_assembly_constraint", type="concentric", component=nx.b.ref,
             geometry=edge, target_component=nx.a.ref, target_geometry=target)  # fmt: skip
    assert ok.ok and not ok.warnings, ok
    nx.assembly.solver_status = STATUS.OverConstrained
    bad = run(nx, "nx_create_assembly_constraint", type="touch", component=nx.b.ref,
              geometry=geometry(nx, nx.b), target_component=nx.a.ref,
              target_geometry=geometry(nx, nx.a))  # fmt: skip
    assert bad.error["code"] == "NX_CONSTRAINT_UNSATISFIED", bad
    assert "align_lock" in bad.error["hint"] and "coplanar" in bad.error["hint"]
    assert bad.error["details"]["others"][0]["type"] == "concentric"
    # The new constraint solves but leaves the earlier one over-constrained: a warning.
    nx.assembly.solver_status = STATUS.Solved

    def over(added):
        for c in nx.assembly.positioner.Constraints:
            if c not in added:
                c.status = STATUS.OverConstrained

    nx.assembly.on_solve = over
    warned = run(nx, "nx_create_assembly_constraint", type="touch", component=nx.b.ref,
                 geometry=geometry(nx, nx.b), target_component=nx.a.ref,
                 target_geometry=geometry(nx, nx.a))  # fmt: skip
    assert warned.ok and "align_lock" in warned.warnings[0]["message"], warned
    assert "concentric: OverConstrained" in warned.warnings[0]["message"]
    # A failing concentric alone gets no bolt advice.
    nx.assembly.on_solve = None
    nx.assembly.positioner.Constraints.clear()
    nx.assembly.solver_status = STATUS.NotSolved
    alone = run(nx, "nx_create_assembly_constraint", type="concentric", component=nx.b.ref,
                geometry=edge, target_component=nx.a.ref, target_geometry=target)  # fmt: skip
    assert alone.error["code"] == "NX_CONSTRAINT_UNSATISFIED"
    assert "align_lock" not in alone.error["hint"]


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


def test_bond_references_the_components_themselves(nx):
    ok = run(nx, "nx_create_assembly_constraint", type="bond", component=nx.b.ref,
             target_component=nx.a.ref)  # fmt: skip
    assert ok.ok, ok
    c = nx.assembly.positioner.Constraints[-1]
    assert c.ConstraintType == TYPE.Bond and [r.GetGeometry() for r in c.refs] == [nx.b, nx.a]
    assert ok.data["type"] == "bond"
    surplus = run(nx, "nx_create_assembly_constraint", type="bond", component=nx.b.ref,
                  target_component=nx.a.ref, geometry=geometry(nx, nx.b))  # fmt: skip
    assert surplus.error["field"] == "/geometry" and surplus.mutation_outcome == "not_started"


def test_align_lock_uses_the_axis_of_a_face_and_fit_needs_faces(nx):
    ok = run(nx, "nx_create_assembly_constraint", type="align_lock", component=nx.b.ref,
             geometry=geometry(nx, nx.b), target_component=nx.a.ref,
             target_geometry=geometry(nx, nx.a, "edge"))  # fmt: skip
    assert ok.ok and ok.data["type"] == "align_lock", ok
    c = nx.assembly.positioner.Constraints[-1]
    assert c.ConstraintType == TYPE.AlignLock and [r.uses_axis for r in c.refs] == [True, False]
    edge = run(nx, "nx_create_assembly_constraint", type="fit", component=nx.b.ref,
               geometry=geometry(nx, nx.b, "edge"), target_component=nx.a.ref,
               target_geometry=geometry(nx, nx.a))  # fmt: skip
    assert edge.error["code"] == "NX_WRONG_GEOMETRY" and edge.error["field"] == "/geometry"
    fit = run(nx, "nx_create_assembly_constraint", type="fit", component=nx.b.ref,
              geometry=geometry(nx, nx.b), target_component=nx.a.ref,
              target_geometry=geometry(nx, nx.a))  # fmt: skip
    assert fit.ok and fit.data["type"] == "fit"
    assert nx.assembly.positioner.Constraints[-1].ConstraintType == TYPE.Fit


def test_center_types_take_the_pairs_in_order(nx):
    b1, b2, a1, a2 = (geometry(nx, c) for c in (nx.b, nx.b, nx.a, nx.a))
    one = run(nx, "nx_create_assembly_constraint", type="center_1_2", component=nx.b.ref,
              geometry=b1, target_component=nx.a.ref, target_geometry=a1,
              target_geometry2=a2)  # fmt: skip
    assert one.ok and one.data["type"] == "center_1_2", one
    c = nx.assembly.positioner.Constraints[-1]
    assert c.ConstraintType == TYPE.Center12
    assert [nx.ref_of(r.GetGeometry(), "face") for r in c.refs] == [a1, a2, b1]  # the pair first
    assert [r.GetMovableObject() for r in c.refs] == [nx.a, nx.a, nx.b]
    assert [r.uses_axis for r in c.refs] == [False, False, False]
    pin = nx.ref_of(nx.b.face("cylinder"), "face")
    axis = run(nx, "nx_create_assembly_constraint", type="center_1_2", component=nx.b.ref,
               geometry=pin, target_component=nx.a.ref, target_geometry=a1,
               target_geometry2=a2)  # fmt: skip
    assert axis.ok, axis
    assert [r.uses_axis for r in nx.assembly.positioner.Constraints[-1].refs] == [
        False,
        False,
        True,
    ]
    two = run(nx, "nx_create_assembly_constraint", type="center_2_2", component=nx.b.ref,
              geometry=b1, geometry2=b2, target_component=nx.a.ref, target_geometry=a1,
              target_geometry2=a2)  # fmt: skip
    assert two.ok and two.data["type"] == "center_2_2"
    assert len(nx.assembly.positioner.Constraints[-1].refs) == 4
    missing = run(nx, "nx_create_assembly_constraint", type="center_2_2", component=nx.b.ref,
                  geometry=b1, target_component=nx.a.ref, target_geometry=a1,
                  target_geometry2=a2)  # fmt: skip
    assert missing.error["field"] == "/geometry2"
    surplus = run(nx, "nx_create_assembly_constraint", type="center_1_2", component=nx.b.ref,
                  geometry=b1, geometry2=b2, target_component=nx.a.ref, target_geometry=a1,
                  target_geometry2=a2)  # fmt: skip
    assert surplus.error["field"] == "/geometry2"
    flipped = run(nx, "nx_create_assembly_constraint", type="center_1_2", component=nx.b.ref,
                  geometry=b1, target_component=nx.a.ref, target_geometry=a1,
                  target_geometry2=a2, alignment="same")  # fmt: skip
    assert flipped.error["field"] == "/alignment"


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


@pytest.mark.parametrize(
    ("span", "count", "step", "xs"),
    [
        (90, 4, 30, None),  # first to last: 3 steps of 30
        (360, 4, 90, [(-60, 0), (0, -60), (0, 60), (60, 0)]),  # full circle: 360 / count
    ],
)
def test_circular_pattern_angle_as_span(nx, span, count, step, xs):
    ok = run(nx, "nx_create_component_pattern", component=nx.b.ref,
             layout={"circular": {"axis": "z", "count": count, "angle": span, "span": True}})  # fmt: skip
    assert ok.ok and ok.data["angle"] == pytest.approx(step), ok
    builder = nx.assembly.pattern_builders[0].PatternService.CircularDefinition
    assert builder.AngularSpacing.PitchAngle.Value == pytest.approx(step)
    if xs:
        got = sorted(
            tuple(round(v, 6) for v in c["translation"][:2]) for c in ok.data["components"]
        )
        assert got == xs
    for bad_span in (0, 400):
        bad = run(nx, "nx_create_component_pattern", component=nx.b.ref,
                  layout={"circular": {"axis": "z", "count": 3, "angle": bad_span, "span": True}})  # fmt: skip
        assert bad.error["field"] == "/layout/circular/angle", bad


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


# ---- natural values (live tester b13) -------------------------------------------------------


def planar(nx, c, origin, normal):
    """``(ref, face)``: a planar occurrence face of ``c`` at ``origin`` along ``normal``."""
    face = c.face()
    nx.face_data[face.Tag] = ("plane", list(origin), list(normal))
    return nx.ref_of(face, "face"), face


def distance(nx, value, **kw):
    return run(nx, "nx_create_assembly_constraint", type="distance", component=nx.b.ref,
               geometry=geometry(nx, nx.b), target_component=nx.a.ref,
               target_geometry=geometry(nx, nx.a), value=value, **kw)  # fmt: skip


@pytest.mark.parametrize(
    ("value", "wanted"), [("10 mm", 10.0), (" 1in ", 25.4), ("-5mm", -5.0), ("7", 7.0), (-3, -3.0)]
)
def test_distance_value_takes_unit_strings_and_a_negative_side(nx, value, wanted):
    ok = distance(nx, value)
    assert ok.ok, ok
    c = nx.assembly.positioner.Constraints[-1]
    assert c.Expression.Value == pytest.approx(wanted) and ok.data["value"] == pytest.approx(wanted)


@pytest.mark.parametrize(
    ("value", "wanted"), [("45deg", 45.0), ("45 DEG", 45.0), ("45\u00b0", 45.0),
                          ("0.5 rad", 28.64788975654116)],
)  # fmt: skip
def test_angle_value_takes_degree_and_radian_strings(nx, value, wanted):
    ok = run(nx, "nx_create_assembly_constraint", type="angle", component=nx.b.ref,
             geometry=geometry(nx, nx.b), target_component=nx.a.ref,
             target_geometry=geometry(nx, nx.a), value=value)  # fmt: skip
    assert ok.ok, ok
    assert nx.assembly.positioner.Constraints[-1].Expression.Value == pytest.approx(wanted)


def test_inch_strings_convert_to_inch_part_units(nx):
    nx.asm.PartUnits = "in"
    ok = distance(nx, "25.4 mm")
    assert ok.ok, ok
    assert nx.assembly.positioner.Constraints[-1].Expression.Value == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("type", "value", "says"),
    [("distance", "45 deg", "a length"), ("angle", "10 mm", "an angle"),
     ("distance", "ten", "not a number"), ("angle", -5, "0 to 180"), ("angle", "4 rad", "0 to 180"),
     ("distance", 2e6, "within")],
)  # fmt: skip
def test_value_errors_name_what_to_send(nx, type, value, says):
    bad = run(nx, "nx_create_assembly_constraint", type=type, component=nx.b.ref,
              geometry=geometry(nx, nx.b), target_component=nx.a.ref,
              target_geometry=geometry(nx, nx.a), value=value)  # fmt: skip
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/value"
    assert says in bad.error["message"] and bad.mutation_outcome == "not_started"
    assert not nx.assembly.positioner.Constraints


def test_edit_value_takes_unit_strings_checked_against_the_type(nx):
    ok = distance(nx, 7)
    ref = ok.data["constraint"]["id"]
    edited = run(nx, "nx_edit_assembly_constraint", constraint=ref, value="12.5 mm")
    assert edited.ok and edited.data["value"] == 12.5, edited
    flipped = run(nx, "nx_edit_assembly_constraint", constraint=ref, value=-12.5)
    assert flipped.ok and flipped.data["value"] == -12.5
    wrong = run(nx, "nx_edit_assembly_constraint", constraint=ref, value="30 deg")
    assert wrong.error["field"] == "/value" and wrong.mutation_outcome == "not_started"


def test_distance_reports_the_face_offset_and_warns_about_an_overlap(nx):
    # A's +x face at x=20; NX put B's -x face at x=10, 10 inside A.
    b_face, b_obj = planar(nx, nx.b, [10, 0, 0], [-1, 0, 0])
    a_face, _ = planar(nx, nx.a, [20, 0, 0], [1, 0, 0])
    overlap = run(nx, "nx_create_assembly_constraint", type="distance", component=nx.b.ref,
                  geometry=b_face, target_component=nx.a.ref, target_geometry=a_face,
                  value=10)  # fmt: skip
    assert overlap.ok, overlap
    assert overlap.data["offset"] == pytest.approx(-10)
    assert overlap.warnings and "overlap by 10" in overlap.warnings[0]["message"]
    # The other side: a 10 gap, no warning.
    nx.face_data[b_obj.Tag] = ("plane", [30, 0, 0], [-1, 0, 0])
    gap = run(nx, "nx_edit_assembly_constraint", constraint=overlap.data["constraint"]["id"],
              value=-10)  # fmt: skip
    assert gap.ok and gap.data["offset"] == pytest.approx(10) and not gap.warnings, gap


def test_touch_reports_offset_but_other_geometry_does_not(nx):
    b_face, _ = planar(nx, nx.b, [20, 0, 0], [-1, 0, 0])
    a_face, _ = planar(nx, nx.a, [20, 0, 0], [1, 0, 0])
    touch = run(nx, "nx_create_assembly_constraint", type="touch", component=nx.b.ref,
                geometry=b_face, target_component=nx.a.ref, target_geometry=a_face)  # fmt: skip
    assert touch.ok and touch.data["offset"] == 0 and not touch.warnings, touch
    edges = run(nx, "nx_create_assembly_constraint", type="touch", component=nx.b.ref,
                geometry=geometry(nx, nx.b, "edge"), target_component=nx.a.ref,
                target_geometry=geometry(nx, nx.a, "edge"))  # fmt: skip
    assert edges.ok and "offset" not in edges.data


def test_unsolved_constraint_names_the_alignment_and_the_other_constraints(nx):
    touch = run(nx, "nx_create_assembly_constraint", type="touch", component=nx.b.ref,
                geometry=geometry(nx, nx.b), target_component=nx.a.ref,
                target_geometry=geometry(nx, nx.a))  # fmt: skip
    nx.assembly.solver_status = STATUS.OverConstrained
    bad = distance(nx, 10, alignment="same")
    assert bad.error["code"] == "NX_CONSTRAINT_UNSATISFIED", bad
    assert "'same' points the face normals" in bad.error["hint"]
    assert "suppress" in bad.error["hint"]
    others = bad.error["details"]["others"]
    assert [o["constraint"]["id"] for o in others] == [touch.data["constraint"]["id"]]
    assert others[0]["type"] == "touch" and bad.mutation_outcome == "rolled_back"


def test_component_doc_names_the_reference_form():
    from nx_mcp.core import catalogue
    from nx_mcp.sidecar import schema

    spec = catalogue.load()["nx_create_assembly_constraint"]
    assert "Component id 'o:N'" in str(schema.input_schema(spec, call_keys=False))


# ---- bare principal names ---------------------------------------------------------------------


def test_pattern_direction_and_axis_take_bare_principal_names(nx):
    ok = run(nx, "nx_create_component_pattern", component=nx.a.ref,
             layout={"linear": {"direction": "y", "count": 3, "spacing": "20 mm",
                                "direction2": "-x", "count2": 2, "spacing2": 10}})  # fmt: skip
    assert ok.ok, ok
    got = sorted((c["translation"][0], c["translation"][1]) for c in ok.data["components"])
    assert got == [(-10, 0), (-10, 20), (-10, 40), (0, 0), (0, 20), (0, 40)]
    circ = run(nx, "nx_create_component_pattern", component=nx.b.ref,
               layout={"circular": {"axis": "z", "count": 4, "angle": "90deg"}})  # fmt: skip
    assert circ.ok, circ
    got = sorted(tuple(round(v, 6) for v in c["translation"]) for c in circ.data["components"])
    assert got == [(-60, 0, 0), (0, -60, 0), (0, 60, 0), (60, 0, 0)]


def test_pattern_rejects_an_unknown_bare_name(nx):
    bad = run(nx, "nx_create_component_pattern", component=nx.a.ref,
              layout={"linear": {"direction": "w", "count": 3, "spacing": 20}})  # fmt: skip
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.mutation_outcome == "not_started"


def test_adding_a_second_direction_by_edit_says_how_to_recreate(nx):
    made = run(nx, "nx_create_component_pattern", component=nx.a.ref, layout=linear())
    bad = run(nx, "nx_edit_component_pattern", pattern=made.data["pattern"]["id"], count2=2,
              spacing2=25)  # fmt: skip
    assert bad.error["code"] == "NX_UNSUPPORTED_OPTION" and bad.error["field"] == "/count2"
    assert "nx_create_component_pattern" in bad.error["hint"] and "direction2" in bad.error["hint"]


def test_update_assembly_lists_suppressed_constraints(nx):
    fix = run(nx, "nx_create_assembly_constraint", type="fix", component=nx.a.ref)
    touch = run(nx, "nx_create_assembly_constraint", type="touch", component=nx.b.ref,
                geometry=geometry(nx, nx.b), target_component=nx.a.ref,
                target_geometry=geometry(nx, nx.a))  # fmt: skip
    off = run(nx, "nx_edit_assembly_constraint", constraint=touch.data["constraint"]["id"],
              suppressed=True)  # fmt: skip
    assert fix.ok and off.ok
    done = run(nx, "nx_update_assembly")
    assert done.ok, done
    rows = {c["constraint"]["id"]: c for c in done.data["constraints"]}
    assert rows[fix.data["constraint"]["id"]]["status"] == "Solved"
    suppressed = rows[touch.data["constraint"]["id"]]
    assert suppressed["suppressed"] and suppressed["status"] == "Suppressed"
