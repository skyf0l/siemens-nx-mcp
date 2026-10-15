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
