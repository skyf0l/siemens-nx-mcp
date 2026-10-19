"""Component placement and editing rules on the assembly fake: moves blocked by constraints,
moves that drag constrained siblings, replacements that keep names and need a saved part,
mirrored copies with their own names, and the asserted centre a new mass keeps."""

from __future__ import annotations

import pytest

from tests.kit import run
from tests.unit.assembly.fakes import STATUS, TYPE, Constraint, Reference, assembly_nx, place
from tests.unit.assembly.test_weights import nx as weights_nx  # noqa: F401 - fixture


@pytest.fixture
def nx():
    return assembly_nx()


def constrain(nx, kind, *components):
    c = Constraint(nx.assembly)
    c.ConstraintType = kind
    c.status = STATUS.Solved
    for component in components:
        c.refs.append(Reference(component, component.face()))
    nx.assembly.positioner.Constraints.append(c)
    return nx.ref_of(c, "assembly_constraint")


def test_a_move_its_constraints_block_names_them(nx):
    b = place(nx, nx.blk, "B", (20, 0, 0))
    touch = constrain(nx, TYPE.Touch, b)
    move = nx.asm.ComponentAssembly.MoveComponent

    def held(c, shift, m):  # the solver keeps z where the touch holds it
        z = c.t[2]
        move(c, shift, m)
        c.t[2] = z

    nx.asm.ComponentAssembly.MoveComponent = held
    bad = run(nx, "nx_set_component_pose", component=b.ref, mode="relative", translation=[0, 0, 5])
    assert bad.error["code"] == "NX_CONSTRAINT_UNSATISFIED", bad
    assert bad.error["retry"] == "after_fix" and bad.error["field"] == "/translation"
    assert [r["constraint"]["id"] for r in bad.error["details"]["constraints"]] == [touch]
    assert bad.error["details"]["constraints"][0]["type"] == "touch"
    assert bad.mutation_outcome == "rolled_back" and b.t == [20, 0, 0]
    free = run(nx, "nx_set_component_pose", component=b.ref, mode="relative", translation=[5, 0, 0])
    assert free.ok and b.t == pytest.approx([25, 0, 0]), free


def test_moving_a_fixed_component_reports_the_siblings_it_drags(nx):
    base = place(nx, nx.blk, "BASE")
    pin = place(nx, nx.pin, "PIN", (30, 0, 0))
    constrain(nx, TYPE.Fix, base)
    constrain(nx, TYPE.Concentric, pin, base)
    move = nx.asm.ComponentAssembly.MoveComponent

    def drag(c, shift, m):
        move(c, shift, m)
        if c is base:
            pin.t = [pin.t[0] + 5, pin.t[1], pin.t[2]]

    nx.asm.ComponentAssembly.MoveComponent = drag
    ok = run(nx, "nx_set_component_pose", component=base.ref, mode="relative",
             translation=[5, 0, 0])  # fmt: skip
    assert ok.ok, ok
    assert sorted(r["id"] for r in ok.data["moved"]) == sorted([base.ref, pin.ref])
    assert any("fix constraint" in w["message"] for w in ok.warnings), ok.warnings
    assert pin.ref in [r["id"] for r in ok.changes["modified"]]
    alone = place(nx, nx.blk, "ALONE", (0, 90, 0))
    quiet = run(nx, "nx_set_component_pose", component=alone.ref, mode="relative",
                translation=[1, 0, 0])  # fmt: skip
    assert quiet.ok and [r["id"] for r in quiet.data["moved"]] == [alone.ref]
    assert not quiet.warnings


def test_replace_keeps_the_component_name(nx):
    b = place(nx, nx.blk, "TAB", (10, 0, 0))
    base = nx.asm.AssemblyManager.CreateReplaceComponentBuilder

    class Renaming(base):  # NX names a replaced component after the new part
        def Commit(self):
            out = super().Commit()
            for c in self.targets:
                c.Name = "PIN"
            return out

    nx.asm.AssemblyManager.CreateReplaceComponentBuilder = Renaming
    ok = run(nx, "nx_edit_component", components=[b.ref], replace_with="parts/pin.prt")
    assert ok.ok and b.Prototype is nx.pin, ok
    assert b.Name == "TAB" and ok.data["components"][0]["name"] == "TAB"
    named = run(nx, "nx_edit_component", components=[b.ref], replace_with="parts/blk.prt",
                name="tab2")  # fmt: skip
    assert named.ok and b.Name == "TAB2", named


def test_replace_needs_the_current_part_saved(nx):
    temp = nx.new_part("parts/never_saved.prt")  # a new_in_context part before its save
    c = place(nx, temp, "TEMP")
    bad = run(nx, "nx_edit_component", components=[c.ref], replace_with="parts/pin.prt")
    assert bad.error["code"] == "NX_UNSAVED_PART", bad
    assert bad.error["field"] == "/components/0" and "never_saved.prt" in bad.error["hint"]
    assert bad.mutation_outcome == "not_started" and c.Prototype is temp


