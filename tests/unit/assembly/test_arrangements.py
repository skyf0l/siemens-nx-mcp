"""Arrangement tools on the assembly fake: named arrangements whose varied components keep
their own poses and whose suppression is per arrangement, restored by undo marks."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.kit import geom
from tests.kit import run
from tests.kit.objects import Object
from tests.unit.assembly.fakes import assembly_nx, place

RX90 = [[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]]
IPOS = 0  # ArrangementOptions.IndividuallyPositioned
SAME = 1  # ArrangementOptions.SamePositionInAll


class Arrangement(Object):
    def __init__(self, fake, name, base=None):
        super().__init__(name)
        self.fake = fake
        # component -> [t, r, suppressed]; poses only matter for varied components
        self.state = {c: [list(c.t), [r[:] for r in c.r], c.IsSuppressed]
                      for c in fake.assembly.components()}  # fmt: skip
        if base is not None:
            self.state = {
                c: [list(t), [r[:] for r in rr], s] for c, (t, rr, s) in base.state.items()
            }

    def SetName(self, name):
        self.Name = str(name)

    def Delete(self, forget):
        self.fake.arrangements.remove(self)


class Arrangements(list):
    def __init__(self, fake):
        super().__init__()
        self.fake = fake

    def Create(self, base, name):
        if any(a.Name.casefold() == name.casefold() for a in self):
            raise RuntimeError("Arrangement name already exists")
        self.fake.save_active()
        made = Arrangement(self.fake, name, base)
        self.append(made)
        return made


class Assembly:
    """Arrangement state on top of ``nx.assembly`` (the component fake)."""

    def __init__(self, nx):
        self.nx = nx
        self.assembly = nx.assembly
        self.varied = set()
        self.arrangements = Arrangements(self)
        first = Arrangement(self, "Arrangement 1")
        self.arrangements.append(first)
        self.active = self.default = first

    def save_active(self):
        for c in self.assembly.components():
            row = self.active.state.setdefault(c, [None, None, False])
            row[2] = c.IsSuppressed
            for a in self.arrangements if c not in self.varied else [self.active]:
                st = a.state.setdefault(c, [None, None, False])
                st[0], st[1] = list(c.t), [r[:] for r in c.r]

    def activate(self, arrangement):
        self.save_active()
        self.active = arrangement
        for c, (t, r, s) in arrangement.state.items():
            if t is not None:
                c.t, c.r = list(t), [row[:] for row in r]
            c.IsSuppressed = s

    def suppressed(self, c, arrangement):
        if arrangement is self.active:
            return c.IsSuppressed
        return arrangement.state.get(c, [None, None, False])[2]

    def set_suppressed(self, comps, arrs, on):
        for c in comps:
            for a in arrs:
                if a is self.active:
                    c.IsSuppressed = on
                a.state.setdefault(c, [list(c.t), [r[:] for r in c.r], False])[2] = on
        return NS(Length=0, Dispose=lambda: None)

    # undo
    def snapshot(self):
        return (list(self.arrangements), self.active, self.default, set(self.varied),
                {id(a): (a.Name, {c: [list(t) if t else t, [r[:] for r in rr] if rr else rr, s]
                                  for c, (t, rr, s) in a.state.items()})
                 for a in self.arrangements})  # fmt: skip

    def restore(self, snap):
        arrs, self.active, self.default, self.varied, states = snap
        self.arrangements[:] = arrs
        for a in arrs:
            a.Name, a.state = states[id(a)]


class ParamsBuilder:
    def __init__(self, fake, objects):
        self.fake = fake
        self.objects = list(objects)
        self.Arrangements = IPOS if all(c in fake.varied for c in self.objects) else SAME

    def Destroy(self):
        pass


class Network:
    """The move network: ``DragByTransform`` takes the matrix rows as R's rows and moves
    about the assembly origin, in the active arrangement (varied) or all of them."""

    def __init__(self, positioner):
        self.positioner = positioner
        self.group = []
        self.applied = False

    def SetMovingGroup(self, objects):
        self.group = list(objects)

    def BeginDrag(self):
        pass

    def EndDrag(self):
        pass

    def ResetDisplay(self):
        pass

    def DragByTransform(self, shift, m):
        delta = geom.transpose(geom.rows(m))
        for c in self.group:
            c.t = geom.add(geom.matvec(delta, c.t), geom.xyz(shift))
            c.r = geom.matmul(delta, c.r)

    def ApplyToModel(self):
        self.applied = True


class Positioner:
    def __init__(self, nx, fake):
        self.fake = fake
        self.PrimaryArrangement = fake.active
        self.moving = False
        self.networks = []

    def ClearNetwork(self):
        pass

    def BeginMoveComponent(self):
        self.moving = True

    def EndMoveComponent(self):
        self.moving = False

    def EstablishNetwork(self):
        assert self.moving and self.PrimaryArrangement is self.fake.active
        self.networks.append(Network(self))
        return self.networks[-1]


@pytest.fixture
def nx():
    nx = assembly_nx()
    nx.lid = place(nx, nx.blk, "lid", translation=(0, 0, 100))
    nx.base = place(nx, nx.pin, "base")
    fake = Assembly(nx)
    nx.arr = fake
    ca = nx.asm.ComponentAssembly
    ca.Arrangements = fake.arrangements
    ca.ActiveArrangement = fake.active
    # NX 2406: (SuppressedState, controlled here); Unsuppressed is 2, Suppressed 1
    ca.GetSuppressedState = lambda c, a: (1 if fake.suppressed(c, a) else 2, True)
    ca.Positioner = Positioner(nx, fake)
    ca.SuppressComponents = lambda comps, arrs: fake.set_suppressed(comps, arrs, True)
    ca.UnsuppressComponents = lambda comps, arrs: fake.set_suppressed(comps, arrs, False)

    def set_default(a):
        fake.default = a

    ca.SetDefault = set_default
    nx.uf.Assem.AskDefaultArrangement = lambda tag: fake.default.Tag
    for c in (nx.lid, nx.base):
        c.SetPositioningVaried = lambda comps, on: (
            fake.varied.update(comps) if on else fake.varied.difference_update(comps)
        )
    nx.asm.PropertiesManager = NS(
        CreateAssembliesParameterPropertiesBuilder=lambda objs: ParamsBuilder(fake, objs)
    )
    nx.nx.Assemblies = NS(
        **vars(nx.nx.Assemblies),
        AssembliesParameterPropertiesBuilder=NS(
            ArrangementOptions=NS(IndividuallyPositioned=IPOS, SamePositionInAll=SAME)
        ),
        ComponentAssembly=NS(SuppressedState=NS(Suppressed=1, Unsuppressed=2, SuppressedByExp=3)),
    )
    nx.nx.Positioning = NS(
        **vars(nx.nx.Positioning), ComponentNetwork=NS(ArrangementsMode=NS(Existing=0))
    )
    # ActiveArrangement: a property on the namespace's class would leak; wrap set/get
    ns_type = type("CA", (NS,), {
        "ActiveArrangement": property(lambda self: fake.active,
                                      lambda self, a: fake.activate(a)),
    })  # fmt: skip
    values = dict(vars(ca))
    values.pop("ActiveArrangement")
    nx.asm.ComponentAssembly = ns_type(**values)
    session = nx.session
    set_mark, undo = session.SetUndoMark, session.UndoToMark
    saved = {}

    def set_undo_mark(*args):
        mark = set_mark(*args)
        saved[mark] = fake.snapshot()
        return mark

    def undo_to_mark(mark, *args):
        undo(mark, *args)
        fake.restore(saved[mark])

    session.SetUndoMark, session.UndoToMark = set_undo_mark, undo_to_mark
    return nx


def _names(result):
    return [a["name"] for a in result.data["arrangements"]]


def test_create_activate_rename_default_delete(nx):
    ok = run(nx, "nx_create_arrangement", part="asm", name="OPEN")
    assert ok.ok and ok.data["arrangement"] == {"name": "OPEN", "active": False,
                                                "default": False}, ok  # fmt: skip
    assert _names(ok) == ["Arrangement 1", "OPEN"]
    clash = run(nx, "nx_create_arrangement", part="asm", name="open")
    assert clash.error["code"] == "NX_ALREADY_EXISTS" and clash.mutation_outcome == "not_started"
    assert clash.error["details"]["arrangement"] == "OPEN", clash
    missing = run(nx, "nx_create_arrangement", part="asm", name="X", copy_from="nope")
    assert missing.error["field"] == "/copy_from"
    act = run(nx, "nx_edit_arrangement", part="asm", arrangement="open", action="activate")
    assert act.ok and act.data["arrangement"]["active"] is True, act
    assert nx.arr.active.Name == "OPEN"
    gone = run(nx, "nx_edit_arrangement", part="asm", arrangement="OPEN", action="delete")
    assert gone.error["field"] == "/arrangement" and "active" in gone.error["message"]
    renamed = run(nx, "nx_edit_arrangement", part="asm", arrangement="Arrangement 1",
                  action="rename", new_name="SHUT")  # fmt: skip
    assert renamed.ok and renamed.data["previous_name"] == "Arrangement 1", renamed
    assert _names(renamed) == ["OPEN", "SHUT"]
    no_name = run(nx, "nx_edit_arrangement", part="asm", arrangement="SHUT", action="rename")
    assert no_name.error["field"] == "/new_name"
    extra = run(nx, "nx_edit_arrangement", part="asm", arrangement="SHUT", action="activate",
                new_name="X")  # fmt: skip
    assert extra.error["field"] == "/new_name"
    default = run(nx, "nx_edit_arrangement", part="asm", arrangement="OPEN", action="set_default")
    assert default.ok and default.data["arrangement"]["default"] is True
    run(nx, "nx_edit_arrangement", part="asm", arrangement="SHUT", action="activate")
    blocked = run(nx, "nx_edit_arrangement", part="asm", arrangement="OPEN", action="delete")
    assert "default" in blocked.error["message"], blocked
    run(nx, "nx_edit_arrangement", part="asm", arrangement="SHUT", action="set_default")
    deleted = run(nx, "nx_edit_arrangement", part="asm", arrangement="OPEN", action="delete")
    assert deleted.ok and _names(deleted) == ["SHUT"], deleted
    last = run(nx, "nx_edit_arrangement", part="asm", arrangement="SHUT", action="delete")
    assert "at least one" in last.error["message"]


def test_pose_and_suppression_per_arrangement(nx):
    run(nx, "nx_create_arrangement", part="asm", name="OPEN")
    ok = run(nx, "nx_set_arrangement_component", part="asm", arrangement="OPEN",
             component=nx.lid.ref,
             pose={"translation": [0, 0, 150], "rotation": {"axis": "x", "angle": 90}})  # fmt: skip
    assert ok.ok and ok.data["varied"] is True, ok
    assert ok.data["component"]["translation"] == pytest.approx([0, 0, 150])
    assert sum(ok.data["component"]["rotation"], []) == pytest.approx(sum(RX90, []))
    assert nx.lid.t == pytest.approx([0, 0, 100])  # the active arrangement keeps its pose
    assert nx.arr.active.Name == "Arrangement 1"
    hide = run(nx, "nx_set_arrangement_component", part="asm", arrangement="OPEN",
               component=nx.base.ref, suppressed=True)  # fmt: skip
    assert hide.ok and hide.data["suppressed"] is True and not nx.base.IsSuppressed, hide
    listed = run(nx, "nx_list_arrangements", part="asm")
    assert listed.ok, listed
    assert [(a["name"], a["active"], [s["name"] for s in a["suppressed"]])
            for a in listed.data["items"]] == [
        ("Arrangement 1", True, []), ("OPEN", False, ["BASE"])]  # fmt: skip
    assert listed.data["items"][1]["suppressed"][0]["id"] == nx.base.ref
    varied = {r["name"]: r["varied"] for r in listed.data["components"]}
    assert varied["LID"] is True, listed
    act = run(nx, "nx_edit_arrangement", part="asm", arrangement="OPEN", action="activate")
    rows = {r["name"]: r for r in act.data["components"]}
    assert rows["LID"]["translation"] == pytest.approx([0, 0, 150])
    assert rows["BASE"]["suppressed"] is True
    back = run(nx, "nx_edit_arrangement", part="asm", arrangement="Arrangement 1",
               action="activate")  # fmt: skip
    rows = {r["name"]: r for r in back.data["components"]}
    assert rows["LID"]["translation"] == pytest.approx([0, 0, 100])
    assert rows["BASE"]["suppressed"] is False


def test_set_component_refusals(nx):
    none = run(nx, "nx_set_arrangement_component", part="asm", arrangement="Arrangement 1",
               component=nx.lid.ref)  # fmt: skip
    assert none.error["field"] == "/pose" and none.mutation_outcome == "not_started"
    nx.arr.set_suppressed([nx.lid], [nx.arr.active], True)
    hidden = run(nx, "nx_set_arrangement_component", part="asm", arrangement="Arrangement 1",
                 component=nx.lid.ref, pose={"translation": [1, 2, 3]})  # fmt: skip
    assert hidden.error["field"] == "/pose" and "suppressed" in hidden.error["message"], hidden
    both = run(nx, "nx_set_arrangement_component", part="asm", arrangement="Arrangement 1",
               component=nx.lid.ref, pose={"translation": [1, 2, 3]}, suppressed=False)  # fmt: skip
    assert both.ok and both.data["suppressed"] is False, both
    assert nx.lid.t == pytest.approx([1, 2, 3])


def test_a_shared_pose_that_moves_elsewhere_rolls_back(nx):
    run(nx, "nx_create_arrangement", part="asm", name="OPEN")
    for c in (nx.lid,):  # NX refuses to vary it: the move then reaches every arrangement
        c.SetPositioningVaried = lambda comps, on: None
    bad = run(nx, "nx_set_arrangement_component", part="asm", arrangement="OPEN",
              component=nx.lid.ref, pose={"translation": [0, 0, 150]})  # fmt: skip
    assert bad.error["category"] == "verification_failed", bad
    assert bad.mutation_outcome == "rolled_back"
    assert nx.lid.t == pytest.approx([0, 0, 100]) and nx.arr.active.Name == "Arrangement 1"
    assert geom.dist(nx.arr.arrangements[1].state[nx.lid][0], [0, 0, 100]) < 1e-9


def _pose_in(nx, component, name):
    a = next(x for x in nx.arr.arrangements if x.Name == name)
    if a is nx.arr.active:
        return component.t
    return a.state[component][0]


def test_compensation_sets_an_arrangement_component_back(nx):
    run(nx, "nx_create_arrangement", part="asm", name="OPEN")
    moved = run(nx, "nx_set_arrangement_component", part="asm", arrangement="OPEN",
                component=nx.lid.ref, pose={"translation": [0, 0, 150]}, suppressed=True)  # fmt: skip
    assert moved.ok, moved
    undo = run(nx, "nx_undo_operation", part="asm", mode="compensate")
    assert undo.ok and undo.data["compensated"], undo
    assert "back to unsuppressed at [0" in undo.data["restored"][0]
    assert nx.lid not in nx.arr.varied and not nx.arr.suppressed(nx.lid, nx.arr.arrangements[1])
    assert _pose_in(nx, nx.lid, "OPEN") == pytest.approx([0, 0, 100])
    assert nx.arr.active.Name == "Arrangement 1"


def test_compensation_refuses_an_arrangement_component_changed_since(nx):
    run(nx, "nx_create_arrangement", part="asm", name="OPEN")
    first = run(nx, "nx_set_arrangement_component", part="asm", arrangement="OPEN",
                component=nx.lid.ref, pose={"translation": [0, 0, 150]})  # fmt: skip
    again = run(nx, "nx_set_arrangement_component", part="asm", arrangement="OPEN",
                component=nx.lid.ref, pose={"translation": [0, 0, 200]})  # fmt: skip
    assert first.ok and again.ok
    later = run(nx, "nx_undo_operation", part="asm", operation=first.operation_id,
                mode="compensate")  # fmt: skip
    assert later.error["code"] == "NX_DEPENDENCY_ORDER", later
    assert _pose_in(nx, nx.lid, "OPEN") == pytest.approx([0, 0, 200])


def test_compensation_sets_an_arrangement_rename_activation_and_default_back(nx):
    run(nx, "nx_create_arrangement", part="asm", name="OPEN")
    for args, check in (
        ({"arrangement": "OPEN", "action": "rename", "new_name": "WIDE"},
         lambda: sorted(a.Name for a in nx.arr.arrangements) == ["Arrangement 1", "OPEN"]),
        ({"arrangement": "OPEN", "action": "activate"},
         lambda: nx.arr.active.Name == "Arrangement 1"),
        ({"arrangement": "OPEN", "action": "set_default"},
         lambda: nx.arr.default.Name == "Arrangement 1"),
    ):  # fmt: skip
        done = run(nx, "nx_edit_arrangement", part="asm", **args)
        assert done.ok, done
        undo = run(nx, "nx_undo_operation", part="asm", mode="compensate")
        assert undo.ok and undo.data["compensated"], (args, undo)
        assert check(), args
