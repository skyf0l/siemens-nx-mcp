"""nx_set_visibility in assemblies, by type and with several selections in one call.

The fake assembly mirrors NX 2406: a component's ``FindOccurrence`` gives the occurrence
of a prototype object, owned by the assembly; blanking it leaves the prototype (and its
part) alone, and only the occurrence's own component shows the change.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from nx_mcp.kit import display
from tests.kit import run
from tests.kit.kernel import services
from tests.kit.objects import Body, Object, Part
from tests.unit.display.fakes import display_nx


@pytest.fixture(autouse=True)
def fresh_snapshots():
    display.snapshots.clear()
    yield
    display.snapshots.clear()


class Comp(Object):
    """A component of ``assembly`` showing ``prototype``."""

    def __init__(self, nx, name, prototype, assembly, parent):
        super().__init__(name)
        self.nx, self.assembly = nx, assembly
        self.Prototype, self.Parent, self.OwningPart = prototype, parent, assembly
        self.IsOccurrence, self.IsSuppressed, self.ReferenceSet = True, False, "Entire Part"
        self.children: list = []
        self.occurrences: dict = {}
        self.ref = nx.ref_of(self, "component")
        parent.children.append(self)

    def GetChildren(self):
        return self.children

    def FindOccurrence(self, obj):
        if getattr(obj, "OwningPart", None) is not self.Prototype:
            return None
        if obj.Tag not in self.occurrences:
            shown = Body(obj.Name, list(obj.box)) if isinstance(obj, Body) else Object(obj.Name)
            shown.IsOccurrence, shown.OwningComponent = True, self
            shown.OwningPart, shown.Prototype = self.assembly, obj
            self.nx.ref_of(shown, self.nx.refs[obj.ref][1])
            self.occurrences[obj.Tag] = shown
        return self.occurrences[obj.Tag]


def _own(nx, part, obj, kind, collection):
    obj.OwningPart, obj.IsOccurrence = part, False
    obj.ref = nx.ref_of(obj, kind)
    getattr(part, collection).append(obj)
    return obj


def assembly_nx(components=("C1", "C2")):
    """``(nx, asm, comp, comps)``: the displayed work part ``work.prt`` holds components
    of ``vis_comp.prt`` (a body, a sketch, two curves and a datum plane)."""
    nx = display_nx()
    nx.nx.DisplayPartOption = NS(AllowAdditional=1, ReplaceExisting=0)
    nx.nx.PartDisplayPartWorkPartOption = NS(SameAsDisplay=0, UseLast=1)
    parts = nx.session.Parts
    asm = parts.Display
    comp = Part(nx.session, Path(services(nx).root) / "vis_comp.prt")
    comp.ModelingViews = NS(WorkView=asm.ModelingViews.WorkView)
    parts.Work = parts.Display = asm
    _own(nx, comp, Body("BLOCK"), "body", "Bodies")
    sketch = _own(nx, comp, Object("SKETCH"), "sketch", "Sketches")
    sketch.GetAllGeometry = lambda: list(comp.Curves)[:1]
    _own(nx, comp, Object("ARC"), "curve", "Curves")
    _own(nx, comp, Object("SPLINE"), "curve", "Curves")
    _own(nx, comp, Object("DATUM_PLANE"), "datum_plane", "Datums")
    root = Object("root")
    root.GetChildren = lambda: root.children
    root.children = []
    asm.ComponentAssembly.RootComponent = root
    comps = [Comp(nx, name, comp, asm, root) for name in components]
    for c in comps:
        c.Parent = None  # top-level components: no parent above the root
    return nx, asm, comp, comps


def test_objects_and_datums_combine_in_one_call():
    nx = display_nx()
    body = nx.block(1, 1, 1)
    planes = [nx.datum_plane(), nx.datum_plane(normal=(1, 0, 0))]
    ok = run(nx, "nx_set_visibility", mode="hide", objects=[body.ref], datums=True)
    assert ok.ok, ok
    assert body.IsBlanked and all(p.IsBlanked for p in planes)
    assert ok.data["count"] == 3 and ok.data["blanked"] == 3
    assert ok.data["by_type"] == {"datum": 2}
    run(nx, "nx_restore_display", restore_id=ok.restore_id)
    assert not body.IsBlanked and not any(p.IsBlanked for p in planes)


def test_types_hide_every_object_of_those_kinds_in_the_part():
    nx = display_nx()
    body = nx.block(1, 1, 1)
    sketch = nx.sketch()
    curves = [nx.curve(sketch), nx.curve()]
    nx.part.Curves.extend(curves)
    plane = nx.datum_plane()
    ok = run(nx, "nx_set_visibility", mode="hide", types=["sketch", "curve"])
    assert ok.ok, ok
    assert sketch.IsBlanked and all(c.IsBlanked for c in curves)
    assert not body.IsBlanked and not plane.IsBlanked
    assert ok.data["by_type"] == {"sketch": 1, "curve": 2} and ok.data["count"] == 3
    assert ok.data["modified_parts"] == ["work.prt"] and "partial" not in ok.data
    again = run(nx, "nx_set_visibility", mode="hide", types=["curve"])
    assert again.ok and again.data["modified_parts"] == []  # nothing changed
    shown = run(nx, "nx_set_visibility", mode="show", types=["curve", "sketch"], datums=True)
    assert shown.ok and not sketch.IsBlanked and not any(c.IsBlanked for c in curves)
    assert shown.data["by_type"] == {"curve": 2, "sketch": 1, "datum": 1}


def test_type_rules_are_checked_before_nx():
    nx = display_nx()
    nx.block(1, 1, 1)
    iso = run(nx, "nx_set_visibility", mode="isolate", types=["curve"])
    assert iso.error["code"] == "NX_INVALID_ARGUMENT" and iso.error["field"] == "/mode"
    assert iso.mutation_outcome == "not_started"
    scope = run(nx, "nx_set_visibility", mode="hide", objects=["b:1"], scope="assembly")
    assert scope.error["field"] == "/scope" and scope.mutation_outcome == "not_started"
    none = run(nx, "nx_set_visibility", mode="hide", types=["point"])
    assert none.error["code"] == "NX_NOT_FOUND" and none.error["field"] == "/types"
    bad = run(nx, "nx_set_visibility", mode="hide", types=["face"])
    assert bad.error["category"] == "invalid_argument"


def test_types_in_the_assembly_hide_occurrences_and_leave_the_component_part_alone():
    nx, asm, comp, comps = assembly_nx()
    ok = run(
        nx, "nx_set_visibility", mode="hide", types=["sketch", "curve"], scope="assembly",
        part="work",
    )  # fmt: skip
    assert ok.ok, ok
    assert ok.data["by_type"] == {"sketch": 2, "curve": 4} and ok.data["count"] == 6
    assert ok.data["blanked"] == 6 and ok.data["modified_parts"] == ["work.prt"]
    shown = [o for c in comps for o in c.occurrences.values()]
    assert len(shown) == 6 and all(o.IsBlanked for o in shown)
    assert not any(o.IsBlanked for o in [*comp.Sketches, *comp.Curves])  # prototypes
    assert ok.warnings == []
    back = run(nx, "nx_restore_display", restore_id=ok.restore_id, part="work")
    assert back.ok and not any(o.IsBlanked for o in shown)


def test_types_in_the_assembly_skip_all_but_bodies_of_partly_loaded_components():
    nx, asm, comp, comps = assembly_nx()
    comp.IsFullyLoaded = False
    ok = run(
        nx, "nx_set_visibility", mode="hide", types=["body", "curve"], scope="assembly",
        part="work",
    )  # fmt: skip
    assert ok.ok, ok
    assert ok.data["by_type"] == {"body": 2, "curve": 0} and ok.data["partial"] == ["C1", "C2"]
    assert any("not fully loaded" in w["message"] for w in ok.warnings)


def test_an_occurrence_ref_hides_in_its_assembly_only():
    nx, asm, comp, comps = assembly_nx()
    arc = comp.Curves[0]
    one = comps[0].FindOccurrence(arc)
    ok = run(nx, "nx_set_visibility", mode="hide", objects=[nx.ref_of(one)])
    assert ok.ok, ok
    assert one.IsBlanked and not arc.IsBlanked
    assert not comps[1].FindOccurrence(arc).IsBlanked
    assert nx.session.Parts.Display is asm and nx.session.Parts.Work is asm
    assert ok.data["modified_parts"] == ["work.prt"]


def test_prototype_objects_of_a_component_part_change_as_occurrences():
    """A call on the assembly naming an object of a component part hides it in every
    component showing it; the component part is not changed."""
    nx, asm, comp, comps = assembly_nx()
    arc = comp.Curves[0]
    ok = run(nx, "nx_set_visibility", mode="hide", objects=[comps[1].ref, arc.ref])
    assert ok.ok, ok
    assert comps[1].IsBlanked and not arc.IsBlanked
    assert all(c.FindOccurrence(arc).IsBlanked for c in comps)
    assert ok.data["count"] == 3 and ok.data["modified_parts"] == ["work.prt"]


def test_objects_of_a_component_part_hidden_in_that_part_carry_a_note():
    """Refs of the component part alone target that part: its objects change there, so
    the result names it and a note says the assembly using it is not where it changed."""
    nx, asm, comp, comps = assembly_nx()
    arc = comp.Curves[0]
    ok = run(nx, "nx_set_visibility", mode="hide", objects=[arc.ref])
    assert ok.ok, ok
    assert arc.IsBlanked and ok.data["modified_parts"] == ["vis_comp.prt"]
    assert nx.session.Parts.Display is asm  # shown for the call, then back
    note = " ".join(w["message"] for w in ok.warnings)
    assert "vis_comp.prt is now modified" in note and "work.prt" in note
    assert "scope=assembly" in note