def test_mirrored_components_get_their_own_names(nx):
    a = place(nx, nx.blk, "A", (10, 5, 0))
    place(nx, nx.blk, "A_MIRROR", (90, 0, 0))
    ok = run(nx, "nx_create_component_mirror", components=[a.ref], plane="yz")
    assert ok.ok, ok
    pair = ok.data["components"][0]
    assert pair["source"]["name"] == "A" and pair["mirrored"]["name"] == "A_MIRROR_2"
    assert pair["mirrored"]["path"] == "A_MIRROR_2"
    again = run(nx, "nx_create_component_mirror", components=[a.ref], plane="xz")
    assert again.data["components"][0]["mirrored"]["name"] == "A_MIRROR_3"
    names = [c.Name for c in nx.assembly.components()]
    assert len(names) == len(set(names)) == 4


def test_a_new_mass_keeps_the_asserted_centre(weights_nx):  # noqa: F811
    nx = weights_nx
    first = run(nx, "nx_set_component_mass", part="asm", component=nx.engine.ref, mass=12.5,
                center=[110, 5, 20])  # fmt: skip
    assert first.ok, first
    ok = run(nx, "nx_set_component_mass", part="asm", component=nx.engine.ref, mass=14)
    assert ok.ok, ok
    assert ok.data["mass"] == 14 and ok.data["center_of_gravity"] == pytest.approx([110, 5, 20])
    assert nx.weight.data[int(nx.engine.Tag)][1] == pytest.approx([5, -10, 20])
    assert any(w["code"] == "NX_NOTE" for w in ok.warnings), ok.warnings
    bare = run(nx, "nx_set_component_mass", part="asm", component=nx.tank.ref, mass=5)
    assert bare.ok and bare.data["center_of_gravity"] is None and not bare.warnings


def test_a_name_a_sibling_has_is_refused_with_a_free_one(nx):
    a = place(nx, nx.blk, "A")
    b = place(nx, nx.blk, "B", (20, 0, 0))
    clash = run(nx, "nx_edit_component", components=[b.ref], name="a")
    assert clash.error["code"] == "NX_ALREADY_EXISTS" and clash.error["field"] == "/name", clash
    assert clash.error["details"]["suggested"] == "A_2" and "A_2" in clash.error["hint"]
    assert clash.error["details"]["existing"]["id"] == a.ref
    assert clash.mutation_outcome == "not_started" and b.Name == "B"
    same = run(nx, "nx_edit_component", components=[a.ref], name="A")  # its own name
    assert same.ok, same
    added = run(nx, "nx_create_component", part="asm", path="parts/pin.prt", name="b")
    assert added.error["code"] == "NX_ALREADY_EXISTS" and added.error["field"] == "/name"
    assert len(nx.assembly.components()) == 2


def test_default_component_names_are_numbered_and_make_unique_keeps_the_name(nx):
    first = run(nx, "nx_create_component", part="asm", path="parts/blk.prt")
    second = run(nx, "nx_create_component", part="asm", path="parts/blk.prt")
    assert first.data["component"]["name"] == "BLK", first
    assert second.data["component"]["name"] == "BLK_2", second
    b = nx.assembly.components()[1]
    ok = run(nx, "nx_edit_component", components=[second.data["component"]["id"]],
             make_unique="parts/blk_u.prt")  # fmt: skip
    assert ok.ok and b.Name == "BLK_2" and ok.data["components"][0]["name"] == "BLK_2", ok


def test_a_mirrored_seed_is_refused_and_copies_take_free_names(nx):
    a = place(nx, nx.blk, "A", (10, 0, 0))
    mirrored = place(nx, nx.blk, "M", (-10, 0, 0), [[-1, 0, 0], [0, 1, 0], [0, 0, 1]])
    layout = {"linear": {"direction": [0, 1, 0], "count": 3, "spacing": 20}}
    bad = run(nx, "nx_create_component_pattern", component=mirrored.ref, layout=layout,
              associative=False)  # fmt: skip
    assert bad.error["code"] == "NX_UNSUPPORTED_OPTION" and bad.error["field"] == "/component"
    assert (
        "nx_create_component_mirror" in bad.error["hint"] and bad.mutation_outcome == "not_started"
    )
    place(nx, nx.blk, "A_2", (90, 90, 0))
    ok = run(nx, "nx_create_component_pattern", component=a.ref, layout=layout,
             associative=False)  # fmt: skip
    assert ok.ok, ok
    names = [c.Name for c in nx.assembly.components()]
    assert names[-2:] == ["A_2_2", "A_3"] and len(names) == len(set(names))


