"""nx_set_component_mass, nx_create_component_group and nx_list_component_groups on the
assembly fake, with UF Weight and component groups that the undo marks restore."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.core import evidence
from tests.kit import run
from tests.unit.assembly.fakes import assembly_nx, place

RZ90 = [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]


class Weight:
    """UF Weight: assertions per tag, centres stored as NX 2406 keeps them (a component's in
    its part frame, millimetres), states 2 = asserted."""

    def __init__(self, nx):
        self.nx = nx
        self.data = {}
        self.lie = False

    def AskProps(self, tag, units):
        mass, center = self.data.get(int(tag), (None, None))
        if self.lie and mass is not None:
            mass += 1.0
        return NS(Mass=mass or 0.0, MassState=2 if mass is not None else 0,
                  CenterOfMass=list(center or [0, 0, 0]),
                  CofmState=2 if center is not None else 0)  # fmt: skip

    def _local(self, tag, center):
        c = next(c for c in self.nx.assembly.components() if int(c.Tag) == int(tag))
        d = [center[i] - c.t[i] for i in range(3)]
        return [sum(c.r[j][i] * d[j] for j in range(3)) for i in range(3)]  # R^T d

    def AssertCompProps(self, tag, props):
        center = props.CenterOfMass if props.CofmState == 2 else None
        if center is not None:
            center = self._local(tag, center)  # given in the assembly frame
        self.data[int(tag)] = (props.Mass, center)

    def AssertPartProps(self, tag, props):
        center = list(props.CenterOfMass) if props.CofmState == 2 else None
        self.data[int(tag)] = (props.Mass, center)

    def DeleteCompAssertion(self, tag):
        self.data.pop(int(tag), None)

    DeletePartAssertion = DeleteCompAssertion


class Group:
    def __init__(self, name):
        self.Name = name
        self.members = []

    def GetComponents(self):
        return list(self.members)

    def AddComponent(self, c, children):
        todo = [c]
        while todo:
            node = todo.pop()
            if node not in self.members:
                self.members.append(node)
            if children:
                todo.extend(node.GetChildren())

    def RemoveComponent(self, c):
        self.members.remove(c)

    def SetName(self, name):
        self.Name = name


class Groups(list):
    def CreateComponentGroup(self, name):
        if any(g.Name.casefold() == name.casefold() for g in self):
            raise RuntimeError("Component set with given name already exists in part")
        g = Group(name)
        self.append(g)
        return g


@pytest.fixture
def nx():
    nx = assembly_nx()
    nx.weight = Weight(nx)
    nx.uf.Weight = nx.weight
    nx.nx.UF = NS(Weight=NS(StateType=NS(ASSERTED=2, UNKNOWN=3), UnitsType=NS(UNITS_KMM=6)))
    nx.licensed = True
    nx.session.LicenseManager = NS(CheckPresence=lambda name: nx.licensed)
    nx.asm.ComponentGroups = Groups([Group("AllComponents")])
    session = nx.session
    set_mark, undo = session.SetUndoMark, session.UndoToMark
    saved = {}

    def set_undo_mark(*args):
        mark = set_mark(*args)
        saved[mark] = (dict(nx.weight.data), list(nx.asm.ComponentGroups),
                       {id(g): (list(g.members), g.Name) for g in nx.asm.ComponentGroups})  # fmt: skip
        return mark

    def undo_to_mark(mark, *args):
        undo(mark, *args)
        data, groups, members = saved[mark]
        nx.weight.data = dict(data)
        nx.asm.ComponentGroups[:] = groups
        for g in groups:
            g.members[:], g.Name = members[id(g)]

    session.SetUndoMark, session.UndoToMark = set_undo_mark, undo_to_mark
    nx.engine = place(nx, nx.blk, "engine", translation=(100, 0, 0), rotation=RZ90)
    nx.tank = place(nx, nx.pin, "tank", translation=(0, 50, 0))
    return nx


def test_asserts_an_occurrence_mass_and_centre_in_the_assembly_frame(nx):
    ok = run(nx, "nx_set_component_mass", part="asm", component=nx.engine.ref, mass=350,
             center=[110, 5, 20])  # fmt: skip
    assert ok.ok and ok.mutation_outcome == "committed", ok
    assert ok.data["mass"] == 350 and ok.data["level"] == "occurrence"
    assert ok.data["center_of_gravity"] == pytest.approx([110, 5, 20])
    assert ok.data["previous"] is None and ok.data["part"] == "blk.prt"
    stored = nx.weight.data[int(nx.engine.Tag)]
    assert stored[1] == pytest.approx([5, -10, 20])  # NX keeps it in the part frame
    assert ok.changes["modified"][0]["id"] == nx.engine.ref
    again = run(nx, "nx_set_component_mass", part="asm", component=nx.engine.ref, mass=360,
                center=[1, 2, 3], frame="component")  # fmt: skip
    assert again.data["previous"]["mass"] == 350
    assert again.data["previous"]["center_of_gravity"] == pytest.approx([110, 5, 20])
    assert again.data["center_of_gravity"] == pytest.approx([98, 1, 3])  # R [1,2,3] + t


def test_part_level_takes_the_centre_in_the_part_frame(nx):
    ok = run(nx, "nx_set_component_mass", part="asm", component=nx.engine.ref, mass=60,
             center=[110, 5, 20], level="part")  # fmt: skip
    assert ok.ok, ok
    assert nx.weight.data[int(nx.blk.Tag)] == (60, pytest.approx([5, -10, 20]))
    assert ok.data["center_of_gravity"] == pytest.approx([110, 5, 20])
    bare = run(nx, "nx_set_component_mass", part="asm", component=nx.tank.ref, mass=5,
               level="part")  # fmt: skip
    assert bare.ok and bare.data["center_of_gravity"] is None
    assert nx.weight.data[int(nx.pin.Tag)] == (5, None)


def test_clear_deletes_the_assertion_or_notes_nothing_to_clear(nx):
    run(nx, "nx_set_component_mass", part="asm", component=nx.tank.ref, mass=40)
    ok = run(nx, "nx_set_component_mass", part="asm", component=nx.tank.ref, clear=True)
    assert ok.ok and ok.data["cleared"] is True and ok.data["previous"]["mass"] == 40, ok
    assert int(nx.tank.Tag) not in nx.weight.data
    none = run(nx, "nx_set_component_mass", part="asm", component=nx.tank.ref, clear=True)
    assert none.ok and none.data["cleared"] is False
    assert none.warnings[0]["code"] == "NX_NO_EFFECT"


def test_refusals_change_nothing(nx):
    cases = [
        ({"clear": True, "mass": 1}, "/clear"),
        ({}, "/mass"),
        ({"mass": 1, "frame": "component"}, "/frame"),
    ]
    for extra, field in cases:
        bad = run(nx, "nx_set_component_mass", part="asm", component=nx.tank.ref, **extra)
        assert bad.error["field"] == field and bad.mutation_outcome == "not_started", bad
    nx.licensed = False
    denied = run(nx, "nx_set_component_mass", part="asm", component=nx.tank.ref, mass=1)
    assert denied.error["code"] == "NX_LICENSE_UNAVAILABLE"
    assert denied.mutation_outcome == "not_started" and nx.weight.data == {}
    nx.licensed = True
    evidence.forget_licenses()  # the kernel refuses a denied tested tool until NX restarts
    child = nx.assembly.add(nx.pin, "nested", ([0, 0, 0], RZ90), parent=nx.engine)
    child.ref = nx.ref_of(child, "component")
    nested = run(nx, "nx_set_component_mass", part="asm", component=child.ref, mass=1)
    assert nested.error["code"] == "NX_WRONG_OWNER", nested


def test_a_failed_readback_rolls_the_assertion_back(nx):
    nx.weight.lie = True
    bad = run(nx, "nx_set_component_mass", part="asm", component=nx.tank.ref, mass=40)
    assert bad.error["category"] == "verification_failed", bad
    assert bad.mutation_outcome == "rolled_back" and nx.weight.data == {}


def test_create_replace_and_list_component_groups(nx):
    ok = run(nx, "nx_create_component_group", part="asm", name="Propulsion",
             components=[nx.engine.ref])  # fmt: skip
    assert ok.ok and ok.data["group"]["count"] == 1 and ok.data["replaced"] is False, ok
    assert ok.data["group"]["members"][0]["id"] == nx.engine.ref
    clash = run(nx, "nx_create_component_group", part="asm", name="PROPULSION",
                components=[nx.tank.ref])  # fmt: skip
    assert clash.error["code"] == "NX_ALREADY_EXISTS" and clash.mutation_outcome == "not_started"
    replaced = run(nx, "nx_create_component_group", part="asm", name="propulsion",
                   components=[nx.tank.ref, nx.engine.ref], replace=True)  # fmt: skip
    assert replaced.data["replaced"] is True and replaced.data["group"]["count"] == 2
    system = run(nx, "nx_create_component_group", part="asm", name="AllComponents",
                 components=[nx.tank.ref])  # fmt: skip
    assert system.error["field"] == "/name"
    twice = run(nx, "nx_create_component_group", part="asm", name="X",
                components=[nx.tank.ref, nx.tank.ref])  # fmt: skip
    assert twice.error["field"] == "/components/1"
    listed = run(nx, "nx_list_component_groups", part="asm")
    assert [g["name"] for g in listed.data["items"]] == ["Propulsion"]
    everything = run(nx, "nx_list_component_groups", part="asm", include_system=True,
                     with_members=False)  # fmt: skip
    assert [g["system"] for g in everything.data["items"]] == [True, False]
    assert everything.data["items"][1]["members"] == []
    nx.licensed = False
    denied = run(nx, "nx_create_component_group", part="asm", name="Fuel",
                 components=[nx.tank.ref])  # fmt: skip
    assert denied.error["code"] == "NX_LICENSE_UNAVAILABLE"
    assert [g.Name for g in nx.asm.ComponentGroups] == ["AllComponents", "Propulsion"]


def test_group_with_children(nx):
    child = nx.assembly.add(nx.pin, "nested", ([0, 0, 0], RZ90), parent=nx.engine)
    ok = run(nx, "nx_create_component_group", part="asm", name="Drive",
             components=[nx.engine.ref], children=True)  # fmt: skip
    assert ok.ok and ok.data["group"]["count"] == 2, ok
    assert child in nx.asm.ComponentGroups[-1].members


def test_clear_at_occurrence_level_names_a_part_level_assertion(nx):
    run(nx, "nx_set_component_mass", part="asm", component=nx.tank.ref, mass=0.25, level="part")
    ok = run(nx, "nx_set_component_mass", part="asm", component=nx.tank.ref, clear=True)
    assert ok.ok and ok.data["cleared"] is False, ok
    (warning,) = ok.warnings
    assert warning["code"] == "NX_NO_EFFECT" and "level='part'" in warning["message"]
    assert "0.25 kg" in warning["message"] and nx.weight.data[int(nx.pin.Tag)][0] == 0.25


def test_system_groups_left_empty_by_nx_are_derived_or_unknown(nx):
    nx.asm.ComponentGroups.append(Group("CurrentComponents"))
    listed = run(nx, "nx_list_component_groups", part="asm", include_system=True,
                 with_members=False)  # fmt: skip
    assert listed.ok, listed
    rows = {g["name"]: g for g in listed.data["items"]}
    assert rows["AllComponents"]["count"] == 2  # the whole tree
    assert rows["CurrentComponents"]["count"] is None
    assert "CurrentComponents" in listed.warnings[0]["message"]


def test_rename_and_delete_a_component_group(nx):
    delete = nx.session.UpdateManager.AddToDeleteList

    def delete_groups(objects):
        for obj in objects:
            if obj in nx.asm.ComponentGroups:
                nx.asm.ComponentGroups.remove(obj)
        delete([o for o in objects if not isinstance(o, Group)])

    nx.session.UpdateManager.AddToDeleteList = delete_groups
    run(nx, "nx_create_component_group", part="asm", name="Drive", components=[nx.engine.ref])
    run(nx, "nx_create_component_group", part="asm", name="Fuel", components=[nx.tank.ref])
    renamed = run(nx, "nx_edit_component_group", part="asm", name="drive", new_name="Propulsion")
    assert renamed.ok and renamed.data["previous_name"] == "Drive", renamed
    assert renamed.data["group"]["name"] == "Propulsion" and renamed.data["group"]["count"] == 1
    cases = [
        ({"name": "Propulsion"}, "NX_INVALID_ARGUMENT", "/delete"),
        ({"name": "Propulsion", "new_name": "X", "delete": True}, "NX_INVALID_ARGUMENT",
         "/new_name"),
        ({"name": "AllComponents", "delete": True}, "NX_INVALID_ARGUMENT", "/name"),
        ({"name": "nope", "delete": True}, "NX_NOT_FOUND", "/name"),
        ({"name": "Propulsion", "new_name": "fuel"}, "NX_ALREADY_EXISTS", "/new_name"),
    ]  # fmt: skip
    for args, code, field in cases:
        bad = run(nx, "nx_edit_component_group", part="asm", **args)
        assert bad.error["code"] == code and bad.error["field"] == field, (args, bad)
        assert bad.mutation_outcome == "not_started"
    gone = run(nx, "nx_edit_component_group", part="asm", name="FUEL", delete=True)
    assert gone.ok and gone.data == {"deleted": "Fuel", "remaining": ["Propulsion"]}, gone
    assert [g.Name for g in nx.asm.ComponentGroups] == ["AllComponents", "Propulsion"]


def test_compensation_sets_component_group_changes_back(nx):
    made = run(nx, "nx_create_component_group", part="asm", name="Drive",
               components=[nx.engine.ref])  # fmt: skip
    assert made.ok, made
    replaced = run(nx, "nx_create_component_group", part="asm", name="drive",
                   components=[nx.tank.ref], replace=True)  # fmt: skip
    assert replaced.ok, replaced
    undo = run(nx, "nx_undo_operation", part="asm", mode="compensate")
    assert undo.ok and undo.data["compensated"], undo
    drive = nx.asm.ComponentGroups[-1]
    assert drive.Name == "Drive" and drive.members == [nx.engine]
    renamed = run(nx, "nx_edit_component_group", part="asm", name="Drive", new_name="Power")
    assert renamed.ok, renamed
    back = run(nx, "nx_undo_operation", part="asm", mode="compensate")
    assert back.ok and drive.Name == "Drive", back
    delete = nx.session.UpdateManager.AddToDeleteList

    def delete_groups(objects):
        for obj in objects:
            if obj in nx.asm.ComponentGroups:
                nx.asm.ComponentGroups.remove(obj)
        delete([o for o in objects if not isinstance(o, Group)])

    nx.session.UpdateManager.AddToDeleteList = delete_groups
    gone = run(nx, "nx_undo_operation", part="asm", operation=made.operation_id,
               mode="compensate")  # fmt: skip
    assert gone.ok, gone
    assert [g.Name for g in nx.asm.ComponentGroups] == ["AllComponents"]