def test_a_seed_patterned_again_gets_a_note(nx):
    a = place(nx, nx.blk, "A")
    layout = {"linear": {"direction": [1, 0, 0], "count": 2, "spacing": 30}}
    first = run(nx, "nx_create_component_pattern", component=a.ref, layout=layout)
    assert first.ok and not first.warnings, first
    again = run(nx, "nx_create_component_pattern", component=a.ref,
                layout={"linear": {"direction": [0, 1, 0], "count": 2, "spacing": 30}})  # fmt: skip
    assert again.ok, again
    notes = [w["message"] for w in again.warnings if w["code"] == "NX_NOTE"]
    assert notes and first.data["pattern"]["id"] in notes[0]


def test_compensation_sets_a_pattern_edit_back(nx):
    a = place(nx, nx.blk, "A")
    made = run(nx, "nx_create_component_pattern", component=a.ref,
               layout={"linear": {"direction": [1, 0, 0], "count": 3, "spacing": 20}})  # fmt: skip
    ref = made.data["pattern"]["id"]
    grown = run(nx, "nx_edit_component_pattern", pattern=ref, count=5, spacing=25)
    assert grown.ok, grown
    undo = run(nx, "nx_undo_operation", part="asm", mode="compensate")
    assert undo.ok and undo.data["compensated"], undo
    pattern = nx.assembly.patterns[0]
    assert pattern.params["n"] == 3 and pattern.params["d"] == 20
    assert "count 3" in undo.data["restored"][0] and "spacing 20" in undo.data["restored"][0]


def test_compensation_refuses_a_pattern_edited_again(nx):
    a = place(nx, nx.blk, "A")
    made = run(nx, "nx_create_component_pattern", component=a.ref,
               layout={"linear": {"direction": [1, 0, 0], "count": 3, "spacing": 20}})  # fmt: skip
    ref = made.data["pattern"]["id"]
    first = run(nx, "nx_edit_component_pattern", pattern=ref, count=4)
    run(nx, "nx_edit_component_pattern", pattern=ref, count=6)
    undo = run(nx, "nx_undo_operation", part="asm", operation=first.operation_id,
               mode="compensate")  # fmt: skip
    assert undo.error["code"] == "NX_DEPENDENCY_ORDER", undo
    assert nx.assembly.patterns[0].params["n"] == 6


def test_compensation_sets_a_constraint_edit_and_its_moves_back(nx):
    a = place(nx, nx.blk, "A")
    b = place(nx, nx.blk, "B", (60, 0, 0))

    def follow(_added):  # the solver puts B at the constraint's value
        b.t = [float(nx.assembly.positioner.Constraints[-1].Expression.Value), 0.0, 0.0]

    nx.assembly.on_solve = follow
    made = run(nx, "nx_create_assembly_constraint", type="distance", component=b.ref,
               geometry=nx.ref_of(b.face(), "face"), target_component=a.ref,
               target_geometry=nx.ref_of(a.face(), "face"), value=7)  # fmt: skip
    assert made.ok and b.t[0] == 7, made
    ref = made.data["constraint"]["id"]
    edited = run(nx, "nx_edit_assembly_constraint", constraint=ref, value=12, alignment="same")
    assert edited.ok and b.t[0] == 12, edited
    undo = run(nx, "nx_undo_operation", part="asm", mode="compensate")
    assert undo.ok and undo.data["compensated"], undo
    c = nx.assembly.positioner.Constraints[-1]
    assert c.Expression.Value == 7 and b.t == [7.0, 0.0, 0.0]
    off = run(nx, "nx_edit_assembly_constraint", constraint=ref, suppressed=True)
    assert off.ok, off
    again = run(nx, "nx_edit_assembly_constraint", constraint=ref, value=9)
    assert again.ok
    late = run(nx, "nx_undo_operation", part="asm", operation=off.operation_id,
               mode="compensate")  # fmt: skip
    assert late.error["code"] == "NX_DEPENDENCY_ORDER", late


def test_associative_instances_named_like_the_seed_get_their_own_names(nx, monkeypatch):
    from tests.unit.assembly import fakes

    commit = fakes.PatternBuilder.Commit

    def like_nx(self):  # NX names every instance after the seed
        pattern = commit(self)
        for member in pattern.members:
            if member.Name.endswith(tuple(f"_P{i}" for i in range(2, 9))):
                member.Name = pattern.seeds[0].Name
        return pattern

    monkeypatch.setattr(fakes.PatternBuilder, "Commit", like_nx)
    a = place(nx, nx.blk, "A")
    made = run(nx, "nx_create_component_pattern", component=a.ref,
               layout={"linear": {"direction": [1, 0, 0], "count": 3, "spacing": 20}})  # fmt: skip
    assert made.ok, made
    assert [c["name"] for c in made.data["components"]] == ["A", "A_2", "A_3"]
    grown = run(nx, "nx_edit_component_pattern", pattern=made.data["pattern"]["id"], count=4)
    assert grown.ok, grown
    names = [c["name"] for c in grown.data["components"]]
    assert names[0] == "A" and len(set(names)) == 4, names
